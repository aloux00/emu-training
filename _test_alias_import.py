# -*- coding: utf-8 -*-
"""离线验证：老师可空导入 + 教师别名匹配 + 自动新增名单外人员（隔离临时目录）。"""
import os, sys, io, csv, shutil, tempfile
_PROJ = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="aliasimp-")
shutil.copy(os.path.join(_PROJ, "schedule.db"), os.path.join(_TMP, "schedule.db"))
os.chdir(_TMP); sys.path.insert(0, _PROJ)

import db, models, api
db.init_db()      # 副本库是旧 schema，先跑迁移补 aliases 列
from sqlmodel import Session, select
from openpyxl import Workbook, load_workbook

FAILS = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)[:220]) if detail and not cond else ""))
    if not cond: FAILS.append(name)

class FakeUp:
    def __init__(self, name, buf): self.filename = name; self.file = io.BytesIO(buf)

import anyio
def resp_bytes(resp):
    async def collect():
        chunks = []
        async for ch in resp.body_iterator:
            chunks.append(ch if isinstance(ch, bytes) else str(ch).encode())
        return b"".join(chunks)
    return anyio.run(collect)

# 0. 迁移：teacher 有 aliases 列
with Session(db.engine) as s:
    t1 = s.exec(select(models.Teacher)).first()
    check("teacher.aliases 字段存在", hasattr(t1, "aliases"))

# 1. 教师模板/导出含「别名」列，9 列
wb = load_workbook(io.BytesIO(resp_bytes(api.teachers_template(None))))
hdr = [c.value for c in next(wb.worksheets[0].iter_rows(min_row=1, max_row=1))]
check("模板含别名列", hdr[2] == "别名" and len(hdr) == 9, hdr)
wb2 = load_workbook(io.BytesIO(resp_bytes(api.teachers_export(None))))
check("导出 9 列", len([c for c in next(wb2.worksheets[0].iter_rows(min_row=1, max_row=1))]) == 9)

# 2. 别名导入与匹配：造两位同名「王霞」，别名分别为 王霞1 / 王霞2
with Session(db.engine) as s:
    s.add(models.Teacher(emp_id="T900", name="王霞", aliases="王霞1"))
    s.add(models.Teacher(emp_id="T901", name="王霞", aliases="王霞2"))
    s.commit()

def mkxlsx(rows):
    wb = Workbook(); ws = wb.active
    ws.append(["日期", "课程", "节", "老师", "主阅", "评审", "指导", "题目"])
    for r in rows: ws.append(r)
    b = io.BytesIO(); wb.save(b); return b.getvalue()

# 2a. 老师列写别名 → 正确映射到两个人
r = api.import_schedule(FakeUp("a.xlsx", mkxlsx([
    ["2026-05-06", "小讲课", 1, "王霞1", "", "", "", "别名测试一"],
    ["2026-05-06", "教学阅片", 2, "", "王霞2", "", "", ""],   # 老师留空（阅片由主阅负责）
])), year=2026, month=0, mode="merge", dry_run=True)
check("别名行无错误", not r["errors"], r["errors"][:3])
check("空老师转为警告", any("未填老师" in w["msg"] for w in r["warnings"]), r["warnings"][:3])

r2 = api.import_schedule(FakeUp("a.xlsx", mkxlsx([
    ["2026-05-06", "小讲课", 1, "王霞1", "", "", "", "别名测试一"],
    ["2026-05-06", "教学阅片", 2, "", "王霞2", "", "", ""],
])), year=2026, month=0, mode="merge", dry_run=False)
check("别名导入零错误", not r2["errors"], r2["errors"])
with Session(db.engine) as s:
    lec = s.exec(select(models.ClassSession).where(
        models.ClassSession.date == "2026-05-06", models.ClassSession.slot == 1)).first()
    read = s.exec(select(models.ClassSession).where(
        models.ClassSession.date == "2026-05-06", models.ClassSession.slot == 2)).first()
    tid1 = s.exec(select(models.Teacher).where(models.Teacher.emp_id == "T900")).first().id
    tid2 = s.exec(select(models.Teacher).where(models.Teacher.emp_id == "T901")).first().id
    check("王霞1 → T900", lec.teacher_id == tid1, (lec.teacher_id, tid1))
    check("王霞2 主阅 → T901，老师为空", read.main_reader_id == tid2 and read.teacher_id == 0,
          (read.teacher_id, read.main_reader_id, tid2))

