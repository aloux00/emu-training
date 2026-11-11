from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from datetime import datetime, timedelta
import json
import io
import csv
import re as _re
import time
import hmac
import base64
import hashlib
import urllib.request
import urllib.parse
import urllib.error

import db
import models
import auth
import rules as rules_mod
import engine as sched_engine
from openpyxl import Workbook, load_workbook as _load_wb
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill

router = APIRouter(prefix="/api")


def admin_only(user: models.User = Depends(auth.get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ---------- AUTH ----------
@router.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends()):
    with Session(db.engine) as s:
        u = s.exec(select(models.User).where(models.User.username == form.username)).first()
        if not u or not auth.verify_password(form.password, u.password_hash):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        if not u.is_active:
            raise HTTPException(status_code=403, detail="账号已禁用")
        return {"access_token": auth.create_token(u.id), "token_type": "bearer",
                "user": {"id": u.id, "name": u.name, "role": u.role}}


@router.get("/auth/me")
def me(user: models.User = Depends(auth.get_current_user)):
    return {"id": user.id, "name": user.name, "role": user.role}


# ---------- MENUS ----------
# 仅管理员可见的页面路由（菜单过滤 + 前端路由守卫共用）
ADMIN_MENUS = {"#/classtypes", "#/holidays", "#/rules", "#/dingtalk", "#/menus", "#/users"}
# 仅管理员/教务可见的页面（教师/学生登录时额外隐藏）
STAFF_MENUS = {"#/teachers", "#/students", "#/schedule-generate", "#/exam"}


@router.get("/menus")
def get_menus(user: models.User = Depends(auth.get_current_user)):
    with Session(db.engine) as s:
        items = s.exec(select(models.Menu).order_by(models.Menu.parent_id, models.Menu.sort)).all()
        dicts = [m.dict() for m in items]
        hidden = set()
        if user.role != "admin":
            hidden |= ADMIN_MENUS
        if user.role not in ("admin", "jiaowu"):
            hidden |= STAFF_MENUS
        if not hidden:
            return dicts
        # 隐藏对应页面，并去掉因此变空的分组
        # （保留带自身路径的顶级菜单，如「首页」；只删纯分组且无子项的）
        visible = [m for m in dicts if (m.get("path") or "") not in hidden]
        parent_ids = {m["id"] for m in visible if m["parent_id"] == 0}
        has_child = {m["parent_id"] for m in visible
                     if m["parent_id"] != 0 and m["parent_id"] in parent_ids}
        return [m for m in visible
                if m["parent_id"] != 0 or m["id"] in has_child or (m.get("path") or "").strip()]


@router.post("/menus")
def add_menu(m: models.Menu, _=Depends(admin_only)):
    with Session(db.engine) as s:
        s.add(m); s.commit(); s.refresh(m); return m.dict()


@router.put("/menus/{mid}")
def upd_menu(mid: int, m: models.Menu, _=Depends(admin_only)):
    with Session(db.engine) as s:
        d = s.get(models.Menu, mid)
        if not d:
            raise HTTPException(404)
        for k, v in m.dict(exclude={"id"}).items():
            setattr(d, k, v)
        s.add(d); s.commit(); s.refresh(d); return d.dict()


@router.delete("/menus/{mid}")
def del_menu(mid: int, _=Depends(admin_only)):
    with Session(db.engine) as s:
        d = s.get(models.Menu, mid)
        if d:
            s.delete(d); s.commit()
        return {"ok": True}


# ---------- TEACHERS ----------
DEFAULT_PASSWORD = "123456"  # 教师工号/学生学号自动建号的初始密码


def _ensure_user(s, username: str, name: str, role: str, password: str = DEFAULT_PASSWORD):
    """教师/学生自动建登录账号（工号/学号即用户名）。用户名已占用则跳过，返回是否新建。"""
    username = (username or "").strip()
    if not username:
        return False
    exists = s.exec(select(models.User).where(models.User.username == username)).first()
    if exists:
        if not exists.name and name:
            exists.name = name
            s.add(exists)
        return False
    s.add(models.User(username=username, name=name or username, role=role,
                      password_hash=auth.hash_password(password)))
    return True


@router.get("/teachers")
def list_teachers(_=Depends(auth.get_current_user)):
    with Session(db.engine) as s:
        return [t.dict() for t in s.exec(select(models.Teacher).order_by(models.Teacher.emp_id)).all()]


@router.post("/teachers")
def add_teacher(t: models.Teacher, _=Depends(auth.staff_only)):
    with Session(db.engine) as s:
        if s.exec(select(models.Teacher).where(models.Teacher.emp_id == t.emp_id)).first():
            raise HTTPException(400, "工号 %s 已存在，请改用编辑修改" % t.emp_id)
        s.add(t); s.flush()                    # flush 拿到 id，但不提交（避免对象过期）
        _ensure_user(s, t.emp_id, t.name, "teacher")
        s.commit(); s.refresh(t)               # 一次提交后刷新，保证返回完整字段
        return t.dict()


@router.put("/teachers/{tid}")
def upd_teacher(tid: int, t: models.Teacher, _=Depends(auth.staff_only)):
    with Session(db.engine) as s:
        d = s.get(models.Teacher, tid)
        if not d:
            raise HTTPException(404)
        old_emp = d.emp_id
        for k, v in t.dict(exclude={"id"}).items():
            setattr(d, k, v)
        s.add(d)
        if old_emp != d.emp_id:  # 工号变更 → 同步登录用户名
            u = s.exec(select(models.User).where(models.User.username == old_emp)).first()
            dup = s.exec(select(models.User).where(models.User.username == d.emp_id)).first()
            if u and not dup and u.role in ("teacher", "student"):
                u.username = d.emp_id
                s.add(u)
        s.commit(); s.refresh(d); return d.dict()


@router.delete("/teachers/{tid}")
def del_teacher(tid: int, _=Depends(admin_only)):
    with Session(db.engine) as s:
        d = s.get(models.Teacher, tid)
        if d:
            s.delete(d)
            for u in s.exec(select(models.User).where(models.User.username == d.emp_id)).all():
                if u.role in ("teacher", "student"):  # 只删自动建的教师/学生账号，不动管理员
                    s.delete(u)
            s.commit()
        return {"ok": True}


@router.post("/teachers/batch-delete")
def del_teachers_batch(payload: dict, _=Depends(admin_only)):
    """批量删除教师（连带删除对应的登录账号）"""
    ids = [int(x) for x in (payload.get("ids") or []) if str(x).isdigit()]
    if not ids:
        raise HTTPException(400, "未选择任何教师")
    deleted = 0
    with Session(db.engine) as s:
        for d in s.exec(select(models.Teacher).where(models.Teacher.id.in_(ids))).all():
            s.delete(d)
            for u in s.exec(select(models.User).where(models.User.username == d.emp_id)).all():
                if u.role in ("teacher", "student"):
                    s.delete(u)
            deleted += 1
        s.commit()
    return {"ok": True, "deleted": deleted}


# ---------- STUDENTS ----------
@router.get("/students")
def list_students(_=Depends(auth.get_current_user)):
    with Session(db.engine) as s:
        return [t.dict() for t in s.exec(select(models.Student).order_by(models.Student.stu_id)).all()]


@router.post("/students")
def add_student(t: models.Student, _=Depends(auth.staff_only)):
    with Session(db.engine) as s:
        if s.exec(select(models.Student).where(models.Student.stu_id == t.stu_id)).first():
            raise HTTPException(400, "学号 %s 已存在，请改用编辑修改" % t.stu_id)
        s.add(t); s.flush()
        _ensure_user(s, t.stu_id, t.name, "student")
        s.commit(); s.refresh(t)
        return t.dict()


@router.put("/students/{sid}")
def upd_student(sid: int, t: models.Student, _=Depends(auth.staff_only)):
    with Session(db.engine) as s:
        d = s.get(models.Student, sid)
        if not d:
            raise HTTPException(404)
        old_stu = d.stu_id
        for k, v in t.dict(exclude={"id"}).items():
            setattr(d, k, v)
        s.add(d)
        if old_stu != d.stu_id:  # 学号变更 → 同步登录用户名
            u = s.exec(select(models.User).where(models.User.username == old_stu)).first()
            dup = s.exec(select(models.User).where(models.User.username == d.stu_id)).first()
            if u and not dup and u.role in ("teacher", "student"):
                u.username = d.stu_id
                s.add(u)
        s.commit(); s.refresh(d); return d.dict()


@router.delete("/students/{sid}")
def del_student(sid: int, _=Depends(admin_only)):
    with Session(db.engine) as s:
        d = s.get(models.Student, sid)
        if d:
            s.delete(d)
            for u in s.exec(select(models.User).where(models.User.username == d.stu_id)).all():
                if u.role in ("teacher", "student"):
                    s.delete(u)
            s.commit()
        return {"ok": True}


@router.post("/students/batch-delete")
def del_students_batch(payload: dict, _=Depends(admin_only)):
    """批量删除学生（连带删除对应的登录账号）"""
    ids = [int(x) for x in (payload.get("ids") or []) if str(x).isdigit()]
    if not ids:
        raise HTTPException(400, "未选择任何学生")
    deleted = 0
    with Session(db.engine) as s:
        for d in s.exec(select(models.Student).where(models.Student.id.in_(ids))).all():
            s.delete(d)
            for u in s.exec(select(models.User).where(models.User.username == d.stu_id)).all():
                if u.role in ("teacher", "student"):
                    s.delete(u)
            deleted += 1
        s.commit()
    return {"ok": True, "deleted": deleted}


# ---------- GRADUATES（研究生名册：第三节研究生汇报的汇报人） ----------
@router.get("/graduates")
def list_graduates(_=Depends(auth.get_current_user)):
    with Session(db.engine) as s:
        return [g.dict() for g in s.exec(select(models.Graduate).order_by(models.Graduate.grad_id)).all()]


@router.post("/graduates")
def add_graduate(g: models.Graduate, _=Depends(auth.staff_only)):
    with Session(db.engine) as s:
        if s.exec(select(models.Graduate).where(models.Graduate.grad_id == g.grad_id)).first():
            raise HTTPException(400, "编号 %s 已存在，请改用编辑修改" % g.grad_id)
        s.add(g); s.commit(); s.refresh(g)
        return g.dict()


@router.post("/graduates/import-roster")
def import_graduates_from_roster(payload: dict, _=Depends(auth.staff_only)):
    """从教师/学生名单勾选导入研究生名册。

    只增不改：编号（工号/学号）或姓名已在名册里的自动跳过，绝不覆盖已有研究生信息。
    teacher_ids / student_ids：勾选的人员 id 列表。
    """
    t_ids = payload.get("teacher_ids") or []
    s_ids = payload.get("student_ids") or []
    if not t_ids and not s_ids:
        raise HTTPException(400, "请先勾选要导入的人员")
    created, skipped = 0, []
    with Session(db.engine) as s:
        gs = s.exec(select(models.Graduate)).all()
        existing_gids = {g.grad_id for g in gs}
        existing_names = {g.name for g in gs}
        for tid in t_ids:
            t = s.get(models.Teacher, tid)
            if not t:
                continue
            if t.emp_id in existing_gids or t.name in existing_names:
                skipped.append("%s（已在名册）" % t.name)
                continue
            s.add(models.Graduate(grad_id=t.emp_id, name=t.name,
                                  major="", phone=(t.phone or "")))
            existing_gids.add(t.emp_id)
            existing_names.add(t.name)
            created += 1
        for sid in s_ids:
            st = s.get(models.Student, sid)
            if not st:
                continue
            if st.stu_id in existing_gids or st.name in existing_names:
                skipped.append("%s（已在名册）" % st.name)
                continue
            s.add(models.Graduate(grad_id=st.stu_id, name=st.name,
                                  major=(st.grade or ""), phone=(st.phone or "")))
            existing_gids.add(st.stu_id)
            existing_names.add(st.name)
            created += 1
        s.commit()
    return {"ok": True, "created": created, "skipped": skipped}


@router.put("/graduates/{gid}")
def upd_graduate(gid: int, g: models.Graduate, _=Depends(auth.staff_only)):
    with Session(db.engine) as s:
        d = s.get(models.Graduate, gid)
        if not d:
            raise HTTPException(404)
        dup = s.exec(select(models.Graduate)
                     .where(models.Graduate.grad_id == g.grad_id, models.Graduate.id != gid)).first()
        if dup:
            raise HTTPException(400, "编号 %s 已被其他研究生使用" % g.grad_id)
        for k, v in g.dict(exclude={"id"}).items():
            setattr(d, k, v)
        s.add(d); s.commit(); s.refresh(d)
        return d.dict()


@router.delete("/graduates/{gid}")
def del_graduate(gid: int, _=Depends(auth.admin_only)):
    with Session(db.engine) as s:
        d = s.get(models.Graduate, gid)
        if d:
            s.delete(d); s.commit()
        return {"ok": True}


@router.post("/graduates/batch-delete")
def del_graduates_batch(payload: dict, _=Depends(auth.admin_only)):
    ids = [int(x) for x in (payload.get("ids") or []) if str(x).isdigit()]
    if not ids:
        raise HTTPException(400, "未选择任何研究生")
    with Session(db.engine) as s:
        for d in s.exec(select(models.Graduate).where(models.Graduate.id.in_(ids))).all():
            s.delete(d)
        s.commit()
    return {"ok": True, "deleted": len(ids)}


# ---------- ROSTER IMPORT / EXPORT（教师、学生名单） ----------

_ROSTER_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_ROSTER_HEADER_FILL = PatternFill("solid", fgColor="185FA5")
_ROSTER_BORDER = Side(style="thin", color="D0D4DA")
_GRADES = ("一年级", "二年级", "三年级")
_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")

TEACHER_COLUMNS = ["工号", "姓名", "别名", "可讲课", "可主阅", "可评审", "可指导", "请假日期", "手机号"]
STUDENT_COLUMNS = ["学号", "姓名", "年级"]


def _roster_wb(sheet_name: str, columns, rows, widths, note_lines):
    """生成名单工作簿：sheet1 数据表（含表头），sheet2 填写说明。"""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    for c, h in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = _ROSTER_HEADER_FONT
        cell.fill = _ROSTER_HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = Border(bottom=_ROSTER_BORDER)
        ws.column_dimensions[cell.column_letter].width = widths[c - 1]
    for r, row in enumerate(rows, start=2):
        for c, v in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = Border(bottom=_ROSTER_BORDER)
    ws.freeze_panes = "A2"

    ns = wb.create_sheet("填写说明")
    ns.column_dimensions["A"].width = 100
    for i, line in enumerate(note_lines, start=1):
        cell = ns.cell(row=i, column=1, value=line)
        if i == 1:
            cell.font = Font(bold=True, size=12)
    return wb


def _roster_response(wb, filename: str):
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return StreamingResponse(
        bio, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _bool_cell(v, default=True) -> bool:
    """是/否 单元格 → bool（空 = 默认值）。"""
    if v is None or str(v).strip() == "":
        return default
    s = str(v).strip().lower()
    if s in ("是", "y", "yes", "true", "1"):
        return True
    if s in ("否", "n", "no", "false", "0"):
        return False
    raise ValueError("「可讲课/可主阅/可评审」只能填 是 或 否（可留空，默认是）")


def _leave_cell(v) -> str:
    """请假日期 → 逗号分隔的 YYYY-MM-DD 字符串。"""
    if v is None or str(v).strip() == "":
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    parts = _re.split(r"[,，;；、\s]+", str(v).strip())
    out = []
    for p in parts:
        if not p:
            continue
        if not _DATE_RE.match(p):
            raise ValueError("请假日期格式应为 YYYY-MM-DD，多个日期用逗号分隔（如 2026-11-10,2026-11-11）")
        out.append(p)
    return ",".join(out)


def _read_rows(file: UploadFile, first_col_names: tuple):
    """读取上传的 xlsx 第一个工作表的数据行（跳过表头），校验首列表头。"""
    try:
        wb = _load_wb(io.BytesIO(file.file.read()))
    except Exception:
        raise HTTPException(400, "无法解析文件，请上传 .xlsx 格式（建议使用系统提供的导入模板）")
    ws = wb.worksheets[0]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    if not header or str(header[0]).strip() not in first_col_names:
        raise HTTPException(400, "表头不符：第 1 列应为「%s」，请使用系统提供的导入模板" % "/".join(first_col_names))
    return list(ws.iter_rows(min_row=2, values_only=True))


@router.get("/teachers/template")
def teachers_template(_=Depends(auth.get_current_user)):
    wb = _roster_wb(
        "教师名单", TEACHER_COLUMNS, [], [12, 12, 12, 8, 8, 8, 8, 34, 14],
        ["教师名单导入格式说明", "",
         "第 1 列 工号（必填，唯一。已存在的工号将更新该教师信息，不存在则新增）",
         "第 2 列 姓名（必填）",
         "第 3 列 别名（可选。逗号分隔，如「王晓1,晓晓」：重名区分或课表文件里的惯用称呼，导入课表时可按别名匹配）",
         "第 4-7 列 可讲课 / 可主阅 / 可评审 / 可指导（填 是 或 否，可留空，留空默认「是」）",
         "第 8 列 请假日期（可选。格式 YYYY-MM-DD，多个日期用逗号分隔，如 2026-11-10,2026-11-11）",
         "第 9 列 手机号（可选。11 位手机号，需与教师钉钉绑定号一致，用于自动创建待办提醒；更新时留空则保持原值）",
         "",
         "提示：请直接在第 1 个工作表从第 2 行开始填写；「填写说明」工作表无需删除，导入时会被忽略。"])
    return _roster_response(wb, "teacher_template.xlsx")


@router.get("/teachers/export")
def teachers_export(_=Depends(auth.get_current_user)):
    with Session(db.engine) as s:
        ts = s.exec(select(models.Teacher).order_by(models.Teacher.emp_id)).all()
    rows = [[t.emp_id, t.name, getattr(t, "aliases", "") or "", "是" if t.can_teach else "否",
             "是" if t.can_main_read else "否", "是" if t.can_review else "否", "是" if t.can_mentor else "否",
             t.leave_dates or "", t.phone or ""] for t in ts]
    wb = _roster_wb("教师名单", TEACHER_COLUMNS, rows, [12, 12, 12, 8, 8, 8, 8, 34, 14], ["教师名单（系统导出）"])
    return _roster_response(wb, "teachers.xlsx")


@router.post("/teachers/import")
def teachers_import(file: UploadFile = File(...), _=Depends(auth.get_current_user)):
    rows = _read_rows(file, ("工号",))
    errors, created, updated, accounts = [], 0, 0, 0
    seen = set()
    with Session(db.engine) as s:
        existing = {t.emp_id: t for t in s.exec(select(models.Teacher)).all()}
        for i, row in enumerate(rows, start=2):
            if row is None or all(v in (None, "") for v in row):
                continue
            try:
                emp_id = str(row[0]).strip() if row[0] is not None else ""
                name = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
                if not emp_id:
                    raise ValueError("工号不能为空")
                if not name:
                    raise ValueError("姓名不能为空")
                if emp_id in seen:
                    raise ValueError("文件内工号重复")
                seen.add(emp_id)
                can_teach = _bool_cell(row[3] if len(row) > 3 else None, True)
                can_main_read = _bool_cell(row[4] if len(row) > 4 else None, True)
                can_review = _bool_cell(row[5] if len(row) > 5 else None, True)
                can_mentor = _bool_cell(row[6] if len(row) > 6 else None, True)
                leave = _leave_cell(row[7] if len(row) > 7 else None)
                phone = str(row[8]).strip() if len(row) > 8 and row[8] is not None else ""
                aliases = str(row[2]).strip() if len(row) > 2 and row[2] is not None else ""
                if phone and not _PHONE_RE.match(phone):
                    raise ValueError("手机号应为 11 位数字（如 13812345678），可留空")
                if emp_id in existing:
                    t = existing[emp_id]
                    t.name, t.aliases, t.can_teach, t.can_main_read, t.can_review, t.can_mentor, t.leave_dates = \
                        name, aliases, can_teach, can_main_read, can_review, can_mentor, leave
                    if phone:          # 留空则保留原有手机号
                        t.phone = phone
                    updated += 1
                else:
                    s.add(models.Teacher(emp_id=emp_id, name=name, aliases=aliases,
                                         can_teach=can_teach, can_main_read=can_main_read,
                                         can_review=can_review, can_mentor=can_mentor,
                                         leave_dates=leave, phone=phone))
                    created += 1
                if _ensure_user(s, emp_id, name, "teacher"):
                    accounts += 1
            except Exception as ex:
                errors.append({"row": i, "msg": str(ex)})
        s.commit()
    return {"ok": True, "created": created, "updated": updated, "accounts": accounts, "errors": errors}


@router.get("/students/template")
def students_template(_=Depends(auth.get_current_user)):
    wb = _roster_wb(
        "学生名单", STUDENT_COLUMNS, [], [12, 12, 12],
        ["学生名单导入格式说明", "",
         "第 1 列 学号（必填，唯一。已存在的学号将更新该学生信息，不存在则新增）",
         "第 2 列 姓名（必填）",
         "第 3 列 年级（必填，只能填：一年级 / 二年级 / 三年级）",
         "",
         "提示：请直接在第 1 个工作表从第 2 行开始填写；「填写说明」工作表无需删除，导入时会被忽略。"])
    return _roster_response(wb, "student_template.xlsx")


@router.get("/students/export")
def students_export(_=Depends(auth.get_current_user)):
    with Session(db.engine) as s:
        ss = s.exec(select(models.Student).order_by(models.Student.stu_id)).all()
    rows = [[t.stu_id, t.name, t.grade or ""] for t in ss]
    wb = _roster_wb("学生名单", STUDENT_COLUMNS, rows, [12, 12, 12], ["学生名单（系统导出）"])
    return _roster_response(wb, "students.xlsx")


@router.post("/students/import")
def students_import(file: UploadFile = File(...), _=Depends(auth.get_current_user)):
    rows = _read_rows(file, ("学号",))
    errors, created, updated, accounts = [], 0, 0, 0
    seen = set()
    with Session(db.engine) as s:
        existing = {t.stu_id: t for t in s.exec(select(models.Student)).all()}
        for i, row in enumerate(rows, start=2):
            if row is None or all(v in (None, "") for v in row):
                continue
            try:
                stu_id = str(row[0]).strip() if row[0] is not None else ""
                name = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
                grade = str(row[2]).strip() if len(row) > 2 and row[2] is not None else ""
                if not stu_id:
                    raise ValueError("学号不能为空")
                if not name:
                    raise ValueError("姓名不能为空")
                if stu_id in seen:
                    raise ValueError("文件内学号重复")
                seen.add(stu_id)
                if grade not in _GRADES:
                    raise ValueError("年级只能填：一年级 / 二年级 / 三年级")
                if stu_id in existing:
                    t = existing[stu_id]
                    t.name, t.grade = name, grade
                    updated += 1
                else:
                    s.add(models.Student(stu_id=stu_id, name=name, grade=grade))
                    created += 1
                if _ensure_user(s, stu_id, name, "student"):
                    accounts += 1
            except Exception as ex:
                errors.append({"row": i, "msg": str(ex)})
        s.commit()
    return {"ok": True, "created": created, "updated": updated, "accounts": accounts, "errors": errors}


# ---------- CLASS TYPES ----------
@router.get("/classtypes")
def list_ct(_=Depends(auth.get_current_user)):
    with Session(db.engine) as s:
        return [c.dict() for c in s.exec(select(models.ClassType).order_by(models.ClassType.id)).all()]


@router.post("/classtypes")
def add_ct(c: models.ClassType, _=Depends(auth.staff_only)):
    with Session(db.engine) as s:
        s.add(c); s.commit(); s.refresh(c); return c.dict()


@router.put("/classtypes/{cid}")
def upd_ct(cid: int, c: models.ClassType, _=Depends(auth.staff_only)):
    with Session(db.engine) as s:
        d = s.get(models.ClassType, cid)
        if not d:
            raise HTTPException(404)
        for k, v in c.dict(exclude={"id"}).items():
            setattr(d, k, v)
        s.add(d); s.commit(); s.refresh(d); return d.dict()


@router.delete("/classtypes/{cid}")
def del_ct(cid: int, _=Depends(admin_only)):
    with Session(db.engine) as s:
        d = s.get(models.ClassType, cid)
        if d:
            s.delete(d); s.commit()
        return {"ok": True}


# ---------- SCHEDULE ----------
try:
    from pypinyin import lazy_pinyin as _lazy_py
except ImportError:          # 未安装 pypinyin 时降级：人员选择器不支持拼音首字母筛选
    _lazy_py = None


def _py_of(name: str) -> dict:
    """返回 {"i": 拼音首字母缩写, "q": 全拼}，用于前端人员选择器按拼音筛选。"""
    if not _lazy_py or not name:
        return {}
    parts = [p for p in _lazy_py(str(name)) if p]
    if not parts:
        return {}
    return {"i": ''.join((p[0] or '') for p in parts).lower(),
            "q": ''.join(parts).lower()}


def _snap_names(x, tmap, smap, gmap):
    """把课时各角色的名字以文本快照存进行内（teacher/main_reader/reviewer/mentor/presenter_name）。
    名单里的人之后被删除或改名，历史课表仍按快照显示，不会变成空白。"""
    x.teacher_name = tmap.get(x.teacher_id, "") if x.teacher_id else ""
    x.main_reader_name = ((smap.get(x.main_reader_id) or tmap.get(x.main_reader_id) or "")
                          if x.main_reader_id else "")
    x.reviewer_name = tmap.get(x.reviewer_id, "") if x.reviewer_id else ""
    x.mentor_name = tmap.get(x.mentor_id, "") if x.mentor_id else ""
    x.presenter_name = gmap.get(x.presenter_id, "") if x.presenter_id else ""


@router.get("/schedule")
def get_schedule(year: int, month: int, _=Depends(auth.get_current_user)):
    with Session(db.engine) as s:
        sessions = s.exec(select(models.ClassSession)
                           .where(models.ClassSession.year == year, models.ClassSession.month == month)
                           .order_by(models.ClassSession.week_index, models.ClassSession.date,
                                     models.ClassSession.slot)).all()
        tlist = s.exec(select(models.Teacher)).all()
        slist = s.exec(select(models.Student)).all()
        glist = s.exec(select(models.Graduate)).all()
        teachers = {t.id: t.name for t in tlist}
        students = {t.id: t.name for t in slist}
        cts = s.exec(select(models.ClassType)).all()
        return {"sessions": [x.dict() for x in sessions],
                "teachers": teachers,
                "students": students,
                "graduates": {g.id: g.name for g in glist},
                "teachers_py": {t.id: _py_of(t.name) for t in tlist},
                "students_py": {t.id: _py_of(t.name) for t in slist},
                "class_types": [c.dict() for c in cts]}


COURSE_MINUTES = {"小讲课": 60, "教学阅片": 90, "病例讨论": 90, "入科教育": 30}


@router.get("/schedule/teacher-load")
def teacher_load(year: int, month: int, _=Depends(auth.get_current_user)):
    """老师讲课负荷：本月节数/分钟、近 90 天节数、上次排课距今天数（权重）。

    排序按「越久没排课越靠前」，教务在表格里换人时第一眼就能看到该轮到谁。
    """
    today = datetime.now().date()

    def as_date(txt):
        try:
            return datetime.strptime(txt or "", "%Y-%m-%d").date()
        except ValueError:
            return None

    with Session(db.engine) as s:
        teachers = s.exec(select(models.Teacher)).all()
        rows = s.exec(select(models.ClassSession)).all()
        out = []
        for t in teachers:
            mine = [r for r in rows if r.teacher_id == t.id]
            m = [r for r in mine if r.year == year and r.month == month]
            dated = [(as_date(r.date), r) for r in mine]
            dated = [(d, r) for d, r in dated if d]
            past = [(d, r) for d, r in dated if d <= today]
            ahead = [d for d, _ in dated if d > today]
            last = max([d for d, _ in past], default=None)
            days = (today - last).days if last else 999
            r90 = sum(1 for d, _ in past if (today - d).days <= 90)
            # 本月已排到课的人不算「久未排课」，排到最后；其余按距上次排课的天数排
            weight = 0 if m else (days if last else 999)
            out.append({
                "id": t.id, "name": t.name,
                "can_teach": bool(t.can_teach), "can_review": bool(t.can_review),
                "can_main_read": bool(t.can_main_read), "can_mentor": bool(getattr(t, "can_mentor", True)),
                "m_count": len(m),
                "m_minutes": sum(COURSE_MINUTES.get(r.class_type_name, 60) for r in m),
                "r90": r90,
                "total": len(mine),
                "last_date": last.isoformat() if last else "",
                "next_date": min(ahead).isoformat() if ahead else "",
                "days_since": days,
                "weight": weight,
            })
    out.sort(key=lambda x: (-x["weight"], -x["days_since"], x["name"]))
    return {"teachers": out, "today": today.isoformat(), "minutes_rule": COURSE_MINUTES}


def build_history(s):
    rows = s.exec(select(models.ClassSession).where(models.ClassSession.status == "published")).all()
    hist = {"teacher": {}, "review": {}, "student": {}}
    for r in rows:
        hist["teacher"].setdefault(r.class_type_name, set()).add(r.teacher_id)
        hist["review"].setdefault(r.class_type_name, set()).add(r.reviewer_id)
        hist["student"].setdefault(r.class_type_name, set()).add(r.main_reader_id)
    return hist


@router.post("/schedule/generate")
def generate(payload: dict, _=Depends(auth.staff_only)):
    year = int(payload["year"]); month = int(payload["month"])
    with Session(db.engine) as s:
        teachers = [t.dict() for t in s.exec(select(models.Teacher)).all()]
        students = [t.dict() for t in s.exec(select(models.Student)).all()]
        graduates = [g.dict() for g in s.exec(select(models.Graduate)).all()]
        cts = [c.dict() for c in s.exec(select(models.ClassType)).all()]
        if not teachers or not cts:
            raise HTTPException(status_code=400, detail="请先维护教师和课程类型")
        holidays = {h.date for h in s.exec(select(models.Holiday)).all()}
        history = build_history(s)
        rules = _load_rules(s)
        # 恢复轮转状态（按角色：ptr + covered）
        rotation = {}
        for r in s.exec(select(models.RotationState)).all():
            try:
                cov = json.loads(r.covered_json) if r.covered_json else {}
            except Exception:
                cov = {}
            rotation[r.role] = {"ptr": r.ptr, "covered": cov}
        try:
            plan, rotation_out = sched_engine.generate_plan(year, month, teachers, students, cts,
                                                            history, holidays, rotation, rules,
                                                            graduates=graduates)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        old = s.exec(select(models.ClassSession)
                     .where(models.ClassSession.year == year, models.ClassSession.month == month)).all()
        for o in old:
            s.delete(o)
        tmap_g = {t["id"]: t["name"] for t in teachers}
        smap_g = {t["id"]: t["name"] for t in students}
        gmap_g = {g["id"]: g["name"] for g in graduates}
        for p in plan:
            row = models.ClassSession(
                year=year, month=month, date=p["date"], slot=p["slot"], week_index=p.get("week_index", 0),
                class_type_id=p["class_type_id"], class_type_name=p["class_type_name"],
                teacher_id=p["teacher_id"], main_reader_id=p["main_reader_id"],
                reviewer_id=p["reviewer_id"], mentor_id=p.get("mentor_id", 0),
                presenter_id=p.get("presenter_id", 0), status="draft",
                created_at=datetime.now().isoformat(timespec="seconds"))
            _snap_names(row, tmap_g, smap_g, gmap_g)
            s.add(row)
        # 持久化轮转状态（跨月连续轮转）
        existing = {x.role: x for x in s.exec(select(models.RotationState)).all()}
        for role, data in rotation_out.items():
            if role in existing:
                existing[role].ptr = data["ptr"]
                existing[role].covered_json = json.dumps(data["covered"], ensure_ascii=False)
                s.add(existing[role])
            else:
                s.add(models.RotationState(role=role, ptr=data["ptr"],
                                           covered_json=json.dumps(data["covered"], ensure_ascii=False)))
        s.commit()
        skipped = sched_engine.count_skipped(year, month, holidays, rules["weekdays"])
        grad_missing = sum(1 for p in plan
                           if p["class_type_name"] == sched_engine.GRAD_TYPE and not p.get("presenter_id"))
        return {"ok": True, "count": len(plan), "skipped_holidays": skipped,
                "grad_missing": grad_missing,
                "rules_summary": {"weekdays": rules["weekdays"], "days_per_month": rules["days_per_month"],
                                  "slots_per_day": rules["slots_per_day"]}}


def _is_summer(month: int, rules=None) -> bool:
    """夏令月份由「排课规则」配置（默认 5–9 月）。"""
    r = rules or rules_mod.DEFAULT_RULES
    return month in (r.get("summer_months") or [])


def _grad_start(month: int, rules=None) -> str:
    """研究生汇报开始时间 = 第二节课型开始 + 该课型时长（自动接在第二节结束后）。"""
    r = rules or rules_mod.DEFAULT_RULES
    for name in ("教学阅片", "病例讨论"):
        v = _course_time(name, month, r)
        if v:
            return _add_minutes(v, COURSE_MINUTES.get(name, 90))
    return "19:30" if _is_summer(month, r) else "19:00"


def _course_time(class_type_name: str, month: int, rules=None) -> str:
    """课型时间由「排课规则」配置；未配置的课型返回空串（研究生汇报自动接第二节之后）。"""
    r = rules or rules_mod.DEFAULT_RULES
    tr = (r.get("time_rules") or {}).get(class_type_name)
    if isinstance(tr, dict):
        got = tr.get("summer" if _is_summer(month, r) else "winter", "") or ""
        if got:
            return got
    elif tr:
        return tr
    if class_type_name == "研究生汇报":
        return _grad_start(month, r)
    return ""


def _add_minutes(t: str, mins: int) -> str:
    if not t:
        return ""
    try:
        dt = datetime.strptime(t, "%H:%M")
    except ValueError:
        return t
    return (dt + timedelta(minutes=mins)).strftime("%H:%M")


def _month_export_rows(s, year: int, month: int, teachers, students, grads, rules):
    """单月导出行（单月 / 全年导出共用）。返回 (rows, 节数)；空月返回 ([], 0)。

    行格式：(日期, 时间, 课程, 题目, 主阅, 老师, 副主阅, 督导)，日期为 2026.11.03 全格式。
    """
    sessions = s.exec(select(models.ClassSession)
                      .where(models.ClassSession.year == year, models.ClassSession.month == month)
                      .order_by(models.ClassSession.week_index, models.ClassSession.date,
                                models.ClassSession.slot)).all()
    if not sessions:
        return [], 0

    # 按日期分组（保持排序）
    groups = {}
    order = []
    for x in sessions:
        if x.date not in groups:
            groups[x.date] = []
            order.append(x.date)
        groups[x.date].append(x)
    order.sort()

    # 构造行：(日期, 时间, 课程, 题目, 主阅, 老师, 副主阅, 督导)
    rows = []
    first_day = order[0]
    ori = rules.get("orientation") or {}
    if ori.get("enabled", True):
        rows.append((first_day.replace("-", "."), ori.get("time", "8:00"),
                     ori.get("name", "入科教育"), "", "", "", "", ""))  # 入科教育首行
    topic = rules.get("default_topic", "自选题")
    reader_src = rules.get("reader_source", "student")
    for d in order:
        dd = d.replace("-", ".")
        prev_t, prev_name = "", ""
        for x in groups[d]:
            t = _course_time(x.class_type_name, month, rules)
            if not t and prev_t:      # 无独立时间配置的节次：接在上一节结束之后
                t = _add_minutes(prev_t, COURSE_MINUTES.get(prev_name, 90))
            if x.class_type_name == "研究生汇报":
                # 研究生汇报：只有汇报人 + 题目（汇报人占「老师」列，便于一眼看出谁讲）
                rows.append((dd, t, x.class_type_name, (x.topic or "").strip(),
                             "", getattr(x, "presenter_name", "") or grads.get(x.presenter_id, ""), "", ""))
                prev_t, prev_name = t, x.class_type_name
                continue
            if x.class_type_name == "小讲课" and rules.get("small_lecture_reader_is_teacher", True):
                main = getattr(x, "main_reader_name", "") or teachers.get(x.main_reader_id, "")
            elif reader_src == "teacher":
                main = getattr(x, "main_reader_name", "") or teachers.get(x.main_reader_id, "")
            else:
                main = getattr(x, "main_reader_name", "") or students.get(x.main_reader_id, "")
            teacher = getattr(x, "teacher_name", "") or teachers.get(x.teacher_id, "")
            sub = (getattr(x, "mentor_name", "") or teachers.get(x.mentor_id, "")) if x.mentor_id else ""
            sup = getattr(x, "reviewer_name", "") or teachers.get(x.reviewer_id, "")  # 评审 → 督导列
            rows.append((dd, t, x.class_type_name, (x.topic or "").strip() or topic,
                         main, teacher, sub, sup))
            prev_t, prev_name = t, x.class_type_name
    # 月末最后一节课后的两个反馈（汇报人来自「月度反馈」，未填则留空）
    fb_items, _, _ = _feedback_rows(s, year, month)
    fb = {f["kind"]: f for f in fb_items}
    last_day = order[-1].replace("-", ".")
    last_t = _course_time(groups[order[-1]][-1].class_type_name, month, rules)
    offsets = rules.get("feedback_offsets") or {}
    for kind in ("出科考核反馈", "月考反馈"):
        offset = int(offsets.get(kind, 60 if kind == "出科考核反馈" else 90))
        f = fb.get(kind) or {}
        rows.append((last_day, _add_minutes(last_t, offset), kind,
                     f.get("note", ""), f.get("reporter", ""), "", "", ""))
    return rows, len(sessions)


@router.get("/schedule/export")
def export_schedule(year: int, month: int, _=Depends(auth.get_current_user)):
    with Session(db.engine) as s:
        teachers = {t.id: t.name for t in s.exec(select(models.Teacher)).all()}
        students = {t.id: t.name for t in s.exec(select(models.Student)).all()}
        grads = {g.id: g.name for g in s.exec(select(models.Graduate)).all()}
        rules = _load_rules(s)
        rows, n_sess = _month_export_rows(s, year, month, teachers, students, grads, rules)
        if not n_sess:
            raise HTTPException(status_code=400, detail="该月尚无课表，请先生成")

    wb = Workbook()
    ws = wb.active
    ws.title = "%d.%02d" % (year, month)

    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # 标题（合并 A1:H1）
    ws.merge_cells("A1:H1")
    ws["A1"] = "放射科基地住院医师规范化培训月培训计划（%d.%02d）" % (year, month)
    ws["A1"].font = Font(bold=True, size=14)
    ws["A1"].alignment = center
    ws.row_dimensions[1].height = 32

    headers = ["日期", "时间", "课程", "题目", "主阅", "老师", "副主阅", "督导"]
    ws.append(headers)  # 第 2 行
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=2, column=c)
        cell.font = Font(bold=True)
        cell.alignment = center
        cell.border = border
    ws.row_dimensions[2].height = 22

    start = 3
    for i, r in enumerate(rows):
        rr = start + i
        for c, val in enumerate(r, start=1):
            cell = ws.cell(row=rr, column=c, value=val)
            cell.alignment = center
            cell.border = border
        ws.row_dimensions[rr].height = 30

    # 合并「日期」列：同一天的连续行
    i = 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1][0] == rows[i][0]:
            j += 1
        if j > i:
            ws.merge_cells(start_row=start + i, start_column=1,
                           end_row=start + j, end_column=1)
        top = ws.cell(row=start + i, column=1, value=rows[i][0])
        top.alignment = center
        top.border = border
        i = j + 1

    for col, w in zip("ABCDEFGH", [13, 8, 15, 10, 10, 10, 10, 10]):
        ws.column_dimensions[col].width = w

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    headers_out = {"Content-Disposition": "attachment; filename=schedule_%d_%02d.xlsx" % (year, month)}
    return StreamingResponse(bio, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers=headers_out)


