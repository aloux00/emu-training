# -*- coding: utf-8 -*-
"""技能考核：发布前自动校正监考时段 + 钉钉手机号匹配提示（隔离临时库）。"""
import os, sys, tempfile

TMP = tempfile.mkdtemp(prefix="_test_exam_pub_")
os.chdir(TMP)
sys.path.insert(0, r"C:\WD\code\教学课表")

import db
db.init_db()
import models, exam, api
from sqlmodel import Session, select

PASS = FAIL = 0
def check(name, ok, detail=None):
    global PASS, FAIL
    if ok: PASS += 1; print("PASS", name)
    else:  FAIL += 1; print("FAIL", name, "|", str(detail)[:200])

# ---------- 造数据 ----------
with Session(db.engine) as s:
    db_cfg = s.exec(select(models.DingTalkConfig)).first()
    if db_cfg is None:
        db_cfg = models.DingTalkConfig()
    db_cfg.app_key, db_cfg.app_secret = "k", "s"
    db_cfg.webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=x"
    db_cfg.enabled = True
    s.add(db_cfg)
    s.commit()

    ts = []
    for i, (emp, nm, ph) in enumerate([("E1", "甲老师", "13800000001"),
                                      ("E2", "乙老师", "13800000002"),
                                      ("E3", "丙老师", "13800000003")]):
        t = models.Teacher(emp_id=emp, name=nm, phone=ph)
        s.add(t); ts.append(t)
    stus = []
    for i, nm in enumerate(["考生A", "考生B"]):
        st = models.Student(stu_id="S%d" % i, name=nm, phone="1390000000%d" % i)
        s.add(st); stus.append(st)
    s.commit()

    p = models.ExamPlan(name="测试考核", exam_date="2026-12-01", start_time="12:30",
                        status="draft", tick_min=10, duty_min=0)
    s.add(p); s.commit()
    pid = p.id

    # 五站：第一站10min、第二站30min、其余10min
    durs = {1: 10, 2: 30, 3: 30, 4: 10, 5: 10}
    for seq, d in durs.items():
        s.add(models.ExamStation(plan_id=pid, seq=seq, name="第%d站" % seq,
                                 duration_min=d, rooms=1, teachers=1))
    cands = []
    for st in stus:
        c = models.ExamCandidate(plan_id=pid, student_id=st.id, name=st.name)
        s.add(c); cands.append(c)
    s.commit()

    # 考生A：第1站 0-10，第2站 10-40，第3站 40-70
    # 考生B：第1站 70-80，第2站 40-70，第3站 0-30  → 第2站实际占用 10..70（连续）
    plan_slots = [
        (cands[0].id, 1, 0, 10), (cands[0].id, 2, 10, 40), (cands[0].id, 3, 40, 70),
        (cands[1].id, 3, 0, 30), (cands[1].id, 2, 40, 70), (cands[1].id, 1, 70, 80),
    ]
    for cid, seq, a, b in plan_slots:
        s.add(models.ExamSlot(plan_id=pid, candidate_id=cid, station_seq=seq,
                              room=1, start_min=a, end_min=b))
    # 故意放一条过时监考：第二站 30-60（与真实占用 10-70 不符）
    s.add(models.ExamDuty(plan_id=pid, station_seq=2, room=-1, start_min=30, end_min=60,
                          teacher_id=ts[1].id, teacher_name=ts[1].name,
                          teachers=ts[1].name, teacher_ids=str(ts[1].id)))
    s.commit()

# ---------- 打桩：不真的发钉钉，记录文本 ----------
sent_texts = []
api._dt_access_token = lambda cfg: "fake-token"
api._dt_userid = lambda token, mobile: "uid-" + mobile
api._dt_send_dm = lambda cfg, token, uid, text: sent_texts.append((uid, text)) or "ok"

with Session(db.engine) as s:
    rep = exam.publish(pid, {"notify": True}, s=s, _=None)

check("发布成功", rep.get("ok") is True, rep)
check("提示包含「重算」", "重算" in (rep.get("note") or ""), rep.get("note"))

with Session(db.engine) as s:
    ds = s.exec(select(models.ExamDuty).where(models.ExamDuty.plan_id == pid)).all()
    cov = {}
    for d in ds:
        cov.setdefault(d.station_seq, []).append((d.start_min, d.end_min))
check("第二站监考时段已覆盖真实占用 10-70", cov.get(2) == [(10, 70)] or sorted(cov.get(2) or []) == [(10, 70)], cov)
check("第一站补出监考 0-10 与 70-80", sorted(cov.get(1) or []) == [(0, 10), (70, 80)], cov)
check("第三站监考 0-70", sorted(cov.get(3) or []) == [(0, 70)], cov)

# 老师收到的文本应出现真实时段
teacher_msgs = [t for u, t in sent_texts if u.startswith("uid-138")]
with Session(db.engine) as s:
    with_phone = set()
    for d in s.exec(select(models.ExamDuty).where(models.ExamDuty.plan_id == pid)).all():
        for x in (d.teacher_ids or "").split(","):
            if x.strip().isdigit():
                t = s.get(models.Teacher, int(x))
                if t and (t.phone or "").strip():
                    with_phone.add(t.id)
check("有手机号的监考老师都收到消息", len(teacher_msgs) == len(with_phone) and teacher_msgs, (len(teacher_msgs), len(with_phone)))
all_text = "\n".join(teacher_msgs)
check("老师文本含 第二站 12:40–13:40", "第二站 12:40–13:40" in all_text or "12:40–13:40" in all_text, all_text[:300])

# 再次发布：时段已一致，不应再提示重算
sent_texts.clear()
with Session(db.engine) as s:
    rep2 = exam.publish(pid, {"notify": True}, s=s, _=None)
check("时段一致时不再重算", "重算" not in (rep2.get("note") or ""), rep2.get("note"))

# ---------- 错误提示 ----------
e60121 = api._dt_mobile_error("13600000000", {"errcode": 60121, "errmsg": "找不到该用户"})
check("60121 提示含可见范围", "可见范围" in e60121 and "通讯录" in e60121, e60121)
e60011 = api._dt_mobile_error("13600000000", {"errcode": 60011, "errmsg": "no permission"})
check("60011 提示含通讯录权限", "通讯录" in e60011 and "权限" in e60011, e60011)

# ---------- 匹配自查 ----------
def fake_oapi(payload, url):
    m = payload.get("mobile", "")
    if m.endswith("1"):
        return {"errcode": 0, "result": {"userid": "u1"}}
    return {"errcode": 60121, "errmsg": "找不到该用户"}
api._dt_oapi = fake_oapi
with Session(db.engine) as s:
    r = api.dt_match_check(None)
check("自查返回统计", r.get("checked") == 5 and len(r["matched"]) >= 1, r.get("note"))
check("自查列出不可匹配者", any(x["kind"] == "老师" for x in r["unmatched"]), r["unmatched"][:2])

print("\n==== %d 通过 / %d 失败 ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
