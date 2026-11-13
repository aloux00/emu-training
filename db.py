from sqlmodel import SQLModel, create_engine, Session, select
import models
import auth
import uuid

DATABASE_URL = "sqlite:///./schedule.db"
engine = create_engine(DATABASE_URL, echo=False)


def init_db():
    SQLModel.metadata.create_all(engine)
    _migrate()
    # 迁移中若有表被重建(如 rotationstate 结构变更)，这里补建缺失的表
    SQLModel.metadata.create_all(engine)
    seed_if_empty()


def _migrate():
    """为已存在的库平滑迁移：增字段 / 重建已变更结构的表，避免删库。"""
    import sqlite3
    try:
        with sqlite3.connect("schedule.db") as conn:
            cur = conn.cursor()
            # ClassSession：补 mentor_id / week_index
            cur.execute("PRAGMA table_info(classsession)")
            cols = [r[1] for r in cur.fetchall()]
            if "mentor_id" not in cols:
                cur.execute("ALTER TABLE classsession ADD COLUMN mentor_id INTEGER NOT NULL DEFAULT 0")
            if "week_index" not in cols:
                cur.execute("ALTER TABLE classsession ADD COLUMN week_index INTEGER NOT NULL DEFAULT 0")
            if "topic_by" not in cols:
                cur.execute("ALTER TABLE classsession ADD COLUMN topic_by INTEGER NOT NULL DEFAULT 0")
            if "topic_at" not in cols:
                cur.execute("ALTER TABLE classsession ADD COLUMN topic_at VARCHAR NOT NULL DEFAULT ''")
            if "presenter_id" not in cols:
                cur.execute("ALTER TABLE classsession ADD COLUMN presenter_id INTEGER NOT NULL DEFAULT 0")
            # Teacher：补「可指导」标记与「别名」
            cur.execute("PRAGMA table_info(teacher)")
            tcols = [r[1] for r in cur.fetchall()]
            if "can_mentor" not in tcols:
                cur.execute("ALTER TABLE teacher ADD COLUMN can_mentor BOOLEAN NOT NULL DEFAULT 1")
            if "aliases" not in tcols:
                cur.execute("ALTER TABLE teacher ADD COLUMN aliases VARCHAR NOT NULL DEFAULT ''")
            # RotationState：旧结构(role,class_type_name,last_id) 与新结构(role,ptr,covered_json)不兼容 → 重建
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rotationstate'")
            if cur.fetchone():
                cur.execute("PRAGMA table_info(rotationstate)")
                rcols = [r[1] for r in cur.fetchall()]
                if "class_type_name" in rcols:
                    cur.execute("DROP TABLE rotationstate")
            # 老库移除已并入「生成课表」页的独立「月度反馈」菜单
            cur.execute("DELETE FROM menu WHERE path='#/feedback'")
            # DingTalkConfig：补 keywords 字段（机器人安全设置的关键词）
            cur.execute("PRAGMA table_info(dingtalkconfig)")
            dcols = [r[1] for r in cur.fetchall()]
            if "keywords" not in dcols:
                cur.execute("ALTER TABLE dingtalkconfig ADD COLUMN keywords VARCHAR NOT NULL DEFAULT '课程表'")
            for col, default in (("app_key", "''"), ("app_secret", "''")):
                if col not in dcols:
                    cur.execute("ALTER TABLE dingtalkconfig ADD COLUMN %s VARCHAR NOT NULL DEFAULT %s" % (col, default))
            if "todo_enabled" not in dcols:
                cur.execute("ALTER TABLE dingtalkconfig ADD COLUMN todo_enabled INTEGER NOT NULL DEFAULT 0")
            if "todo_lead_days" not in dcols:
                cur.execute("ALTER TABLE dingtalkconfig ADD COLUMN todo_lead_days INTEGER NOT NULL DEFAULT 1")
            if "reminder_days" not in dcols:
                cur.execute("ALTER TABLE dingtalkconfig ADD COLUMN reminder_days VARCHAR NOT NULL DEFAULT '10,7,3'")
            if "bot_enabled" not in dcols:
                cur.execute("ALTER TABLE dingtalkconfig ADD COLUMN bot_enabled INTEGER NOT NULL DEFAULT 0")
            if "todo_base_url" not in dcols:
                cur.execute("ALTER TABLE dingtalkconfig ADD COLUMN todo_base_url VARCHAR NOT NULL DEFAULT ''")
            if "calendar_enabled" not in dcols:
                cur.execute("ALTER TABLE dingtalkconfig ADD COLUMN calendar_enabled INTEGER NOT NULL DEFAULT 0")
            if "event_duration" not in dcols:
                cur.execute("ALTER TABLE dingtalkconfig ADD COLUMN event_duration INTEGER NOT NULL DEFAULT 60")
            if "event_reminder_min" not in dcols:
                cur.execute("ALTER TABLE dingtalkconfig ADD COLUMN event_reminder_min INTEGER NOT NULL DEFAULT 15")
            if "card_enabled" not in dcols:
                cur.execute("ALTER TABLE dingtalkconfig ADD COLUMN card_enabled INTEGER NOT NULL DEFAULT 0")
            if "card_template_id" not in dcols:
                cur.execute("ALTER TABLE dingtalkconfig ADD COLUMN card_template_id VARCHAR NOT NULL DEFAULT ''")
            # Teacher：补 phone / invite_token（钉钉待办提醒与课程内容填报链接）
            cur.execute("PRAGMA table_info(teacher)")
            tcols = [r[1] for r in cur.fetchall()]
            if "phone" not in tcols:
                cur.execute("ALTER TABLE teacher ADD COLUMN phone VARCHAR NOT NULL DEFAULT ''")
            if "invite_token" not in tcols:
                cur.execute("ALTER TABLE teacher ADD COLUMN invite_token VARCHAR NOT NULL DEFAULT ''")
            # Student：补 phone（技能考核发布时给考生推钉钉单聊）
            cur.execute("PRAGMA table_info(student)")
            scols = [r[1] for r in cur.fetchall()]
            # ExamDuty：补 多监考姓名/ID（一站多人时保存完整名单）
            cur.execute("PRAGMA table_info(examduty)")
            dcols = [r[1] for r in cur.fetchall()]
            if dcols and "teachers" not in dcols:
                cur.execute("ALTER TABLE examduty ADD COLUMN teachers VARCHAR NOT NULL DEFAULT ''")
            if dcols and "teacher_ids" not in dcols:
                cur.execute("ALTER TABLE examduty ADD COLUMN teacher_ids VARCHAR NOT NULL DEFAULT ''")
            if scols and "phone" not in scols:
                cur.execute("ALTER TABLE student ADD COLUMN phone VARCHAR NOT NULL DEFAULT ''")
            cur.execute("SELECT id FROM teacher WHERE invite_token='' OR invite_token IS NULL")
            for row in cur.fetchall():
                cur.execute("UPDATE teacher SET invite_token=? WHERE id=?",
                            (uuid.uuid4().hex, row[0]))
            # ClassSession：补 topic（讲课内容/题目）
            cur.execute("PRAGMA table_info(classsession)")
            ccols = [r[1] for r in cur.fetchall()]
            if "topic" not in ccols:
                cur.execute("ALTER TABLE classsession ADD COLUMN topic VARCHAR NOT NULL DEFAULT ''")
            # ClassSession：补名字文本快照（删除名单人员后历史课表仍显示名字）
            for col in ("teacher_name", "main_reader_name", "reviewer_name",
                        "mentor_name", "presenter_name"):
                if col not in ccols:
                    cur.execute("ALTER TABLE classsession ADD COLUMN %s VARCHAR NOT NULL DEFAULT ''" % col)
            # 老库补种「节假日管理」菜单（建库早于该功能，之前遗漏）
            cur.execute("SELECT id FROM menu WHERE path='#/holidays' LIMIT 1")
            if not cur.fetchone():
                cur.execute("SELECT id FROM menu WHERE name='基础数据' AND path='' LIMIT 1")
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "INSERT INTO menu (parent_id, name, path, icon, sort, permission, visible) "
                        "VALUES (?, '节假日管理', '#/holidays', 'calendar-off', 4, '', 1)", (row[0],))
            # 老库补种「排课规则」菜单
            cur.execute("SELECT id FROM menu WHERE path='#/rules' LIMIT 1")
            if not cur.fetchone():
                cur.execute("SELECT id FROM menu WHERE name='系统设置' AND path='' LIMIT 1")
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "INSERT INTO menu (parent_id, name, path, icon, sort, permission, visible) "
                        "VALUES (?, '排课规则', '#/rules', 'sliders', 0, '', 1)", (row[0],))
            # 老库补种「技能考核」菜单（月末多站考核考务）
            cur.execute("SELECT id FROM menu WHERE path='#/exam' LIMIT 1")
            if not cur.fetchone():
                cur.execute("SELECT id FROM menu WHERE name='排课管理' AND path='' LIMIT 1")
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "INSERT INTO menu (parent_id, name, path, icon, sort, permission, visible) "
                        "VALUES (?, '技能考核', '#/exam', 'clipboard', 3, '', 1)", (row[0],))
            # ExamStation：补 弹性考站 / 自动扩容 标记
            cur.execute("PRAGMA table_info(examstation)")
            ecols = [r[1] for r in cur.fetchall()]
            cur.execute("PRAGMA table_info(examplan)")
            pcols = [r[1] for r in cur.fetchall()]
            if pcols and "auto_scale" not in pcols:
                cur.execute("ALTER TABLE examplan ADD COLUMN auto_scale INTEGER NOT NULL DEFAULT 1")
            if pcols and "expand_penalty" not in pcols:
                # 扩容取舍：每多派 1 名监考折算的分钟数，默认 30（倾向老师最少）
                cur.execute("ALTER TABLE examplan ADD COLUMN expand_penalty INTEGER NOT NULL DEFAULT 20")
            if ecols:
                if "elastic" not in ecols:
                    cur.execute("ALTER TABLE examstation ADD COLUMN elastic INTEGER NOT NULL DEFAULT 0")
                if "auto_rooms" not in ecols:
                    cur.execute("ALTER TABLE examstation ADD COLUMN auto_rooms INTEGER NOT NULL DEFAULT 0")
                if "teacher_ids" not in ecols:
                    # 人工指定的本站考官（逗号分隔 id），留空由系统按历史权重派
                    cur.execute("ALTER TABLE examstation ADD COLUMN teacher_ids VARCHAR NOT NULL DEFAULT ''")
                if "align" not in ecols:
                    # 时间对齐：同站考生尽量排在同一时间块，表格更整齐（默认给第二站开启）
                    cur.execute("ALTER TABLE examstation ADD COLUMN align INTEGER NOT NULL DEFAULT 0")
                # 一次性迁移：第一/二站为电脑考试（机位不限、1 名老师可盯多名考生）。
                # 用 user_version 打版本号，避免每次启动覆盖用户手工取消的勾选。
                cur.execute("PRAGMA user_version")
                ver = (cur.fetchone() or [0])[0] or 0
                if ver < 2:
                    cur.execute("UPDATE examstation SET elastic=1 WHERE seq IN (1, 2)")
                    cur.execute("PRAGMA user_version=2")
                # 一次性迁移 v3：第二站（集体考核站）默认要求时间对齐
                cur.execute("PRAGMA user_version")
                ver = (cur.fetchone() or [0])[0] or 0
                if ver < 3:
                    cur.execute("UPDATE examstation SET align=1 WHERE seq=2")
                    cur.execute("PRAGMA user_version=3")
                # 一次性迁移 v4：课程类型/节假日归入「系统设置」（管理员页面），
                # 高权限操作集中管理，非管理员登录时该组整体隐藏
                cur.execute("PRAGMA user_version")
                ver = (cur.fetchone() or [0])[0] or 0
                if ver < 4:
                    cur.execute("SELECT id FROM menu WHERE name='系统设置' AND path='' LIMIT 1")
                    row = cur.fetchone()
                    if row:
                        gid = row[0]
                        cur.execute("UPDATE menu SET parent_id=?, sort=1 WHERE path='#/classtypes'", (gid,))
                        cur.execute("UPDATE menu SET parent_id=?, sort=2 WHERE path='#/holidays'", (gid,))
                        cur.execute("UPDATE menu SET sort=3 WHERE path='#/rules'")
                        cur.execute("UPDATE menu SET sort=4 WHERE path='#/dingtalk'")
                        cur.execute("UPDATE menu SET sort=5 WHERE path='#/menus'")
                    cur.execute("PRAGMA user_version=4")
                # 一次性迁移 v5：系统设置组新增「账号管理」（管理员页面）
                cur.execute("PRAGMA user_version")
                ver = (cur.fetchone() or [0])[0] or 0
                if ver < 5:
                    cur.execute("SELECT id FROM menu WHERE name='系统设置' AND path='' LIMIT 1")
                    row = cur.fetchone()
                    if row and not cur.execute(
                            "SELECT 1 FROM menu WHERE path='#/users'").fetchone():
                        cur.execute(
                            "INSERT INTO menu (parent_id, name, path, icon, sort, permission, visible)"
                            " VALUES (?,?,?,?,6,'',1)",
                            (row[0], "账号管理", "#/users", "key"))
                    cur.execute("PRAGMA user_version=5")
                # 一次性迁移 v6：研究生名册（第三节「研究生汇报」）
                cur.execute("PRAGMA user_version")
                ver = (cur.fetchone() or [0])[0] or 0
                if ver < 6:
                    cur.execute("SELECT id FROM menu WHERE name='基础数据' AND path='' LIMIT 1")
                    row = cur.fetchone()
                    if row and not cur.execute(
                            "SELECT 1 FROM menu WHERE path='#/graduates'").fetchone():
                        cur.execute(
                            "INSERT INTO menu (parent_id, name, path, icon, sort, permission, visible)"
                            " VALUES (?,?,?,?,3,'',1)",
                            (row[0], "研究生管理", "#/graduates", "graduation"))
                    if not cur.execute(
                            "SELECT 1 FROM classtype WHERE name='研究生汇报'").fetchone():
                        cur.execute(
                            "INSERT INTO classtype (name, color, freq_desc) VALUES (?,?,?)",
                            ("研究生汇报", "#6941C6", "每周1次"))
                    cur.execute("PRAGMA user_version=6")
            # 一次性迁移 v8：回填课时名字快照（删人后历史课表仍显示名字）
            cur.execute("PRAGMA user_version")
            ver = (cur.fetchone() or [0])[0] or 0
            if ver < 8:
                # COALESCE 兜底：历史行可能引用已删除的教师/学生（子查询返回 NULL 会违反 NOT NULL）
                cur.execute(
                    "UPDATE classsession SET teacher_name=COALESCE("
                    "(SELECT name FROM teacher WHERE teacher.id=classsession.teacher_id),'') "
                    "WHERE teacher_id>0 AND teacher_name=''")
                cur.execute(
                    "UPDATE classsession SET main_reader_name=COALESCE("
                    "(SELECT name FROM student WHERE student.id=classsession.main_reader_id),"
                    "(SELECT name FROM teacher WHERE teacher.id=classsession.main_reader_id),'') "
                    "WHERE main_reader_id>0 AND main_reader_name=''")
                cur.execute(
                    "UPDATE classsession SET reviewer_name=COALESCE("
                    "(SELECT name FROM teacher WHERE teacher.id=classsession.reviewer_id),'') "
                    "WHERE reviewer_id>0 AND reviewer_name=''")
                cur.execute(
                    "UPDATE classsession SET mentor_name=COALESCE("
                    "(SELECT name FROM teacher WHERE teacher.id=classsession.mentor_id),'') "
                    "WHERE mentor_id>0 AND mentor_name=''")
                cur.execute(
                    "UPDATE classsession SET presenter_name=COALESCE("
                    "(SELECT name FROM graduate WHERE graduate.id=classsession.presenter_id),'') "
                    "WHERE presenter_id>0 AND presenter_name=''")
                cur.execute("PRAGMA user_version=8")
            # 一次性迁移 v9：教师启用/停用字段
            cur.execute("PRAGMA user_version")
            ver = (cur.fetchone() or [0])[0] or 0
            if ver < 9:
                cur.execute("PRAGMA table_info(teacher)")
                tcols = [r[1] for r in cur.fetchall()]
                if "enabled" not in tcols:
                    cur.execute("ALTER TABLE teacher ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
                cur.execute("PRAGMA user_version=9")
            conn.commit()
    except Exception:
        # 迁移失败不能静默：异常会导致整个事务回滚（连前面已执行的 ALTER 也丢失）
        import traceback
        print("[db] 迁移异常（本次迁移未生效，请把下面的堆栈反馈给维护者）：", flush=True)
        traceback.print_exc()


def seed_if_empty():
    with Session(engine) as s:
        if s.exec(select(models.User)).first():
            return
        s.add(models.User(username="admin", password_hash=auth.hash_password("admin123"),
                          name="管理员", role="admin"))
        for i in range(1, 19):
            s.add(models.Teacher(emp_id=f"T{i:02d}", name=f"教师{i:02d}"))
        grades = ["一年级", "二年级", "三年级"]
        for i in range(1, 19):
            s.add(models.Student(stu_id=f"S{i:02d}", name=f"学生{i:02d}",
                                 grade=grades[(i - 1) // 6]))
        s.add(models.ClassType(name="小讲课", color="#185FA5", freq_desc="每月4次"))
        s.add(models.ClassType(name="病例讨论", color="#BA7517", freq_desc="每2周1次"))
        s.add(models.ClassType(name="教学阅片", color="#0F6E56", freq_desc="每2周1次"))
        s.add(models.ClassType(name="研究生汇报", color="#6941C6", freq_desc="每周1次"))

        base =             models.Menu(name="基础数据", path="", icon="data", sort=2)
        s.add(base); s.flush()
        paike = models.Menu(name="排课管理", path="", icon="calendar", sort=3)
        s.add(paike); s.flush()
        sys_ = models.Menu(name="系统设置", path="", icon="setting", sort=4)
        s.add(sys_); s.flush()
        items = [
            models.Menu(name="首页", path="#/dashboard", icon="home", sort=1),
            models.Menu(name="教师管理", path="#/teachers", parent_id=base.id, icon="user", sort=1),
            models.Menu(name="学生管理", path="#/students", parent_id=base.id, icon="users", sort=2),
            models.Menu(name="研究生管理", path="#/graduates", parent_id=base.id, icon="graduation", sort=3),
            models.Menu(name="生成课表", path="#/schedule-generate", parent_id=paike.id, icon="edit", sort=1),
            models.Menu(name="查看课表", path="#/schedule-view", parent_id=paike.id, icon="view", sort=2),
            models.Menu(name="技能考核", path="#/exam", parent_id=paike.id, icon="clipboard", sort=3),
            # 系统设置 = 管理员页面（仅 admin 角色可见，由 /menus 接口按角色过滤）
            models.Menu(name="课程类型", path="#/classtypes", parent_id=sys_.id, icon="book", sort=1),
            models.Menu(name="节假日管理", path="#/holidays", parent_id=sys_.id, icon="calendar-off", sort=2),
            models.Menu(name="排课规则", path="#/rules", parent_id=sys_.id, icon="sliders", sort=3),
            models.Menu(name="钉钉配置", path="#/dingtalk", parent_id=sys_.id, icon="robot", sort=4),
            models.Menu(name="账号管理", path="#/users", parent_id=sys_.id, icon="key", sort=5),
            models.Menu(name="菜单管理", path="#/menus", parent_id=sys_.id, icon="menu", sort=6),
        ]
        for m in items:
            s.add(m)
        s.add(models.DingTalkConfig())
        s.commit()