@router.get("/schedule/export-year")
def export_schedule_year(year: int, _=Depends(auth.get_current_user)):
    """全年课表导出：有课的月份各成一个块，纵向排进同一个工作表。

    每块 = 标题行 + 表头 + 数据行，格式与单月导出完全一致；
    导回去时「导入课表」会按文件日期自动分月，可直接整年回导。
    """
    with Session(db.engine) as s:
        teachers = {t.id: t.name for t in s.exec(select(models.Teacher)).all()}
        students = {t.id: t.name for t in s.exec(select(models.Student)).all()}
        grads = {g.id: g.name for g in s.exec(select(models.Graduate)).all()}
        rules = _load_rules(s)
        blocks = []
        for m in range(1, 13):
            rows, n_sess = _month_export_rows(s, year, m, teachers, students, grads, rules)
            if n_sess:
                blocks.append((m, rows, n_sess))
        if not blocks:
            raise HTTPException(status_code=400, detail="%d 年还没有任何课表，请先生成" % year)

    wb = Workbook()
    ws = wb.active
    ws.title = "%d全年" % year

    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    headers = ["日期", "时间", "课程", "题目", "主阅", "老师", "副主阅", "督导"]

    rr = 1
    for bi, (m, rows, n_sess) in enumerate(blocks):
        if bi:
            rr += 1                       # 块之间空一行
        # 标题行
        ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=8)
        ws.cell(row=rr, column=1,
                value="放射科基地住院医师规范化培训月培训计划（%d.%02d）" % (year, m))
        ws.cell(row=rr, column=1).font = Font(bold=True, size=14)
        ws.cell(row=rr, column=1).alignment = center
        ws.row_dimensions[rr].height = 32
        rr += 1
        # 表头行
        for c, h in enumerate(headers, start=1):
            cell = ws.cell(row=rr, column=c, value=h)
            cell.font = Font(bold=True)
            cell.alignment = center
            cell.border = border
        ws.row_dimensions[rr].height = 22
        rr += 1
        # 数据行 + 同日日期列合并
        start = rr
        for i, r in enumerate(rows):
            for c, val in enumerate(r, start=1):
                cell = ws.cell(row=start + i, column=c, value=val)
                cell.alignment = center
                cell.border = border
            ws.row_dimensions[start + i].height = 30
        i = 0
        while i < len(rows):
            j = i
            while j + 1 < len(rows) and rows[j + 1][0] == rows[i][0]:
                j += 1
            if j > i:
                ws.merge_cells(start_row=start + i, start_column=1,
                               end_row=start + j, end_column=1)
            top = ws.cell(row=start + i, column=1, value=rows[i][0])
            top.alignment = center
            top.border = border
            i = j + 1
        rr = start + len(rows)

    for col, w in zip("ABCDEFGH", [13, 8, 15, 10, 10, 10, 10, 10]):
        ws.column_dimensions[col].width = w

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    headers_out = {"Content-Disposition": "attachment; filename=schedule_year_%d.xlsx" % year}
    return StreamingResponse(bio, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers=headers_out)


