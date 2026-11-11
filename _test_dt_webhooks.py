# -*- coding: utf-8 -*-
"""离线验证：多钉钉群 Webhook（其他钉钉群推送）管理接口与推送链路（隔离临时目录）。"""
import os, sys, shutil, tempfile, json, base64, hashlib, hmac, time
import urllib.parse
_PROJ = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="dtwebhook-")
shutil.copy(os.path.join(_PROJ, "schedule.db"), os.path.join(_TMP, "schedule.db"))
os.chdir(_TMP); sys.path.insert(0, _PROJ)

import db, models, api
db.init_db()
from sqlmodel import Session, select

FAILS = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)[:220]) if detail and not cond else ""))
    if not cond: FAILS.append(name)

class FakeResp:
    def read(self): return b'{"errcode":0,"errmsg":"ok"}'
    def __enter__(self): return self
    def __exit__(self, *a): return False

CAPTURED = []
def fake_urlopen(req, timeout=20, **kw):
    CAPTURED.append({"url": req.full_url, "data": json.loads(req.data.decode("utf-8"))})
    return FakeResp()
api.urllib.request.urlopen = fake_urlopen

W1 = "https://oapi.dingtalk.com/robot/send?access_token=" + "a" * 20
W2 = "https://oapi.dingtalk.com/robot/send?access_token=" + "b" * 20

# 0. 清空既有群（拷贝的生产库可能已有用户真实数据），保证测试确定性
api.put_dts({"webhooks": []}, None)

# 1. 初始为空
r0 = api.get_dts(None)
check("初始列表为空", r0 == [], r0)

# 2. 添加两个群（1 启用 1 停用，含加签）
r = api.put_dts({"webhooks": [
    {"id": None, "name": "教研室群", "webhook_url": W1, "secret": "SEC111",
     "keywords": "课程表,教研", "enabled": True},
    {"id": None, "name": "教学办群", "webhook_url": W2, "secret": "",
     "keywords": "", "enabled": False},
]}, None)
check("保存返回 ok/2 群", r.get("ok") and r["count"] == 2, r)
items = r["items"]
check("列表脱敏 Webhook", api._MASK in items[0]["webhook_url"] and items[0]["webhook_url"].startswith("https://oapi"), items)
check("列表脱敏 Secret", api._MASK in items[0]["secret"], items)
check("空关键词回填默认", items[1]["keywords"] == "课程表", items[1])

# 3. 掩码还原：保存时带掩码 = 维持原值
with Session(db.engine) as s:
    rows = s.exec(select(models.DingTalkWebhook)).all()
    check("库中 2 行", len(rows) == 2, len(rows))
    row1 = [x for x in rows if x.name == "教研室群"][0]
    OLD_URL1, OLD_SEC1 = row1.webhook_url, row1.secret
    OLD_ID1 = row1.id
r = api.put_dts({"webhooks": [
    {"id": OLD_ID1, "name": "教研室群", "webhook_url": api._mask_webhook(OLD_URL1),
     "secret": api._mask_tail(OLD_SEC1), "keywords": "课程表,教研", "enabled": True},
]}, None)
with Session(db.engine) as s:
    rows = s.exec(select(models.DingTalkWebhook)).all()
    check("掩码保存=未修改 URL", len(rows) == 1 and rows[0].webhook_url == OLD_URL1,
          [x.webhook_url for x in rows])
    check("掩码保存=未修改 Secret", rows[0].secret == OLD_SEC1, rows[0].secret)

# 4. 校验：坏 URL / 空 URL 报错；空白卡片忽略；无群名自动命名
import fastapi
def expect400(name, payload, kw):
    try:
        api.put_dts(payload, None)
        check(name, False, "未报错")
    except fastapi.HTTPException as e:
        check(name, e.status_code == 400 and kw in e.detail, e.detail)
    except Exception as e:
        check(name, False, str(e)[:150])
expect400("坏 URL 报错", {"webhooks": [{"name": "x", "webhook_url": "http://evil.com/robot", "enabled": False}]}, "格式不正确")
expect400("空 URL 报错", {"webhooks": [{"name": "x", "webhook_url": "", "enabled": False}]}, "为空")

# 4a. 空白卡片（全空、无 id）保存时被忽略，不报错（整表保存语义：旧行被替换）
with Session(db.engine) as s:
    n_before = len(s.exec(select(models.DingTalkWebhook)).all())
r = api.put_dts({"webhooks": [
    {"id": None, "name": "", "webhook_url": "", "secret": "", "keywords": "课程表", "enabled": False},
    {"id": None, "name": "教研室群", "webhook_url": W1, "secret": "SEC111", "keywords": "课程表", "enabled": True},
]}, None)
with Session(db.engine) as s:
    rows = s.exec(select(models.DingTalkWebhook)).all()
check("空白卡片被忽略不报错", r.get("ok") and len(rows) == 1 and rows[0].name == "教研室群",
      [(x.name, x.enabled) for x in rows])
check("有效行正常保存", any(x["name"] == "教研室群" for x in r["items"]), r)

# 4b. 只填 Webhook 没填群名 → 自动命名「其他群N」，不再 400（保留教研室群）
with Session(db.engine) as s:
    jy = s.exec(select(models.DingTalkWebhook)).first()
    jy_id = jy.id
r = api.put_dts({"webhooks": [
    {"id": jy_id, "name": "教研室群", "webhook_url": api._mask_webhook(W1),
     "secret": api._mask_tail("SEC111"), "keywords": "课程表", "enabled": True},
    {"id": None, "name": "", "webhook_url": W2, "secret": "", "keywords": "课程表", "enabled": False},
]}, None)
names = [x["name"] for x in r["items"]]
check("无群名自动命名", r.get("ok") and any(n.startswith("其他群") for n in names) and "教研室群" in names, names)

