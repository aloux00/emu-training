# -*- coding: utf-8 -*-
"""钉钉群机器人（Stream 模式）：老师在群里 @机器人 或私聊直接发，无需打开页面。

指令：
  填报：列表 / 12-03 课程名 / 课程名 / 改 12-03 新名
  查询：课表 [12月] / 本周 / 下周 / 今天 / 明天
  自助：提醒 12-05 14:00 事情（建本人日程）/ 待办 12-05 事情（建本人待办）
  请假：请假 12-03 [至 12-05] / 销假 12-03
  管理：催办
  研究生汇报：汇报人（研究生）发「12-03 题目」填报本人汇报题目
身份校验：发送者钉钉 userid ↔ 教师管理里的手机号（通讯录 userid 反查），只能填自己的课；
  研究生汇报人按「研究生管理」里的手机号识别，只能填自己的汇报题目。
回复：直接回消息所在会话（sessionWebhook），群聊和单聊都可用，无需额外权限。
"""
import re
import json
import sys
import time
import types
import asyncio
import datetime
import logging
import urllib.request
import websockets.exceptions   # websockets>=15 不会自动暴露子模块，SDK 内部要用 websockets.exceptions

import dingtalk_stream

import db
import models
import dingtalk_card
from sqlmodel import Session, select


# userid → Teacher / Graduate 的缓存。聊天指令和卡片回调两个 handler 共用，避免各自重复打通讯录接口。
_UID_CACHE = {"map": {}, "grads": {}, "at": 0.0}


def _refresh_uid_cache():
    """手机号 → userid 反查（10 分钟缓存）。"""
    import api as app_api
    with Session(db.engine) as s:
        cfg = s.exec(select(models.DingTalkConfig)).first()
        if not cfg or not cfg.app_key:
            return
        try:
            token = app_api._dt_access_token(cfg)
        except Exception:
            return
        mapping = {}
        for t in s.exec(select(models.Teacher)).all():
            phone = (t.phone or "").strip()
            if not app_api._PHONE_RE.match(phone):
                continue
            try:
                j = app_api._dt_oapi({"mobile": phone},
                                     "https://oapi.dingtalk.com/topapi/v2/user/getbymobile?access_token=" + token)
                if j.get("errcode") == 0:
                    mapping[j["result"]["userid"]] = t
            except Exception:
                pass
        # 研究生汇报人：同样按手机号反查（研究生管理里登记了手机号的才识别得到）
        gmap = {}
        for g in s.exec(select(models.Graduate)).all():
            phone = (g.phone or "").strip()
            if not app_api._PHONE_RE.match(phone):
                continue
            try:
                j = app_api._dt_oapi({"mobile": phone},
                                     "https://oapi.dingtalk.com/topapi/v2/user/getbymobile?access_token=" + token)
                if j.get("errcode") == 0:
                    gmap[j["result"]["userid"]] = g
            except Exception:
                pass
        _UID_CACHE["map"] = mapping
        _UID_CACHE["grads"] = gmap
        _UID_CACHE["at"] = time.time()


def teacher_by_userid(userid: str):
    if not _UID_CACHE["map"] or time.time() - _UID_CACHE["at"] > 600:
        _refresh_uid_cache()
    return _UID_CACHE["map"].get(userid)


def card_rows(s, tobj, limit: int = 12):
    """某位老师已发布的教学任务：[(date, time, class_type_name, role, topic)]。"""
    import api as app_api
    rules = app_api._load_rules(s)
    rows = s.exec(select(models.ClassSession)
                  .where(models.ClassSession.status == "published")
                  .order_by(models.ClassSession.date)).all()
    out = []
    for x in rows:
        if x.teacher_id == tobj.id:
            role = "讲课"
        elif x.main_reader_id == tobj.id:
            role = "主阅"
        elif x.reviewer_id == tobj.id:
            role = "评审"
        elif x.mentor_id == tobj.id:
            role = "指导"
        else:
            continue
        out.append((x.date,
                    app_api._course_time(x.class_type_name, x.month, rules) or "17:30",
                    x.class_type_name, role, (x.topic or "").strip()))
        if len(out) >= limit:
            break
    return out