# ---------- 课表导入（xlsx / csv） ----------
# 与「导出 Excel」同构：导出的表可以直接改完再导回来。
# 兼容点：日期列合并单元格（下方单元格留空）自动沿用上一行；节次留空则按顺序编号。
_SCH_COLS = [
    ("date",        ("日期", "上课日期", "date")),
    ("slot",        ("节次", "第几节", "节", "时段", "slot")),
    ("class_type",  ("课程", "课型", "课程类型", "类型", "class", "class_type")),
    ("topic",       ("题目", "内容", "讲课内容", "主题", "topic")),
    ("teacher",     ("老师", "授课老师", "教师", "主讲", "讲课老师", "老师姓名", "teacher")),
    ("main_reader", ("主阅", "主阅人", "主阅学生", "学生", "main_reader")),
    ("reviewer",    ("评审", "督导", "评审老师", "督导老师", "reviewer")),
    ("mentor",      ("指导", "副主阅", "指导老师", "副主阅老师", "mentor")),
]
# 这几类由系统按「排课规则 / 月度反馈」自动生成，导入时跳过，避免重复
_SCH_AUTO_TYPES = ("入科教育", "出科考核反馈", "月考反馈")
_SCH_PREVIEW_MAX = 300
_SCH_MAX_ERRORS = 60


def _hname(v) -> str:
    return _re.sub(r"\s+", "", str(v or "")).replace("（", "(").replace("）", ")").lower()


def _sch_read_table(file: UploadFile):
    """上传文件 → 二维元组列表。支持 .xlsx/.xlsm 与 .csv（UTF-8 / GBK）。"""
    raw = file.file.read()
    name = (file.filename or "").lower()
    if name.endswith(".csv"):
        text = None
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise HTTPException(400, "CSV 文件编码无法识别，请另存为 UTF-8 或 GBK 后重试")
        return [tuple(r) for r in csv.reader(io.StringIO(text))]
    try:
        wb = _load_wb(io.BytesIO(raw))
    except Exception:
        raise HTTPException(400, "无法解析文件：请上传 .xlsx 格式的 Excel（.xls 旧格式请先另存为 .xlsx，也支持 .csv）")
    ws = wb.worksheets[0]
    # 合并单元格展开：区域内非左上角的格在 values_only 模式下读出来是空，
    # 统一继承左上角的值（日期/老师/主阅等列的纵向、横向合并都能正确读取）
    fill = {}
    for rng in ws.merged_cells.ranges:
        a = ws.cell(rng.min_row, rng.min_col).value
        if a is None or (isinstance(a, str) and not a.strip()):
            continue
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                fill[(r, c)] = a
    out = []
    for ri, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if fill:
            row = tuple((fill[(ri, ci)] if (ri, ci) in fill else v)
                        if v is None or (isinstance(v, str) and not v.strip()) else v
                        for ci, v in enumerate(row, start=1))
        out.append(row)
    return out


def _sch_header(rows):
    """在前 15 行内定位表头行，返回 (表头行下标, {字段: 列号})。"""
    for i, row in enumerate(rows[:15]):
        cells = [_hname(v) for v in row]
        mapping = {}
        for key, names in _SCH_COLS:
            for j, c in enumerate(cells):
                if c and c in names and key not in mapping:
                    mapping[key] = j
        if "date" in mapping and "class_type" in mapping:
            return i, mapping
    raise HTTPException(400, "表头未识别：请确保表格里有「日期」和「课程」两列（可先下载导入模板对照）")


def _sch_date(v, year: int, month: int):
    """单元格 → date。支持 Excel 日期、2026-11-03、2026.11.3、11.3、2026年11月3日。"""
    if v is None or str(v).strip() == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if hasattr(v, "year") and hasattr(v, "month") and hasattr(v, "day"):
        try:
            return datetime(v.year, v.month, v.day).date()
        except Exception:
            pass
    s = str(v).strip()
    for a, b in (("年", "-"), ("月", "-"), ("日", ""), (".", "-"), ("/", "-"), ("\\", "-")):
        s = s.replace(a, b)
    parts = [p for p in s.split("-") if p.strip()]
    try:
        nums = [int(_re.sub(r"[^0-9]", "", p)) for p in parts]
    except ValueError:
        raise ValueError("日期写法无法识别（应为 2026-11-03 或 11.03）")
    if len(nums) == 3:
        y, m, d = nums
    elif len(nums) == 2:
        y, m, d = year, nums[0], nums[1]
    else:
        raise ValueError("日期写法无法识别（应为 2026-11-03 或 11.03）")
    try:
        return datetime(y, m, d).date()
    except ValueError:
        raise ValueError("日期不存在：%s" % s)


def _sch_slot(v):
    """节次单元格 → int。留空返回 None（由调用方按顺序自动编号）。"""
    if v is None or str(v).strip() == "":
        return None
    m = _re.search(r"\d+", str(v))
    if not m:
        raise ValueError("节次应填 1 或 2（可留空，按顺序自动编号）")
    n = int(m.group())
    if n < 1 or n > 9:
        raise ValueError("节次超出范围（1-9）")
    return n


