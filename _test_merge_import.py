# -*- coding: utf-8 -*-
"""离线验证：xlsx 合并单元格（老师/日期纵向合并）导入（隔离临时目录，不碰生产库）。"""
import os, sys, io, shutil, tempfile
_PROJ = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="mergeimp-")
shutil.copy(os.path.join(_PROJ, "schedule.db"), os.path.join(_TMP, "schedule.db"))
os.chdir(_TMP); sys.path.insert(0, _PROJ)

import db, models, api
db.init_db()      # 副本库补齐新列（aliases 等）
from sqlmodel import Session, select
from openpyxl import Workbook

FAILS = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)[:200]) if detail and not cond else ""))
    if not cond: FAILS.append(name)

class FakeUp:
    def __init__(self, name, buf): self.filename = name; self.file = io.BytesIO(buf)

# 找两名真实教师
with Session(db.engine) as s:
    ts = s.exec(select(models.Teacher)).all()[:2]
    T1, T2 = ts[0].name, ts[1].name

# 造一个含合并单元格的 xlsx：
# 行2-3 同一天两节课，老师纵向合并为一格；行4-5 另一天，日期和老师都纵向合并
wb = Workbook()
ws = wb.active
ws.append(["状态", "日期", "周", "节", "课程", "老师", "主阅", "评审", "指导", "题目"])
ws.append(["", "2026-05-06", "", 1, "小讲课", T1, "", "", "", "课题A"])
ws.append(["", None,          "", 2, "教学阅片", None, "", "", "", ""])   # 日期、老师都是合并格
ws.append(["", "2026-05-13", "", 1, "小讲课", T2, "", "", "", "课题B"])
ws.append(["", None,          "", 2, "教学阅片", None, "", "", "", ""])
ws.merge_cells("B2:B3")  # 日期 2026-05-06 纵向合并 2-3 行
ws.merge_cells("F2:F3")  # 老师 T1 纵向合并 2-3 行
ws.merge_cells("B4:B5")  # 日期 2026-05-13 合并 4-5 行
ws.merge_cells("F4:F5")  # 老师 T2 合并 4-5 行
buf = io.BytesIO(); wb.save(buf)

# 1. 底层读取：合并格已继承
rows = api._sch_read_table(FakeUp("合并格.xlsx", buf.getvalue()))
check("行3老师继承T1", rows[2][5] == T1, rows[2])
check("行3日期继承", str(rows[2][1])[:10] == "2026-05-06", rows[2])
check("行5老师继承T2", rows[4][5] == T2, rows[4])

# 2. 导入 dry_run：不再报「缺少老师」
r = api.import_schedule(FakeUp("合并格.xlsx", buf.getvalue()), year=2026, month=0,
                        mode="merge", dry_run=True)
msgs = [e["msg"] for e in r["errors"]]
check("无缺少老师错误", not any("缺少老师" in m for m in msgs), msgs)
check("识别到5月", 5 in {m["month"] for m in r["months"]}, r["months"])
m5 = [m for m in r["months"] if m["month"] == 5][0]
check("5月4行有效", m5["total"] == 4, m5)

# 3. 真实导入后落库校验
r2 = api.import_schedule(FakeUp("合并格.xlsx", buf.getvalue()), year=2026, month=0,
                         mode="merge", dry_run=False)
check("真实导入零错误", not r2["errors"], r2["errors"])
with Session(db.engine) as s:
    got = s.exec(select(models.ClassSession).where(
        models.ClassSession.year == 2026, models.ClassSession.month == 5,
        models.ClassSession.class_type_name == "教学阅片")).all()
    check("两节阅片都有老师", len(got) == 2 and all(x.teacher_id for x in got),
          [(x.date, x.teacher_id) for x in got])

# 4. CSV 不受影响（无合并概念，回归）
b2 = io.BytesIO()
b2.write(("日期,课程,节,老师\n2026-05-20,小讲课,1,%s\n" % T1).encode("utf-8"))
r3 = api.import_schedule(FakeUp("c.csv", b2.getvalue()), year=2026, month=0,
                         mode="merge", dry_run=True)
check("CSV 回归正常", not any("缺少老师" in e["msg"] for e in r3["errors"]), r3["errors"])

# 5. 无合并格的普通 xlsx 回归
wb3 = Workbook(); w3 = wb3.active
w3.append(["日期", "课程", "节", "老师"])
w3.append(["2026-05-21", "教学阅片", 1, T2])
b3 = io.BytesIO(); wb3.save(b3)
r4 = api.import_schedule(FakeUp("plain.xlsx", b3.getvalue()), year=2026, month=0,
                         mode="merge", dry_run=True)
check("普通xlsx回归正常", not r4["errors"], r4["errors"])

print()
print("== %d FAIL ==" % len(FAILS) if FAILS else "== ALL PASS ==")
for f in FAILS: print(" -", f)
