# -*- coding: utf-8 -*-
"""离线验证：研究生「从教师/学生导入」接口（隔离临时目录）。"""
import os, sys, shutil, tempfile
_PROJ = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="gradimp-")
shutil.copy(os.path.join(_PROJ, "schedule.db"), os.path.join(_TMP, "schedule.db"))
os.chdir(_TMP); sys.path.insert(0, _PROJ)

import db, models, api
db.init_db()
from sqlmodel import Session, select

FAILS = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)[:200]) if detail and not cond else ""))
    if not cond: FAILS.append(name)

with Session(db.engine) as s:
    grad_before = len(s.exec(select(models.Graduate)).all())
    t1, t2 = s.exec(select(models.Teacher)).all()[:2]
    s1 = s.exec(select(models.Student)).first()
    T1, T2, S1 = t1.id, t2.id, s1.id
    T1N, T2N, S1N = t1.name, t2.name, s1.name
    T2E = t2.emp_id
    T1E, S1S, S1G = t1.emp_id, s1.stu_id, (s1.grade or "")
    s.add(models.Graduate(grad_id="G-EXIST", name=T1N))   # 同名已在名册
    s.commit()

# 1. 正常导入：1 教师（同名跳过）+ 1 教师 + 1 学生
r = api.import_graduates_from_roster({"teacher_ids": [T1, T2], "student_ids": [S1]}, None)
check("created=2", r["created"] == 2, r)
check("同名教师被跳过", any(T1N in x for x in r["skipped"]), r["skipped"])
with Session(db.engine) as s:
    gs = s.exec(select(models.Graduate)).all()
    check("名册增加 3 人(含预置同名)", len(gs) - grad_before == 3, (grad_before, len(gs)))
    g2 = s.exec(select(models.Graduate).where(models.Graduate.name == T2N)).first()
    check("教师 grad_id=工号", g2 and g2.grad_id == T2E, g2 and (g2.grad_id, T2E))
    gstu = s.exec(select(models.Graduate).where(models.Graduate.name == S1N)).first()
    check("学生 grad_id=学号、major=年级", gstu and gstu.grad_id == S1S and gstu.major == S1G,
          gstu and (gstu.grad_id, gstu.major, S1G))

# 2. 重复导入：全部跳过
r2 = api.import_graduates_from_roster({"teacher_ids": [T2], "student_ids": [S1]}, None)
check("重复导入 created=0", r2["created"] == 0, r2)
check("重复导入提示跳过", len(r2["skipped"]) == 2, r2["skipped"])

# 3. 空勾选报错清晰
try:
    api.import_graduates_from_roster({}, None)
    check("空勾选应报错", False)
except Exception as e:
    check("空勾选应报错", "勾选" in str(getattr(e, "detail", e)), str(getattr(e, "detail", e)))

print()
print("== %d FAIL ==" % len(FAILS) if FAILS else "== ALL PASS ==")
for f in FAILS: print(" -", f)