def _sch_person(v):
    """「张三」「张三(1001)」→ (姓名, 工号/学号)。"""
    s = _re.sub(r"[（(]\s*[）)]", "", str(v or "")).strip()
    if not s:
        return "", ""
    m = _re.match(r"^(.*?)[（(]\s*([A-Za-z0-9_\-]+)\s*[）)]$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return s, ""


def _sch_resolve(v, by_name, by_code):
    """姓名/编号 → 人员 id。返回 (id, 错误信息)；空值返回 (0, None)。"""
    name, code = _sch_person(v)
    if not name and not code:
        return 0, None
    if code:
        if code in by_code:
            return by_code[code], None
        return 0, "查无编号 %s 的人员" % code
    ids = by_name.get(name)
    if not ids:
        return 0, "查无此人：%s（请先在教师/学生名单里添加）" % name
    if len(ids) > 1:
        return 0, "「%s」有 %d 个同名的人，请写成「%s(工号/学号)」" % (name, len(ids), name)
    return ids[0], None


@router.get("/schedule/template")
def schedule_template(year: int = 0, month: int = 0, _=Depends(auth.staff_only)):
    """课表导入模板：数据表 + 可选项（课程类型/教师/学生）+ 填写说明。"""
    now = datetime.now()
    year = year or now.year
    month = month or now.month
    with Session(db.engine) as s:
        ts = s.exec(select(models.Teacher).order_by(models.Teacher.emp_id)).all()
        ss = s.exec(select(models.Student).order_by(models.Student.stu_id)).all()
        cts = s.exec(select(models.ClassType).order_by(models.ClassType.id)).all()
        gs_tpl = s.exec(select(models.Graduate).order_by(models.Graduate.grad_id)).all()
    t_teach = [t for t in ts if t.can_teach]
    t_rev = [t for t in ts if t.can_review]
    a = t_teach[0].name if t_teach else ""
    b = t_rev[0].name if t_rev else ""
    c = t_rev[1].name if len(t_rev) > 1 else b
    stu = ss[0].name if ss else ""
    ct_names = [x.name for x in cts]
    rows = [
        ["%d-%02d-03" % (year, month), 1, ct_names[0] if ct_names else "教学阅片", "自选题", a, stu, b, ""],
        ["%d-%02d-03" % (year, month), 2, ct_names[1] if len(ct_names) > 1 else "小讲课", "", c, "", a, ""],
        ["%d-%02d-10" % (year, month), 1, ct_names[2] if len(ct_names) > 2 else "病例讨论", "", a, stu, b, ""],
    ]
    wb = _roster_wb(
        "课表", ["日期", "节次", "课程", "题目", "老师", "主阅", "评审", "指导"], rows,
        [13, 8, 14, 12, 12, 10, 10, 10],
        ["课表导入格式说明", "",
         "第 1 列 日期（必填。如 2026-11-03 或 11.03；下方单元格留空表示同上一个日期，兼容合并单元格）",
         "第 2 列 节次（填 1 / 2；留空则按当天出现顺序自动编号）",
         "第 3 列 课程（必填，必须是「课程类型」里已有的名称，可参考「可选项」工作表）",
         "第 4 列 题目（可选，留空显示为「排课规则」里设置的默认题目）",
         "第 5 列 老师（必填，填姓名；有重名时写成「张三(1001)」）",
         "第 6 列 主阅（课型为小功课时自动＝老师本人，可留空；其他课型填学生姓名，可留空）",
         "第 7 列 评审（可选，填教师姓名）",
         "第 8 列 指导 / 副主阅（可选，填教师姓名）",
         "",
         "提示：从系统「导出 Excel」得到的课表可直接改完再导入，日期列合并单元格无需拆分。",
         "提示：「入科教育」「出科考核反馈」「月考反馈」由系统自动生成，写在表里也会被跳过。"])
    extra = wb.create_sheet("可选项")
    extra.cell(row=1, column=1, value="课程类型").font = Font(bold=True)
    extra.cell(row=1, column=2, value="老师（姓名 / 工号 / 可讲课）").font = Font(bold=True)
    extra.cell(row=1, column=3, value="学生（姓名 / 学号）").font = Font(bold=True)
    extra.cell(row=1, column=4, value="研究生（姓名 / 编号，用于研究生汇报）").font = Font(bold=True)
    for i, x in enumerate(cts, start=2):
        extra.cell(row=i, column=1, value=x.name)
    for i, t in enumerate(ts, start=2):
        extra.cell(row=i, column=2, value="%s / %s / %s" % (t.name, t.emp_id, "是" if t.can_teach else "否"))
    for i, t in enumerate(ss, start=2):
        extra.cell(row=i, column=3, value="%s / %s" % (t.name, t.stu_id))
    for i, g in enumerate(gs_tpl, start=2):
        extra.cell(row=i, column=4, value="%s / %s" % (g.name, g.grad_id))
    extra.column_dimensions["A"].width = 16
    extra.column_dimensions["B"].width = 26
    extra.column_dimensions["C"].width = 22
    extra.column_dimensions["D"].width = 26
    return _roster_response(wb, "schedule_template_%d_%02d.xlsx" % (year, month))


@router.post("/schedule/import")
def import_schedule(file: UploadFile = File(...), year: int = 0, month: int = 0,
                    mode: str = "merge", dry_run: bool = False, auto_create: bool = False,
                    _=Depends(auth.staff_only)):
    """导入课表：dry_run=1 只校验并返回预览，dry_run=0 真正写入。

    mode=merge   合并：同一天同一节已存在则更新，其余新增（保留已发布状态）
    mode=replace 覆盖：先删掉「文件中涉及的每个月」的整份课表，再按文件重建
    支持全年/跨月文件：按行内日期自动分月，各月独立编号周次、独立合并/覆盖；
    year 参数仅用于「11.03」这类短格式日期的默认年份。
    auto_create=1：名单外人员自动入册且只增不改——老师/评审/指导列进教师表（工号 IMP-xxx），
    主阅列进学生表（学号 IMPxxx）；已有名字仅匹配，不覆盖任何名单信息。
    """
    if mode not in ("merge", "replace"):
        raise HTTPException(400, "mode 只能是 merge（合并）或 replace（覆盖）")
    if not year:
        year = datetime.now().year
    rows = _sch_read_table(file)
    hi, col = _sch_header(rows)

    with Session(db.engine) as s:
        ts = s.exec(select(models.Teacher)).all()
        ss = s.exec(select(models.Student)).all()
        cts = s.exec(select(models.ClassType)).all()
        rules = _load_rules(s)

        t_name, t_code = {}, {}
        for t in ts:
            t_name.setdefault(t.name.strip(), []).append(t.id)
            t_code[t.emp_id.strip()] = t.id
            # 别名也进姓名映射：课表文件里可写「王晓1」等惯用称呼（重名老师区分）
            for al in _re.split(r"[,，、;；\s]+", getattr(t, "aliases", "") or ""):
                al = al.strip()
                if al and al != t.name.strip():
                    t_name.setdefault(al, []).append(t.id)
        s_name, s_code = {}, {}
        for t in ss:
            s_name.setdefault(t.name.strip(), []).append(t.id)
            s_code[t.stu_id.strip()] = t.id
        gs = s.exec(select(models.Graduate)).all()
        g_name, g_code = {}, {}
        for g in gs:
            g_name.setdefault(g.name.strip(), []).append(g.id)
            g_code[g.grad_id.strip()] = g.id
        ct_map = {c.name.strip(): c for c in cts}
        tnames = {t.id: t.name for t in ts}
        snames = {t.id: t.name for t in ss}
        default_topic = (rules.get("default_topic") or "").strip()
        lecture_reader_is_teacher = bool(rules.get("small_lecture_reader_is_teacher", True))

        auto_cache = {}

        def rsv(v, by_name, by_code, row_no=0, kind="teacher"):
            """人员解析；勾选「自动新增」时，名单外的人自动入册。

            只增不改：名单里已有的名字只做匹配，绝不覆盖教师/学生表的任何信息；
            仅对完全查无此人的新增——老师/评审/指导列进教师表（工号 IMP-xxx），
            主阅列进学生表（学号 IMPxxx）。
            """
            i, e = _sch_resolve(v, by_name, by_code)
            if not e or not auto_create:
                return i, e
            nm, _cd = _sch_person(v)
            nm = nm.strip()
            if not nm:
                return i, e
            ck = (kind, nm)
            if ck in auto_cache:
                return auto_cache[ck], None
            if kind == "student":
                used_ids = {x.stu_id for x in ss}
                n = 1
                while ("IMP%03d" % n) in used_ids:
                    n += 1
                ns = models.Student(stu_id="IMP%03d" % n, name=nm)
                s.add(ns)
                s.flush()
                auto_cache[ck] = ns.id
                ss.append(ns)
                snames[ns.id] = nm
                s_name.setdefault(nm, []).append(ns.id)
                s_code[ns.stu_id] = ns.id
                warnings.append({"row": row_no, "msg": "已自动新增学生：%s（学号 %s，请在学生管理里补全信息）"
                                 % (nm, ns.stu_id)})
                return ns.id, None
            used_ids = {t.emp_id for t in ts}
            n = 1
            while ("IMP-%03d" % n) in used_ids:
                n += 1
            nt = models.Teacher(emp_id="IMP-%03d" % n, name=nm)
            s.add(nt)
            s.flush()
            auto_cache[ck] = nt.id
            ts.append(nt)
            tnames[nt.id] = nm
            t_name.setdefault(nm, []).append(nt.id)
            t_code[nt.emp_id] = nt.id
            warnings.append({"row": row_no, "msg": "已自动新增教师：%s（工号 %s，请在教师管理里补全信息）"
                             % (nm, nt.emp_id)})
            return nt.id, None

        errors, warnings, items = [], [], []
        last_date, slot_seq, used = None, {}, set()
        for ri, row in enumerate(rows[hi + 1:], start=hi + 2):
            if row is None or all(v is None or str(v).strip() == "" for v in row):
                continue

            # 全年导出的月块结构：跳过「…培训计划（2026.11）」标题行与每块重复的表头行
            first = _hname(row[0]) if row and len(row) else ""
            if first:
                if "培训计划" in first:
                    continue
                if first == "日期":
                    continue

            def cell(key, _row=row):
                j = col.get(key)
                if j is None or j >= len(_row):
                    return None
                return _row[j]

            try:
                d = _sch_date(cell("date"), year, month)
                if d is None:                       # 合并单元格：沿用上一行日期
                    if last_date is None:
                        raise ValueError("日期为空")
                    d = last_date
                last_date = d
                ds = d.isoformat()

                ct_name = str(cell("class_type") or "").strip()
                if not ct_name:
                    raise ValueError("课程（课型）不能为空")
                if ct_name in _SCH_AUTO_TYPES:
                    warnings.append({"row": ri, "msg": "「%s」由系统自动生成，已跳过" % ct_name})
                    continue
                ct = ct_map.get(ct_name)
                if not ct:
                    raise ValueError("课程「%s」不在课程类型里（可选：%s）"
                                     % (ct_name, "、".join(ct_map.keys()) or "请先维护课程类型"))

                slot = _sch_slot(cell("slot"))
                if slot is None:
                    slot_seq[ds] = slot_seq.get(ds, 0) + 1
                    slot = slot_seq[ds]
                else:
                    slot_seq[ds] = max(slot_seq.get(ds, 0), slot)

                # 研究生汇报：老师列填「汇报人」（研究生名册），其余角色留空
                if ct_name == "研究生汇报":
                    pid_, err = _sch_resolve(cell("teacher"), g_name, g_code)
                    if err:
                        raise ValueError(err)
                    if not pid_:
                        # 汇报人留空（全年导出常见：名册尚未指派）：
                        # 合并导入时保留库里已指派的汇报人，不当作错误
                        warnings.append({"row": ri, "msg": "%s 第 %d 节研究生汇报未填汇报人，"
                                                         "合并导入时保留库里已指派的汇报人" % (ds, slot)})
                    topic = str(cell("topic") or "").strip()
                    key = (ds, slot)
                    if key in used:
                        raise ValueError("同一天第 %d 节出现了两次" % slot)
                    used.add(key)
                    items.append({
                        "row": ri, "date": ds, "slot": slot, "class_type_id": ct.id,
                        "class_type_name": ct.name, "teacher_id": 0, "main_reader_id": 0,
                        "reviewer_id": 0, "mentor_id": 0, "presenter_id": pid_, "topic": topic,
                    })
                    continue

                tid, err = rsv(cell("teacher"), t_name, t_code, ri)
                if err:
                    raise ValueError(err)
                if not tid:
                    # 阅片/讨论等由「主阅」负责的节次，老师列常为空，不算错误
                    warnings.append({"row": ri, "msg": "%s 第 %d 节「%s」未填老师，已留空（该节由主阅负责）"
                                     % (ds, slot, ct_name)})
                rid, err = rsv(cell("reviewer"), t_name, t_code, ri)
                if err:
                    raise ValueError(err)
                mid, err = rsv(cell("mentor"), t_name, t_code, ri)
                if err:
                    raise ValueError(err)

                raw_main = cell("main_reader")
                if ct_name == "小讲课" and lecture_reader_is_teacher:
                    main_id = tid                  # 小讲课：主阅默认＝讲课老师本人
                else:
                    main_id, err = _sch_resolve(raw_main, s_name, s_code)
                    if err:                        # 也允许填教师姓名（主阅来源=教师）
                        main_id, err2 = _sch_resolve(raw_main, t_name, t_code)
                        if err2:
                            # 名单外的主阅：自动新增时进学生表（主阅由学生承担为主）
                            main_id, err3 = rsv(raw_main, s_name, s_code, ri, kind="student")
                            if err3:
                                raise ValueError(err)
                    if not main_id:
                        warnings.append({"row": ri, "msg": "%s %d 节未填主阅，先留空" % (ds, slot)})

                topic = str(cell("topic") or "").strip()
                if default_topic and topic == default_topic:
                    topic = ""                     # 导出时补的默认题目，回导时还原为空

                key = (ds, slot)
                if key in used:
                    raise ValueError("同一天第 %d 节出现了两次" % slot)
                used.add(key)
                items.append({
                    "row": ri, "date": ds, "slot": slot, "class_type_id": ct.id,
                    "class_type_name": ct.name, "teacher_id": tid, "main_reader_id": main_id,
                    "reviewer_id": rid, "mentor_id": mid, "topic": topic,
                })
            except ValueError as ex:
                if len(errors) < _SCH_MAX_ERRORS:
                    errors.append({"row": ri, "msg": str(ex)})

        # 按月分组：全年/跨月文件各月独立导入（周次按月独立编号）
        groups = {}                                  # (y, m) -> [items]
        for it in items:
            y_, m_ = int(it["date"][:4]), int(it["date"][5:7])
            groups.setdefault((y_, m_), []).append(it)

        mctx = {}                                    # (y, m) -> {existing, map, wk}
        months = []                                  # 每月汇总
        for (y_, m_) in sorted(groups):
            its = groups[(y_, m_)]
            existing_m = s.exec(select(models.ClassSession)
                                .where(models.ClassSession.year == y_,
                                       models.ClassSession.month == m_)).all()
            emap = {(x.date, x.slot): x for x in existing_m}
            all_dates = sorted({it["date"] for it in its} |
                               ({x.date for x in existing_m} if mode == "merge" else set()))
            wk = {d: i + 1 for i, d in enumerate(all_dates)}
            mctx[(y_, m_)] = {"existing": existing_m, "map": emap, "wk": wk}
            created_ = sum(1 for it in its if (it["date"], it["slot"]) not in emap)
            months.append({"year": y_, "month": m_, "total": len(its),
                           "created": created_, "updated": len(its) - created_,
                           "existing": len(existing_m),
                           "deleted": len(existing_m) if mode == "replace" else 0})

        def disp(pid, students_first=False):
            if not pid:
                return ""
            if students_first and pid in snames:
                return snames[pid]
            return tnames.get(pid) or snames.get(pid) or ""

        gnames = {g.id: g.name for g in gs}
        preview = []
        for it in sorted(items, key=lambda x: (x["date"], x["slot"])):
            ctx_ = mctx[(int(it["date"][:4]), int(it["date"][5:7]))]
            gp = it.get("presenter_id", 0)
            preview.append({
                "row": it["row"], "date": it["date"], "week": ctx_["wk"].get(it["date"], 0), "slot": it["slot"],
                "class_type": it["class_type_name"],
                "teacher": gnames.get(gp, "") if gp else tnames.get(it["teacher_id"], ""),
                "main_reader": disp(it["main_reader_id"], it["class_type_name"] != "小讲课"),
                "reviewer": tnames.get(it["reviewer_id"], ""),
                "mentor": tnames.get(it["mentor_id"], ""),
                "topic": it["topic"],
                "action": "更新" if (it["date"], it["slot"]) in ctx_["map"] else "新增",
            })
        created = sum(1 for p in preview if p["action"] == "新增")
        updated = len(preview) - created
        deleted = sum(m["deleted"] for m in months)
        existing_total = sum(m["existing"] for m in months)
        months_brief = "、".join("%d年%02d月" % (m["year"], m["month"]) for m in months[:6]) \
                       + ("等 %d 个月" % len(months) if len(months) > 6 else "")

        if dry_run:
            return {"ok": True, "dry_run": True, "mode": mode, "total": len(preview),
                    "created": created, "updated": updated,
                    "deleted": deleted, "existing": existing_total,
                    "months": months, "months_brief": months_brief,
                    "errors": errors, "warnings": warnings,
                    "preview": preview[:_SCH_PREVIEW_MAX], "truncated": len(preview) > _SCH_PREVIEW_MAX}

        if not items:
            raise HTTPException(400, "没有可导入的有效数据行" +
                                ("，请先修正表格（共 %d 处问题）" % len(errors) if errors else ""))

        for (y_, m_) in sorted(groups):
            ctx_ = mctx[(y_, m_)]
            if mode == "replace":
                for x in ctx_["existing"]:
                    s.delete(x)
                s.commit()                         # 先删干净，避免 (日期, 节次) 唯一性冲突
                ctx_["map"] = {}
        tmap_i = {t.id: t.name for t in s.exec(select(models.Teacher)).all()}
        smap_i = {t.id: t.name for t in s.exec(select(models.Student)).all()}
        gmap_i = {g.id: g.name for g in s.exec(select(models.Graduate)).all()}
        for it in groups[(y_, m_)]:
            old = ctx_["map"].get((it["date"], it["slot"]))
            if old:
                old.class_type_id = it["class_type_id"]
                old.class_type_name = it["class_type_name"]
                old.teacher_id = it["teacher_id"]
                old.main_reader_id = it["main_reader_id"]
                old.reviewer_id = it["reviewer_id"]
                old.mentor_id = it["mentor_id"]
                old.presenter_id = it.get("presenter_id", 0) or old.presenter_id
                # 汇报人留空时保留库里已指派的汇报人（见导入解析处的 warning）
                old.topic = it["topic"]
                old.week_index = ctx_["wk"].get(it["date"], old.week_index)
                _snap_names(old, tmap_i, smap_i, gmap_i)
                s.add(old)
            else:
                row = models.ClassSession(
                    year=y_, month=m_, date=it["date"], slot=it["slot"],
                    week_index=ctx_["wk"].get(it["date"], 0),
                    class_type_id=it["class_type_id"], class_type_name=it["class_type_name"],
                    teacher_id=it["teacher_id"], main_reader_id=it["main_reader_id"],
                    reviewer_id=it["reviewer_id"], mentor_id=it["mentor_id"],
                    presenter_id=it.get("presenter_id", 0),
                    topic=it["topic"], status="draft",
                    created_at=datetime.now().isoformat(timespec="seconds"))
                _snap_names(row, tmap_i, smap_i, gmap_i)
                s.add(row)
            s.commit()
            # 统一该月周次（合并导入时老行也可能需要跟着挪）
            for x in s.exec(select(models.ClassSession)
                            .where(models.ClassSession.year == y_,
                                   models.ClassSession.month == m_)).all():
                if x.date in ctx_["wk"] and x.week_index != ctx_["wk"][x.date]:
                    x.week_index = ctx_["wk"][x.date]
                    s.add(x)
            s.commit()
    return {"ok": True, "dry_run": False, "mode": mode, "total": len(items),
            "created": created, "updated": updated,
            "deleted": deleted, "months": months, "months_brief": months_brief,
            "errors": errors, "warnings": warnings}


@router.put("/schedule/{sid}")
def upd_session(sid: int, payload: dict, _=Depends(auth.staff_only)):
    with Session(db.engine) as s:
        d = s.get(models.ClassSession, sid)
        if not d:
            raise HTTPException(404)
        for k in ("teacher_id", "main_reader_id", "reviewer_id", "mentor_id", "presenter_id", "date"):
            if k in payload:
                if k == "date":
                    setattr(d, k, str(payload[k]))
                else:
                    setattr(d, k, int(payload[k]))
        if "topic" in payload:
            d.topic = str(payload["topic"] or "")
        # 角色变动后刷新名字快照（历史课表不因名单删除而变空白）
        tmap = {t.id: t.name for t in s.exec(select(models.Teacher)).all()}
        smap = {t.id: t.name for t in s.exec(select(models.Student)).all()}
        gmap = {g.id: g.name for g in s.exec(select(models.Graduate)).all()}
        _snap_names(d, tmap, smap, gmap)
        s.add(d); s.commit(); s.refresh(d); return d.dict()


@router.delete("/schedule/{sid}")
def del_session(sid: int, _=Depends(auth.admin_only)):
    """删除单节课安排"""
    with Session(db.engine) as s:
        d = s.get(models.ClassSession, sid)
        if not d:
            raise HTTPException(404)
        s.delete(d); s.commit()
        return {"ok": True, "deleted": 1}


@router.delete("/schedule")
def clear_schedule(year: int, month: int, _=Depends(auth.admin_only)):
    """删除某月整份课表（草稿或已发布），用于推倒重排"""
    with Session(db.engine) as s:
        rows = s.exec(select(models.ClassSession)
                      .where(models.ClassSession.year == year,
                             models.ClassSession.month == month)).all()
        for x in rows:
            s.delete(x)
        s.commit()
        return {"ok": True, "deleted": len(rows)}


@router.post("/schedule/publish")
def publish(payload: dict, _=Depends(auth.staff_only)):
    year = int(payload["year"]); month = int(payload["month"])
    with Session(db.engine) as s:
        sessions = s.exec(select(models.ClassSession)
                          .where(models.ClassSession.year == year, models.ClassSession.month == month)).all()
        if not sessions:
            raise HTTPException(status_code=400, detail="请先生成课表")
        for x in sessions:
            x.status = "published"
        s.commit()
        cfg = s.exec(select(models.DingTalkConfig)).first()
        configured = bool(cfg and cfg.enabled and cfg.webhook_url)
        teacher_objs = {t.id: t for t in s.exec(select(models.Teacher)).all()}
        tnames = {tid: t.name for tid, t in teacher_objs.items()}
        students = {t.id: t.name for t in s.exec(select(models.Student)).all()}
        grads = {g.id: g.name for g in s.exec(select(models.Graduate)).all()}
        text = build_dingtalk_text(year, month, sessions, tnames, students, grads)
        # 配了系统对外地址 → 发带「查看完整课表」按钮的卡片；否则退回纯 markdown
        base = (getattr(cfg, "todo_base_url", "") or "").strip().rstrip("/")
        buttons = [{"title": "查看完整课表", "url": base + "/#/schedule-view"}] if base else None
        note = send_dingtalk(cfg, text, buttons)
        pushed = note.startswith("钉钉推送成功")
        # 同步推送到其他钉钉群（后台启用中的群）
        extra_push = [{"name": n, "note": t} for n, t in push_extra_groups(s, text, buttons)]
        # 自动为参与者创建钉钉待办提醒（需配置企业应用并启用）
        todo_note = ""
        try:
            _n, todo_note = create_schedule_todos(cfg, year, month, sessions,
                                                  teacher_objs, students, _load_rules(s))
        except Exception as e:
            todo_note = "钉钉待办创建异常：" + str(e)[:120]
        # 把每节课同步成钉钉日历日程（老师手机日历直接看到色块，到点自动提醒）
        cal_note = ""
        try:
            _a, _b, _c, _d, cal_note = sync_calendar_events(cfg, year, month, sessions,
                                                            teacher_objs, students, _load_rules(s))
        except Exception as e:
            cal_note = "钉钉日程同步异常：" + str(e)[:120]
        return {"ok": True, "count": len(sessions), "configured": configured,
                "pushed": pushed, "dingtalk": note, "extra_push": extra_push,
                "todo_note": todo_note, "calendar_note": cal_note}


def build_dingtalk_text(year, month, sessions, teachers, students, grads=None):
    grads = grads or {}
    lines = [f"### {year}年{month}月 教学课表"]
    for x in sessions:
        if x.class_type_name == "研究生汇报":
            line = f"- {x.date} 第{x.slot}节 【研究生汇报】汇报人:{getattr(x, 'presenter_name', '') or grads.get(x.presenter_id, '')}"
            if (x.topic or "").strip():
                line += f" 题目:{x.topic.strip()}"
            lines.append(line)
            continue
        t = getattr(x, "teacher_name", "") or teachers.get(x.teacher_id, "")
        # 主阅：小讲课=老师本人（教师名册），其余课型=学生名册
        m = (getattr(x, "main_reader_name", "")
             or (teachers.get(x.main_reader_id, "") if x.class_type_name == "小讲课"
                 else students.get(x.main_reader_id, "")))
        r = getattr(x, "reviewer_name", "") or teachers.get(x.reviewer_id, "")
        line = f"- {x.date} 第{x.slot}节 【{x.class_type_name}】老师:{t} 主阅:{m} 评审:{r}"
        if x.mentor_id:
            line += f" 指导:{getattr(x, 'mentor_name', '') or teachers.get(x.mentor_id, '')}"
        lines.append(line)
    return "\n".join(lines)


def _dt_first_kw(cfg) -> str:
    """机器人安全设置的关键词：取配置的第一个（默认「课程表」），保证消息必含关键词。"""
    kws = (getattr(cfg, "keywords", "") or "").strip()
    for kw in _re.split(r"[,，;；、]+", kws):
        kw = kw.strip()
        if kw:
            return kw
    return "课程表"


def _dt_kw_title(cfg) -> str:
    return "%s | 教学课表发布" % _dt_first_kw(cfg)


def _dt_text_with_kw(cfg, text: str) -> str:
    """正文不含关键词时在开头补上，兼容钉钉「关键词校验」安全模式。"""
    kw = _dt_first_kw(cfg)
    if kw in text:
        return text
    return "**%s**\n\n%s" % (kw, text)


def _build_dt_payload(cfg, text, buttons=None):
    """构造群消息体：有可点按钮 → ActionCard，否则 markdown。"""
    body_text = _dt_text_with_kw(cfg, text)
    title = _dt_kw_title(cfg)
    valid_buttons = [b for b in (buttons or []) if (b.get("url") or "").strip()]
    if valid_buttons:
        return {"msgtype": "actionCard",
                "actionCard": {
                    "title": title,
                    "text": body_text,
                    "btnOrientation": "1",
                    "btns": [{"title": b["title"], "actionURL": b["url"].strip()} for b in valid_buttons],
                }}
    return {"msgtype": "markdown", "markdown": {"title": title, "text": body_text}}


def _sign_url(url: str, secret: str) -> str:
    """机器人开启「加签」时在 URL 上追加 timestamp+sign，否则原样返回。"""
    if not secret:
        return url
    ts = str(round(time.time() * 1000))
    string_to_sign = "%s\n%s" % (ts, secret)
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                      hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    sep = "&" if "?" in url else "?"
    return "%s%stimestamp=%s&sign=%s" % (url, sep, ts, sign)


def _send_robot(obj, text, buttons=None):
    """向一个群机器人发送消息。obj 需有 webhook_url / secret / keywords 属性
    （DingTalkConfig 或 DingTalkWebhook 均可）。返回结果说明。"""
    url = (getattr(obj, "webhook_url", "") or "").strip()
    if not url:
        return "未配置 Webhook，跳过"
    # 防御：URL 里混入中文/空白时给出可读错误（Webhook 必须是纯 ASCII）
    try:
        url.encode("ascii")
    except UnicodeEncodeError:
        bad = [ch for ch in url if ord(ch) > 127]
        return ("钉钉推送失败: Webhook 地址含非法字符「%s」，请到钉钉群机器人设置里重新完整复制 Webhook 地址"
                % "".join(sorted(set(bad))))
    payload = _build_dt_payload(obj, text, buttons)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(_sign_url(url, obj.secret or ""), data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            try:
                j = json.loads(body)
                if j.get("errcode", 0) != 0:
                    return "钉钉推送失败: " + body[:150]
            except Exception:
                pass
            return "钉钉推送成功: " + body[:80]
    except Exception as e:
        return "钉钉推送失败: " + str(e)[:150]


def send_dingtalk(cfg, text, buttons=None):
    """推送主群消息。buttons 非空且配置了系统对外地址时发 ActionCard（带可点按钮），否则发 markdown。"""
    if not cfg or not cfg.enabled or not cfg.webhook_url:
        return "未配置钉钉，仅本地发布"
    return _send_robot(cfg, text, buttons)


def push_extra_groups(s, text, buttons=None):
    """把同一份消息推送到「其他钉钉群」里所有启用的 Webhook。
    返回 [(群名, 结果说明), ...]，未启用/未配置 Webhook 的群自动跳过。"""
    out = []
    rows = s.exec(select(models.DingTalkWebhook).order_by(models.DingTalkWebhook.id)).all()
    for r in rows:
        if not r.enabled or not (r.webhook_url or "").strip():
            continue
        out.append((r.name or ("群#%d" % r.id), _send_robot(r, text, buttons)))
    return out


# ---------- 钉钉待办提醒（企业内部应用 API） ----------

_DT_TIMEOUT = 15
_PHONE_RE = _re.compile(r"^1\d{10}$")


def _dt_open_api(payload: dict, url: str, token: str, method: str = "POST"):
    """调用 api.dingtalk.com（新版接口）。返回 JSON；非 2xx/错误码抛异常。"""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json",
                 "x-acs-dingtalk-access-token": token})
    try:
        with urllib.request.urlopen(req, timeout=_DT_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 403 的响应体里才写着缺哪个权限码，直接抛 HTTPError 用户只看到 "HTTP Error 403: Forbidden"
        try:
            j = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise ValueError("钉钉接口返回 HTTP %s（响应体无法解析）" % e.code)
        msg = j.get("message") or j.get("errmsg") or json.dumps(j, ensure_ascii=False)
        scopes = (j.get("accessdenieddetail") or {}).get("requiredScopes") or []
        if scopes:
            msg = "%s｜需要权限：%s" % (msg, "、".join(scopes))
        raise ValueError(str(msg)[:300])


def _dt_oapi(payload: dict, url_path_qs: str, method: str = "POST"):
    """调用 oapi.dingtalk.com（旧版接口）。POST 时 access_token/参数在 query 或 body；GET 则必须无请求体。"""
    data = json.dumps(payload).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(url_path_qs, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=_DT_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _dt_access_token(cfg) -> str:
    url = ("https://oapi.dingtalk.com/gettoken?appkey=%s&appsecret=%s"
           % (urllib.parse.quote(cfg.app_key), urllib.parse.quote(cfg.app_secret)))
    j = _dt_oapi({}, url, method="GET")   # gettoken 仅支持 GET，POST 会报 43001 需要GET请求
    if j.get("errcode") != 0:
        raise ValueError("获取 access_token 失败: %s" % json.dumps(j, ensure_ascii=False)[:120])
    return j["access_token"]


def _dt_userid(token: str, mobile: str) -> str:
    """手机号 → 钉钉 userid（单聊、待办都要用）。"""
    j = _dt_oapi({"mobile": mobile},
                 "https://oapi.dingtalk.com/topapi/v2/user/getbymobile?access_token=" + token)
    if j.get("errcode") != 0:
        raise ValueError("手机号 %s 未匹配到钉钉用户: %s"
                         % (mobile, str(j.get("errmsg", ""))[:120]))
    userid = (j.get("result") or {}).get("userid")
    if not userid:
        raise ValueError("手机号 %s 未匹配到钉钉用户（接口未返回 userid）" % mobile)
    return userid


def _dt_send_dm(cfg, token: str, userid: str, text: str) -> str:
    """机器人单聊发一条文本。返回 processQueryKey；失败抛异常。

    走 /v1.0/robot/oToMessages/batchSend（实测可用，/oToMessages/send 不存在）。
    单聊是纯出站调用，内网部署无需任何对外地址。
    """
    body = {"robotCode": cfg.app_key, "userIds": [userid],
            "msgKey": "sampleText",
            "msgParam": json.dumps({"content": text}, ensure_ascii=False)}
    j = _dt_open_api(body, "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend", token)
    bad = j.get("invalidStaffIdList") or []
    if bad:
        raise ValueError("钉钉拒绝投递：invalidStaffIdList=%s" % bad)
    return str(j.get("processQueryKey") or "")


# ---------- 一节课只有一个责任人（避免多老师互相覆盖课程名） ----------

ROLE_ORDER = ("讲课", "主阅", "评审", "指导")


def session_roles(x):
    """返回该课的角色列表 [(角色名, Teacher.id)]，按讲课→主阅→评审→指导排序。"""
    pairs = (("讲课", x.teacher_id), ("主阅", x.main_reader_id),
             ("评审", x.reviewer_id), ("指导", x.mentor_id))
    out, seen = [], set()
    for role, tid in pairs:
        if tid and tid not in seen:
            seen.add(tid)
            out.append((role, tid))
    return out


def session_owner(x, teachers: dict):
    """一节课唯一的责任填报人。

    规则：讲课老师优先；若讲课老师没登记手机号（钉钉触达不到），顺延给本课
    其他角色中第一个有手机号的人，并在消息里注明"暂由您代填"。
    这样一节课在钉钉侧只会有一个人收到填报请求、只有一个人写入 topic，
    不会出现两位老师先后填写、课程名反复被覆盖的情况。

    返回 (Teacher 对象或 None, 角色名, 是否为代理)。
    """
    cands = [(role, teachers[tid]) for role, tid in session_roles(x) if tid in teachers]
    if not cands:
        return None, "", False
    for role, t in cands:
        if _PHONE_RE.match((t.phone or "").strip()):
            return t, role, role != "讲课"
    return None, cands[0][0], False


def _fmt_hhmm(t: str) -> str:
    """把 8:00 统一成 08:00，消息里对齐好看。"""
    try:
        return datetime.strptime(t, "%H:%M").strftime("%H:%M")
    except Exception:
        return t or ""


def dt_dm_brief(s, teacher, items, rules, year=None, month=None):
    """生成一条单聊待填清单。items = [(ClassSession, 角色名, 是否代理)]。"""
    lines = []
    for i, (x, role, proxy) in enumerate(items, 1):
        t_ = _fmt_hhmm(_course_time(x.class_type_name, x.month, rules) or "17:30")
        wd = "周" + "一二三四五六日"[datetime.fromisoformat(x.date).weekday()]
        lines.append("%d. %s %s %s【%s】%s"
                     % (i, x.date[5:], wd, t_, x.class_type_name, role))
    head = "%s老师，您好。" % (teacher.name or "老师")
    if year and month:
        head += "\n%d年%d月您有 %d 节课待填课程名：" % (year, month, len(items))
    else:
        head += "\n您有 %d 节课待填课程名：" % len(items)
    proxy_note = ""
    if any(p for _, _, p in items):
        proxy_note = "\n（其中有课程的主讲老师未登记手机号，暂由您代填。）"
    tail = ("直接回复「12-03 课程名」即可登记，回一条填一节。\n"
            "已填过的课如需修改，回复「改 12-03 新课程名」。\n"
            "查看全部：回复「列表」")
    return "\n".join([head, ""] + lines + [proxy_note, tail])


def _dt_union_id(token: str, mobile: str) -> str:
    """手机号 → userid → unionId（待办接口需要 unionId）。"""
    j = _dt_oapi({"mobile": mobile},
                 "https://oapi.dingtalk.com/topapi/v2/user/getbymobile?access_token=" + token)
    if j.get("errcode") != 0:
        raise ValueError("手机号 %s 未匹配到钉钉用户: %s"
                         % (mobile, str(j.get("errmsg", ""))[:120]))
    userid = (j.get("result") or {}).get("userid")
    if not userid:
        raise ValueError("手机号 %s 未匹配到钉钉用户（接口未返回 userid）" % mobile)
    # 注意：旧版 /topapi/user/get 已下线，调用会返回 isp.top-remote-connection-error（远程服务不存在），必须用 v2
    j2 = _dt_oapi({"userid": userid, "language": "zh_CN"},
                  "https://oapi.dingtalk.com/topapi/v2/user/get?access_token=" + token)
    if j2.get("errcode") != 0:
        # 注意：切片必须作用在字符串上，写成 ValueError(...)[:120] 会抛 TypeError 并吞掉真实错误
        raise ValueError("获取 unionId 失败: %s" % str(j2.get("errmsg", ""))[:120])
    union_id = (j2.get("result") or {}).get("unionid")
    if not union_id:
        raise ValueError("userid %s 未返回 unionId（需开通「个人手机号信息 Contact.User.mobile」权限）" % userid)
    return union_id


def _dt_create_todo(token: str, union_id: str, subject: str, description: str, due_ms: int):
    url = "https://api.dingtalk.com/v1.0/todo/users/%s/tasks" % union_id
    return _dt_open_api({
        "subject": subject,
        "description": description,
        "dueDate": due_ms,
        "executorIds": [union_id],
    }, url, token)


def _dt_create_todo_body(token: str, union_id: str, body: dict):
    """创建待办（完整 body 版本，支持 detailUrl 跳转填报）。"""
    url = "https://api.dingtalk.com/v1.0/todo/users/%s/tasks" % union_id
    return _dt_open_api(body, url, token)


def _dt_delete_todo(token: str, union_id: str, task_id: str):
    """删除待办（内容变更时先删旧建新，避免老师手里堆重复待办）。"""
    url = "https://api.dingtalk.com/v1.0/todo/users/%s/tasks/%s" % (union_id, task_id)
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"x-acs-dingtalk-access-token": token})
    try:
        with urllib.request.urlopen(req, timeout=_DT_TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        raise ValueError("删除旧待办失败 HTTP %s: %s" % (e.code, e.read().decode()[:120]))


def _session_start_ms(d: str, t: str) -> int:
    return int(datetime.strptime(d + " " + t, "%Y-%m-%d %H:%M").timestamp() * 1000)


# ---------- 钉钉日历日程 ----------
# 官方接口（已验证可用，权限 Calendar.Event.Write）：
#   POST   /v1.0/calendar/users/{unionId}/calendars/primary/events
#   PUT    /v1.0/calendar/users/{unionId}/calendars/primary/events/{eventId}
#   DELETE /v1.0/calendar/users/{unionId}/calendars/primary/events/{eventId}
_EVENT_BASE = "https://api.dingtalk.com/v1.0/calendar/users/%s/calendars/primary/events"


def _dt_create_event(token: str, union_id: str, body: dict):
    return _dt_open_api(body, _EVENT_BASE % union_id, token)


def _dt_update_event(token: str, union_id: str, event_id: str, body: dict):
    """更新日程。注意：钉钉要求 body 里必须带 id 字段，否则报「parameter id is not found」。"""
    url = _EVENT_BASE % union_id + "/" + urllib.parse.quote(event_id, safe="")
    payload = dict(body)
    payload["id"] = event_id
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="PUT",
                                 headers={"Content-Type": "application/json",
                                          "x-acs-dingtalk-access-token": token})
    try:
        with urllib.request.urlopen(req, timeout=_DT_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raise ValueError("更新日程失败 HTTP %s: %s" % (e.code, e.read().decode()[:160]))


def _dt_delete_event(token: str, union_id: str, event_id: str):
    url = _EVENT_BASE % union_id + "/" + urllib.parse.quote(event_id, safe="")
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"x-acs-dingtalk-access-token": token})
    try:
        with urllib.request.urlopen(req, timeout=_DT_TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        raise ValueError("删除日程失败 HTTP %s: %s" % (e.code, e.read().decode()[:160]))


def _event_role_ids(x, teachers) -> list:
    """一节课的参与者（教师侧）：讲课 / 主阅（仅教师名册的）/ 评审 / 指导。"""
    ids = [("讲课", x.teacher_id), ("评审", x.reviewer_id)]
    if x.main_reader_id in teachers:
        ids.append(("主阅", x.main_reader_id))
    if x.mentor_id:
        ids.append(("指导", x.mentor_id))
    return ids


def sync_calendar_events(cfg, year: int, month: int, sessions, teachers, students, rules, prune: bool = True):
    """把当月每节课同步为钉钉日历日程（按人建日程 + 邀请其他参与者）。
    返回 (新建数, 更新数, 跳过数, 删除数, 提示消息)。

    - 每节课只建一条日程，挂在「讲课老师」的日历上；讲课老师没手机号时挂到任一位有号的参与者。
    - 参与者（讲课/主阅/评审/指导）作为 attendees 一并邀请，各自日历里都能看到。
    - 内容指纹未变则跳过；课程改名/换人/改期则原地更新该日程；
      prune=True 时（整月同步）还会删掉「课表里已不存在」的日程，只同步单节课时请传 False。
    """
    if not getattr(cfg, "calendar_enabled", False) or not cfg.app_key or not cfg.app_secret:
        return 0, 0, 0, 0, "未启用日程同步或未配置企业应用"

    token = _dt_access_token(cfg)
    dur = max(15, int(getattr(cfg, "event_duration", 60) or 60))
    remind = int(getattr(cfg, "event_reminder_min", 15) or 0)
    uid_cache = {}   # phone -> unionId（同一次发布内复用，避免重复请求钉钉）

    def uid_of(tid):
        tobj = teachers.get(tid)
        if not tobj:
            return None
        phone = (getattr(tobj, "phone", "") or "").strip()
        if not _PHONE_RE.match(phone):
            return None
        if phone not in uid_cache:
            try:
                uid_cache[phone] = _dt_union_id(token, phone)
            except Exception:
                uid_cache[phone] = None
        return uid_cache[phone]

    def uid_of_grad(gobj):
        """研究生汇报人按「研究生管理」的手机号反查 unionId（与教师同一缓存）。"""
        if not gobj:
            return None
        phone = (getattr(gobj, "phone", "") or "").strip()
        if not _PHONE_RE.match(phone):
            return None
        if phone not in uid_cache:
            try:
                uid_cache[phone] = _dt_union_id(token, phone)
            except Exception:
                uid_cache[phone] = None
        return uid_cache[phone]

    with Session(db.engine) as s0:
        prev_map = {r.session_id: r for r in s0.exec(
            select(models.EventSent).where(models.EventSent.year == year,
                                           models.EventSent.month == month)).all()}
        grads = {g.id: g for g in s0.exec(select(models.Graduate)).all()}

    created = updated = skipped = deleted = no_phone = 0
    fails = []
    keep_ids = set()

    for x in sessions:
        keep_ids.add(x.id)
        t_ = _course_time(x.class_type_name, x.month, rules) or "17:30"
        try:
            start_dt = datetime.strptime(x.date + " " + t_, "%Y-%m-%d %H:%M")
        except ValueError:
            fails.append("%s 时间格式异常" % x.date)
            continue
        end_dt = start_dt + timedelta(minutes=dur)

        if x.class_type_name == "研究生汇报":
            # 汇报节没有教师角色：日程挂在汇报人日历上（需登记手机号）
            g = grads.get(x.presenter_id)
            u = uid_of_grad(g)
            if not u:
                no_phone += 1      # 汇报人没登记手机号，暂时建不了日程（不算错误）
                continue
            att = [("汇报", (g.name if g else ""), u)]
            org_uid = u
        else:
            parts = _event_role_ids(x, teachers)
            att = []   # [(role, name, uid)]
            for role, tid in parts:
                u = uid_of(tid)
                if u:
                    tobj = teachers.get(tid)
                    att.append((role, (tobj.name if tobj else ""), u))
            if not att:
                no_phone += 1      # 参与者都没填手机号，这节课暂时建不了日程（不算错误）
                continue
            # 组织者：优先讲课老师，其次第一位有号的参与者
            org_uid = next((u for (r, _, u) in att if r == "讲课"), att[0][2])

        topic = (x.topic or "").strip() or "（待填报）"
        summary = "【%s】%s" % (x.class_type_name, topic)
        tname = lambda tid: (teachers.get(tid).name if teachers.get(tid) else "")
        if x.class_type_name == "研究生汇报":
            g = grads.get(x.presenter_id)
            desc_lines = ["课型：研究生汇报",
                          "汇报人：%s" % ((g.name if g else "—") or "—"),
                          "题目：%s" % topic]
        else:
            desc_lines = ["课型：%s" % x.class_type_name,
                          "课程名：%s" % topic,
                          "讲课：%s" % (tname(x.teacher_id) or "—")]
            if x.main_reader_id in teachers:
                desc_lines.append("主阅：%s" % tname(x.main_reader_id))
            elif x.main_reader_id:
                desc_lines.append("主阅：%s" % (students.get(x.main_reader_id) or "—"))
            desc_lines.append("评审：%s" % (tname(x.reviewer_id) or "—"))
            if x.mentor_id:
                desc_lines.append("指导：%s" % (tname(x.mentor_id) or "—"))
        if (getattr(cfg, "todo_base_url", "") or "").strip():
            desc_lines.append("填报/查看：%s" % cfg.todo_base_url.strip().rstrip("/"))
        desc = "\n".join(desc_lines)

        att_ids = sorted({u for (_, _, u) in att})
        seed = "|".join([summary, start_dt.isoformat(), end_dt.isoformat(), org_uid] + att_ids)
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
        body = {
            "summary": summary,
            "description": desc,
            "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00"), "timeZone": "Asia/Shanghai"},
            "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00"), "timeZone": "Asia/Shanghai"},
            "attendees": [{"id": u, "isOptional": False} for u in att_ids],
        }
        if remind > 0:
            body["reminders"] = [{"method": "dingtalk", "minutes": remind}]

        prev = prev_map.get(x.id)
        try:
            if prev and prev.digest == digest and prev.event_id:
                skipped += 1
                continue
            if prev and prev.event_id:
                # 内容或归属人变了：能原地改就改，改不动（换了组织者）就删掉重建
                if prev.organizer_uid == org_uid:
                    _dt_update_event(token, org_uid, prev.event_id, body)
                    resp = {"id": prev.event_id}
                    updated += 1
                else:
                    try:
                        _dt_delete_event(token, prev.organizer_uid, prev.event_id)
                    except Exception:
                        pass
                    resp = _dt_create_event(token, org_uid, body)
                    created += 1
            else:
                resp = _dt_create_event(token, org_uid, body)
                created += 1
            ev_id = str((resp or {}).get("id") or (prev.event_id if prev else ""))
            with Session(db.engine) as s5:
                row = s5.exec(select(models.EventSent).where(
                    models.EventSent.session_id == x.id)).first()
                if not row:
                    row = models.EventSent(session_id=x.id)
                row.year, row.month = year, month
                row.digest = digest
                row.event_id = ev_id
                row.organizer_uid = org_uid
                row.sent_at = datetime.now().isoformat(timespec="seconds")
                s5.add(row); s5.commit()
        except Exception as e:
            fails.append("%s【%s】%s" % (x.date, x.class_type_name, str(e)[:80]))

    # 已从课表移除的课程：删掉它的日程
    for sid, prev in (prev_map.items() if prune else []):
        if sid in keep_ids or not prev.event_id:
            continue
        try:
            _dt_delete_event(token, prev.organizer_uid, prev.event_id)
            deleted += 1
        except Exception:
            pass
        with Session(db.engine) as s6:
            row = s6.exec(select(models.EventSent).where(models.EventSent.session_id == sid)).first()
            if row:
                s6.delete(row); s6.commit()

    msg = "钉钉日程：新建 %d 节、更新 %d 节、未变跳过 %d 节" % (created, updated, skipped)
    if deleted:
        msg += "、删除 %d 节（课程已移除）" % deleted
    if no_phone:
        msg += "、%d 节因参与者未填手机号跳过" % no_phone
    if fails:
        msg += "；失败 %d 节：%s" % (len(fails), "；".join(fails[:5]) + ("…" if len(fails) > 5 else ""))
    return created, updated, skipped, deleted, msg


