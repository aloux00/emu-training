# -*- coding: utf-8 -*-
"""离线验证：课表导入按文件日期自动分月（隔离临时目录运行，不碰生产库）。"""
import os
import sys
import io
import csv
import shutil
import tempfile

_PROJ = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="schimp-")
shutil.copy(os.path.join(_PROJ, "schedule.db"), os.path.join(_TMP, "schedule.db"))
os.chdir(_TMP)
sys.path.insert(0, _PROJ)
print("隔离工作目录:", _TMP)

import db
import models
import api
db.init_db()      # 副本库补齐新列（aliases 等）
from sqlmodel import Session, select

FAILS = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)[:200]) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

class FakeUp:
    def __init__(self, name, buf):
        self.filename = name
        self.file = io.BytesIO(buf)

# 取一名真实教师/学生姓名
with Session(db.engine) as s:
    t = s.exec(select(models.Teacher)).first()
    t2 = s.exec(select(models.Teacher).where(models.Teacher.id != t.id)).first()
    st = s.exec(select(models.Student)).first()
    TN, T2N, SN = t.name, t2.name, st.name
print("测试人员: 老师=%s 老师2=%s 学生=%s" % (TN, T2N, SN))

# 跨月文件：1月、2月、11月 三个月 + 一行错误（查无此人）+ 短格式日期（用所选年份）
rows = [
    ["日期", "节次", "课程", "题目", "老师", "主阅", "评审", "指导"],
    ["2026-01-06", 1, "小讲课", "头颅CT入门", TN, "", "", ""],
    ["", 2, "教学阅片", "", T2N, SN, TN, ""],            # 沿用 2026-01-06（合并单元格）
    ["2026-01-13", 1, "小讲课", "肺炎影像", T2N, "", "", ""],
    ["2026-02-03", 1, "小讲课", "骨关节X线", TN, "", "", ""],
    ["02.10", 1, "小讲课", "短格式日期", T2N, "", "", ""],  # 2 个数字 → 默认 2026 年
    ["2026-11-03", 1, "小讲课", "心脏磁共振入门", TN, "", "", ""],
    ["", 2, "教学阅片", "", T2N, SN, TN, ""],
    ["2026-11-10", 1, "小讲课", "中耳乳突炎", TN, "", "", ""],
    ["2026-11-10", 2, "教学阅片", "", T2N, SN, TN, ""],
    ["2026-11-17", 2, "教学阅片", "", "查无此人", SN, "", ""],  # 错误行
    ["2026-11-17", 3, "研究生汇报", "颅脑感染", "张研", "", "", ""],  # 研究生（库里若有）
]
buf = io.StringIO()
csv.writer(buf).writerows(rows)
def mkup():
    return FakeUp("全年课表.csv", buf.getvalue().encode("utf-8"))
up = mkup()

# ---------- 1. dry_run：无跨月报错、按月分组 ----------
r = api.import_schedule(up, year=2026, month=0, mode="merge", dry_run=True)
ms = {(m["year"], m["month"]): m for m in r["months"]}
check("识别 3 个月份", set(ms.keys()) == {(2026, 1), (2026, 2), (2026, 11)}, r["months"])
check("1月 3 行", ms[(2026, 1)]["total"] == 3, ms.get((2026, 1)))
check("2月 2 行", ms[(2026, 2)]["total"] == 2, ms.get((2026, 2)))
check("11月 4 行(含汇报+错误行跳过)", ms[(2026, 11)]["total"] == 4, ms.get((2026, 11)))
check("不再报跨月错误", not any("不在所选月份" in e["msg"] for e in r["errors"]), r["errors"])
check("错误行仍被识别(查无此人)", any("查无此人" in e["msg"] for e in r["errors"]), r["errors"])
check("months_brief", "2026年01月" in r["months_brief"] and "2026年11月" in r["months_brief"], r["months_brief"])
check("周次按月独立(11月第1周=11-03)", all(p["week"] >= 1 for p in r["preview"]), r["preview"][:5])
wk11 = {p["date"]: p["week"] for p in r["preview"] if p["date"].startswith("2026-11")}
check("11月周次 03→1 10→2", wk11.get("2026-11-03") == 1 and wk11.get("2026-11-10") == 2, wk11)

# ---------- 2. 真正导入：写入各月、year/month 正确 ----------
with Session(db.engine) as s:
    nov_before = len(s.exec(select(models.ClassSession).where(models.ClassSession.year == 2026,
                                                   models.ClassSession.month == 11)).all())
    jan_before = len(s.exec(select(models.ClassSession).where(models.ClassSession.year == 2026,
                                                   models.ClassSession.month == 1)).all())
    feb_before = len(s.exec(select(models.ClassSession).where(models.ClassSession.year == 2026,
                                                   models.ClassSession.month == 2)).all())
