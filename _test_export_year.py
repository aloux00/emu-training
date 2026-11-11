# -*- coding: utf-8 -*-
"""离线验证：全年导出（隔离临时目录，不碰生产库）。"""
import os, sys, io, shutil, tempfile, anyio
_PROJ = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="expyear-")
shutil.copy(os.path.join(_PROJ, "schedule.db"), os.path.join(_TMP, "schedule.db"))
os.chdir(_TMP); sys.path.insert(0, _PROJ)
print("隔离工作目录:", _TMP)

import db, models, api
db.init_db()      # 副本库补齐新列（aliases 等）
from sqlmodel import Session, select
from openpyxl import load_workbook

def resp_bytes(resp):
    async def collect():
        chunks = []
        async for ch in resp.body_iterator:
            chunks.append(ch if isinstance(ch, bytes) else str(ch).encode())
        return b"".join(chunks)
    return anyio.run(collect)

FAILS = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)[:220]) if detail and not cond else ""))
    if not cond: FAILS.append(name)

class FakeUp:
    def __init__(self, name, buf): self.filename = name; self.file = io.BytesIO(buf)

# 在干净的 2026-05 造数据（库里 5 月无课）
with Session(db.engine) as s:
    ct = s.exec(select(models.ClassType)).first()
    t = s.exec(select(models.Teacher)).first()
    s.add(models.ClassSession(year=2026, month=5, date="2026-05-12", slot=1,
          class_type_id=ct.id, class_type_name="小讲课", teacher_id=t.id,
          main_reader_id=t.id, topic="五月测试课", status="draft", created_at="now"))
    s.add(models.ClassSession(year=2026, month=5, date="2026-05-12", slot=2,
          class_type_id=ct.id, class_type_name="教学阅片", teacher_id=t.id,
          main_reader_id=t.id, status="draft", created_at="now"))
    s.commit()

# 1. 全年导出：11、12、5 月三个块
ybytes = resp_bytes(api.export_schedule_year(2026))
wb = load_workbook(io.BytesIO(ybytes))
ws = wb.active
check("sheet 名", ws.title == "2026全年", ws.title)
titles = [ws.cell(row=r, column=1).value for r in range(1, ws.max_row + 1)
          if str(ws.cell(row=r, column=1).value or "").startswith("放射科基地")]
check("标题按月分块(含5/11)", any("2026.05" in t for t in titles) and any("2026.11" in t for t in titles)
      and len(titles) >= 2, titles)

# 2. 全年导出 → 回导（自动分月）
up = FakeUp("schedule_year_2026.xlsx", ybytes)
r = api.import_schedule(up, year=2026, month=0, mode="merge", dry_run=True)
check("回导识别月份", {5, 11} <= {m["month"] for m in r["months"]}, r["months"])
check("回导零错误", not r["errors"], r["errors"][:3])
m5 = [m for m in r["months"] if m["month"] == 5][0]
check("5月全部更新(命中自造节)", m5["updated"] == 2 and m5["created"] == 0, m5)
check("空汇报人行转警告", any("未填汇报人" in w["msg"] for w in r["warnings"]), r["warnings"][:3])
r2 = api.import_schedule(FakeUp("y.xlsx", ybytes), year=2026, month=0, mode="merge", dry_run=False)
check("真实导入零错误", not r2["errors"], r2["errors"][:3])
# 3. 已指派汇报人的保留：给 11-17 slot3 的汇报节指派张研再导出回导
with Session(db.engine) as s:
    g = models.Graduate(grad_id="G001", name="张研", phone="13800000001")
    s.add(g); s.commit()
    gid = g.id
    for gsess in s.exec(select(models.ClassSession).where(
            models.ClassSession.class_type_name == "研究生汇报",
            models.ClassSession.month == 11)).all():
        gsess.presenter_id = gid; s.add(gsess)
    s.commit()
ybytes2 = resp_bytes(api.export_schedule_year(2026))
up2 = FakeUp("y2.xlsx", ybytes2)
r3 = api.import_schedule(up2, year=2026, month=0, mode="merge", dry_run=True)
check("指派后回导零错误", not r3["errors"], r3["errors"][:3])
bad = [w for w in r3["warnings"] if "11-17" in w["msg"] and "未填汇报人" in w["msg"]]
check("11-17 汇报行不再告警", not bad, bad)
with Session(db.engine) as s:
    gsess = s.exec(select(models.ClassSession).where(
        models.ClassSession.class_type_name == "研究生汇报",
        models.ClassSession.month == 11)).first()
    check("库里汇报人未被清空", gsess.presenter_id == gid, gsess.presenter_id)

# 4. 单月导出不受影响
mbytes = resp_bytes(api.export_schedule(2026, 5, None))
wb2 = load_workbook(io.BytesIO(mbytes))
check("单月导出正常", "2026.05" in str(wb2.active["A1"].value), wb2.active["A1"].value)

# 5. 空年份报错清晰
try:
    api.export_schedule_year(2025)
    check("空年应报错", False)
except Exception as e:
    check("空年应报错", "2025" in str(getattr(e, "detail", e)), str(getattr(e, "detail", e)))

print()
print("== %d FAIL ==" % len(FAILS) if FAILS else "== ALL PASS ==")
for f in FAILS: print(" -", f)