def _reminder_days(cfg) -> list:
    """解析提醒提前天数配置（逗号分隔），去重排序，范围 1~60。"""
    raw = (getattr(cfg, "reminder_days", "") or "").strip()
    days = set()
    for p in raw.replace("，", ",").split(","):
        p = p.strip()
        if not p:
            continue
        try:
            n = int(float(p))
        except ValueError:
            continue
        if 1 <= n <= 60:
            days.add(n)
    return sorted(days)


def check_and_send_reminders():
    """定时提醒：对已发布课程，在开始前 N 天的 08:00 给相关老师发钉钉待办（含课程名，未填报显示「自定义」）。
    以 reminder_sent 表按（课程×教师×提前天数）去重；只处理近 36 小时内到期的触发点，避免长时间未运行后补发刷屏。"""
    now = datetime.now()
    with Session(db.engine) as s:
        cfg = s.exec(select(models.DingTalkConfig)).first()
        if not cfg or not getattr(cfg, "todo_enabled", False) or not cfg.app_key or not cfg.app_secret:
            return 0, "未启用定时提醒或未配置企业应用"
        days = _reminder_days(cfg)
        if not days:
            return 0, "未配置提醒提前天数"
        token = _dt_access_token(cfg)
        base = (getattr(cfg, "todo_base_url", "") or "").strip().rstrip("/")
        rules = _load_rules(s)
        teachers = {t.id: t for t in s.exec(select(models.Teacher)).all()}
        sessions = s.exec(select(models.ClassSession).where(models.ClassSession.status == "published")).all()

        # 收集到期的（课程×教师×lead）触发点，聚合为 触发日 -> 教师 -> [课程明细]
        trig = {}  # (trigger_iso, tid) -> [(topic, date, time, cname, role, sid, lead)]
        for x in sessions:
            t_ = _course_time(x.class_type_name, x.month, rules) or "17:30"
            start = datetime.strptime(x.date + " " + t_, "%Y-%m-%d %H:%M")
            roles = [("讲课", x.teacher_id)]
            if x.main_reader_id in teachers:
                roles.append(("主阅", x.main_reader_id))
            roles.append(("评审", x.reviewer_id))
            if x.mentor_id:
                roles.append(("指导", x.mentor_id))
            for lead in days:
                trig_dt = (start - timedelta(days=lead)).replace(hour=8, minute=0)
                if not (now - timedelta(hours=36) <= trig_dt <= now):
                    continue
                for role, tid in roles:
                    trig.setdefault((trig_dt.date().isoformat(), tid), []).append(
                        (x.topic or "自定义", x.date, t_, x.class_type_name, role, x.id, lead))

        if not trig:
            return 0, "本轮无到期提醒"

        # 该教师已发送的（session×lead）记录
        sent = 0
        fail_msgs = []
        for (trig_iso, tid), items in sorted(trig.items()):
            tobj = teachers.get(tid)
            if not tobj:
                continue
            keys = {(it[5], it[6]) for it in items}
            done = {(r.session_id, r.lead_days) for r in
                    s.exec(select(models.ReminderSent).where(models.ReminderSent.teacher_id == tid)).all()}
            fresh = [it for it in items if (it[5], it[6]) not in done]
            if not fresh:
                # 已全部发过，补记录缺口后跳过
                continue
            tname = tobj.name or ("#%d" % tid)
            phone = (getattr(tobj, "phone", "") or "").strip()
            inv = (getattr(tobj, "invite_token", "") or "").strip()
            try:
                union_id = _dt_union_id(token, phone)
                lines = ["%s %s 【%s】%s · 课程名：%s" %
                         (d, t_, cname, role, topic)
                         for (topic, d, t_, cname, role, _, _) in sorted(fresh, key=lambda z: (z[1], z[2]))]
                subject = "教学提醒（提前%d天档）：%s 共 %d 项" % (
                    min(it[6] for it in fresh), trig_iso, len(fresh))
                body = {"subject": subject, "description": "\n".join(lines),
                        "dueDate": int(now.timestamp() * 1000) + 10 * 60000,
                        "executorIds": [union_id]}
                if base and inv:
                    body["detailUrl"] = {"url": base + "/#/topic/" + inv}
                _dt_create_todo_body(token, union_id, body)
                for it in fresh:
                    s.add(models.ReminderSent(session_id=it[5], teacher_id=tid,
                                              lead_days=it[6], sent_at=now.isoformat(timespec="seconds")))
                s.commit()
                sent += 1
            except Exception as e:
                fail_msgs.append("%s：%s" % (tname, str(e)[:80]))
        msg = "本轮已发送提醒：%d 位老师" % sent
        if fail_msgs:
            msg += "；失败：" + "；".join(fail_msgs)
        return sent, msg