class CourseBotHandler(dingtalk_stream.chatbot.AsyncChatbotHandler):
    """SDK 的 AsyncChatbotHandler 用线程池执行同步 process（它的 process 必须写成同步 def）。"""
    def __init__(self):
        super().__init__()
        self._uid_map = {}      # userid -> Teacher
        self._grad_map = {}     # userid -> Graduate（研究生汇报人）
        self._uid_map_at = 0    # 缓存时间

    # ---------- 钉钉侧 ----------
    def _refresh_uid_map(self):
        """手机号 → userid 反查缓存（10 分钟）。实际缓存由模块级 _UID_CACHE 维护。"""
        if time.time() - _UID_CACHE["at"] > 600 or not _UID_CACHE["map"]:
            _refresh_uid_cache()
        self._uid_map = _UID_CACHE["map"]
        self._grad_map = _UID_CACHE.get("grads", {})

    # ---------- 指令处理 ----------
    def handle_command(self, raw_text: str, sender_userid: str, conversation_id: str = "") -> str:
        text = (raw_text or "").strip()
        # 当天/次日课表属于公开信息：还没登记手机号的老师、甚至学员也能问
        if text in ("今天", "今日", "今天有什么课", "明天", "明日", "明天有什么课"):
            with Session(db.engine) as s:
                return self._day_schedule(s, "明天" if text.startswith("明") else "今天")
        self._refresh_uid_map()
        tobj = self._uid_map.get(sender_userid)
        gobj = self._grad_map.get(sender_userid)
        if not tobj and not gobj:
            return ("未识别到您的身份：请在系统的「教师管理」或「研究生管理」里填写与钉钉一致的手机号，再试一次。\n"
                    "（未登记手机号时仍可查当天课表：发「今天」或「明天」）")
        if not text:
            if gobj and not tobj:
                return self._grad_help(gobj)
            return ("用法：\n"
                    "· 列表 —— 本人待填报的课\n"
                    "· 课表 / 课表 12月 —— 本人某月全部课程\n"
                    "· 本周 / 下周 —— 本人未来一周的课\n"
                    "· 今天 / 明天 —— 全院当日课表\n"
                    "· 12-03 课程名 —— 给指定日期的课填课程名\n"
                    "· 改 12-03 新名 —— 修改已填的课程名\n"
                    "· 提醒 12-05 14:00 事情 —— 在本人钉钉日历加日程\n"
                    "· 待办 12-05 事情 —— 给本人建一条钉钉待办\n"
                    "· 请假 12-03 / 请假 12-03 至 12-05 —— 登记请假\n"
                    "· 催办 —— 列出未填课程并 @ 相关老师")

        with Session(db.engine) as s:
            # 研究生汇报人（非教师身份）：只开放汇报相关的指令
            if gobj and not tobj:
                return self._grad_command(s, gobj, text)
            if text in ("列表", "list", "待填"):
                return self._list_courses(s, tobj)
            if text in ("催办", "催一催", "催", "填报进度", "谁没填", "未填"):
                return self._urge(s)
            if text in ("帮助", "指令", "help", "?", "？"):
                return self._help(tobj)
            # 查询类
            if (re.match(r"^(课表|我的课|日程表|本月|这个月)", text)):
                return self._my_month(s, tobj, text)
            if text in ("本周", "这周", "这星期", "这个星期"):
                return self._my_range(s, tobj, "本周")
            if text in ("下周", "下星期", "下个星期"):
                return self._my_range(s, tobj, "下周")
            # 自助日程 / 待办 / 请假
            if re.match(r"^(提醒|日程|加日程)", text):
                return self._add_event(s, tobj, text)
            if re.match(r"^(待办|任务|加待办)", text):
                return self._add_todo(s, tobj, text)
            if re.match(r"^(请假|休假|销假|取消请假)", text):
                return self._leave(s, tobj, text)
            # 「改 12-03 新名」= 显式覆盖已填课程名（默认不允许静默覆盖别人的填写）
            force = False
            mf = re.match(r"^(?:改|修改|覆盖|变更)\s*[:：]?\s*(.+)$", text)
            if mf:
                force = True
                text = mf.group(1).strip()
            # 解析日期
            m = re.match(r"^(1[0-2]|0?[1-9])\s*[月\-.]\s*(\d{1,2})\s*日?\s+(.+)$", text)
            date_iso, topic = None, text
            if m:
                mo, dy = int(m.group(1)), int(m.group(2))
                topic = m.group(3).strip()
                if not topic:
                    return "课程名不能为空。用法：12-03 课程名"
                date_iso = "2026-%02d-%02d" % (mo, dy)  # 年份按可填报课程自动匹配，见下
            editable = self._editable_courses(s, tobj)
            if not editable:
                return self._no_duty_hint(s, tobj)
            target = None
            if date_iso:
                md = date_iso[5:]
                target = next((x for x in editable if x.date[5:] == md), None)
                if not target:
                    # 可能是"由别人负责的课"，给出更准确的原因，而不是含糊的"没有可填报课程"
                    other = self._related_courses(s, tobj)
                    hit = next((x for x in other if x.date[5:] == md), None)
                    if hit:
                        return self._not_owner_hint(s, tobj, hit)
                    return "%s老师：您在 %s 没有需要您填报的课程。" % (tobj.name, md)
            else:
                target = editable[0]  # 最早一节未填
            if not topic:
                return "课程名不能为空。"
            if len(topic) > 100:
                return "课程名过长（限 100 字）。"
            # 写保护：已有他人填写的值，除非显式「改」，否则不覆盖
            old = (target.topic or "").strip()
            if old and target.topic_by and target.topic_by != tobj.id and not force:
                who = self._teacher_name(s, target.topic_by)
                return ("这节课的课程名已经由 %s 老师填写：\n"
                        "「%s」（%s）\n"
                        "如需修改，请回复「改 %s 新课程名」。"
                        % (who, old, target.topic_at or "时间不详", target.date[5:]))
            if old == topic and not force:
                return "✓ 这节已经是「%s」，无需重复填写。" % topic
            target.topic = topic
            target.topic_by = tobj.id
            target.topic_at = time.strftime("%Y-%m-%d %H:%M")
            s.add(target); s.commit()
            self._sync_event(target.id)   # 课程名变了 → 同步改钉钉日程标题
            role = self._role_of(target, tobj.id)
            tag = "（已更新）" if old and old != topic else ""
            return ("✓ 已登记%s：%s %s【%s】%s\n课程名：%s\n（%s老师 · %s）"
                    % (tag, target.date, self._course_time(target), target.class_type_name,
                       role, topic, tobj.name, role))

    def _course_time(self, x):
        import api as app_api
        with Session(db.engine) as s:
            rules = app_api._load_rules(s)
        return app_api._fmt_hhmm(app_api._course_time(x.class_type_name, x.month, rules) or "17:30")

    def _help(self, tobj) -> str:
        return ("%s老师，可用指令：\n"
                "【填报】\n"
                "· 列表 —— 本人待填报的课\n"
                "· 12-03 课程名 —— 给指定日期的课填课程名（也支持 12月3日、12.3）\n"
                "· 课程名 —— 不写日期时，填到本人最早一节未填的课\n"
                "· 改 12-03 新课程名 —— 修改已填的课程名\n"
                "【查询】\n"
                "· 课表 / 课表 12月 —— 本人某月全部课程（默认本月）\n"
                "· 本周 / 下周 —— 本人这一周、下一周的课\n"
                "· 今天 / 明天 —— 全院当日/次日课表（不用登记手机号也能查）\n"
                "【自助】\n"
                "· 提醒 12-05 14:00 技能考核 —— 加到本人钉钉日历，提前 15 分钟提醒\n"
                "· 待办 12-05 提交出科考核表 —— 给本人建钉钉待办（默认 18:00 截止）\n"
                "· 请假 12-03 / 请假 12-03 至 12-05 —— 登记请假，下次排课自动避开\n"
                "· 销假 12-03 —— 取消某天请假\n"
                "【其他】\n"
                "· 催办 —— 列出还没填课程名的课，并 @ 负责老师\n"
                "【研究生汇报】\n"
                "· 第三节研究生汇报的题目由汇报人自己填：汇报人在群里发「12-03 题目」\n"
                "· 汇报人需在系统「研究生管理」里登记与钉钉一致的手机号，机器人才能识别"
                % (tobj.name or "老师"))

    # ---------- 研究生汇报人指令 ----------
    def _grad_sessions(self, s, gobj):
        """本人作为汇报人的已发布研究生汇报节，按日期排序。"""
        return s.exec(select(models.ClassSession)
                      .where(models.ClassSession.status == "published",
                             models.ClassSession.class_type_name == "研究生汇报",
                             models.ClassSession.presenter_id == gobj.id)
                      .order_by(models.ClassSession.date)).all()

    def _grad_help(self, gobj=None) -> str:
        name = ((gobj.name if gobj else "") or "同学")
        return ("%s，您好！您是研究生汇报人，可用指令：\n"
                "· 列表 —— 本人待填报题目的汇报\n"
                "· 课表 12月 / 本周 / 下周 —— 本人汇报安排\n"
                "· 12-03 题目 —— 填报当天汇报的题目（也支持 12月3日、12.3）\n"
                "· 改 12-03 新题目 —— 修改已填的题目\n"
                "· 今天 / 明天 —— 全院当日/次日课表\n"
                "（汇报人需在系统「研究生管理」里登记与钉钉一致的手机号）" % name)

    def _grad_rows_text(self, s, rows, title):
        import api as app_api
        if not rows:
            return title + "\n（没有汇报安排）"
        rules = app_api._load_rules(s)
        lines = []
        for x in rows:
            t_ = app_api._fmt_hhmm(app_api._course_time(x.class_type_name, x.month, rules) or "17:30")
            state = (x.topic or "").strip() or "**未定**"
            lines.append("%s %s【研究生汇报】题目：%s" % (x.date[5:], t_, state))
        return "%s\n%s" % (title, "\n".join(lines))

    def _grad_command(self, s, gobj, text):
        import api as app_api
        rows = self._grad_sessions(s, gobj)

        if text in ("帮助", "指令", "help", "?", "？"):
            return self._grad_help(gobj)

        # 查询类
        if re.match(r"^(课表|我的课|本月|这个月)", text):
            today = datetime.date.today()
            y, m = today.year, today.month
            mm = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})", text)
            if mm:
                y, m = int(mm.group(1)), int(mm.group(2))
            else:
                m2 = re.search(r"(\d{1,2})\s*月", text) or re.search(r"(?:课表|日程)\s*(\d{1,2})\s*$", text)
                if m2:
                    m = int(m2.group(1))
                    if m < today.month - 5:
                        y += 1
            if not (1 <= m <= 12):
                return "月份不对。用法：课表 12月"
            picked = [x for x in rows if x.year == y and x.month == m]
            return self._grad_rows_text(s, picked,
                                        "%s，%d年%d月您的汇报（共 %d 场）："
                                        % (gobj.name or "同学", y, m, len(picked)))
        if text in ("列表", "list", "待填"):
            unfilled = [x for x in rows if not (x.topic or "").strip()]
            body = self._grad_rows_text(s, unfilled, "%s，待您填报题目的汇报 %d 场："
                                        % (gobj.name or "同学", len(unfilled)))
            return body + "\n\n填报：发「12-03 题目」；修改：发「改 12-03 新题目」。"
        if text in ("本周", "这周", "这星期", "这个星期", "下周", "下星期", "下个星期"):
            today = datetime.date.today()
            if text.startswith("本"):
                a = today
                b = today + datetime.timedelta(days=6 - today.weekday())
                label = "本周"
            else:
                a = today + datetime.timedelta(days=7 - today.weekday())
                b = a + datetime.timedelta(days=6)
                label = "下周"
            picked = [x for x in rows if a.isoformat() <= x.date <= b.isoformat()]
            return self._grad_rows_text(s, picked, "%s，%s（%s ~ %s）共 %d 场："
                                        % (gobj.name or "同学", label, a.isoformat()[5:], b.isoformat()[5:], len(picked)))

        # 填报题目：「12-03 题目」/「改 12-03 新题目」
        force = False
        mf = re.match(r"^(?:改|修改|覆盖|变更)\s*[:：]?\s*(.+)$", text)
        if mf:
            force = True
            text = mf.group(1).strip()
        m = re.match(r"^(1[0-2]|0?[1-9])\s*[月\-.]\s*(\d{1,2})\s*日?\s+(.+)$", text)
        if not m:
            return ("没看懂这条指令。发「帮助」查看研究生汇报的用法。\n"
                    "填题目：12-03 题目　　改题目：改 12-03 新题目")
        mo, dy = int(m.group(1)), int(m.group(2))
        topic = m.group(3).strip()
        if not topic:
            return "题目不能为空。用法：12-03 题目"
        if len(topic) > 100:
            return "题目过长（限 100 字）。"
        md = "%02d-%02d" % (mo, dy)
        target = next((x for x in rows if x.date[5:] == md), None)
        if not target:
            return "%s：您在 %s 没有安排研究生汇报（或课表尚未发布）。" % (gobj.name or "同学", md)
        old = (target.topic or "").strip()
        if old and not force:
            return ("这场的题目已经填过：\n「%s」（%s）\n"
                    "如需修改，请回复「改 %s 新题目」。"
                    % (old, target.topic_at or "时间不详", target.date[5:]))
        if old == topic and not force:
            return "✓ 这场已经是「%s」，无需重复填写。" % topic
        target.topic = topic
        target.topic_at = time.strftime("%Y-%m-%d %H:%M")
        s.add(target)
        s.commit()
        self._sync_event(target.id)   # 题目变了 → 同步改钉钉日程标题
        tag = "（已更新）" if old and old != topic else ""
        return ("✓ 已登记%s：%s %s【研究生汇报】\n题目：%s\n（汇报人：%s）"
                % (tag, target.date, self._course_time(target), topic, gobj.name or "同学"))

    # ---------- 日期解析小工具 ----------
    @staticmethod
    def _near_date(mo: int, dy: int):
        """把「12-05」解析成日期：默认取最近的将来（当前月前后 6 个月为界判跨年）。"""
        today = datetime.date.today()
        y = today.year
        if mo < today.month - 5:      # 现在 11 月问 1 月 → 次年
            y += 1
        try:
            return datetime.date(y, mo, dy)
        except ValueError:
            return None

    @staticmethod
    def _date_re():
        return r"(\d{1,2})\s*[-月./]\s*(\d{1,2})\s*日?"

    # ---------- 查询课表 ----------
    def _participated(self, s, tobj):
        """本人参与（任一角色）的已发布课程，按日期排序。"""
        import api as app_api
        rows = s.exec(select(models.ClassSession)
                      .where(models.ClassSession.status == "published")
                      .order_by(models.ClassSession.date, models.ClassSession.slot)).all()
        return [x for x in rows if any(tid == tobj.id for _, tid in app_api.session_roles(x))]

    def _rows_text(self, s, rows, title, tobj=None, with_teachers=False):
        """把课程列表渲染成多行文本。tobj 为空时不显示角色（全院视图）。"""
        import api as app_api
        rules = app_api._load_rules(s)
        grads = {g.id: g.name for g in s.exec(select(models.Graduate)).all()}
        if not rows:
            return title + "\n（没有课程安排）"
        lines = []
        for x in rows:
            t_ = app_api._fmt_hhmm(app_api._course_time(x.class_type_name, x.month, rules) or "17:30")
            if x.class_type_name == "研究生汇报":
                # 研究生汇报：显示汇报人 + 题目（不是老师的课，不套用老师角色视图）
                who = "汇报:%s" % (getattr(x, "presenter_name", "") or grads.get(x.presenter_id) or "?")
                state = "题目:%s" % ((x.topic or "").strip() or "未定")
                lines.append("%s %s【%s】%s · %s" % (x.date[5:], t_, x.class_type_name, who, state))
                continue
            state = (x.topic or "").strip() or "未填报"
            if with_teachers:
                who = "%s/%s/%s" % (getattr(x, "teacher_name", "") or self._teacher_name(s, x.teacher_id),
                                    (getattr(x, "main_reader_name", "")
                                     or self._teacher_name(s, x.main_reader_id)) if x.main_reader_id else "—",
                                    (getattr(x, "reviewer_name", "")
                                     or self._teacher_name(s, x.reviewer_id)) if x.reviewer_id else "—")
                lines.append("%s %s【%s】%s｜%s" % (x.date[5:], t_, x.class_type_name, who, state))
            else:
                role = (self._role_of(x, tobj.id) if tobj else "") or "参与"
                lines.append("%s %s【%s】%s · %s" % (x.date[5:], t_, x.class_type_name, role, state))
        return "%s\n%s" % (title, "\n".join(lines))

    def _my_month(self, s, tobj, text):
        today = datetime.date.today()
        y, m = today.year, today.month
        mm = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})", text)
        if mm:
            y, m = int(mm.group(1)), int(mm.group(2))
        else:
            m2 = re.search(r"(\d{1,2})\s*月", text) or re.search(r"(?:课表|日程)\s*(\d{1,2})\s*$", text)
            if m2:
                m = int(m2.group(1))
                if m < today.month - 5:
                    y += 1
        if not (1 <= m <= 12):
            return "月份不对。用法：课表 12月"
        rows = [x for x in self._participated(s, tobj) if x.year == y and x.month == m]
        txt = self._rows_text(s, rows, "%s老师，%d年%d月您的课程（共 %d 节）："
                               % (tobj.name, y, m, len(rows)), tobj)
        if not rows:
            # 区分「排了但没发布」和「这个月压根没排课」，避免老师以为漏排
            any_row = s.exec(select(models.ClassSession)
                             .where(models.ClassSession.year == y,
                                    models.ClassSession.month == m)).first()
            if any_row:
                txt += "\n（该月课表已生成但尚未发布，发布后这里才会显示）"
        return txt

    def _my_range(self, s, tobj, label):
        """本周 = 今天到本周日；下周 = 下周一至下周日。"""
        today = datetime.date.today()
        if label == "本周":
            a = today
            b = today + datetime.timedelta(days=6 - today.weekday())
        else:
            a = today + datetime.timedelta(days=7 - today.weekday())
            b = a + datetime.timedelta(days=6)
        rows = [x for x in self._participated(s, tobj) if a.isoformat() <= x.date <= b.isoformat()]
        return self._rows_text(s, rows, "%s老师，%s（%s ~ %s）共 %d 节："
                               % (tobj.name, label, a.isoformat()[5:], b.isoformat()[5:], len(rows)), tobj)

    def _day_schedule(self, s, which):
        """当天/次日的全院课表。属于公开信息，未登记手机号的人也能查。"""
        today = datetime.date.today()
        d = today + datetime.timedelta(days=1 if which in ("明天", "明日的课", "明日") else 0)
        iso = d.isoformat()
        hol = s.exec(select(models.Holiday).where(models.Holiday.date == iso)).first()
        rows = s.exec(select(models.ClassSession)
                      .where(models.ClassSession.date == iso,
                             models.ClassSession.status == "published")
                      .order_by(models.ClassSession.slot)).all()
        title = "%s %s 周%s 的课程：" % ("明天" if d != today else "今天",
                                       iso[5:], "一二三四五六日"[d.weekday()])
        if hol:
            title = "【%s】%s%s" % (hol.name or "停课", title, "\n（当天为节假日安排，以实际通知为准）")
        return self._rows_text(s, rows, title, None, with_teachers=True)

    # ---------- 老师自助：个人日程 / 个人待办 ----------
    def _dt_ctx(self, s, tobj):
        """取企业应用凭据 + 本人 unionId。失败抛 ValueError（消息直接回给老师）。"""
        import api as app_api
        cfg = s.exec(select(models.DingTalkConfig)).first()
        if not (cfg and cfg.app_key and cfg.app_secret):
            raise ValueError("系统还没配置钉钉企业应用，暂时不能代您建日程/待办。")
        phone = (tobj.phone or "").strip()
        if not app_api._PHONE_RE.match(phone):
            raise ValueError("您的账号还没登记手机号，无法关联到钉钉，请让教务补一下。")
        token = app_api._dt_access_token(cfg)
        return cfg, token, app_api._dt_union_id(token, phone)

    def _add_event(self, s, tobj, text):
        """提醒 12-05 14:00 内容 → 在本人钉钉日历建一条日程（手机原生提醒）。"""
        import api as app_api
        m = re.match(r"^(?:提醒|日程|加日程)\s*[:：]?\s*" + self._date_re()
                     + r"\s*(?:(\d{1,2})\s*[:：]\s*(\d{2}))?\s*(.+)$", text)
        if not m:
            return "用法：提醒 12-05 14:00 技能考核\n（日期必填，时间可省略，默认 09:00，时长 30 分钟）"
        d = self._near_date(int(m.group(1)), int(m.group(2)))
        if not d:
            return "日期不对，请用 12-05 这种写法。"
        hh = int(m.group(3)) if m.group(3) else 9
        mi = int(m.group(4)) if m.group(4) else 0
        content = m.group(5).strip()
        if not (0 <= hh <= 23 and 0 <= mi <= 59):
            return "时间不对，请用 14:00 这种写法。"
        start = datetime.datetime.combine(d, datetime.time(hh, mi))
        end = start + datetime.timedelta(minutes=30)
        try:
            cfg, token, union = self._dt_ctx(s, tobj)
            app_api._dt_create_event(token, union, {
                "summary": content[:100],
                "description": "由教学排课助手创建（您本人发起的个人日程）",
                "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                          "timeZone": "Asia/Shanghai"},
                "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                        "timeZone": "Asia/Shanghai"},
                "reminders": [{"method": "dingtalk", "minutes": 15}],
            })
        except Exception as e:
            return "建日程失败：" + str(e)[:120]
        return ("✓ 已加到您的钉钉日历：%s %02d:%02d  %s\n（提前 15 分钟提醒，时长 30 分钟；手机上直接可见）"
                % (d.strftime("%m月%d日"), hh, mi, content))

    def _add_todo(self, s, tobj, text):
        """待办 12-05 内容 → 给本人建一条钉钉待办。"""
        import api as app_api
        m = re.match(r"^(?:待办|任务|加待办)\s*[:：]?\s*" + self._date_re()
                     + r"\s*(?:(\d{1,2})\s*[:：]\s*(\d{2}))?\s*(.+)$", text)
        if not m:
            return "用法：待办 12-05 提交出科考核表\n（日期必填，时间可省略，默认当天 18:00 截止）"
        d = self._near_date(int(m.group(1)), int(m.group(2)))
        if not d:
            return "日期不对，请用 12-05 这种写法。"
        hh = int(m.group(3)) if m.group(3) else 18
        mi = int(m.group(4)) if m.group(4) else 0
        content = m.group(5).strip()
        due = datetime.datetime.combine(d, datetime.time(hh, mi))
        try:
            cfg, token, union = self._dt_ctx(s, tobj)
            app_api._dt_create_todo_body(token, union, {
                "subject": content[:100],
                "description": "由教学排课助手创建（您本人发起的个人待办）",
                "dueDate": int(due.timestamp() * 1000),
                "executorIds": [union],
            })
        except Exception as e:
            return "建待办失败：" + str(e)[:120]
        return ("✓ 已建待办：%s\n截止 %s %02d:%02d，在钉钉「待办」里可以看到并勾选完成。"
                % (content, d.strftime("%m月%d日"), hh, mi))

    # ---------- 请假登记 ----------
    def _leave(self, s, tobj, text):
        """请假 12-03 / 请假 12-03 至 12-05 → 写入教师空闲日，下次排课自动避开。"""
        if re.match(r"^销假|^取消请假", text):
            m = re.match(r"^(?:销假|取消请假)\s*[:：]?\s*" + self._date_re(), text)
            if not m:
                return "用法：销假 12-03"
            d = self._near_date(int(m.group(1)), int(m.group(2)))
            if not d:
                return "日期不对。"
            days = [x for x in (tobj.leave_dates or "").split(",") if x.strip()]
            if d.isoformat() not in days:
                return "%s 本来就不在您的请假记录里。" % d.isoformat()
            days.remove(d.isoformat())
            tobj.leave_dates = ",".join(sorted(days))
            s.add(tobj); s.commit()
            return "✓ 已销假：%s（剩余请假 %d 天）" % (d.isoformat(), len(days))

        m = re.match(r"^(?:请假|休假)\s*[:：]?\s*" + self._date_re()
                     + r"\s*(?:(?:至|到|~|～)\s*" + self._date_re() + r")?\s*(.*)$", text)
        if not m:
            return "用法：请假 12-03\n或：请假 12-03 至 12-05 家里有事"
        d1 = self._near_date(int(m.group(1)), int(m.group(2)))
        if not d1:
            return "开始日期不对，请用 12-03 这种写法。"
        if m.group(3):
            d2 = self._near_date(int(m.group(3)), int(m.group(4)))
            if not d2:
                return "结束日期不对，请用 12-05 这种写法。"
        else:
            d2 = d1
        if d2 < d1:
            return "结束日期早于开始日期了。"
        span = (d2 - d1).days + 1
        if span > 90:
            return "一次最多登记 90 天，请分段提交。"

        days = [x for x in (tobj.leave_dates or "").split(",") if x.strip()]
        new = [(d1 + datetime.timedelta(days=i)).isoformat() for i in range(span)]
        added = [x for x in new if x not in days]
        days = sorted(set(days) | set(new))
        tobj.leave_dates = ",".join(days)
        s.add(tobj); s.commit()

        # 冲突检查：这些天本人已有已发布的课，需要教务调整
        clash = [x for x in self._participated(s, tobj) if x.date in new]
        period = d1.strftime("%m月%d日") if d1 == d2 else "%s ~ %s" % (d1.strftime("%m月%d日"), d2.strftime("%m月%d日"))
        head = ("✓ 已登记请假：%s，共 %d 天（新增 %d 天）" % (period, span, len(added)))
        tail = "\n下次生成课表会自动避开这些日期。"
        if clash:
            lines = ["%s %s【%s】%s" % (x.date[5:], self._course_time(x),
                                        x.class_type_name, self._role_of(x, tobj.id))
                     for x in clash]
            tail += ("\n\n⚠ 其中 %d 节已发布的课与请假重叠，需要教务调整后才能生效：\n%s"
                     "\n请把这几天的情况同步给教务。"
                     % (len(clash), "\n".join(lines)))
        return head + tail


    def _sync_event(self, sid: int):
        """老师填完课程名后，把该节课的钉钉日程标题同步改掉（未启用日程则什么都不做）。"""
        import api as app_api
        with Session(db.engine) as s:
            cfg = s.exec(select(models.DingTalkConfig)).first()
            if not cfg or not getattr(cfg, "calendar_enabled", False):
                return
            row = s.get(models.ClassSession, sid)
            if not row:
                return
            teachers = {t.id: t for t in s.exec(select(models.Teacher)).all()}
            students = {t.id: t.name for t in s.exec(select(models.Student)).all()}
            try:
                app_api.sync_calendar_events(cfg, row.year, row.month, [row], teachers, students,
                                             app_api._load_rules(s), prune=False)
            except Exception as e:
                # 填报已落库，日程同步失败只记日志，别让填报人收到「处理出错」
                print("[bot] 课程名/题目变更后的日程同步失败（不影响填报）：%s" % str(e)[:150], flush=True)

    def _teachers(self, s):
        return {t.id: t for t in s.exec(select(models.Teacher)).all()}

    def _editable_courses(self, s, tobj):
        """本人是「责任填报人」的已发布课程。

        一节课有讲课/主阅/评审/指导多个角色，但课程名只该有一个人写。
        责任人 = 讲课老师；讲课老师没登记手机号时顺延给本课其他角色（见 api.session_owner）。
        因此同一节课不会同时出现在两位老师的待填清单里，也不会互相覆盖。
        """
        import api as app_api
        teachers = self._teachers(s)
        rows = s.exec(select(models.ClassSession)
                      .where(models.ClassSession.status == "published")
                      .order_by(models.ClassSession.date)).all()
        out = []
        for x in rows:
            owner, _, _ = app_api.session_owner(x, teachers)
            if owner and owner.id == tobj.id:
                out.append(x)
        out.sort(key=lambda x: (0 if not x.topic else 1, x.date))
        return out

    def _related_courses(self, s, tobj):
        """本人参与、但课程名不由本人负责的已发布课程（只读）。"""
        import api as app_api
        teachers = self._teachers(s)
        rows = s.exec(select(models.ClassSession)
                      .where(models.ClassSession.status == "published")
                      .order_by(models.ClassSession.date)).all()
        out = []
        for x in rows:
            if not any(tid == tobj.id for _, tid in app_api.session_roles(x)):
                continue
            owner, _, _ = app_api.session_owner(x, teachers)
            if not owner or owner.id != tobj.id:
                out.append(x)
        return out

    def _role_of(self, x, tid):
        import api as app_api
        for role, t in app_api.session_roles(x):
            if t == tid:
                return role
        return ""

    def _teacher_name(self, s, tid):
        t = s.get(models.Teacher, tid) if tid else None
        return (t.name if t else "") or ("教师#%d" % tid)

    def _no_duty_hint(self, s, tobj):
        """本人没课可填时的说明：区分「真没课」和「有课但由别人负责」。"""
        rel = self._related_courses(s, tobj)
        if rel:
            import api as app_api
            teachers = self._teachers(s)
            lines = []
            for x in rel[:8]:
                owner, _, _ = app_api.session_owner(x, teachers)
                whose = ("由 %s 老师填报" % owner.name) if owner else "由讲课老师填报（该老师未登记手机号）"
                lines.append("%s【%s】您是%s，%s"
                             % (x.date[5:], x.class_type_name, self._role_of(x, tobj.id), whose))
            return ("%s老师：您参与的课程中，课程名由讲课老师负责填写，您不需要填报：\n%s"
                    % (tobj.name, "\n".join(lines)))
        return "%s老师：您当前没有需要填报的已发布课程。" % tobj.name

    def _not_owner_hint(self, s, tobj, x):
        import api as app_api
        owner, role, proxy = app_api.session_owner(x, self._teachers(s))
        whose = ("%s 老师（%s）" % (owner.name, role)) if owner else "未指定"
        return ("%s 这节课的课程名由 %s 负责，您是%s，不需要填写。\n"
                "如确需您来定，请联系教务调整排课，或让负责人在群里填报。"
                % (x.date[5:], whose, self._role_of(x, tobj.id) or "参与人"))

    def _list_courses(self, s, tobj):
        items = self._editable_courses(s, tobj)
        lines = []
        for x in items:
            state = ("%s（%s）" % (x.topic, self._teacher_name(s, x.topic_by) + "填")
                     if x.topic and x.topic_by else (x.topic or "**未填报**"))
            lines.append("%s %s【%s】%s · %s"
                         % (x.date, self._course_time(x), x.class_type_name,
                            self._role_of(x, tobj.id), state))
        head = "%s老师，待您填报 %d 节：" % (tobj.name, len(items))
        body = "\n".join(lines) if lines else "（没有待填报的课）"
        rel = self._related_courses(s, tobj)
        tail = ""
        if rel:
            tail = ("\n\n另有 %d 节您参与、由他人负责填报的课，在钉钉日历里可见，无需您操作。" % len(rel))
        if not items:
            return "%s\n%s\n%s" % (head, body, tail.strip())
        return ("%s\n%s%s\n\n填报：直接回复「12-03 课程名」；改已填的：回复「改 12-03 新课程名」。"
                % (head, body, tail))

    def _urge(self, s):
        """催办：列出已发布课程中还没填课程名的课，按负责老师聚合，并返回需要 @ 的手机号。"""
        import api as app_api
        rows = s.exec(select(models.ClassSession)
                      .where(models.ClassSession.status == "published")
                      .order_by(models.ClassSession.date)).all()
        teachers = {t.id: t for t in s.exec(select(models.Teacher)).all()}
        rules = app_api._load_rules(s)

        missing = [x for x in rows if not (x.topic or "").strip()]
        # 研究生汇报的「题目」由汇报人自己填报，不走老师催办
        grad_missing = [x for x in missing if x.class_type_name == "研究生汇报"]
        missing = [x for x in missing if x.class_type_name != "研究生汇报"]
        grad_note = ("\n另有 %d 场研究生汇报未定题目，由汇报人自己在群里发「12-03 题目」填报。"
                     % len(grad_missing)) if grad_missing else ""
        if not rows:
            return "还没有已发布的课表，发布后再催办。", []
        if not missing:
            return "✓ 已发布课程的课程名都填好了，没有需要催办的。" + grad_note, []

        today = datetime.date.today().isoformat()
        future = [x for x in missing if x.date >= today]
        past = [x for x in missing if x.date < today]

        agg = {}     # tid -> [描述]
        no_duty = [] # 连责任人都定不出来的课（本课老师都没手机号）
        for x in future:
            t_ = app_api._course_time(x.class_type_name, x.month, rules) or "17:30"
            owner, role, proxy = app_api.session_owner(x, teachers)
            if not owner:
                no_duty.append("%s %s【%s】" % (x.date[5:], t_, x.class_type_name))
                continue
            tag = role + ("(代)" if proxy else "")
            agg.setdefault(owner.id, []).append("%s %s【%s】%s" % (x.date[5:], t_, x.class_type_name, tag))

        if not agg:
            tail = "（另有 %d 节已过期课程未填，可让老师补填）" % len(past) if past else ""
            extra = ("\n%d 节课的相关老师都没登记手机号，无法催办：%s"
                     % (len(no_duty), "、".join(no_duty[:5]))) if no_duty else ""
            return "✓ 今天之后没有可催办的待填课程。%s%s%s" % (tail, grad_note, extra), []

        lines, mobiles = [], []
        for tid in sorted(agg, key=lambda i: (teachers[i].name or "")):
            t = teachers[tid]
            ph = (t.phone or "").strip()
            tagged = "@%s " % ph if app_api._PHONE_RE.match(ph) else ""
            lines.append("%s%s：%s（%d 节）" % (tagged, t.name, "、".join(agg[tid]), len(agg[tid])))
            if app_api._PHONE_RE.match(ph):
                mobiles.append(ph)
        head = "催办：还有 %d 节课程没填课程名" % len(future)
        tail = "填报：@排课助手 12-03 课程名（只发课程名则填最早一节）"
        if past:
            tail += "\n另有 %d 节已过期课程未填，可让老师补填。" % len(past)
        if no_duty:
            tail += ("\n%d 节课的相关老师都没登记手机号，无法 @ 到人：%s"
                     % (len(no_duty), "、".join(no_duty[:4])))
        tail += grad_note
        return "\n".join([head, ""] + lines + ["", tail]), mobiles

    # ---------- Stream 回调 ----------
    def process(self, callback: dingtalk_stream.CallbackMessage):
        msg = dingtalk_stream.ChatbotMessage.from_dict(callback.data or {})
        try:
            text = (msg.text.content or "").strip() if msg.text else ""
            # 调试用：记录群会话 ID，便于排查卡片投放（cid 取错会报 cidParse.wrong）
            print("[bot] 收到消息 from=%s cid=%s conv_type=%s" %
                  (msg.sender_nick, msg.conversation_id, msg.conversation_type), flush=True)
            if text.startswith("@"):
                text = re.sub(r"^@\S+\s*", "", text)          # 去掉 @机器人名
            out = self.handle_command(text, msg.sender_staff_id or "",
                                      msg.conversation_id or "")
            reply, ats = out if isinstance(out, tuple) else (out, None)
            if msg.session_webhook:
                self._reply(msg.session_webhook, reply, ats)
        except Exception as e:
            try:
                if msg.session_webhook:
                    self._reply(msg.session_webhook, "处理出错：" + str(e)[:100])
            except Exception:
                pass
        return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    @staticmethod
    def _reply(session_webhook: str, text: str, at_mobiles=None):
        def _send(payload):
            req = urllib.request.Request(session_webhook, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        payload = {"msgtype": "text", "text": {"content": text}}
        if at_mobiles:
            payload["at"] = {"atMobiles": list(at_mobiles), "isAtAll": False}
        try:
            _send(payload)
        except Exception:
            # @ 失败（如 sessionWebhook 不支持 at）则退回纯文本，保证消息能发出去
            if at_mobiles:
                _send({"msgtype": "text", "text": {"content": text}})
            else:
                raise


class CourseCardHandler(dingtalk_stream.CallbackHandler):
    """互动卡片按钮回调（topic: /v1.0/card/instances/callback）。

    这条路走的是机器人已经建好的那条 WebSocket，钉钉不会访问本系统的任何地址，
    所以系统部署在纯内网、没有公网域名时，按钮照样能点。
    """

    async def process(self, callback):
        try:
            # 同步逻辑（查库 + 调钉钉接口）丢到线程池，别阻塞长连接的读循环
            return await asyncio.to_thread(self._handle, callback.data or {})
        except Exception as e:
            print("[bot] 卡片回调异常：%s" % str(e)[:150], flush=True)
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    def _handle(self, payload):
        print("[bot] 卡片回调原文：%s" % json.dumps(payload, ensure_ascii=False)[:800], flush=True)
        action, userid, otid = dingtalk_card.parse_action(payload)
        if not action or not otid:
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"
        tid = dingtalk_card.teacher_of_card(otid)
        with Session(db.engine) as s:
            row = s.exec(select(models.DingTalkConfig)).first()
            if not row:
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"
            cfg = types.SimpleNamespace(app_key=row.app_key, app_secret=row.app_secret,
                                        card_template_id=row.card_template_id,
                                        card_enabled=row.card_enabled)
            owner = s.get(models.Teacher, tid) if tid else None
            if not owner:
                print("[bot] 卡片回调找不到对应教师：%s" % otid, flush=True)
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"
            clicker = teacher_by_userid(userid) if userid else None
            if clicker and clicker.id != owner.id:
                # 卡片发在群里人人都看得见，别人点了不写任何数据，静默忽略
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

            hint = ""
            if action == dingtalk_card.ACT_ACK:
                self._ack(s, otid, userid, owner)
            elif action == dingtalk_card.ACT_FILL:
                hint = "填报方式：在群里发「@排课助手 12-03 课程名」，只发课程名则填到最早一节未填的课。"
            else:
                print("[bot] 未识别的卡片按钮 action=%r" % action, flush=True)
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

            acks = s.exec(select(models.CardAck)
                          .where(models.CardAck.out_track_id == otid)).all()
            names = [a.name for a in acks]
            rows = card_rows(s, owner)
            # 普通版卡片：整卡重绘，确认过就把「我已确认」置灰
            std = dingtalk_card.build_standard_card(owner, rows, names, hint,
                                                    disabled_ack=bool(names))
            # 模板版卡片：只换变量
            data = dingtalk_card.build_card_data(owner, rows, names, hint)

        ok, note = dingtalk_card.update_standard_card(cfg, otid, std)
        if not ok:
            print("[bot] 普通版更新失败（改试模板版）：%s" % note, flush=True)
            if dingtalk_card.card_ready(cfg):
                ok, note = dingtalk_card.update_card(cfg, otid, data)
                if not ok:
                    print("[bot] %s" % note, flush=True)
        return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    @staticmethod
    def _ack(s, otid, userid, owner):
        """记录「已确认」，同一人重复点只算一次。"""
        exists = s.exec(select(models.CardAck).where(
            models.CardAck.out_track_id == otid,
            models.CardAck.userid == (userid or ""))).first()
        if exists:
            return
        s.add(models.CardAck(out_track_id=otid, userid=userid or "",
                             name=owner.name or "", action=dingtalk_card.ACT_ACK,
                             created_at=datetime.datetime.now().isoformat(timespec="seconds")))
        s.commit()


def _patch_ws_timeout():
    """SDK 用 websockets.connect 的默认握手超时（10s），内网/弱网易超时 → 放宽到 30s。
    同时把心跳间隔设成 20s，避免长连接被中间设备掐断。"""
    try:
        from dingtalk_stream import stream as ds_stream
        ws = ds_stream.websockets
        if getattr(ws, "_wb_timeout_patched", False):
            return
        orig = ws.connect

        def connect(*a, **kw):
            kw.setdefault("open_timeout", 30)     # 默认 10s，内网/弱网易超时
            kw.setdefault("ping_interval", 20)    # 心跳，防中间设备掐断
            kw.setdefault("ping_timeout", 20)
            return orig(*a, **kw)

        ws.connect = connect
        ws._wb_timeout_patched = True
    except Exception:
        pass


def _setup_logger():
    """把 SDK 的日志接到控制台（uvicorn 下 root logger 没有 handler，SDK 日志默认看不见）。"""
    lg = logging.getLogger("dingtalk_stream")
    lg.setLevel(logging.INFO)
    if not lg.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("[bot] %(levelname)s %(message)s"))
        lg.addHandler(h)
    lg.propagate = False