# 5. push_extra_groups：只发启用的群；加签参数正确；正文含该群关键词
CAPTURED.clear()
with Session(db.engine) as s:
    out = api.push_extra_groups(s, "### 测试推送正文")
check("只推 1 个启用群", len(out) == 1 and out[0][0] == "教研室群", out)
check("结果为成功", out[0][1].startswith("钉钉推送成功"), out[0])
check("请求落网 1 次", len(CAPTURED) == 1, CAPTURED)
u = CAPTURED[0]["url"]
check("推送 URL 指向群1 token", "access_token=" + "a" * 20 in u, u)
check("加签 timestamp+sign 已附加", "timestamp=" in u and "sign=" in u, u)
ts, sign = u.split("timestamp=")[1].split("&sign=")
ss = ts.rstrip("&").rstrip()
ss = u.split("timestamp=")[1].split("&sign=")[0]
string_to_sign = "%s\n%s" % (ss, "SEC111")
digest = hmac.new("SEC111".encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
expect = urllib.parse.quote_plus(base64.b64encode(digest))
check("加签算法与钉钉规则一致", sign.rstrip("&") == expect or sign == expect, (sign, expect))
kw = CAPTURED[0]["data"]["markdown"]["title"]
check("标题含该群第一个关键词", kw.startswith("课程表"), kw)

# 6. 未启用/空 Webhook 的群被跳过（直接库里造数据）
with Session(db.engine) as s:
    s.add(models.DingTalkWebhook(name="停用群", webhook_url=W2, enabled=False))
    s.add(models.DingTalkWebhook(name="空URL群", webhook_url="", enabled=True))
    s.commit()
CAPTURED.clear()
with Session(db.engine) as s:
    out = api.push_extra_groups(s, "x")
check("停用/空URL群被跳过", len(out) == 1 and len(CAPTURED) == 1, (out, CAPTURED))

# 7. test_dt：主群 + 其他群一起测，note 汇总各群结果
# 待办链路自检走 mock 无法返回 access_token，与 _test_bot_grad.py 同法打桩
api._dt_access_token = lambda cfg: "fake-token"
api._dt_union_id = lambda token, mobile: "fake-unionid"
with Session(db.engine) as s:
    cfg = s.exec(select(models.DingTalkConfig)).first()
    if not cfg:
        cfg = models.DingTalkConfig(enabled=True, webhook_url=W1); s.add(cfg); s.commit()
    else:
        cfg.enabled = True; cfg.webhook_url = W1; s.add(cfg); s.commit()
CAPTURED.clear()
r = api.test_dt(None)
check("test_dt ok=True", r.get("ok") is True, r)
check("test_dt note 含其他群汇总", "其他群" in r.get("note", "") and "教研室群" in r.get("note", ""), r.get("note"))
check("test_dt 落网含主群共 2 次", len(CAPTURED) == 2, len(CAPTURED))

# 8. send_dingtalk：停用主群时返回未配置，不影响其他群
CAPTURED.clear()
with Session(db.engine) as s:
    cfg = s.exec(select(models.DingTalkConfig)).first()
    cfg.enabled = False; s.add(cfg); s.commit()
    note = api.send_dingtalk(cfg, "x")
    out = api.push_extra_groups(s, "x")
check("主群停用返回未配置", note.startswith("未配置钉钉"), note)
check("其他群不受主群开关影响", len(out) == 1, out)

# 9. publish 返回 extra_push 字段（走真实 publish 流程，mock 待办/日程）
CAPTURED.clear()
with Session(db.engine) as s:
    cfg = s.exec(select(models.DingTalkConfig)).first()
    cfg.enabled = True; s.add(cfg); s.commit()
    n = len(s.exec(select(models.ClassSession)
                   .where(models.ClassSession.year == 2099, models.ClassSession.month == 1)).all())
    if n == 0:
        s.add(models.ClassSession(year=2099, month=1, date="2099-01-05", slot=1,
                                  class_type_name="小讲课", teacher_id=1, main_reader_id=1,
                                  reviewer_id=2, status="draft")); s.commit()
orig_todo = api.create_schedule_todos
orig_cal = api.sync_calendar_events
api.create_schedule_todos = lambda *a, **k: (0, "")
api.sync_calendar_events = lambda *a, **k: (0, 0, 0, 0, "")
r = api.publish({"year": 2099, "month": 1}, None)
api.create_schedule_todos = orig_todo
api.sync_calendar_events = orig_cal
check("publish pushed=True", r.get("pushed") is True, r)
check("publish 返回 extra_push", isinstance(r.get("extra_push"), list) and
      any(x["name"] == "教研室群" for x in r["extra_push"]), r.get("extra_push"))
check("publish 落网主群+其他群共 2 次", len(CAPTURED) == 2, len(CAPTURED))

# 10. 删除：保存时缺失的行被删除
with Session(db.engine) as s:
    ids = [x.id for x in s.exec(select(models.DingTalkWebhook)).all()]
api.put_dts({"webhooks": [{"id": ids[0], "name": "只剩一个", "webhook_url": W1, "secret": "", "enabled": False}]}, None)
with Session(db.engine) as s:
    rows = s.exec(select(models.DingTalkWebhook)).all()
    check("未出现行被删除", len(rows) == 1 and rows[0].name == "只剩一个", [x.name for x in rows])

print()
print("== %d FAIL ==" % len(FAILS) if FAILS else "== ALL PASS ==")
for f in FAILS: print(" -", f)