def create_schedule_todos(cfg, year: int, month: int, sessions, teachers, students, rules):
    """发布课表后按参与者聚合创建钉钉待办。
    返回 (成功人数, 明细消息)。每位教师一条待办：subject=当月教学安排，dueDate=最早一节课开始时间。"""
    if not getattr(cfg, "todo_enabled", False) or not cfg.app_key or not cfg.app_secret:
        return 0, "未启用待办提醒或未配置企业应用"

    # 按教师聚合本月的课（学生无手机号，暂不创建）
    agg = {}  # tid -> [(date, time, ctype, role)]
    for x in sessions:
        t = _course_time(x.class_type_name, month, rules)
        if not t:
            t = "17:30"
        items = [(x.date, t, x.class_type_name, "讲课", x.teacher_id)]
        if x.main_reader_id in teachers:
            items.append((x.date, t, x.class_type_name, "主阅", x.main_reader_id))
        items.append((x.date, t, x.class_type_name, "评审", x.reviewer_id))
        if x.mentor_id:
            items.append((x.date, t, x.class_type_name, "指导", x.mentor_id))
        for (d, t_, cname, role, tid) in items:
            agg.setdefault(tid, []).append((d, t_, cname, role))

    token = _dt_access_token(cfg)
    base = (getattr(cfg, "todo_base_url", "") or "").strip().rstrip("/")
    ok, fails, skipped = 0, [], 0
    # 已创建记录：重复发布时内容没变就跳过，内容变了则删旧建新（避免老师手里堆重复待办）
    with Session(db.engine) as s0:
        prev_map = {r.teacher_id: r for r in s0.exec(
            select(models.TodoSent).where(models.TodoSent.year == year,
                                          models.TodoSent.month == month)).all()}
    for tid, lst in agg.items():
        tobj = teachers.get(tid)
        if not tobj:
            continue
        tname = tobj.name or ("#%d" % tid)
        phone = (getattr(tobj, "phone", "") or "").strip()
        if not _PHONE_RE.match(phone):
            fails.append("%s：未填有效手机号（11 位）" % tname)
            continue
        try:
            union_id = _dt_union_id(token, phone)
            lines = ["%s %s 【%s】%s" % (d, t_, cname, role) for (d, t_, cname, role) in sorted(lst)]
            due = min(_session_start_ms(d, t_) for (d, t_, _, _) in lst)
            subject = "%d年%d月教学安排（%d节）" % (year, month, len(lst))
            desc = "\n".join(lines)
            digest = hashlib.sha1((subject + "|" + desc).encode("utf-8")).hexdigest()[:16]
            prev = prev_map.get(tid)
            if prev and prev.digest == digest:
                skipped += 1
                continue
            body = {"subject": subject, "description": desc,
                    "dueDate": due, "executorIds": [union_id]}
            # 待办跳转：教师点开可直接填报讲课内容（需配置系统对外地址）
            inv = (getattr(tobj, "invite_token", "") or "").strip()
            if base and inv:
                body["detailUrl"] = {"url": base + "/#/topic/" + inv}
            if prev and prev.task_id:
                try:
                    _dt_delete_todo(token, union_id, prev.task_id)   # 安排有变更：删旧建新
                except Exception:
                    pass                                            # 删不掉也继续，保证新待办内容正确
            resp = _dt_create_todo_body(token, union_id, body)
            task_id = str((resp or {}).get("id") or "")
            with Session(db.engine) as s5:
                row = s5.exec(select(models.TodoSent).where(
                    models.TodoSent.year == year, models.TodoSent.month == month,
                    models.TodoSent.teacher_id == tid)).first()
                if not row:
                    row = models.TodoSent(year=year, month=month, teacher_id=tid)
                row.digest = digest
                row.task_id = task_id or (row.task_id or "")
                row.sent_at = datetime.now().isoformat(timespec="seconds")
                s5.add(row)
                s5.commit()
            ok += 1
        except Exception as e:
            fails.append("%s：%s" % (tname, str(e)[:100]))

    # —— 到期提醒待办：发布时一次性预推，钉钉到点自动提醒，无需系统后台常驻 ——
    # 每（课程×参与者×档位）一条：dueDate = 开课时间−N天的 08:00；课程名快照（未填报→"自定义"）
    lead_days = _reminder_days(cfg)
    rem_created, rem_skip = 0, 0
    if lead_days:
        with Session(db.engine) as s2:
            sids = [x.id for x in sessions]
            done = set()
            if sids:
                done = {(r.session_id, r.teacher_id, r.lead_days) for r in
                        s2.exec(select(models.ReminderSent).where(models.ReminderSent.session_id.in_(sids))).all()}
            now_ms = int(time.time() * 1000)
            pending = []  # (session, time, lead, role, tid, tobj, trig)
            seen = set(done)
            for x in sessions:
                t_ = _course_time(x.class_type_name, x.month, rules) or "17:30"
                start = datetime.strptime(x.date + " " + t_, "%Y-%m-%d %H:%M")
                parts = [("讲课", x.teacher_id), ("主阅", x.main_reader_id), ("评审", x.reviewer_id)]
                if x.mentor_id:
                    parts.append(("指导", x.mentor_id))
                for lead in lead_days:
                    trig = (start - timedelta(days=lead)).replace(hour=8, minute=0)
                    if trig.timestamp() * 1000 < now_ms:      # 提醒时点已过则不推
                        continue
                    for role, tid in parts:
                        tobj = teachers.get(tid)
                        if not tobj or (x.id, tid, lead) in seen:
                            continue
                        seen.add((x.id, tid, lead))
                        pending.append((x, t_, lead, role, tid, tobj, trig))
            for (x, t_, lead, role, tid, tobj, trig) in pending:
                phone = (getattr(tobj, "phone", "") or "").strip()
                inv = (getattr(tobj, "invite_token", "") or "").strip()
                if not _PHONE_RE.match(phone):
                    continue
                try:
                    union_id = _dt_union_id(token, phone)
                    topic = x.topic or "自定义"
                    body = {"subject": "教学提醒（提前%d天）：%s %s【%s】" % (lead, x.date, t_, x.class_type_name),
                            "description": "您的角色：%s\n课程名：%s\n如需修改课程名，请点下方链接填报。"
                                           % (role, topic),
                            "dueDate": int(trig.timestamp() * 1000),
                            "executorIds": [union_id]}
                    if base and inv:
                        body["detailUrl"] = {"url": base + "/#/topic/" + inv}
                    _dt_create_todo_body(token, union_id, body)
                    s2.add(models.ReminderSent(session_id=x.id, teacher_id=tid, lead_days=lead,
                                               sent_at=datetime.now().isoformat(timespec="seconds")))
                    rem_created += 1
                except Exception as e:
                    rem_skip += 1
                    fails.append("提醒待办 %s %s【%s】%s：%s" % (x.date, t_, x.class_type_name, role, str(e)[:60]))
            if rem_created or rem_skip:
                s2.commit()

    if skipped:
        msg = "钉钉待办：新建/更新 %d 人安排待办，%d 人安排未变已跳过；预推到期提醒 %d 条（到点由钉钉自动提醒）" % (ok, skipped, rem_created)
    else:
        msg = "钉钉待办：已为 %d 人创建安排待办；预推到期提醒 %d 条（到点由钉钉自动提醒）" % (ok, rem_created)
    if rem_skip:
        msg += "；提醒失败 %d 条" % rem_skip
    if fails:
        msg += "；失败：%s" % ("；".join(fails[:8]) + ("…" if len(fails) > 8 else ""))
    return ok, msg


