# -*- coding: utf-8 -*-
"""钉钉互动卡片（Stream 回调版）。

与旧版 ActionCard 的区别：按钮不再是「跳转 URL」，而是「回传请求」——
老师点按钮时，钉钉不访问本系统，而是通过机器人已建立的那条 WebSocket 长连接
把点击事件推下来（官方称"五零接入"：零公网 IP / 域名 / 证书 / 网关 / 内网穿透）。
因此系统部署在纯内网、没有任何对外地址时，卡片按钮依然可用。

卡片模板需在钉钉卡片平台（https://card.dingtalk.com/card-builder）创建并发布，
模板里按钮的「点击事件类型」必须选「回传请求」，actionId 分别填 ack / fill，
模板 ID 填到「钉钉配置 → 卡片模板 ID」。
"""
import json
import uuid

CARD_API = "https://api.dingtalk.com/v1.0/card/instances"

# 普通版卡片（StandardCard）：cardData 直接就是整张卡片的 JSON，不需要在卡片平台建模板。
# 这是"自带模板"的发送方式，最适合内网部署：没有模板 ID、没有变量绑定，也就没有
# param.invalid 那一类模板元数据问题。按钮用 actionType=request（回传请求）。
STD_TEMPLATE = "StandardCard"
STD_SEND_API = "https://api.dingtalk.com/v1.0/im/v1.0/robot/interactiveCards/send"
STD_UPDATE_API = "https://api.dingtalk.com/v1.0/im/robots/interactiveCards"

ACT_ACK = "ack"      # 我已确认
ACT_FILL = "fill"    # 如何填报

# 卡片平台里手填回传参数时常见的小差异，统一映射过来（act 是 ack 的常见笔误）
_ACTION_ALIAS = {
    "act": ACT_ACK, "confirm": ACT_ACK, "confirmed": ACT_ACK, "ok": ACT_ACK, "yes": ACT_ACK,
    "help": ACT_FILL, "howto": ACT_FILL, "how_to": ACT_FILL, "input": ACT_FILL,
}


def card_ready(cfg) -> bool:
    """卡片功能是否可用（开关 + 模板 ID + 企业应用凭据）。"""
    return bool(cfg and getattr(cfg, "card_enabled", False)
                and (getattr(cfg, "card_template_id", "") or "").strip()
                and cfg.app_key and cfg.app_secret)


def new_card_id(teacher_id: int) -> str:
    """自定义 outTrackId：把教师 id 编进去，回调时据此判断这张卡片属于谁。
    钉钉要求同一模板下 outTrackId 唯一，用 uuid 后缀保证不冲突。"""
    return "course_%d_%s" % (int(teacher_id), uuid.uuid4().hex[:12])


def teacher_of_card(out_track_id: str) -> int:
    """从 outTrackId 反解教师 id；不属于本系统发出的卡片返回 0。"""
    try:
        head = (out_track_id or "").split("_")
        if len(head) >= 2 and head[0] == "course":
            return int(head[1])
    except Exception:
        pass
    return 0


def _render(teacher, rows, acked_names=None, hint=""):
    """rows: [(date, time, class_type_name, role, topic)]"""
    lines = []
    for (d, t_, cname, role, topic) in rows:
        state = topic or "未填报"
        lines.append("• %s %s 【%s】%s · %s" % (d[5:], t_, cname, role, state))
    body = "**%s**老师，本月共 **%d** 节教学任务：\n\n%s" % (
        teacher.name or "老师", len(rows), "\n\n".join(lines))
    if acked_names:
        body += "\n\n---\n✓ 已确认：%s" % "、".join(acked_names)
    if hint:
        body += "\n\n%s" % hint
    tips = "已确认，无需重复点击" if acked_names else "点击「我已确认」表示已核对以上安排"
    return {"title": "我的教学任务", "markdown": body, "tips": tips}


