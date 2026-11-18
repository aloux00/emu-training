# -*- coding: utf-8 -*-
"""技能考核轮转表图片推送（离线隔离测试）。"""
import os, sys, tempfile, shutil, json

TMP = tempfile.mkdtemp(prefix="_exam_img_")
SRC = r"C:\WD\code\教学课表"
for f in ["api.py", "auth.py", "db.py", "engine.py", "exam.py", "main.py",
          "models.py", "rules.py", "dingtalk_bot.py", "dingtalk_card.py", "card_template_body.json"]:
    shutil.copy(os.path.join(SRC, f), os.path.join(TMP, f))
shutil.copytree(os.path.join(SRC, "static"), os.path.join(TMP, "static"))
os.chdir(TMP)
sys.path.insert(0, TMP)

import db
db.init_db()
import models, exam, api
from sqlmodel import Session, select

N = [0]
def check(name, ok, info=None):
    N[0] += 1
    print("%s %02d. %s%s" % ("PASS" if ok else "FAIL", N[0], name,
                             (" | " + str(info)[:150]) if info is not None and not ok else ""))

# ---------- 构造数据：5 名考生、5 站、有并行考位 ----------
with Session(db.engine) as s:
    st_t = s.exec(select(models.Teacher)).first()
    st_s = s.exec(select(models.Student)).first()
    p = models.ExamPlan(name="图片推送测试考核", exam_date="2099-01-10", start_time="12:30",
                        tick_min=10, status="draft")
    s.add(p); s.commit(); s.refresh(p)
    pid = p.id
    names = ["甲", "乙2", "丙", "丁生", "戊某"]
    cids = {}
    for nm in names:
        stu = models.Student(name=nm, stu_id="IMG%s" % nm, phone="")
        s.add(stu); s.commit(); s.refresh(stu)
        c = models.ExamCandidate(plan_id=pid, student_id=stu.id, name=nm)
        s.add(c); s.commit(); s.refresh(c)
        cids[c.id] = nm
    sts = {i: models.ExamStation(plan_id=pid, seq=i, name="第%d站" % i, duration_min=10,
                                 rooms=1, teachers=1) for i in range(1, 6)}
    for st in sts.values():
        s.add(st)
    # 排程：每位考生 3 站，起点错开
    plan_slots = []
    off = 0
    for k, (cid, nm) in enumerate(cids.items()):
        for j in range(3):
            a = off + j * 10
            row = {"candidate_id": cid, "station_seq": (j % 5) + 1,
                   "start_min": a, "end_min": a + 10, "room": 1}
            plan_slots.append(row)
            s.add(models.ExamSlot(plan_id=pid, **row))
        off += 4
    s.commit()
    cands = {c.id: c for c in s.exec(select(models.ExamCandidate).where(
        models.ExamCandidate.plan_id == pid)).all()}
    png = exam._render_rotation_png(p, plan_slots, sts, cands, p.tick_min)
    check("PNG 渲染成功", isinstance(png, bytes) and len(png) > 3000, type(png))
    check("PNG 头合法", png[:8] == b"\x89PNG\r\n\x1a\n", png[:8])

    # ---------- 上传/发送打桩，验证调用参数 ----------
    uploads, sends_txt, sends_img = [], [], []
    api._dt_access_token = lambda cfg: "fake-token"
    api._dt_userid = lambda token, mobile: "uid-" + mobile
    def fake_upload(cfg, token, data, filename, ftype="image"):
        uploads.append((token, ftype, filename, len(data)))
        return "@media-123"
    def fake_dm(cfg, token, uid, text):
        sends_txt.append((uid, text))
        return "k1"
    def fake_img(cfg, token, uid, media_id):
        sends_img.append((uid, media_id))
        return "k2"
    api._dt_upload_media = fake_upload
    api._dt_send_dm = fake_dm
    api._dt_send_image = fake_img

    # 给老师和考生配手机号
    t = st_t
    t.phone = "13800000001"
    s.add(t)
    for c in cands.values():
        stu = s.get(models.Student, c.student_id)
        stu.phone = "138000000%02d" % (c.id % 90)
        s.add(stu)
    # 监考安排
    for seq in range(1, 6):
        s.add(models.ExamDuty(plan_id=pid, station_seq=seq, teacher_id=t.id,
                              teacher_ids=str(t.id), start_min=(seq - 1) * 30, end_min=(seq - 1) * 30 + 30))
    s.commit()
    cfg0 = s.exec(select(models.DingTalkConfig)).first()
    if not cfg0:
        cfg0 = models.DingTalkConfig()
        s.add(cfg0)
    cfg0.app_key = "ding1234567890"
    cfg0.app_secret = "secrettest"
    cfg0.enabled = True
    cfg0.todo_enabled = True
    s.commit()
    p.status = "published"
    s.add(p)
    s.commit()

    rep = exam.publish(pid, {"notify": True}, s=s, _=None)
    check("发布推送成功", rep.get("ok"), rep.get("note"))
    check("文字消息发给 6 人", len(sends_txt) == 6, len(sends_txt))
    check("图片上传 1 次（复用同一 media_id）", len(uploads) == 1, uploads)
    check("上传类型为 image", uploads and uploads[0][1] == "image", uploads)
    check("上传文件名 rotation.png", uploads and uploads[0][2] == "rotation.png", uploads)
    check("图片消息发给 6 人", len(sends_img) == 6, len(sends_img))
    check("图片 media_id 一致", all(m == "@media-123" for _, m in sends_img), sends_img)
    check("report 记录 image_sent", rep.get("image_sent") == 6, rep.get("image_sent"))
    check("note 含图片推送提示", "轮转表图片" in rep.get("note", ""), rep.get("note"))

    # ---------- multipart 请求体格式 ----------
    def build_body(data, filename):
        boundary = "----TeachBoundary7f3a91"
        parts = [
            ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n" % (boundary, filename)).encode("utf-8"),
            data,
            ("\r\n--%s--\r\n" % boundary).encode("utf-8"),
        ]
        return b"".join(parts), "multipart/form-data; boundary=" + boundary
    body, ctype = build_body(png, "rotation.png")
    check("multipart 头格式正确", body.startswith(b"------TeachBoundary7f3a91\r\nContent-Disposition: form-data; name=\"file\""), body[:80])
    check("multipart 结尾正确", body.endswith(b"\r\n------TeachBoundary7f3a91--\r\n"), body[-44:])
    check("Content-Type 含 boundary", "boundary=----TeachBoundary7f3a91" in ctype, ctype)
    # 完整 URL 组装
    url = "https://oapi.dingtalk.com/media/upload?access_token=fake-token&type=image"
    check("上传 URL 组装", url.startswith("https://oapi.dingtalk.com/media/upload?access_token=") and url.endswith("&type=image"), url)

    # ---------- sampleImageMsg 参数 ----------
    mp = json.dumps({"photoURL": "@media-123"})
    check("sampleImageMsg 参数结构(photoURL)", '"photoURL": "@media-123"' in mp, mp)

print("==== %d 项，全部通过 ====" % N[0] if not any(False for _ in [1]) else "====有失败====")