# ---------- HOLIDAYS ----------
@router.get("/holidays")
def list_holidays(_=Depends(auth.get_current_user)):
    with Session(db.engine) as s:
        return [h.dict() for h in s.exec(select(models.Holiday).order_by(models.Holiday.date)).all()]


@router.post("/holidays")
def add_holiday(h: models.Holiday, _=Depends(auth.staff_only)):
    with Session(db.engine) as s:
        s.add(h); s.commit(); s.refresh(h); return h.dict()


@router.delete("/holidays/{hid}")
def del_holiday(hid: int, _=Depends(admin_only)):
    with Session(db.engine) as s:
        d = s.get(models.Holiday, hid)
        if d:
            s.delete(d); s.commit()
        return {"ok": True}


# ---------- 排课规则 ----------
def _load_saved_rules(s):
    row = s.exec(select(models.RuleConfig).where(models.RuleConfig.key == "rules")).first()
    if not row or not row.value_json:
        return {}
    try:
        return json.loads(row.value_json) or {}
    except Exception:
        return {}


def _load_rules(s):
    return rules_mod.merge_rules(_load_saved_rules(s))


@router.get("/rules")
def get_rules(_=Depends(auth.admin_only)):
    with Session(db.engine) as s:
        saved = _load_saved_rules(s)
        return {"rules": rules_mod.merge_rules(saved),
                "defaults": rules_mod.DEFAULT_RULES,
                "customized": saved}


@router.put("/rules")
def save_rules(payload: dict, _=Depends(auth.admin_only)):
    incoming = payload.get("rules") if isinstance(payload, dict) and "rules" in payload else payload
    with Session(db.engine) as s:
        names = [c.name for c in s.exec(select(models.ClassType)).all()]
        errs = rules_mod.validate(incoming, class_type_names=names)
        if errs:
            raise HTTPException(status_code=400, detail="；".join(errs))
        merged = rules_mod.merge_rules(incoming)
        stored = rules_mod.diff_rules(merged)
        row = s.exec(select(models.RuleConfig).where(models.RuleConfig.key == "rules")).first()
        if row:
            row.value_json = json.dumps(stored, ensure_ascii=False)
            row.updated_at = datetime.now().isoformat(timespec="seconds")
            s.add(row)
        else:
            s.add(models.RuleConfig(key="rules", value_json=json.dumps(stored, ensure_ascii=False),
                                    updated_at=datetime.now().isoformat(timespec="seconds")))
        s.commit()
        return {"ok": True, "rules": merged, "customized": stored}


@router.post("/rules/reset")
def reset_rules(_=Depends(auth.admin_only)):
    with Session(db.engine) as s:
        row = s.exec(select(models.RuleConfig).where(models.RuleConfig.key == "rules")).first()
        if row:
            s.delete(row)
            s.commit()
        return {"ok": True, "rules": rules_mod.DEFAULT_RULES, "customized": {}}


# ---------- 月度反馈（出科考核反馈 / 月考反馈 的汇报人） ----------
FEEDBACK_KINDS = ("出科考核反馈", "月考反馈")


def _feedback_rows(s, year: int, month: int):
    """返回该月两条反馈行的统一结构（未录入的返回空汇报人）。"""
    teachers = {t.id: t.name for t in s.exec(select(models.Teacher)).all()}
    students = {t.id: t.name for t in s.exec(select(models.Student)).all()}
    saved = {f.kind: f for f in s.exec(select(models.MonthlyFeedback)
                                      .where(models.MonthlyFeedback.year == year,
                                             models.MonthlyFeedback.month == month)).all()}
    items = []
    for kind in FEEDBACK_KINDS:
        f = saved.get(kind)
        rtype = (f.reporter_type if f else "") or ""
        rid = (f.reporter_id if f else 0) or 0
        if rtype == "teacher":
            name = teachers.get(rid, "")
        elif rtype == "student":
            name = students.get(rid, "")
        else:
            name = ""
        items.append({
            "id": f.id if f else 0,
            "year": year, "month": month, "kind": kind,
            "reporter_type": rtype, "reporter_id": rid,
            "reporter": name,
            "note": (f.note if f else "") or "",
        })
    return items, teachers, students


@router.get("/feedback")
def get_feedback(year: int, month: int, _=Depends(auth.get_current_user)):
    with Session(db.engine) as s:
        items, teachers, students = _feedback_rows(s, year, month)
        return {"items": items, "teachers": teachers, "students": students}


@router.put("/feedback")
def save_feedback(payload: dict, _=Depends(auth.get_current_user)):
    year = int(payload.get("year", 0)); month = int(payload.get("month", 0))
    if not year or not month:
        raise HTTPException(status_code=400, detail="请指定年份和月份")
    items = payload.get("items") or []
    with Session(db.engine) as s:
        existing = {f.kind: f for f in s.exec(select(models.MonthlyFeedback)
                                             .where(models.MonthlyFeedback.year == year,
                                                    models.MonthlyFeedback.month == month)).all()}
        for it in items:
            kind = it.get("kind") or ""
            if kind not in FEEDBACK_KINDS:
                continue
            rtype = it.get("reporter_type") or ""
            rid = int(it.get("reporter_id") or 0)
            note = it.get("note") or ""
            # 汇报人身份与反馈类型绑定：出科考核反馈=老师，月考反馈=学生；
            # 前端下拉已按类型过滤，这里兜底纠正历史误存/绕过前端的提交
            want = "teacher" if kind == "出科考核反馈" else "student"
            if rtype and rtype != want:
                rtype, rid = "", 0   # 类型不符 → 视为未指定，避免名字查不出来
            if rtype:
                pool = models.Teacher if rtype == "teacher" else models.Student
                if not s.get(pool, rid):
                    rtype, rid = "", 0
            if kind in existing:
                row = existing[kind]
                row.reporter_type, row.reporter_id, row.note = rtype, rid, note
                s.add(row)
            else:
                s.add(models.MonthlyFeedback(year=year, month=month, kind=kind,
                                             reporter_type=rtype, reporter_id=rid, note=note))
        s.commit()
        items_out, _, _ = _feedback_rows(s, year, month)
        return {"ok": True, "items": items_out}


# ---------- DINGTALK ----------
_MASK = "••••"


def _mask_tail(v: str) -> str:
    """脱敏显示：只保留末 4 位。"""
    if not v:
        return ""
    return _MASK + (v[-4:] if len(v) > 6 else "")


def _mask_webhook(url: str) -> str:
    """只掩码 access_token 部分，保留 URL 前缀便于核对格式。"""
    if not url or _MASK in url:
        return url or ""
    m = _re.match(r"^(https://oapi\.dingtalk\.com/robot/send\?access_token=)(.+)$", url)
    if not m:
        return _mask_tail(url)
    return m.group(1) + _mask_tail(m.group(2))


@router.get("/dingtalk/config")
def get_dt(_=Depends(auth.admin_only)):
    """仅管理员可读；密钥字段脱敏返回（掩码值保存时自动还原）。"""
    with Session(db.engine) as s:
        cfg = s.exec(select(models.DingTalkConfig)).first()
        if not cfg:
            return {}
        d = cfg.dict()
        d["webhook_url"] = _mask_webhook(d.get("webhook_url") or "")
        d["secret"] = _mask_tail(d.get("secret") or "")
        d["app_secret"] = _mask_tail(d.get("app_secret") or "")
        return d


_DT_URL_RE = _re.compile(r"^https://oapi\.dingtalk\.com/robot/send\?access_token=[0-9a-zA-Z]{16,}$")


@router.put("/dingtalk/config")
def upd_dt(cfg: models.DingTalkConfig, _=Depends(auth.admin_only)):
    # 掩码值 = 未修改，还原为库中原值后再校验
    with Session(db.engine) as s:
        old = s.exec(select(models.DingTalkConfig)).first()
        for field in ("webhook_url", "secret", "app_secret"):
            if getattr(cfg, field, None) and _MASK in getattr(cfg, field):
                setattr(cfg, field, (getattr(old, field, "") or "") if old else "")
    cfg.webhook_url = (cfg.webhook_url or "").strip()
    if cfg.webhook_url and not _DT_URL_RE.match(cfg.webhook_url):
        hint = "Webhook 地址格式不正确。应为 https://oapi.dingtalk.com/robot/send?access_token=xxxx 的完整形式（access_token 为字母数字串）。"
        if any(ord(ch) > 127 for ch in cfg.webhook_url):
            bad = "".join(sorted({ch for ch in cfg.webhook_url if ord(ch) > 127}))
            hint += " 当前地址中混入了中文/全角字符「%s」，请到钉钉群机器人设置里重新完整复制。" % bad
        elif " " in cfg.webhook_url:
            hint += " 地址中含有空格，请重新复制。"
        raise HTTPException(400, hint)
    if cfg.secret:
        cfg.secret = cfg.secret.strip()
        if any(ord(ch) > 127 for ch in cfg.secret):
            raise HTTPException(400, "加签密钥中混入了非 ASCII 字符，请到钉钉群机器人设置里重新复制 SEC 密钥。")
    cfg.keywords = (getattr(cfg, "keywords", "") or "").strip()
    cfg.todo_lead_days = max(0, min(int(getattr(cfg, "todo_lead_days", 0) or 0), 30))
    cfg.reminder_days = ",".join(str(d) for d in _reminder_days(cfg))
    cfg.event_duration = max(15, min(int(getattr(cfg, "event_duration", 60) or 60), 240))
    cfg.event_reminder_min = max(0, min(int(getattr(cfg, "event_reminder_min", 15) or 0), 120))
    with Session(db.engine) as s:
        d = s.exec(select(models.DingTalkConfig)).first()
        if not d:
            s.add(cfg); s.commit(); s.refresh(cfg); d = cfg
        else:
            for k, v in cfg.dict(exclude={"id"}).items():
                setattr(d, k, v)
            s.add(d); s.commit(); s.refresh(d)
        out = d.dict()
        out["webhook_url"] = _mask_webhook(out.get("webhook_url") or "")
        out["secret"] = _mask_tail(out.get("secret") or "")
        out["app_secret"] = _mask_tail(out.get("app_secret") or "")
        return out


