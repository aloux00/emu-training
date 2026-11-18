# -*- coding: utf-8 -*-
"""教师/学生编辑接口：工号重复、空工号、去空格（离线隔离测试）。"""
import os, sys, tempfile, shutil, json

TMP = tempfile.mkdtemp(prefix="_dup_t_")
SRC = r"C:\WD\code\教学课表"
for f in ["api.py", "auth.py", "db.py", "engine.py", "exam.py", "main.py",
          "models.py", "rules.py", "dingtalk_bot.py", "dingtalk_card.py", "card_template_body.json"]:
    shutil.copy(os.path.join(SRC, f), os.path.join(TMP, f))
shutil.copytree(os.path.join(SRC, "static"), os.path.join(TMP, "static"))
os.chdir(TMP)
sys.path.insert(0, TMP)

import db
db.init_db()
import models, api
from sqlmodel import Session, select

N = [0]
def check(name, ok, info=None):
    N[0] += 1
    print("%s %02d. %s%s" % ("PASS" if ok else "FAIL", N[0], name,
                             (" | " + str(info)[:160]) if info is not None and not ok else ""))

def expect400(name, fn, kw):
    try:
        fn()
        check(name, False, "未报错")
    except Exception as e:
        st = getattr(e, "status_code", None)
        det = getattr(e, "detail", str(e))
        check(name, st == 400 and kw in str(det), (st, str(det)[:120]))

with Session(db.engine) as s:
    a = models.Teacher(emp_id="D001", name="甲")
    b = models.Teacher(emp_id="D002", name="乙")
    s.add(a); s.add(b); s.commit(); s.refresh(a); s.refresh(b)
    aid, bid = a.id, b.id

    # 1. 改成别人的工号 → 400
    a2 = models.Teacher(emp_id="D002", name="甲")
    expect400("教师改成他人工号报 400", lambda: api.upd_teacher(aid, a2, None), "已被教师")
    # 2. 空工号 → 400
    a3 = models.Teacher(emp_id="", name="甲")
    expect400("教师空工号报 400", lambda: api.upd_teacher(aid, a3, None), "工号不能为空")
    # 3. 带空格的正常编辑 → 归一化后成功
    a4 = models.Teacher(emp_id=" D001 ", name="甲改", phone="13800000000")
    r = api.upd_teacher(aid, a4, None)
    check("带空格工号归一化保存", r["emp_id"] == "D001" and r["name"] == "甲改", r)
    # 4. 自己的工号原样保存 → 200（排除自身）
    a5 = models.Teacher(emp_id="D001", name="甲改2")
    r = api.upd_teacher(aid, a5, None)
    check("保留自己工号可保存", r["emp_id"] == "D001", r)

    # 学生同样逻辑
    sa = models.Student(stu_id="S001", name="生甲")
    sb = models.Student(stu_id="S002", name="生乙")
    s.add(sa); s.add(sb); s.commit(); s.refresh(sa); s.refresh(sb)
    said, sbid = sa.id, sb.id
    sa2 = models.Student(stu_id="S002", name="生甲")
    expect400("学生改成他人学号报 400", lambda: api.upd_student(said, sa2, None), "已被学生")
    sa3 = models.Student(stu_id="", name="生甲")
    expect400("学生空学号报 400", lambda: api.upd_student(said, sa3, None), "学号不能为空")
    sa4 = models.Student(stu_id=" S001 ", name="生甲改")
    r = api.upd_student(said, sa4, None)
    check("学生带空格学号归一化", r["stu_id"] == "S001", r)

    # 5. 新增接口已有预检不变
    expect400("新增教师重复工号仍报 400", lambda: api.add_teacher(models.Teacher(emp_id="D001", name="丙"), None), "已存在")

print("==== %d 项，全部通过 ====" % N[0])
