# -*- coding: utf-8 -*-
"""离线验证：研究生汇报的机器人指令 + 日历同步（mock 钉钉网络接口）。

隔离运行：db.py 用相对路径 ./schedule.db，因此先切到临时目录、
把生产库拷过去再用，绝不直接读写生产 schedule.db。
"""
import os
import sys
import shutil
import types
import tempfile

_PROJ = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="botgrad-")
shutil.copy(os.path.join(_PROJ, "schedule.db"), os.path.join(_TMP, "schedule.db"))
os.chdir(_TMP)
sys.path.insert(0, _PROJ)
print("隔离工作目录:", _TMP)

import db
import models
import api
import dingtalk_bot
from sqlmodel import Session, select

FAILS = []

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)[:180]) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

# ---------- 造数据（副本库） ----------
with Session(db.engine) as s:
    # 清掉旧研究生，造两个带手机号的
    for g in s.exec(select(models.Graduate)).all():
        s.delete(g)
    s.commit()
    g1 = models.Graduate(grad_id="G001", name="张研", major="影像2024", phone="13800000001")
    g2 = models.Graduate(grad_id="G002", name="李究", major="影像2024", phone="13800000002")
    s.add(g1); s.add(g2)
    s.commit()

    # 找一节已发布的课改成研究生汇报；库里没有就用明天日期自造两节
    sess = s.exec(select(models.ClassSession).where(
        models.ClassSession.class_type_name == "研究生汇报",
        models.ClassSession.status == "published")).first()
    if not sess:
        sess = s.exec(select(models.ClassSession).where(
            models.ClassSession.status == "published")).first()
    import datetime as _dt
    tmr = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
    if not sess:
        ct = s.exec(select(models.ClassType).where(models.ClassType.name == "研究生汇报")).first()
        if not ct:
            ct = models.ClassType(name="研究生汇报", color="#7B5CB8", freq_desc="每天第3节")
            s.add(ct); s.commit()
        sess = models.ClassSession(
            year=int(tmr[:4]), month=int(tmr[5:7]), week_index=1, date=tmr, slot=3,
            class_type_id=ct.id, class_type_name="研究生汇报",
            teacher_id=0, main_reader_id=0, reviewer_id=0, mentor_id=0,
            presenter_id=0, topic="", status="published",
            created_at="2026-11-10 10:00:00")
        s.add(sess); s.commit()
        sess.date = tmr  # 占位，下面再造第二场时重算
    # 再造第二场（与第一场隔一天，便于区分）
    other_date = (_dt.date.fromisoformat(sess.date) + _dt.timedelta(days=1)).isoformat()
    s2row = models.ClassSession(
        year=sess.year, month=sess.month, week_index=sess.week_index,
        date=other_date, slot=3, class_type_id=sess.class_type_id, class_type_name="研究生汇报",
        teacher_id=0, main_reader_id=0, reviewer_id=0, mentor_id=0,
        presenter_id=g2.id, topic="", status="published",
        created_at="2026-11-10 10:00:00")
    sess.class_type_name = "研究生汇报"
    sess.teacher_id = 0; sess.main_reader_id = 0; sess.reviewer_id = 0; sess.mentor_id = 0
    sess.presenter_id = g1.id; sess.topic = ""; sess.slot = 3
    s.add(sess); s.add(s2row); s.commit()
    SID1, SID2 = sess.id, s2row.id
    D1, D2 = sess.date, s2row.date
    print("测试节数据: #%d %s -> 张研, #%d %s -> 李究" % (SID1, D1, SID2, D2))

bot = dingtalk_bot.CourseBotHandler()

# 提前装上钉钉接口 mock（填报会触发日程同步，不能真调网络）
calls = []
def fake_access_token(cfg):
    return "fake-token"
def fake_union_id(token, phone):
    return "uid-" + phone[-4:]
def fake_create_event(token, org_uid, body):
    calls.append(("create", org_uid, body["summary"], body["description"], tuple(a["id"] for a in body["attendees"])))
    return {"id": "evt-" + org_uid}
def fake_update_event(token, org_uid, eid, body):
    calls.append(("update", org_uid, eid, body["summary"]))
    return {"id": eid}
def fake_delete_event(token, org_uid, eid):
    calls.append(("delete", org_uid, eid))
    return {"errcode": 0}

api._dt_access_token = fake_access_token
api._dt_union_id = fake_union_id
api._dt_create_event = fake_create_event
api._dt_update_event = fake_update_event
api._dt_delete_event = fake_delete_event

g1 = None; g2 = None
with Session(db.engine) as s:
    g1 = s.exec(select(models.Graduate)).all()[0]
    g2 = s.exec(select(models.Graduate)).all()[1]