r2 = api.import_schedule(mkup(), year=2026, month=0, mode="merge", dry_run=False)
check("导入返回 months", len(r2["months"]) == 3, r2)
with Session(db.engine) as s:
    jan = s.exec(select(models.ClassSession).where(models.ClassSession.year == 2026,
                                                   models.ClassSession.month == 1)).all()
    feb = s.exec(select(models.ClassSession).where(models.ClassSession.year == 2026,
                                                   models.ClassSession.month == 2)).all()
    nov = s.exec(select(models.ClassSession).where(models.ClassSession.year == 2026,
                                                   models.ClassSession.month == 11)).all()
    check("1月新增 3 节", len(jan) - jan_before == 3, (len(jan), jan_before))
    check("2月新增 2 节", len(feb) - feb_before == 2, (len(feb), feb_before))
    m11 = [m for m in r2["months"] if m["month"] == 11][0]
    check("11月: 命中已有4节全部更新、库不增加", (len(nov) - nov_before == 0) and m11["updated"] == 4 and m11["created"] == 0, (len(nov), nov_before, m11))
    check("汇报行因名册无此人被跳过", any("张研" in e["msg"] or "查无此人" in e["msg"] for e in r2["errors"]), r2["errors"])
    j2 = next((x for x in jan if x.slot == 2), None)
    check("合并单元格沿用日期", j2 and j2.date == "2026-01-06" and j2.class_type_name == "教学阅片",
          j2 and (j2.date, j2.class_type_name))
    feb02 = [x for x in feb if x.date == "2026-02-10"]
    check("短格式日期按所选年份解析", len(feb02) == 1, [x.date for x in feb])
    wk = {x.date: x.week_index for x in nov}
    check("11月周次落库 03→1 10→2 17→3", wk.get("2026-11-03") == 1 and wk.get("2026-11-10") == 2
          and wk.get("2026-11-17") == 3, wk)
    status = {x.status for x in nov}
    check("导入为草稿", status == {"draft"}, status)

# ---------- 3. replace：只删文件涉及的月份 ----------
with Session(db.engine) as s:
    # 记录 12 月现有节（不在文件里），验证 replace 不会误删；另造一节 published
    dec_before = {x.id for x in s.exec(select(models.ClassSession).where(models.ClassSession.month == 12)).all()}
    ct = s.exec(select(models.ClassType)).first()
    extra = models.ClassSession(year=2026, month=12, date="2026-12-08", slot=9,
                                class_type_id=ct.id, class_type_name=ct.name,
                                teacher_id=1, status="published", created_at="now")
    s.add(extra); s.commit()
    dec_before.add(extra.id)
up2 = mkup()
r3 = api.import_schedule(up2, year=2026, month=0, mode="replace", dry_run=True)
check("replace 预览含删除数", sum(m["deleted"] for m in r3["months"]) == jan_before + 3 + feb_before + 2 + nov_before,
      (sum(m["deleted"] for m in r3["months"]), jan_before + 3 + feb_before + 2 + nov_before))
up3 = mkup()
r4 = api.import_schedule(up3, year=2026, month=0, mode="replace", dry_run=False)
with Session(db.engine) as s:
    dec = s.exec(select(models.ClassSession).where(models.ClassSession.year == 2026,
                                                   models.ClassSession.month == 12)).all()
    check("replace 未误删 12 月", {x.id for x in dec} == dec_before, (sorted(dec_before), sorted(x.id for x in dec)))
    nov2 = s.exec(select(models.ClassSession).where(models.ClassSession.month == 11)).all()
    check("11 月被重建为草稿", len(nov2) == 4 and {x.status for x in nov2} == {"draft"}, len(nov2))

# ---------- 4. 单月文件（导出回导）仍正常 ----------
with Session(db.engine) as s:
    nov_rows = s.exec(select(models.ClassSession).where(models.ClassSession.month == 11)).all()
    out = [["日期", "周", "节", "课程", "题目", "老师", "主阅", "评审", "指导"]]
    for x in sorted(nov_rows, key=lambda v: (v.date, v.slot)):
        tn = {t.id: t.name for t in s.exec(select(models.Teacher)).all()}
        out.append([x.date, x.week_index, x.slot, x.class_type_name, x.topic or "",
                    tn.get(x.teacher_id, ""), "", tn.get(x.reviewer_id, ""), tn.get(x.mentor_id, "")])
buf2 = io.StringIO()
csv.writer(buf2).writerows(out)
up4 = FakeUp("11月导出.csv", buf2.getvalue().encode("utf-8"))
r5 = api.import_schedule(up4, year=2026, month=11, mode="merge", dry_run=True)
check("单月导出回导只 1 个月", len(r5["months"]) == 1 and r5["months"][0]["month"] == 11, r5["months"])
check("回导全部为更新", r5["months"][0]["updated"] == 4 and r5["months"][0]["created"] == 0, r5["months"][0])

# ---------- 5. Vue 模板编译（前端改动）
import re as _re
html = open(os.path.join(_PROJ, "static", "index.html"), encoding="utf-8").read()
import subprocess
out_js = subprocess.run(["node", os.path.join(_PROJ, "_test_vue_compile.js")],
                        capture_output=True, text=True)
print(out_js.stdout.strip()[-300:] if out_js.stdout else out_js.stderr.strip()[-300:])
check("前端模板编译脚本通过", out_js.returncode == 0)

print()
print("== %d FAIL ==" % len(FAILS) if FAILS else "== ALL PASS ==")
for f in FAILS:
    print(" -", f)
