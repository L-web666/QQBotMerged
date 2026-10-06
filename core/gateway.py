# -*- coding: utf-8 -*-
"""WebSocket 网关（每个机器人一条独立连接）

职责：鉴权（IDENTIFY / RESUME）、心跳与假死检测、指数退避重连、事件解析，
把 QQ 事件统一成内部消息字典后交给回调（消息总线）。

合并来源：`API_qqbot/core/qq_client.py` 的 RESUME/心跳逻辑 +
`app/qq_receiver.py` 的事件解析（附件/引用/提及/群内所有消息）与 intents 自动降级。
"""

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import websocket

logger = logging.getLogger(__name__)

INTENT_GUILDS = 1 << 0
INTENT_GUILD_MEMBERS = 1 << 1
INTENT_GROUP_MEMBER_EVENT = 1 << 24     # 群成员变动事件（官方事件页标注；总表暂未同步）
INTENT_GROUP_AND_C2C_EVENT = 1 << 25    # 群 @ 消息 + 群内所有消息 + 单聊（官方合并位）
INTENT_INTERACTION = 1 << 26            # 交互事件（按钮回调等）
DEFAULT_INTENTS = INTENT_GROUP_AND_C2C_EVENT | INTENT_INTERACTION   # 100663296
FALLBACK_INTENTS = INTENT_GROUP_AND_C2C_EVENT                        # 33554432
#
# 说明（依据 QQ 机器人开放平台文档）：
# - 官方**没有**独立的“群聊所有消息”intent 位：开启“接收所有消息”后，
#   `GROUP_MESSAGE_CREATE` 事件挂在 GROUP_AND_C2C_EVENT(1<<25) 上一起推送；
# - 群成员变动事件使用 1<<24（GROUP_MEMBER_EVENT，见 group_member_add 事件页）；
# - 订阅了未获权限的位会导致网关直接关闭（4013 无效 intent / 4014 intent 无权限），
#   因此本模块内置“自动降级”：连续鉴权失败后回退到默认 intents 再试。

MESSAGE_EVENTS = ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE",
                  "C2C_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE")
QUOTE_FIELDS = ("message_reference", "msg_reference", "reference", "quote")


