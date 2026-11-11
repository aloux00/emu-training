# -*- coding: utf-8 -*-
"""离线验证：课时名字快照（删人不影响历史课表显示）+ 月末反馈汇报人类型限制（隔离临时目录）。"""
import os, sys, shutil, tempfile, json
_PROJ = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="snapname-")
shutil.copy(os.path.join(_PROJ, "schedule.db"), os.path.join(_TMP, "schedule.db"))
os.chdir(_TMP); sys.path.insert(0, _PROJ)

import db, models, api
db.init_db()
from sqlmodel import Session, select

FAILS = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)[:220]) if detail and not cond else ""))
    if not cond: FAILS.append(name)

# 1. 迁移：快照列已存在
import sqlite3
conn = sqlite3.connect("schedule.db")
cols = [r[1] for r in conn.execute("PRAGMA table_info(classsession)").fetchall()]
conn.close()
need = ("teacher_name", "main_reader_name", "reviewer_name", "mentor_name", "presenter_name")
check("迁移：快照列齐全", all(c in cols for c in need), [c for c in need if c not in cols])

# 2. 生成课表 → 快照写入
api.generate({"year": 2098, "month": 3}, None)
with Session(db.engine) as s:
    rows = s.exec(select(models.ClassSession)
                  .where(models.ClassSession.year == 2098, models.ClassSession.month == 3)).all()
    check("生成课表有产出", len(rows) > 0, len(rows))
    teachers = {t.id: t.name for t in s.exec(select(models.Teacher)).all()}
    students = {t.id: t.name for t in s.exec(select(models.Student)).all()}
    grads = {g.id: g.name for g in s.exec(select(models.Graduate)).all()}
    bad_t = [x.id for x in rows if x.teacher_id and x.teacher_name != teachers.get(x.teacher_id)]
    bad_m = [x.id for x in rows if x.main_reader_id and not x.main_reader_name]
    bad_p = [x.id for x in rows if x.presenter_id and x.presenter_name != grads.get(x.presenter_id)]
    check("老师快照=实时名", not bad_t, bad_t[:5])
    check("主阅快照非空", not bad_m, bad_m[:5])
    check("汇报人快照=实时名", not bad_p, bad_p[:5])
    # 找一节学生主阅的课，记录学生名与学生 id
    target = next((x for x in rows if x.main_reader_id in students), None)
    check("存在学生主阅的课", target is not None)
    if target:
        STU_ID = target.main_reader_id
        STU_NAME = students[STU_ID]

# 3. 删除该学生 → 导出/钉钉文本仍显示名字（快照回退）
if target:
    api.del_student(STU_ID, None)
    with Session(db.engine) as s:
        rows = s.exec(select(models.ClassSession)
                      .where(models.ClassSession.year == 2098, models.ClassSession.month == 3)).all()
        rules = api._load_rules(s)
        students_map = {t.id: t.name for t in s.exec(select(models.Student)).all()}
        export_rows, _n = api._month_export_rows(s, 2098, 3, teachers, students_map, grads, rules)
        col_main = [r[4] for r in export_rows]
        check("删学生后导出仍显示主阅名（快照）", STU_NAME in col_main, col_main[:8])
        text = api.build_dingtalk_text(2098, 3, rows, teachers, students_map, grads)
        check("删学生后钉钉文本仍显示主阅名（快照）", STU_NAME in text, text[:200])

# 4. upd 改评审 → 快照同步刷新
with Session(db.engine) as s:
    rows = s.exec(select(models.ClassSession)
                  .where(models.ClassSession.year == 2098, models.ClassSession.month == 3)).all()
    t2 = next(t for t in s.exec(select(models.Teacher)).all())
    x = rows[0]
    SID = x.id
    new_rev = t2.id
r = api.upd_session(SID, {"reviewer_id": new_rev}, None)
check("upd 返回快照已刷新", r["reviewer_name"] != "", r.get("reviewer_name"))
with Session(db.engine) as s:
    t2name = s.get(models.Teacher, new_rev).name
check("upd 快照=新评审名", r["reviewer_name"] == t2name, (r["reviewer_name"], t2name))

# 5. 旧数据（快照为空）回退实时查询：2026-11 既有课表导出主阅列仍有名字
with Session(db.engine) as s:
    old_rows = s.exec(select(models.ClassSession)
                      .where(models.ClassSession.year == 2026, models.ClassSession.month == 11)).all()
    if old_rows:
        filled = sum(1 for x in old_rows if x.main_reader_id and x.main_reader_name)
        check("旧课表快照已被 v8 回填", filled > 0, filled)
        students = {t.id: t.name for t in s.exec(select(models.Student)).all()}
        teachers_m = {t.id: t.name for t in s.exec(select(models.Teacher)).all()}
        grads_m = {g.id: g.name for g in s.exec(select(models.Graduate)).all()}
        rules = api._load_rules(s)
        export_rows, _n = api._month_export_rows(s, 2026, 11, teachers_m, students, grads_m, rules)
        has_name = any(r[4] for r in export_rows)
        check("旧课表导出回退实时名仍有主阅", has_name, [r[4] for r in export_rows][:6])
    else:
        print("SKIP 旧课表回退检查（2026-11 无数据）")

# 6. 月末反馈汇报人类型限制：月考=学生、出科=老师，类型不符自动清空
r = api.save_feedback({"year": 2098, "month": 3, "items": [
    {"kind": "月考反馈", "reporter_type": "teacher", "reporter_id": 1, "note": "x"},
    {"kind": "出科考核反馈", "reporter_type": "student", "reporter_id": 1, "note": "y"},
]}, None)
items = {x["kind"]: x for x in r["items"]}
check("月考反馈误选老师→清空", items["月考反馈"]["reporter_type"] == "" and items["月考反馈"]["reporter_id"] == 0, items["月考反馈"])
check("出科反馈误选学生→清空", items["出科考核反馈"]["reporter_type"] == "" and items["出科考核反馈"]["reporter_id"] == 0, items["出科考核反馈"])

with Session(db.engine) as s:
    stu = s.exec(select(models.Student)).first()
    tea = s.exec(select(models.Teacher)).first()
    STU2, STU2N, TEA2, TEA2N = stu.id, stu.name, tea.id, tea.name
r = api.save_feedback({"year": 2098, "month": 3, "items": [
    {"kind": "月考反馈", "reporter_type": "student", "reporter_id": STU2, "note": "ok"},
    {"kind": "出科考核反馈", "reporter_type": "teacher", "reporter_id": TEA2, "note": "ok"},
]}, None)
items = {x["kind"]: x for x in r["items"]}
check("月考反馈=学生保存成功", items["月考反馈"]["reporter"] == STU2N, items["月考反馈"])
check("出科反馈=老师保存成功", items["出科考核反馈"]["reporter"] == TEA2N, items["出科考核反馈"])

# 7. 汇报人不存在的 id → 清空（不产生悬空引用）
r = api.save_feedback({"year": 2098, "month": 3, "items": [
    {"kind": "月考反馈", "reporter_type": "student", "reporter_id": 99999, "note": ""},
]}, None)
items = {x["kind"]: x for x in r["items"]}
check("不存在的学生 id→清空", items["月考反馈"]["reporter_type"] == "", items["月考反馈"])

print()
print("== %d FAIL ==" % len(FAILS) if FAILS else "== ALL PASS ==")
for f in FAILS: print(" -", f)