@router.get("/dingtalk/webhooks")
def get_dts(_=Depends(auth.admin_only)):
    """其他钉钉群机器人列表（密钥脱敏返回）。"""
    with Session(db.engine) as s:
        rows = s.exec(select(models.DingTalkWebhook).order_by(models.DingTalkWebhook.id)).all()
        return [{"id": r.id, "name": r.name,
                 "webhook_url": _mask_webhook(r.webhook_url or ""),
                 "secret": _mask_tail(r.secret or ""),
                 "keywords": r.keywords, "enabled": r.enabled} for r in rows]


@router.put("/dingtalk/webhooks")
def put_dts(payload: dict, _=Depends(auth.admin_only)):
    """整体保存其他群列表：有 id 的行按 id 更新，没有的行新增，未出现的行删除。
    掩码值 = 未修改，还原为库中原值后再校验。"""
    items = payload.get("webhooks") or []
    with Session(db.engine) as s:
        old = {r.id: r for r in s.exec(select(models.DingTalkWebhook)).all()}
        keep_ids = set()
        seq = len(old)
        for it in items:
            it_id = it.get("id") or None
            url = (it.get("webhook_url") or "").strip()
            secret = (it.get("secret") or "").strip()
            name = (it.get("name") or "").strip()
            # 新增且名称/Webhook/加签全空的行 = 没填写的空白卡片，直接忽略不报错
            if not it_id and not name and not url and not secret:
                continue
            # 掩码还原（与主配置同规则）
            if url and _MASK in url:
                o = old.get(it_id)
                url = (o.webhook_url if o else "")
            if secret and _MASK in secret:
                o = old.get(it_id)
                secret = (o.secret if o else "")
            if not name:
                # 只填了 Webhook 没填群名：自动命名为「其他群N」，不再拦截保存
                seq += 1
                name = "其他群%d" % seq
            if not url:
                raise HTTPException(400, "群「%s」的 Webhook 地址为空。若想放弃该群请点「删除该群」后再保存。" % name)
            if not _DT_URL_RE.match(url):
                raise HTTPException(
                    400, "群「%s」的 Webhook 地址格式不正确。应为 "
                    "https://oapi.dingtalk.com/robot/send?access_token=xxxx 的完整形式，"
                    "请到钉钉群机器人设置里重新完整复制。" % name)
            row = old.get(it_id) or models.DingTalkWebhook()
            row.name = name
            row.webhook_url = url
            row.secret = secret
            row.keywords = (it.get("keywords") or "").strip() or "课程表"
            row.enabled = bool(it.get("enabled"))
            s.add(row); s.flush()
            keep_ids.add(row.id)
        for rid in set(old) - keep_ids:
            s.delete(old[rid])
        s.commit()
        rows = s.exec(select(models.DingTalkWebhook).order_by(models.DingTalkWebhook.id)).all()
        return {"ok": True, "count": len(rows),
                "items": [{"id": r.id, "name": r.name,
                           "webhook_url": _mask_webhook(r.webhook_url or ""),
                           "secret": _mask_tail(r.secret or ""),
                           "keywords": r.keywords, "enabled": r.enabled} for r in rows]}


@router.post("/reminders/check")
def reminders_check(_=Depends(auth.admin_only)):
    """手动触发一轮定时提醒检查（后台任务每 20 分钟自动执行，此接口用于验证）。"""
    n, msg = check_and_send_reminders()
    return {"ok": True, "sent": n, "note": msg}


@router.post("/calendar/sync")
def calendar_sync(payload: dict, _=Depends(auth.admin_only)):
    """手动把某月课表同步为钉钉日程（发布时会自动执行，这里用于单独补推/验证）。"""
    year = int(payload["year"]); month = int(payload["month"])
    with Session(db.engine) as s:
        cfg = s.exec(select(models.DingTalkConfig)).first()
        sessions = s.exec(select(models.ClassSession)
                          .where(models.ClassSession.year == year,
                                 models.ClassSession.month == month)).all()
        if not sessions:
            raise HTTPException(400, "该月还没有课表")
        teachers = {t.id: t for t in s.exec(select(models.Teacher)).all()}
        students = {t.id: t.name for t in s.exec(select(models.Student)).all()}
        a, b, c, d, msg = sync_calendar_events(cfg, year, month, sessions, teachers, students, _load_rules(s))
    return {"ok": True, "created": a, "updated": b, "skipped": c, "deleted": d, "note": msg}


@router.get("/reminders/log")
def reminders_log(_=Depends(auth.admin_only)):
    """最近提醒发送记录。"""
    with Session(db.engine) as s:
        rows = s.exec(select(models.ReminderSent).order_by(models.ReminderSent.id.desc()).limit(50)).all()
        return [{"session_id": r.session_id, "teacher_id": r.teacher_id,
                 "lead_days": r.lead_days, "sent_at": r.sent_at} for r in rows]


@router.post("/dingtalk/test")
def test_dt(_=Depends(auth.admin_only)):
    """发送一条测试消息，验证 Webhook / 加签 / 关键词；若配置了企业应用则顺带验证待办接口。
    除主群外，还会发到「其他钉钉群」里所有启用的群，逐一返回结果。"""
    with Session(db.engine) as s:
        cfg = s.exec(select(models.DingTalkConfig)).first()
        if not cfg or not cfg.webhook_url:
            return {"ok": False, "note": "未配置 Webhook 地址"}
        if not cfg.enabled:
            return {"ok": False, "note": "推送开关未启用"}
        text = "### 教学排课系统 · 测试消息\n- 这是一条来自排课系统的测试推送，收到即表示 Webhook 配置成功。"
        note = send_dingtalk(cfg, text)
        ok = note.startswith("钉钉推送成功")
        # 其他群逐个发送并汇总
        extras = push_extra_groups(s, text)
        extra_note = ""
        if extras:
            parts = ["「%s」%s" % (n, t) for n, t in extras]
            extra_note = "；其他群：" + "；".join(parts)
            ok = ok and all(t.startswith("钉钉推送成功") for _n, t in extras)
            note += extra_note
        # 待办链路自检：企业应用凭据 + 手机号 → unionId
        if ok and cfg.todo_enabled and cfg.app_key and cfg.app_secret:
            try:
                token = _dt_access_token(cfg)
                has_phone = any(_PHONE_RE.match((t.phone or "").strip())
                                for t in s.exec(select(models.Teacher)).all())
                if has_phone:
                    mobile = next((t.phone.strip() for t in s.exec(select(models.Teacher)).all()
                                   if _PHONE_RE.match((t.phone or "").strip())))
                    _dt_union_id(token, mobile)
                    note += "；待办链路连通（已按手机号匹配到钉钉用户）"
                else:
                    note += "；待办链路：尚未填写任何教师手机号，发布时将无法创建提醒"
            except Exception as e:
                note += "；待办链路异常：" + str(e)[:100]
                ok = False
        return {"ok": ok, "note": note}


# ---------- 单聊待填清单推送（P0：老师回复即填报） ----------

@router.post("/dingtalk/dm/notify")
def dt_dm_notify(payload: dict = None, _=Depends(auth.admin_only)):
    """把每位老师自己的待填课单聊推给他，老师直接回复即可填报。

    一节课只会推给一个"责任人"（讲课优先，讲课没手机号才顺延给本课其他角色），
    所以不会出现同一节课被两位老师各推一遍、然后互相覆盖课程名的情况。
    """
    payload = payload or {}
    year = int(payload.get("year") or 0)
    month = int(payload.get("month") or 0)
    only_owner = payload.get("only_owner", True)
    with Session(db.engine) as s:
        cfg = s.exec(select(models.DingTalkConfig)).first()
        if not cfg or not cfg.app_key or not cfg.app_secret:
            return {"ok": False, "note": "未配置企业应用 AppKey / AppSecret"}
        teachers = {t.id: t for t in s.exec(select(models.Teacher)).all()}
        rules = _load_rules(s)
        q = select(models.ClassSession).where(models.ClassSession.status == "published")
        if year:
            q = q.where(models.ClassSession.year == year)
        if month:
            q = q.where(models.ClassSession.month == month)
        rows = [x for x in s.exec(q.order_by(models.ClassSession.date,
                                             models.ClassSession.slot)).all()
                if not (x.topic or "").strip()]

        buckets = {}      # tid -> [(session, 角色, 代理)]
        no_phone = []     # 无人可推的课
        for x in rows:
            t, role, proxy = session_owner(x, teachers)
            if not t:
                no_phone.append({"date": x.date, "class_type": x.class_type_name,
                                 "reason": "本课所有老师都没登记手机号" if session_roles(x) else "本课未安排老师"})
                continue
            buckets.setdefault(t.id, []).append((x, role, proxy))

        if not buckets:
            return {"ok": False, "sent": 0, "skipped": no_phone,
                    "note": "没有可推送的对象：待填课程的相关老师都还没登记手机号"}

        try:
            token = _dt_access_token(cfg)
        except Exception as e:
            return {"ok": False, "note": "获取 access_token 失败：" + str(e)[:150]}

        sent, failed = [], []
        for tid, items in buckets.items():
            t = teachers[tid]
            mobile = (t.phone or "").strip()
            if not _PHONE_RE.match(mobile):
                no_phone.append({"teacher": t.name, "reason": "未登记手机号",
                                 "courses": len(items)})
                continue
            try:
                uid = _dt_userid(token, mobile)
                text = dt_dm_brief(s, t, items, rules, year, month)
                _dt_send_dm(cfg, token, uid, text)
                sent.append({"teacher": t.name, "role": items[0][1],
                             "proxy": any(p for _, _, p in items),
                             "courses": [{"date": x.date, "class_type": x.class_type_name}
                                         for x, _, _ in items]})
            except Exception as e:
                failed.append({"teacher": t.name, "error": str(e)[:150]})

        note = "已推送 %d 人" % len(sent)
        if failed:
            note += "，失败 %d 人" % len(failed)
        if no_phone:
            note += "，%d 项因无手机号跳过" % len(no_phone)
        return {"ok": not failed, "sent": len(sent), "note": note,
                "details": sent, "failed": failed, "skipped": no_phone}


# ---------- 课程内容填报（教师经钉钉待办链接，免登录 token 模式） ----------

@router.get("/my-course/{token}")
def my_course(token: str):
    """教师用个人填报令牌查看与自己相关的已发布课表（讲课内容待填）。"""
    with Session(db.engine) as s:
        t = s.exec(select(models.Teacher).where(models.Teacher.invite_token == token)).first()
        if not t:
            raise HTTPException(404, "链接无效或已重置，请联系教务重新获取")
        items = []
        for x in s.exec(select(models.ClassSession)
                        .where(models.ClassSession.status == "published")
                        .order_by(models.ClassSession.date, models.ClassSession.slot)).all():
            role, editable = "", False
            if x.teacher_id == t.id:
                role, editable = "讲课", True
            elif x.main_reader_id == t.id:
                role, editable = "主阅", True
            elif x.reviewer_id == t.id:
                role = "评审"
            elif x.mentor_id == t.id:
                role = "指导"
            else:
                continue
            items.append({"id": x.id, "date": x.date, "slot": x.slot,
                          "class_type_name": x.class_type_name, "role": role,
                          "topic": x.topic or "", "editable": editable})
        return {"name": t.name, "items": items}


@router.post("/my-course")
def save_my_course(payload: dict):
    """教师提交讲课内容/题目（仅讲课老师本人可改所在课的题目）。"""
    token = str(payload.get("token") or "").strip()
    items = payload.get("items") or []
    with Session(db.engine) as s:
        t = s.exec(select(models.Teacher).where(models.Teacher.invite_token == token)).first()
        if not t:
            raise HTTPException(404, "链接无效或已重置，请联系教务重新获取")
        saved = 0
        for it in items:
            x = s.get(models.ClassSession, int(it.get("id", 0)))
            if not x:
                continue
            if x.teacher_id != t.id:
                continue  # 题目只允许讲课老师本人填写
            x.topic = str(it.get("topic") or "").strip()[:100]
            s.add(x)
            saved += 1
        s.commit()
        return {"ok": True, "saved": saved}


# ---------- DASHBOARD ----------
@router.get("/dashboard")
def dashboard(_=Depends(auth.get_current_user)):
    with Session(db.engine) as s:
        tc = len(s.exec(select(models.Teacher)).all())
        sc = len(s.exec(select(models.Student)).all())
        now = datetime.now()
        sess = s.exec(select(models.ClassSession)
                      .where(models.ClassSession.year == now.year,
                             models.ClassSession.month == now.month)).all()
        draft = sum(1 for x in sess if x.status == "draft")
        published = sum(1 for x in sess if x.status == "published")
        return {"teachers": tc, "students": sc, "sessions_total": len(sess),
                "draft": draft, "published": published}


# ---------- USERS ----------
_ROLES = ("admin", "jiaowu", "teacher", "student")


def _admin_count(s) -> int:
    return sum(1 for u in s.exec(select(models.User)).all()
               if u.role == "admin" and u.is_active)


@router.get("/users")
def list_users(_=Depends(admin_only)):
    with Session(db.engine) as s:
        tids = {t.emp_id for t in s.exec(select(models.Teacher)).all()}
        sids = {t.stu_id for t in s.exec(select(models.Student)).all()}
        out = []
        for u in s.exec(select(models.User)).all():
            src = ""
            if u.username in tids:
                src = "教师工号"
            elif u.username in sids:
                src = "学生学号"
            out.append({"id": u.id, "username": u.username, "name": u.name,
                        "role": u.role, "is_active": u.is_active, "src": src})
        return out


@router.post("/users")
def add_user(payload: dict, user: models.User = Depends(admin_only)):
    username = (payload.get("username") or "").strip()
    name = (payload.get("name") or "").strip()
    role = payload.get("role") or "teacher"
    password = (payload.get("password") or "").strip() or DEFAULT_PASSWORD
    if role not in _ROLES:
        raise HTTPException(400, "角色不合法")
    if not username:
        raise HTTPException(400, "用户名不能为空")
    with Session(db.engine) as s:
        if s.exec(select(models.User).where(models.User.username == username)).first():
            raise HTTPException(400, "用户名 %s 已存在" % username)
        u = models.User(username=username, name=name or username, role=role,
                        password_hash=auth.hash_password(password))
        s.add(u); s.commit(); s.refresh(u)
        return {"id": u.id, "username": u.username, "name": u.name, "role": u.role}


@router.put("/users/{uid}")
def upd_user(uid: int, payload: dict, user: models.User = Depends(admin_only)):
    with Session(db.engine) as s:
        u = s.get(models.User, uid)
        if not u:
            raise HTTPException(404, "账号不存在")
        if "name" in payload:
            u.name = (payload.get("name") or "").strip()
        if "role" in payload:
            role = payload["role"]
            if role not in _ROLES:
                raise HTTPException(400, "角色不合法")
            if u.id == user.id and role != "admin":
                raise HTTPException(400, "不能修改自己的角色")
            if u.role == "admin" and role != "admin" and _admin_count(s) <= 1:
                raise HTTPException(400, "系统至少保留一名管理员")
            u.role = role
        if "is_active" in payload:
            if u.id == user.id and not payload["is_active"]:
                raise HTTPException(400, "不能停用自己的账号")
            if u.role == "admin" and not payload["is_active"] and _admin_count(s) <= 1:
                raise HTTPException(400, "系统至少保留一名启用的管理员")
            u.is_active = bool(payload["is_active"])
        if (payload.get("password") or "").strip():
            u.password_hash = auth.hash_password(payload["password"].strip())
        s.add(u); s.commit()
        return {"ok": True}


@router.delete("/users/{uid}")
def del_user(uid: int, user: models.User = Depends(admin_only)):
    with Session(db.engine) as s:
        u = s.get(models.User, uid)
        if not u:
            raise HTTPException(404, "账号不存在")
        if u.id == user.id:
            raise HTTPException(400, "不能删除自己的账号")
        if u.role == "admin" and _admin_count(s) <= 1:
            raise HTTPException(400, "系统至少保留一名管理员")
        s.delete(u); s.commit()
        return {"ok": True}


@router.post("/users/sync")
def sync_users(_=Depends(admin_only)):
    """为所有教师/学生补建登录账号（工号/学号即用户名，初始密码 123456）。幂等，已存在的跳过。"""
    created = []
    with Session(db.engine) as s:
        for t in s.exec(select(models.Teacher)).all():
            if _ensure_user(s, t.emp_id, t.name, "teacher"):
                created.append(t.emp_id)
        for t in s.exec(select(models.Student)).all():
            if _ensure_user(s, t.stu_id, t.name, "student"):
                created.append(t.stu_id)
        s.commit()
    return {"ok": True, "created": len(created), "usernames": created}


# ---------- 技能考核考务 ----------
import exam as exam_mod
router.include_router(exam_mod.router, prefix="/exam")
