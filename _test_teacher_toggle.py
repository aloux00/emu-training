# -*- coding: utf-8 -*-
"""教师启用/停用功能隔离测试（不触碰生产库）。"""
import os, sys, tempfile, shutil, io

TMP = tempfile.mkdtemp(prefix="_test_tea_toggle_")
os.chdir(TMP)
sys.path.insert(0, r"C:\WD\code\教学课表")
sys.path.insert(0, r"C:\WD\code\教学课表")  # 确保 import 的是项目代码

import db
db.init_db()

import models
import api
from sqlmodel import Session, select
import fastapi

PASS, FAIL = 0, 0
def check(name, ok, detail=None):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS", name)
    else:
        FAIL += 1
        print("FAIL", name, "|", str(detail)[:200])

# 1. 迁移后 teacher 表含 enabled 列，默认 True
import sqlite3
conn = sqlite3.connect("schedule.db")
cols = [r[1] for r in conn.execute("PRAGMA table_info(teacher)").fetchall()]
check("v9 迁移：teacher 表含 enabled 列", "enabled" in cols, cols)
check("user_version>=9", conn.execute("PRAGMA user_version").fetchone()[0] >= 9)
conn.close()

# 2. 造数据：4 名教师 + 2 名学生 + 课型
with Session(db.engine) as s:
    for i, (emp, nm, teach) in enumerate([("T001", "张老师", True), ("T002", "李老师", True),
                                          ("T003", "王老师", True), ("T004", "赵老师", False)]):
        s.add(models.Teacher(emp_id=emp, name=nm, can_teach=teach,
                             can_main_read=teach, can_review=True, can_mentor=True))
    for i in (1, 2, 3):
        s.add(models.Student(stu_id="S%03d" % i, name="学生%d" % i, grade="一年级"))
    s.add(models.ClassType(name="小讲课", color="#1a7f37", freq_desc="每周1次"))
    s.add(models.ClassType(name="病例讨论", color="#175cd3", freq_desc="每周1次"))
    s.commit()

def get_t(t):
    with Session(db.engine) as s:
        d = s.exec(select(models.Teacher).where(models.Teacher.emp_id == t)).first()
        return d.id, d.enabled

T1, _ = get_t("T001")
T4, en4 = get_t("T004")
check("新增教师默认启用", en4 is True, en4)

# 3. batch-toggle 停用 T001+T002
r = api.toggle_teachers({"ids": [T1, get_t("T002")[0]], "enabled": False}, None)
check("批量停用返回 count=2", r.get("ok") and r["count"] == 2, r)
check("T001 已停用", get_t("T001")[1] is False)
check("T002 已停用", get_t("T002")[1] is False)

# 4. 单个启用
r = api.toggle_teachers({"ids": [T1], "enabled": True}, None)
check("单个启用成功", r.get("ok") and r["count"] == 1 and get_t("T001")[1] is True, r)

# 5. 空 ids 报 400
try:
    api.toggle_teachers({"ids": [], "enabled": True}, None)
    check("空 ids 报 400", False, "未报错")
except fastapi.HTTPException as e:
    check("空 ids 报 400", e.status_code == 400)
except Exception as e:
    check("空 ids 报 400", False, str(e)[:120])

# 6. 重新停用 T002，验证排课引擎排除停用教师
api.toggle_teachers({"ids": [get_t("T002")[0]], "enabled": False}, None)
with Session(db.engine) as s:
    teachers = [t.dict() for t in s.exec(select(models.Teacher)).all() if getattr(t, "enabled", True)]
    students = [t.dict() for t in s.exec(select(models.Student)).all()]
    cts = [c.dict() for c in s.exec(select(models.ClassType)).all()]
ids_in = [t["id"] for t in teachers]
check("排课教师池排除停用者", get_t("T002")[0] not in ids_in, ids_in)
plan, _rot = __import__("engine").generate_plan(2099, 5, teachers, students, cts,
                                                None, set(), {}, {})
used_t = {p["teacher_id"] for p in plan if p.get("teacher_id")}
check("生成的排课计划不含停用教师", get_t("T002")[0] not in used_t, used_t)

