"""排课规则：默认值 / 合并 / 校验。

设计原则：DEFAULT_RULES 就是系统原本"内定"的行为，
未配置任何规则时，排课结果与改造前完全一致（零风险迁移）。
"""

import re
from copy import deepcopy

# 讲课日：0=周一 … 6=周日（Python weekday 约定）
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

DEFAULT_RULES = {
    # ---------- 一、结构 ----------
    "weekdays": [1],         # 讲课日：可多选（0=周一 … 6=周日），默认周二
    "days_per_month": 4,     # 每月讲授日数
    "slots_per_day": 3,      # 每天节次数（第3节为研究生汇报）

    # ---------- 二、课型编排 ----------
    # 长度 = slots_per_day；每项是该节的"课型循环序列"，按周次循环取用
    "slot_types": [
        ["小讲课"],                     # 第1节：恒为小讲课
        ["教学阅片", "病例讨论"],        # 第2节：第1周阅片、第2周病例（与基地模板一致）
        ["研究生汇报"],                  # 第3节：研究生汇报（只有汇报人 + 题目）
    ],

    # ---------- 三、角色规则 ----------
    "small_lecture_reader_is_teacher": True,  # 小讲课：主阅 = 讲课老师本人
    "reader_source": "student",               # 主阅来源：student / teacher
    "distinct_roles": True,                   # 老师 ≠ 评审
    "reviewer_avoid_teacher_in_month": True,  # 评审避开本月已讲过课的人
    "mentor_enabled": True,                   # 生成时自动安排指导老师（取自「可指导」教师）

    # ---------- 四、轮转规则 ----------
    "monthly_limit": {"teacher": 1, "review": 1, "student": 1, "mentor": 1},  # 每人每月上限，0 = 不限
    "coverage_bias": {"teacher": True, "review": True, "student": False, "mentor": True},  # 三型覆盖偏置
    "respect_leave": True,                    # 生成时跳过请假日期

    # ---------- 五、节假日 ----------
    # same_weekday=顺延到下一个同星期几 / next_workday=顺延到下一个工作日 / none=不补
    "holiday_policy": "same_weekday",

    # ---------- 六、时间 ----------
    "summer_months": [5, 6, 7, 8, 9],         # 夏令月份，其余为冬令
    "time_rules": {
        "小讲课": "8:00",
        "入科教育": "8:00",
        "教学阅片": {"summer": "18:00", "winter": "17:30"},
        "病例讨论": {"summer": "18:00", "winter": "17:30"},
    },
    "orientation": {"enabled": True, "name": "入科教育", "time": "8:00"},  # 导出首行
    "feedback_offsets": {"出科考核反馈": 60, "月考反馈": 90},               # 末节课后偏移分钟
    "default_topic": "自选题",
}

HOLIDAY_POLICIES = ("same_weekday", "next_workday", "none")
READER_SOURCES = ("student", "teacher")

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def merge_rules(saved):
    """把已保存的规则叠加到默认值上（浅合并两层，保证新增字段自动带默认值）。

    兼容旧版：旧的 weekday（单选 int）自动映射为 weekdays = [weekday]。
    """
    r = deepcopy(DEFAULT_RULES)
    if not isinstance(saved, dict):
        return r
    saved = dict(saved)
    if "weekday" in saved and "weekdays" not in saved:
        try:
            saved["weekdays"] = [int(saved["weekday"])]
        except (TypeError, ValueError):
            pass
    for k, v in saved.items():
        if k not in r:
            continue
        if isinstance(r[k], dict) and isinstance(v, dict):
            for kk, vv in v.items():
                if isinstance(r[k].get(kk), dict) and isinstance(vv, dict):
                    r[k][kk].update(vv)
                else:
                    r[k][kk] = vv
        else:
            r[k] = v
    return r