def _new_loop():
    """为机器人线程建一个独立事件循环；Windows 优先 Selector（Proactor 在多线程下握手会卡死）。"""
    try:
        return asyncio.SelectorEventLoop()
    except Exception:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def run_forever():
    """阻塞运行 Stream 客户端；断线自动重连。在 daemon 线程中调用。"""
    _setup_logger()
    _patch_ws_timeout()
    # SDK 里有一处 logger.exception('unknown exception', e) 的格式串写错（msg 无 %s 却带了参数），
    # 每次重连失败都会让 logging 内部打印大段 "--- Logging error ---"。关掉这层噪音，错误由我们自己打印。
    logging.raiseExceptions = False
    while True:
        try:
            with Session(db.engine) as s:
                cfg = s.exec(select(models.DingTalkConfig)).first()
            if not cfg or not getattr(cfg, "bot_enabled", False) or not cfg.app_key or not cfg.app_secret:
                time.sleep(60)
                continue
            # 注意：DingTalkStreamClient 第一个参数是 Credential 对象（不是 app_key），
            # 传错会把 app_secret 当成 logger，导致 'str' object has no attribute 'info'
            client = dingtalk_stream.DingTalkStreamClient(
                dingtalk_stream.Credential(cfg.app_key, cfg.app_secret),
                logger=logging.getLogger("dingtalk_stream.client"))
            client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC, CourseBotHandler())
            # 卡片按钮回调也走这条 WebSocket（callbackType=STREAM），不依赖任何对外地址
            client.register_callback_handler(
                dingtalk_stream.CallbackHandler.TOPIC_CARD_CALLBACK, CourseCardHandler())
            print("[bot] 群机器人 Stream 连接中…", flush=True)
            # 注意：Windows 默认的 ProactorEventLoop 在「主线程跑 uvicorn + 子线程跑机器人」时，
            # websocket 握手会卡死且不超时（线程栈表现为空转）。用 SelectorEventLoop 可稳定建连。
            loop = _new_loop()
            try:
                loop.run_until_complete(client.start())
            finally:
                loop.close()
        except BaseException as e:   # 含 CancelledError：任何情况都不能让机器人线程死掉
            print("[bot] 断开，60 秒后重连：%s %s" % (type(e).__name__, str(e)[:120]), flush=True)
            time.sleep(60)