# 7. teacher_load 排除停用教师
load = api.teacher_load(2026, 11, None)
load_ids = [t["id"] for t in load["teachers"]]
check("负荷表排除停用教师", get_t("T002")[0] not in load_ids, load_ids)
check("负荷表保留启用教师", T1 in load_ids and T4 in load_ids, load_ids)

# 8. 导入：旧 9 列文件（无「启用」列）→ 保留原状态；10 列带 否 → 停用
old_row = ["T001", "张老师", "", "是", "是", "是", "是", "", "13800000001"]  # 无第10列
new_row_disable = ["T004", "赵老师", "", "是", "是", "是", "是", "", "", "否"]
new_row_enable = ["T002", "李老师", "", "是", "是", "是", "是", "", "", "是"]
new_row_blank = ["T003", "王老师", "", "是", "是", "是", "是", "", "", ""]

class FakeFile:
    def __init__(self, rows, headers):
        self.filename = "t.xlsx"
        self._rows = [headers] + rows
        self.file = io.BytesIO()
    async def read(self, n=-1):
        return self.file.read(n)

# 直接构造 _read_rows 可识别的 xlsx 需要真文件；改走 openpyxl 生成内存文件
from openpyxl import Workbook

def make_xlsx(rows, headers):
    wb = Workbook(); ws = wb.active
    ws.append(headers)
    for r in rows: ws.append(r)
    bio = io.BytesIO(); wb.save(bio); bio.seek(0)
    f = FakeFile(rows, headers)
    f.file = bio
    return f

hdr_old = ["工号", "姓名", "别名", "可讲课", "可主阅", "可评审", "可指导", "请假日期", "手机号"]
hdr_new = hdr_old + ["启用"]
r = api.teachers_import(make_xlsx([old_row], hdr_old), None)
check("旧9列导入成功", r["ok"] and r["errors"] == [], r)
check("旧9列导入保留启用状态（T001 仍启用）", get_t("T001")[1] is True)
check("旧9列导入保留启用状态（T002 仍停用）", get_t("T002")[1] is False)

r = api.teachers_import(make_xlsx([new_row_disable, new_row_enable, new_row_blank], hdr_new), None)
check("10列导入成功", r["ok"] and r["errors"] == [], r)
check("启用=否 → 停用（T004）", get_t("T004")[1] is False)
check("启用=是 → 启用（T002 恢复）", get_t("T002")[1] is True)
check("启用留空 → 保留原状态（T003 仍启用）", get_t("T003")[1] is True)

# 9. 导出含「启用」列（StreamingResponse → 读字节流解析）
resp = api.teachers_export(None)
import asyncio
async def _collect(r):
    bio = io.BytesIO()
    async for chunk in r.body_iterator:
        bio.write(chunk if isinstance(chunk, bytes) else chunk.encode())
    return bio.getvalue()
from openpyxl import load_workbook
wb = load_workbook(io.BytesIO(asyncio.get_event_loop().run_until_complete(_collect(resp))))
ws = wb["教师名单"] if "教师名单" in wb.sheetnames else wb.active
hdr = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
check("导出表头含「启用」列", "启用" in hdr, hdr)
data = {row[0]: row for row in ws.iter_rows(min_row=2, values_only=True)}
check("导出值正确（T002=是）", data.get("T002") and data["T002"][-1] == "是", data.get("T002"))

# 10. upd_teacher：编辑弹窗提交带 enabled=False → 保持停用
with Session(db.engine) as s:
    d = s.get(models.Teacher, T4)
    payload = d.dict()
api.toggle_teachers({"ids": [T4], "enabled": True}, None)   # 先启用
payload["enabled"] = False
r = api.upd_teacher(T4, models.Teacher(**payload), None)
check("编辑接口可停用", r["enabled"] is False)
r = api.upd_teacher(T4, models.Teacher(**{**payload, "enabled": True}), None)
check("编辑接口可启用", r["enabled"] is True)

# 11. list_teachers 返回 enabled 字段
lst = api.list_teachers(None)
ours = {t["emp_id"]: t for t in lst if t["emp_id"] in ("T001", "T002", "T003", "T004")}
check("教师列表含 enabled 字段", len(ours) == 4 and all("enabled" in t for t in ours.values()), list(ours)[:2])

print("\n==== %d 通过 / %d 失败 ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