def diff_rules(rules):
    """只保存与默认值不同的项，便于以后升级默认值。"""
    out = {}
    for k, v in (rules or {}).items():
        if k not in DEFAULT_RULES:
            continue
        if v != DEFAULT_RULES[k]:
            out[k] = v
    return out


def validate(rules, class_type_names=None):
    """返回错误信息列表（空 = 通过）。class_type_names 用于校验课型名是否存在。"""
    errs = []
    r = merge_rules(rules)

    wds = r.get("weekdays")
    if (not isinstance(wds, list) or not wds
            or any((not isinstance(w, int)) or w < 0 or w > 6 for w in wds)):
        errs.append("讲课日需为周一~周日（0~6）的多选列表，至少选 1 天")
    elif len(set(wds)) != len(wds):
        errs.append("讲课日存在重复选择")

    dpw = r.get("days_per_month")
    if not isinstance(dpw, int) or not (1 <= dpw <= 12):
        errs.append("每月讲授日数需在 1~12 之间")

    spd = r.get("slots_per_day")
    if not isinstance(spd, int) or not (1 <= spd <= 6):
        errs.append("每天节次数需在 1~6 之间")

    seqs = r.get("slot_types")
    if not isinstance(seqs, list) or not seqs:
        errs.append("课型编排不能为空")
    else:
        if isinstance(spd, int) and len(seqs) != spd:
            errs.append("课型编排的节数（%d）必须等于每天节次数（%d）" % (len(seqs), spd))
        for i, seq in enumerate(seqs, start=1):
            if not isinstance(seq, list) or not seq:
                errs.append("第 %d 节的课型序列不能为空" % i)
                continue
            for name in seq:
                if not isinstance(name, str) or not name.strip():
                    errs.append("第 %d 节存在空的课型名" % i)
                elif class_type_names is not None and name not in class_type_names:
                    errs.append("第 %d 节的课型「%s」不存在，请先在课程类型里添加" % (i, name))

    if r.get("reader_source") not in READER_SOURCES:
        errs.append("主阅来源只能是 学生 / 教师")

    ml = r.get("monthly_limit") or {}
    for role, label in (("teacher", "老师"), ("review", "评审"), ("student", "主阅"), ("mentor", "指导")):
        v = ml.get(role, 1)
        if not isinstance(v, int) or v < 0 or v > 20:
            errs.append("%s每月上限需在 0~20 之间（0 = 不限）" % label)

    if r.get("holiday_policy") not in HOLIDAY_POLICIES:
        errs.append("节假日策略不合法")

    sm = r.get("summer_months") or []
    if not isinstance(sm, list) or any((not isinstance(m, int)) or m < 1 or m > 12 for m in sm):
        errs.append("夏令月份需为 1~12 的整数列表")

    tr = r.get("time_rules") or {}
    if not isinstance(tr, dict) or not tr:
        errs.append("时间规则不能为空")
    else:
        for name, val in tr.items():
            if isinstance(val, dict):
                for season in ("summer", "winter"):
                    tv = val.get(season, "")
                    if tv and not _TIME_RE.match(str(tv)):
                        errs.append("「%s」的%s时间格式应为 H:MM（如 17:30）" % (name, "夏令" if season == "summer" else "冬令"))
            else:
                if val and not _TIME_RE.match(str(val)):
                    errs.append("「%s」的时间格式应为 H:MM（如 8:00）" % name)

    off = r.get("feedback_offsets") or {}
    for k, v in off.items():
        if not isinstance(v, int) or v < 0 or v > 600:
            errs.append("「%s」时间偏移需为 0~600 分钟" % k)

    return errs


def parse_seq(text):
    """把「教学阅片,病例讨论」这类输入解析成列表（支持中英文逗号、顿号、空格）。"""
    if isinstance(text, list):
        return [str(x).strip() for x in text if str(x).strip()]
    if not text:
        return []
    parts = re.split(r"[,，、;；\s]+", str(text))
    return [p for p in (x.strip() for x in parts) if p]