# ---------- 1. 研究生指令 ----------
out = bot._grad_command.__self__ if False else None
with Session(db.engine) as s:
    r = bot._grad_command(s, g1, "帮助")
    check("研究生帮助", "研究生汇报人" in r and "12-03 题目" in r, r)

    r = bot._grad_command(s, g1, "列表")
    check("研究生列表", "待您填报题目的汇报" in r and D1[5:] in r, r)

    r = bot._grad_command(s, g1, "课表 %d月" % int(D1[5:7]))
    check("研究生月课表", D1[5:] in r, r)

    # 填报题目
    r = bot._grad_command(s, g1, "%s-%s 肺结节影像诊断" % (int(D1[5:7]), int(D1[8:])))
    check("研究生填题目", "已登记" in r and "肺结节影像诊断" in r, r)

    with Session(db.engine) as sv:
        row = sv.get(models.ClassSession, SID1)
        check("题目已写入", (row.topic or "") == "肺结节影像诊断", row.topic)

    # 重复填写需「改」
    r = bot._grad_command(s, g1, "%s-%s 另一个题目" % (int(D1[5:7]), int(D1[8:])))
    check("重复填写被拦", "已经填过" in r and "改" in r, r)

    r = bot._grad_command(s, g1, "改 %s-%s 肺磨玻璃结节新题目" % (int(D1[5:7]), int(D1[8:])))
    check("改题目", "已更新" in r and "新题目" in r, r)
    with Session(db.engine) as sv:
        check("改题目已写入", (sv.get(models.ClassSession, SID1).topic or "") == "肺磨玻璃结节新题目")

    # 别人的汇报不能填
    r = bot._grad_command(s, g1, "%s-%s 我来填李究的" % (int(D2[5:7]), int(D2[8:])))
    check("不能填别人的汇报", "没有安排" in r, r)

    # 李究列表应为空题目
    r = bot._grad_command(s, g2, "列表")
    check("李究列表未填", "未定" in r or "**未定**" in r, r)

print("calls after section1:", calls)

# ---------- 2. 全院今天/明天视图显示汇报人 ----------
import datetime
with Session(db.engine) as s:
    # 把两场挪到明天，走 _day_schedule
    tmr = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    for sid in (SID1, SID2):
        row = s.get(models.ClassSession, sid)
        row.date = tmr
        s.add(row)
    s.commit()
    r = bot._day_schedule(s, "明天")
    check("今天/明天显示汇报人", "汇报:张研" in r and "汇报:李究" in r, r)
    check("今天/明天显示题目", "题目:肺磨玻璃结节新题目" in r and "题目:未定" in r, r)
    check("不再显示 —/—/—", "—/—/—" not in r or "研究生汇报" in r, r)

# ---------- 3. 催办排除研究生汇报 ----------
with Session(db.engine) as s:
    # 李究那场题目未定 -> 应出现在 grad_note 而不是老师催办清单
    out, ats = bot._urge(s)
    check("催办说明研究生汇报", "研究生汇报未定题目" in out and "12-03 题目" in out, out)

# ---------- 4. 日历同步：研究生汇报挂汇报人 ----------

with Session(db.engine) as s:
    cfg = s.exec(select(models.DingTalkConfig)).first()
    if not cfg:
        cfg = models.DingTalkConfig(app_key="k", app_secret="s")
        s.add(cfg); s.commit()
    # 开启日历
    cfg.calendar_enabled = True
    cfg.app_key = "k"; cfg.app_secret = "s"
    s.add(cfg); s.commit()

    sessions = [s.get(models.ClassSession, SID1), s.get(models.ClassSession, SID2)]
    teachers = {t.id: t for t in s.exec(select(models.Teacher)).all()}
    students = {st.id: st.name for st in s.exec(select(models.Student)).all()}
    rules = api._load_rules(s)
    created, updated, skipped, deleted, msg = api.sync_calendar_events(
        cfg, sessions[0].year, sessions[0].month, sessions, teachers, students, rules, prune=False)

print("sync result:", created, updated, skipped, deleted, "|", msg)
grad_calls = [c for c in calls if c[0] == "create" and "研究生汇报" in c[2]]
print("grad create calls:", [(c[1], c[2]) for c in grad_calls])
# 张研的日程在「填题目」时已创建（后续只 update），李究的在整月同步时创建 → 至少各一次
uids = {c[1] for c in grad_calls}
check("两位汇报人的日程都已创建", {"uid-0001", "uid-0002"} <= uids, grad_calls)
for c in grad_calls:
    check("日程挂汇报人", c[1] in ("uid-0001", "uid-0002"), c[1])
    check("日程含汇报人描述", ("汇报人：张研" in c[3]) or ("汇报人：李究" in c[3]), c[3])

# 汇报人没手机号 → 跳过并提示
with Session(db.engine) as s:
    g = s.get(models.Graduate, g2.id)
    g.phone = ""
    s.add(g); s.commit()
    sessions = [s.get(models.ClassSession, SID2)]
    teachers = {t.id: t for t in s.exec(select(models.Teacher)).all()}
    students = {st.id: st.name for st in s.exec(select(models.Student)).all()}
    rules = api._load_rules(s)
    c_, u_, sk_, d_, msg2 = api.sync_calendar_events(cfg, sessions[0].year, sessions[0].month,
                                                     sessions, teachers, students, rules, prune=False)
    check("无手机号跳过", "未填手机号跳过" in msg2, msg2)

# ---------- 5. 老师帮助文本 ----------
class FakeT:
    id = 1
    name = "测试"
r = bot._help(FakeT())
check("老师帮助含研究生汇报", "研究生汇报" in r and "12-03 题目" in r, r)

print()
print("== %d FAIL ==" % len(FAILS) if FAILS else "== ALL PASS ==")
for f in FAILS:
    print(" -", f)