# 3. auto_create=1：老师列名单外→教师表；主阅列名单外→学生表；只增不改
rows = [["2026-05-13", "小讲课", 1, "白丽", "", "", "", "自动新增测试"],
        ["2026-05-13", "教学阅片", 2, "", "刘小瑜", "于东洋", "", ""]]
r3 = api.import_schedule(FakeUp("b.xlsx", mkxlsx(rows)), year=2026, month=0,
                         mode="merge", dry_run=True, auto_create=True)
check("auto_create 预览零错误", not r3["errors"], r3["errors"][:3])
check("提示新增教师", any("自动新增教师" in w["msg"] for w in r3["warnings"]), r3["warnings"][:4])
check("提示新增学生", any("自动新增学生" in w["msg"] for w in r3["warnings"]), r3["warnings"][:4])
r4 = api.import_schedule(FakeUp("b.xlsx", mkxlsx(rows)), year=2026, month=0,
                         mode="merge", dry_run=False, auto_create=True)
check("auto_create 写入零错误", not r4["errors"], r4["errors"])
with Session(db.engine) as s:
    bl = s.exec(select(models.Teacher).where(models.Teacher.name == "白丽")).all()
    check("白丽进了教师表且只建一个", len(bl) == 1, len(bl))
    check("工号 IMP- 前缀", bl and bl[0].emp_id.startswith("IMP-"), bl and bl[0].emp_id)
    lxy_t = s.exec(select(models.Teacher).where(models.Teacher.name == "刘小瑜")).all()
    lxy_s = s.exec(select(models.Student).where(models.Student.name == "刘小瑜")).all()
    check("主阅刘小瑜进学生表(不进教师表)", len(lxy_s) == 1 and not lxy_t,
          (len(lxy_t), len(lxy_s)))
    check("学生学号 IMP 前缀", lxy_s and lxy_s[0].stu_id.startswith("IMP"), lxy_s and lxy_s[0].stu_id)
    check("评审于东洋进教师表", bool(s.exec(select(models.Teacher).where(models.Teacher.name == "于东洋")).first()))
    read2 = s.exec(select(models.ClassSession).where(
        models.ClassSession.date == "2026-05-13", models.ClassSession.slot == 2)).first()
    check("主阅落库为学生 id", read2.main_reader_id == lxy_s[0].id,
          (read2.main_reader_id, lxy_s[0].id))
    # 只增不改：已有教师信息不被覆盖
    t0 = s.exec(select(models.Teacher).where(models.Teacher.emp_id == "T900")).first()
    check("已有教师别名未被覆盖", t0 and t0.aliases == "王霞1", t0 and t0.aliases)

# 3b. 主阅列同名已存在教师 → 直接匹配教师，不重复建学生
rows_b = [["2026-05-20", "教学阅片", 2, "", "白丽", "", "", ""]]
r4b = api.import_schedule(FakeUp("bb.xlsx", mkxlsx(rows_b)), year=2026, month=0,
                          mode="merge", dry_run=False, auto_create=True)
check("主阅命中已有教师零错误", not r4b["errors"], r4b["errors"])
with Session(db.engine) as s:
    bl_s = s.exec(select(models.Student).where(models.Student.name == "白丽")).all()
    bl_t = s.exec(select(models.Teacher).where(models.Teacher.name == "白丽")).all()
    check("白丽未重复建学生", len(bl_s) == 0 and len(bl_t) == 1, (len(bl_t), len(bl_s)))

# 4. auto_create=0：名单外仍报错（用步骤 3 没建过的人名）
rows5 = [["2026-05-13", "小讲课", 1, "赵无名", "", "", "", ""]]
r5 = api.import_schedule(FakeUp("b5.xlsx", mkxlsx(rows5)), year=2026, month=0,
                         mode="merge", dry_run=True, auto_create=False)
check("不勾选仍报查无此人", any("查无此人" in e["msg"] for e in r5["errors"]), r5["errors"][:3])

# 5. 教师导出→回导：别名保留
bio = api.teachers_export(None)
buf = io.BytesIO(resp_bytes(bio)); buf.seek(0)
r6 = api.teachers_import(FakeUp("teachers.xlsx", buf.getvalue()), None)
check("教师回导零错误", not r6["errors"], r6["errors"][:3])
with Session(db.engine) as s:
    t = s.exec(select(models.Teacher).where(models.Teacher.emp_id == "T900")).first()
    check("别名经导出回导保留", t and t.aliases == "王霞1", t and t.aliases)

print()
print("== %d FAIL ==" % len(FAILS) if FAILS else "== ALL PASS ==")
for f in FAILS: print(" -", f)