def build_card_data(teacher, rows, acked_names=None, hint=""):
    """构造 cardParamMap。值必须是字符串，否则钉钉接口报参数错误。"""
    data = _render(teacher, rows, acked_names, hint)
    return {k: str(v) for k, v in data.items()}


def build_standard_card(teacher, rows, acked_names=None, hint="", disabled_ack=False,
                        with_buttons=False):
    """构造普通版（StandardCard）整卡 JSON。

    与模板版的区别：这里没有任何"变量"，文字就是文字，按钮就是按钮，
    所以钉钉不可能因为变量没定义而报 param.invalid。

    注意：普通版卡片的按钮回调只能走「HTTP callbackUrl」，走不了机器人这条
    WebSocket（钉钉文档：callbackUrl 不填则无需回调，实测点击无任何事件推来）。
    所以纯内网部署时不要放按钮——放了也是"点了没反应"，比没有更糟。
    with_buttons=True 只在系统真的能收到回调时（模板版链路）才用。
    """
    r = _render(teacher, rows, acked_names, hint)
    contents = [{"type": "markdown", "text": r["markdown"], "id": "md_main"}]
    if with_buttons:
        if r.get("tips"):
            contents.append({"type": "text", "text": r["tips"], "id": "tip_main"})
        ack_btn = {
            "type": "button",
            "label": {"type": "text", "text": "已确认" if disabled_ack else "我已确认"},
            "actionType": "request",
            "value": ACT_ACK,
            "status": "primary",
            "id": "btn_ack",
        }
        if disabled_ack:
            ack_btn["disabled"] = True
        contents.append({
            "type": "action",
            "id": "act_main",
            "actions": [
                ack_btn,
                {"type": "button",
                 "label": {"type": "text", "text": "如何填报"},
                 "actionType": "request", "value": ACT_FILL,
                 "status": "normal", "id": "btn_fill"},
            ],
        })
    else:
        tip = "填报方式：在群里发「@排课助手 12-03 课程名」，只发课程名则填到最早一节未填的课。"
        if hint:
            tip = hint
        contents.append({"type": "text", "text": tip, "id": "tip_main"})
    return {
        "config": {"autoLayout": True, "enableForward": True},
        "header": {"title": {"type": "text", "text": "我的教学任务"}},
        "contents": contents,
    }


def send_standard_card(cfg, conversation_id=None, card=None, biz_id=None, user_id=None):
    """普通版发送：群聊传 conversation_id，单聊传 user_id，二选一。返回 (ok, note)。"""
    import api as app_api
    if not conversation_id and not user_id:
        return False, "缺少接收方（群会话 ID 或 userId）"
    token = app_api._dt_access_token(cfg)
    body = {
        "cardTemplateId": STD_TEMPLATE,
        "cardBizId": biz_id or ("card_" + uuid.uuid4().hex[:20]),
        "robotCode": cfg.app_key,
        "cardData": json.dumps(card, ensure_ascii=False),
    }
    if conversation_id:
        body["openConversationId"] = conversation_id
    else:
        body["singleChatReceiver"] = json.dumps({"userId": user_id})
    try:
        app_api._dt_open_api(body, STD_SEND_API, token)
    except Exception as e:
        return False, "发送卡片失败：%s" % str(e)[:180]
    return True, body["cardBizId"]


def update_standard_card(cfg, biz_id: str, card: dict):
    """普通版原地更新（按 cardBizId）。"""
    import api as app_api
    token = app_api._dt_access_token(cfg)
    body = {"cardBizId": biz_id, "cardData": json.dumps(card, ensure_ascii=False)}
    try:
        app_api._dt_open_api(body, STD_UPDATE_API, token, method="PUT")
        return True, "已更新"
    except Exception as e:
        return False, "更新卡片失败：%s" % str(e)[:180]