class QQGateway:
    """一个机器人的 WebSocket 长连接。"""

    def __init__(self, bot_id: str, api_client, intents: int = None,
                 on_message: Callable[[Dict[str, Any]], None] = None,
                 on_status: Callable[[bool, str], None] = None,
                 reconnect_interval: float = 5.0, max_reconnect_interval: float = 30.0,
                 heartbeat_timeout_factor: float = 3.0, logger_obj: logging.Logger = None):
        self.bot_id = bot_id
        self.api = api_client
        self.log = logger_obj or logger
        self.intents = int(intents or DEFAULT_INTENTS)
        self._effective_intents = self.intents
        self.on_message = on_message
        self.on_status = on_status
        self.reconnect_interval = max(1.0, float(reconnect_interval or 5.0))
        self.max_reconnect_interval = max(self.reconnect_interval, float(max_reconnect_interval or 30.0))
        self.heartbeat_timeout_factor = max(1.0, float(heartbeat_timeout_factor or 3.0))

        self.ws: Optional[websocket.WebSocketApp] = None
        self.session_id: Optional[str] = None
        self.last_seq = 0
        self._should_resume = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self.heartbeat_interval = 30.0
        self.last_heartbeat_ack = 0.0
        self._invalid_session_count = 0
        self._fell_back = False
        self._connected = False
        self.last_error = ""
        self.ready_at = 0.0
        self.event_counts: Dict[str, int] = {}
        self.last_event_type = ""
        # 是否收到过「群消息（全量模式）」事件：开启后群里每条消息都会推过来，
        # @ 机器人也只能靠消息里的 mentions 判断（见 _is_mentioned）。
        self.all_message_mode = False

    # ------------------------------------------------------------------ 状态
    @property
    def connected(self) -> bool:
        try:
            return bool(self.ws and self.ws.sock and self.ws.sock.connected)
        except Exception:
            return False

    @property
    def ready(self) -> bool:
        """收到过 READY（说明鉴权成功）。"""
        return bool(self.session_id) and self.connected

    def status_text(self) -> str:
        if self.ready:
            return "在线"
        if self.connected:
            return "已连接（鉴权中）"
        if self._running:
            return "重连中"
        return "未连接"

    def _set_status(self, online: bool, error: str = ""):
        self._connected = online
        if error:
            self.last_error = error
        if self.on_status:
            try:
                self.on_status(online, error)
            except Exception as exc:
                self.log.debug("[%s] 状态回调异常: %s", self.bot_id, exc)

    # ------------------------------------------------------------------ 生命周期
    def start(self):
        if self._running:
            return
        if not self.api.configured:
            self.last_error = "未配置 app_id / app_secret"
            self.log.warning("[%s] 未配置凭据，跳过连接", self.bot_id)
            self._set_status(False, self.last_error)
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name=f"gateway-{self.bot_id}", daemon=True)
        self._thread.start()
        self.log.info("[%s] 网关线程已启动（intents=%s）", self.bot_id, self._effective_intents)

    def stop(self):
        self._running = False
        self.session_id = None
        self.last_seq = 0
        self._should_resume = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self._set_status(False, "")
        self.log.info("[%s] 网关已停止", self.bot_id)

    def restart(self):
        """按新配置重连（凭据变更后调用）。"""
        self.stop()
        time.sleep(0.2)
        self._fell_back = False
        self._invalid_session_count = 0
        self._effective_intents = self.intents
        self.start()

    # ------------------------------------------------------------------ 主循环
    def _run_loop(self):
        backoff = self.reconnect_interval
        while self._running:
            token = self.api.get_access_token()
            if not token:
                self._set_status(False, self.api.last_error or "无法获取 Access Token")
                self.log.warning("[%s] 无可用 Token，%s 秒后重试", self.bot_id, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, self.max_reconnect_interval)
                continue

            self.ws = websocket.WebSocketApp(
                self.api.ws_gateway,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            try:
                self.ws.run_forever()
            except Exception as exc:
                self.log.error("[%s] WebSocket 运行异常: %s", self.bot_id, exc)
                backoff = min(backoff * 2, self.max_reconnect_interval)

            if not self._running:
                break

            if self.session_id:
                self._should_resume = True
            self.log.info("[%s] %s 秒后重连…", self.bot_id, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 1.5, self.max_reconnect_interval)

    # ------------------------------------------------------------------ 回调
    def _on_open(self, ws):
        self.log.info("[%s] WebSocket 已连接，等待 Hello…", self.bot_id)
        self._set_status(True, "")

    def _on_error(self, ws, error):
        self.last_error = str(error)
        self.log.error("[%s] WebSocket 错误: %s", self.bot_id, error)

    def _on_close(self, ws, status_code, message):
        self.log.info("[%s] WebSocket 已关闭: %s %s", self.bot_id, status_code, message)
        self.last_heartbeat_ack = 0.0
        self._set_status(False, self.last_error)
        if self._running and self.session_id:
            self._should_resume = True

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if isinstance(data, str):     # 兼容双重编码
                data = json.loads(data)
            if not isinstance(data, dict):
                self.log.warning("[%s] 收到非对象帧，已忽略", self.bot_id)
                return
            self._handle_frame(ws, data)
        except ValueError as exc:
            self.log.error("[%s] 解析帧失败: %s", self.bot_id, exc)
        except Exception as exc:
            # 回调抛出异常会被 websocket-client 当成连接错误，必须吞掉
            self.log.error("[%s] 处理帧异常: %s", self.bot_id, exc)

    # ------------------------------------------------------------------ 帧处理
    def _handle_frame(self, ws, data: Dict[str, Any]):
        op = data.get("op")
        if op == 10:                       # Hello
            payload = data.get("d") or {}
            self.heartbeat_interval = max(5.0, float(payload.get("heartbeat_interval", 30000)) / 1000.0)
            self.last_heartbeat_ack = time.time()
            self.log.info("[%s] 收到 Hello，心跳间隔 %.0f 秒", self.bot_id, self.heartbeat_interval)
            self._ensure_heartbeat()
            self._send_identify_or_resume(ws)
            return
        if op == 11:                       # 心跳回执
            self.last_heartbeat_ack = time.time()
            return
        if op == 0:                        # 事件推送
            self.last_heartbeat_ack = time.time()
            if "s" in data:
                self.last_seq = data["s"]
            self._handle_dispatch(data)
            return
        if op == 7:                        # 服务端要求重连
            self.log.warning("[%s] 收到 RECONNECT，将重连并尝试恢复会话", self.bot_id)
            self._should_resume = bool(self.session_id)
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
            return
        if op == 9:                        # 会话无效
            payload = data.get("d")
            if payload is False:
                self._invalid_session_count += 1
                # 多次无效且订阅了扩展 intents → 自动降级（常见原因：未申请某个 intent 的权限）
                if (not self._fell_back and self._invalid_session_count >= 2
                        and self._effective_intents != FALLBACK_INTENTS):
                    self._fell_back = True
                    self._effective_intents = FALLBACK_INTENTS
                    self.log.warning("[%s] 鉴权连续失败，自动降级 intents 为 %s（原因通常是订阅了未获权限的 intent）",
                                     self.bot_id, FALLBACK_INTENTS)
                self.session_id = None
                self.last_seq = 0
                self._should_resume = False
                try:
                    if ws:
                        ws.close()
                except Exception:
                    pass
            else:
                self._invalid_session_count = 0
                self._send_identify_or_resume(ws)
            return

    def _send_identify_or_resume(self, ws):
        token = self.api.get_access_token()
        if not token:
            self.log.error("[%s] 无 Token，无法鉴权", self.bot_id)
            return
        try:
            if self.session_id and self._should_resume:
                ws.send(json.dumps({"op": 6, "d": {
                    "token": f"QQBot {token}", "session_id": self.session_id, "seq": self.last_seq}}))
                self.log.info("[%s] 已发送 RESUME（seq=%s）", self.bot_id, self.last_seq)
            else:
                ws.send(json.dumps({"op": 2, "d": {
                    "token": f"QQBot {token}", "intents": self._effective_intents}}))
                self.log.info("[%s] 已发送 IDENTIFY（intents=%s）", self.bot_id, self._effective_intents)
                self.session_id = None
                self.last_seq = 0
                self._should_resume = False
        except Exception as exc:
            self.log.error("[%s] 发送鉴权包失败: %s", self.bot_id, exc)

    # ------------------------------------------------------------------ 心跳
    def _ensure_heartbeat(self):
        if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, name=f"heartbeat-{self.bot_id}", daemon=True)
            self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        while self._running:
            time.sleep(self.heartbeat_interval)
            if not self.connected:
                continue
            timeout = max(self.heartbeat_interval * self.heartbeat_timeout_factor, 30)
            if self.last_heartbeat_ack and time.time() - self.last_heartbeat_ack > timeout:
                self.log.warning("[%s] 心跳超时（%.0f 秒无响应），主动断开触发重连", self.bot_id, timeout)
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:
                    pass
                continue
            try:
                self.ws.send(json.dumps({"op": 1, "d": None}))
            except Exception as exc:
                self.log.error("[%s] 发送心跳失败: %s", self.bot_id, exc)

    # ------------------------------------------------------------------ 事件解析
    def _handle_dispatch(self, data: Dict[str, Any]):
        event_type = data.get("t") or ""
        payload = data.get("d") or {}
        self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1
        self.last_event_type = event_type

        if event_type == "READY":
            self.session_id = payload.get("session_id")
            self._should_resume = True
            self._invalid_session_count = 0
            self.ready_at = time.time()
            self._fell_back = False
            self.log.info("[%s] READY，session_id=%s", self.bot_id, self.session_id)
            self._set_status(True, "")
            return

        if event_type in ("GROUP_MSG_RECEIVE", "GROUP_MSG_REJECT",
                          "C2C_MSG_RECEIVE", "C2C_MSG_REJECT"):
            self.log.info("[%s] 消息接收开关事件: %s", self.bot_id, event_type)
            self._emit({"type": "system", "event": event_type, "bot_id": self.bot_id, "raw": payload})
            return

        if event_type not in MESSAGE_EVENTS:
            self.log.debug("[%s] 忽略事件: %s", self.bot_id, event_type)
            return

        author = payload.get("author") or {}
        is_group = event_type in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE")
        is_channel = event_type == "DIRECT_MESSAGE_CREATE"
        msg_type = "group" if is_group else ("channel" if is_channel else "private")
        group_openid = payload.get("group_openid", "") if is_group else ""
        openid = author.get("user_openid") or author.get("member_openid", "")
        member_openid = author.get("member_openid") or author.get("user_openid", "")

        # 是否「在群里 @ 了机器人」：
        # 1) GROUP_AT_MESSAGE_CREATE 本身就等于「@ 了机器人」；
        # 2) 但机器人开启「接收所有消息」后，群消息一律以 GROUP_MESSAGE_CREATE 下发
        #    （官方文档：全量模式事件字段与 @ 事件完全一致），此时事件名不再是判断依据，
        #    必须看消息 mentions 里平台标注的 is_you。
        #    只认事件名曾导致「群里 @ 机器人不回复」（被“需要 @ 才回复”直接拦掉），
        #    而私聊没有这道闸门，所以私聊一切正常。
        at_by_event = event_type == "GROUP_AT_MESSAGE_CREATE"
        at_by_mention = is_group and self._mentions_self(payload)
        if event_type == "GROUP_MESSAGE_CREATE":
            if not self.all_message_mode:
                self.all_message_mode = True
                self.log.info("[%s] 检测到「接收所有消息」全量群消息事件（GROUP_MESSAGE_CREATE）："
                              "群里每条消息都会推送，@ 机器人改由消息 mentions 判断", self.bot_id)
        if is_group and at_by_mention and not at_by_event:
            self.log.debug("[%s] 全量群消息里检测到 @ 机器人（mentions.is_you）", self.bot_id)

        message = {
            "bot_id": self.bot_id,
            "direction": "in",
            "type": msg_type,
            "group_openid": group_openid,
            "openid": openid,
            "member_openid": member_openid,
            "unified_openid": openid,
            "username": author.get("username", ""),
            "content": payload.get("content", "") or "",
            "attachments": self._collect_attachments(payload),
            "quote": self._extract_quote(payload),
            "mentions": self._collect_mentions(payload),
            "msg_idx": self._extract_msg_idx(payload),
            "message_scene": payload.get("message_scene") or {},
            "msg_id": payload.get("id", "") or "",
            "timestamp": payload.get("timestamp", "") or "",
            "raw_event": json.dumps(payload, ensure_ascii=False)[:20000],
            "is_at_bot": bool(at_by_event or at_by_mention),
            "at_bot_source": "event" if at_by_event else ("mention" if at_by_mention else ""),
            "event": event_type,
        }
        self.log.info("[%s] 收到%s消息(%s) openid=%s @机器人=%s content=%.60s 附件=%d",
                      self.bot_id, {"group": "群聊", "private": "私聊"}.get(msg_type, "频道"),
                      event_type, openid, "是" if message["is_at_bot"] else "否",
                      message["content"], len(message["attachments"]))
        self._emit(message)

    def _emit(self, message: Dict[str, Any]):
        if not self.on_message:
            return
        try:
            self.on_message(message)
        except Exception as exc:
            self.log.error("[%s] 消息回调异常: %s", self.bot_id, exc)

    # ------------------------------------------------------------------ 字段收集
    @staticmethod
    def _collect_attachments(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """收集附件（兼容 attachments / images / media / medias 等字段）。"""
        out: List[Dict[str, Any]] = []
        for key in ("attachments", "images", "image", "medias", "media"):
            raw = payload.get(key)
            if isinstance(raw, dict):
                raw = [raw]
            elif isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list):
                continue
            for item in raw:
                if isinstance(item, str):
                    if item.lower().startswith("http"):
                        out.append({"url": item, "content_type": ""})
                    continue
                if not isinstance(item, dict):
                    continue
                entry = dict(item)
                if not entry.get("url") and isinstance(entry.get("image"), dict):
                    entry = dict(entry["image"])
                if entry.get("url") or entry.get("file_info") or entry.get("filename") or entry.get("file_name"):
                    out.append(entry)
        return out

    @classmethod
    def _extract_quote(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """提取“本条消息引用了哪条消息”。"""
        quote: Dict[str, Any] = {}
        for field in QUOTE_FIELDS:
            raw = payload.get(field)
            if isinstance(raw, str) and raw:
                quote.setdefault("msg_id", raw)
            elif isinstance(raw, dict) and raw:
                quote.setdefault("msg_id", raw.get("message_id") or raw.get("msg_id") or raw.get("id") or "")
                quote.setdefault("msg_idx", raw.get("msg_idx") or raw.get("ref_idx") or "")
                quote.setdefault("content", raw.get("content") or raw.get("text") or "")
                attachments = cls._collect_attachments(raw)
                if attachments:
                    quote.setdefault("attachments", attachments)
        for element in (payload.get("msg_elements") or []):
            if not isinstance(element, dict):
                continue
            if element.get("message_id") or element.get("msg_idx") or element.get("ref_idx"):
                quote.setdefault("msg_id", element.get("message_id") or "")
                quote.setdefault("msg_idx", element.get("msg_idx") or element.get("ref_idx") or "")
                quote.setdefault("content", element.get("content") or "")
        return {key: value for key, value in quote.items() if value}

    @staticmethod
    def _extract_msg_idx(payload: Dict[str, Any]) -> str:
        """本条消息自身的引用索引（便于以后引用它）。"""
        scene = payload.get("message_scene") or {}
        ext = scene.get("ext")
        items: List[str] = []
        if isinstance(ext, str):
            items = [ext]
        elif isinstance(ext, list):
            items = [str(part) for part in ext]
        for chunk in items:
            for pair in str(chunk).split("&"):
                if "=" not in pair:
                    continue
                key, _, value = pair.partition("=")
                if key.strip().lower() in ("msg_idx", "msgidx", "ref_idx", "refidx"):
                    return value.strip()
        return ""

    @staticmethod
    def _mentions_self(payload: Dict[str, Any]) -> bool:
        """判断这条群消息是否 @ 了机器人自己。

        「接收所有消息」（全量模式）打开后，群里每条消息都走 GROUP_MESSAGE_CREATE，
        @ 机器人不会再单独用 GROUP_AT_MESSAGE_CREATE 下发，事件名判断会失效；
        平台会在 mentions 里把被 @ 的机器人标成 is_you=true，
        而 @ 其他机器人/用户是 is_you=false（实测）。
        「@全体成员」虽然也被标成 is_you，但带 scope=all 且没有 id，
        不能算作 @ 了机器人本身（否则机器人会对每个 @全体成员 都应答）。
        """
        for item in payload.get("mentions") or []:
            if not isinstance(item, dict):
                continue
            if not item.get("is_you"):
                continue
            if str(item.get("scope") or "").strip().lower() == "all":
                continue
            if not (item.get("id") or item.get("member_openid") or item.get("user_openid")
                    or item.get("openid")):
                # 没有具体用户 id 的“@”只可能是 @全体成员
                continue
            return True
        return False

    @staticmethod
    def _collect_mentions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw = payload.get("mentions")
        if isinstance(raw, dict):
            raw = [raw]
        mentions = []
        for item in (raw or [])[:20]:
            if not isinstance(item, dict):
                continue
            openid = (item.get("member_openid") or item.get("id")
                      or item.get("user_openid") or item.get("openid") or "")
            if not openid:
                continue
            mentions.append({
                "openid": str(openid)[:200],
                "username": str(item.get("username") or "")[:60],
                "is_you": bool(item.get("is_you")),
                "is_bot": bool(item.get("bot") or item.get("is_you")),
            })
        return mentions