def create_and_deliver(cfg, conversation_id: str, card_data: dict, out_track_id: str):
    """建卡片实例 → 投放到群。返回 (ok, note)。"""
    import api as app_api
    if not conversation_id:
        return False, "缺少群会话 ID（卡片只能发到群里）"
    token = app_api._dt_access_token(cfg)
    tid = (getattr(cfg, "card_template_id", "") or "").strip()
    body = {
        "cardTemplateId": tid,
        "outTrackId": out_track_id,
        "cardData": {"cardParamMap": card_data},
        # STREAM：按钮点击事件走机器人那条 WebSocket，不回调任何 HTTP 地址
        "callbackType": "STREAM",
        "imGroupOpenSpaceModel": {"supportForward": True},
        "imRobotOpenSpaceModel": {"supportForward": True},
    }
    try:
        app_api._dt_open_api(body, CARD_API, token)
    except Exception as e:
        return False, "创建卡片失败：%s" % str(e)[:180]
    deliver = {
        "outTrackId": out_track_id,
        "userIdType": 1,
        "openSpaceId": "dtv1.card//IM_GROUP.%s" % conversation_id,
        "imGroupOpenDeliverModel": {"robotCode": cfg.app_key},
    }
    try:
        app_api._dt_open_api(deliver, CARD_API + "/deliver", token)
    except Exception as e:
        return False, "投放卡片失败：%s" % str(e)[:180]
    return True, out_track_id


def update_card(cfg, out_track_id: str, card_data: dict):
    """原地刷新卡片内容（老师点完按钮后，同一张卡片立刻变成新状态）。"""
    import api as app_api
    token = app_api._dt_access_token(cfg)
    body = {"outTrackId": out_track_id,
            "cardData": {"cardParamMap": {k: str(v) for k, v in card_data.items()}}}
    try:
        app_api._dt_open_api(body, CARD_API, token, method="PUT")
        return True, "已更新"
    except Exception as e:
        return False, "更新卡片失败：%s" % str(e)[:180]


def parse_action(payload: dict):
    """从 Stream 推下来的卡片回调里取出 (actionId, userid, outTrackId)。
    钉钉不同版本的回调体层级略有差异，这里逐种兼容，取不到就返回空。"""
    raw = payload or {}
    content = raw.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content or "{}")
        except Exception:
            content = {}
    if not isinstance(content, dict):
        content = {}
    # 有的版本把真正的业务体再包一层 content
    inner = content.get("content")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except Exception:
            inner = None
    if isinstance(inner, dict):
        content = inner
    cpd = content.get("cardPrivateData") or content.get("cardPrivateDataMap") or {}
    if isinstance(cpd, str):
        try:
            cpd = json.loads(cpd)
        except Exception:
            cpd = {}
    ids = cpd.get("actionIds") or content.get("actionIds") or []
    if isinstance(ids, str):
        ids = [ids]
    action = ids[0] if ids else ""

    # 新版卡片搭建平台里，回传请求往往只给 params（按钮回传参数），不给 actionIds，
    # 所以这里再从 params 里兜一遍：{"action":"ack"} / {"ack":"1"} / {"ackId":"..."} 都认。
    params = cpd.get("params") or content.get("params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params or "{}")
        except Exception:
            params = {}
    if not action and isinstance(params, dict):
        for k in ("action", "actionId", "actionid", "act", "type", "id", "value"):
            if params.get(k):
                action = str(params[k])
                break
        if not action and len(params) == 1:
            k, v = list(params.items())[0]
            if k.lower() in (ACT_ACK, ACT_FILL):
                action = k
            elif str(v).lower() in (ACT_ACK, ACT_FILL):
                action = str(v)
    action = (action or "").strip().lower()
    if action not in (ACT_ACK, ACT_FILL) and action:
        # 容忍 ack_btn / btn_ack / ACK 这类写法
        if ACT_ACK in action:
            action = ACT_ACK
        elif ACT_FILL in action:
            action = ACT_FILL
        else:
            action = _ACTION_ALIAS.get(action, action)

    userid = raw.get("userId") or content.get("userId") or ""
    otid = (raw.get("outTrackId") or content.get("outTrackId")
            or content.get("cardBizId") or raw.get("cardBizId") or "")
    return action, userid, otid
