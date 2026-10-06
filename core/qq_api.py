# -*- coding: utf-8 -*-
"""QQ 开放平台 API 客户端（每个机器人一个实例）

合并了：
- 原 `API_qqbot/core/qq_client.py` 的发送/指令面板/富媒体换取下载链接能力；
- 原 `app/app.py` 的 token 单飞刷新、指数退避重试、错误分类、图片链接多级降级策略。

设计要点：
- **按机器人实例化**：app_id / app_secret / 沙箱 / token 缓存全在实例内，互不干扰；
- 发送失败自动降级（引用字段被拒 → 去掉引用重发；富媒体附带文字被拒 → 去掉文字）；
- 图片链接支持四种来源：公网 URL 交给 QQ 拉取 / 本地已留存文件 / 本机地址由本服务先下载
  再上传 / 上传的 base64；
- 文件上传支持三档尝试，失败时给出可读的错误说明（平台权限不足时不会静默失败）。


**拆分说明（2026-10-06）**：上传相关逻辑已拆到 `core/qq_upload.py`（`QQUploadMixin`），
错误类型与分类拆到 `core/qq_errors.py`；本文件继续**再导出**这些名字，
老写法 `from core.qq_api import FILE_TYPE_IMAGE / is_param_error / ...` 照旧可用。
"""

import base64
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs
import requests

from core.qq_errors import (  # noqa: F401  （再导出，保持公开名字不变）
    TRANSIENT_HTTP_STATUS,
    TOKEN_INVALID_CODES,
    URL_UPLOAD_ERROR_HINTS,
    PARAM_ERROR_HINTS,
    TransientError,
    APIError,
    is_param_error,
    is_url_upload_error,
)
from core.qq_upload import (  # noqa: F401  （再导出，保持公开名字不变）
    FILE_TYPE_IMAGE,
    FILE_TYPE_VIDEO,
    FILE_TYPE_AUDIO,
    FILE_TYPE_FILE,
    IMAGE_EXTS,
    MIME_EXT,
    LOOPBACK_HOSTS,
    MEDIA_SOFT_LIMIT_MB,
    MEDIA_HARD_LIMIT_MB,
    CHUNKED_UPLOAD_THRESHOLD,
    CHUNK_FALLBACK_BLOCK_SIZE,
    CHUNK_MD5_HEAD_BYTES,
    VIDEO_EXTS,
    AUDIO_EXTS,
    _is_loopback,
    QQUploadMixin,
)


logger = logging.getLogger(__name__)


def _rfc3339(timestamp: float) -> str:
    """转成官方要求的时间格式，例如 2026-08-05T11:23:05+08:00（北京时间）。"""
    import datetime as _dt
    tz = _dt.timezone(_dt.timedelta(hours=8))
    return _dt.datetime.fromtimestamp(float(timestamp), tz).strftime("%Y-%m-%dT%H:%M:%S+08:00")


# ======================================================================================
# 群禁言状态（官方 GET restrict_chat_setting）解析
# ======================================================================================
MUTE_MODE_TEXT = {
    "none": "未开启全员禁言",
    "always": "全员禁言中（始终）",
    "schedule": "定时/周期禁言",
}
WEEKDAY_NAMES = "一二三四五六日"


def _beijing_tz():
    import datetime as _dt
    return _dt.timezone(_dt.timedelta(hours=8))


def _parse_rfc3339(value: Any) -> float:
    """把官方时间（2026-08-05T11:23:04+08:00）转成时间戳；解析不了返回 0。"""
    import datetime as _dt
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:                     # 没带时区就按北京时间理解
        parsed = parsed.replace(tzinfo=_beijing_tz())
    return parsed.timestamp()


def format_weekdays(days: Any) -> str:
    """[1,2,7] → 周一、周二、周日。"""
    out = []
    for day in (days or []):
        try:
            index = int(day)
        except (TypeError, ValueError):
            continue
        if 1 <= index <= 7:
            out.append("周" + WEEKDAY_NAMES[index - 1])
    return "、".join(out)


def _rule_text(rule: Dict[str, Any]) -> str:
    """把一条定时/周期规则说成人话（含启用状态）。"""
    if not isinstance(rule, dict):
        return ""
    enabled = bool(rule.get("enabled"))
    flag = "" if enabled else "（已停用）"
    if rule.get("weekdays") is not None:
        days = format_weekdays(rule.get("weekdays")) or "每天"
        return f"{days} {rule.get('start_time') or '--'}~{rule.get('end_time') or '--'}{flag}"
    start = str(rule.get("start_at") or "")
    end = str(rule.get("end_at") or "")
    return f"{start} ~ {end}{flag}"


def _in_schedule_now(now, schedule_rules: List[Any], recurring_rules: List[Any]) -> bool:
    """当前是否正处于定时/周期禁言时段内（按北京时间判断，周期规则支持跨天）。"""
    import datetime as _dt
    stamp = now.timestamp()
    for rule in (schedule_rules or []):
        if not isinstance(rule, dict) or not rule.get("enabled"):
            continue
        start = _parse_rfc3339(rule.get("start_at"))
        end = _parse_rfc3339(rule.get("end_at"))
        if start and end and start <= stamp <= end:
            return True
    weekday = now.isoweekday()
    minutes = now.hour * 60 + now.minute
    for rule in (recurring_rules or []):
        if not isinstance(rule, dict) or not rule.get("enabled"):
            continue
        days = []
        for day in (rule.get("weekdays") or []):
            try:
                days.append(int(day))
            except (TypeError, ValueError):
                continue
        if days and weekday not in days:
            continue
        try:
            start_h, start_m = str(rule.get("start_time") or "").split(":")
            end_h, end_m = str(rule.get("end_time") or "").split(":")
            start_at = int(start_h) * 60 + int(start_m)
            end_at = int(end_h) * 60 + int(end_m)
        except ValueError:
            continue
        if start_at <= end_at:
            if start_at <= minutes <= end_at:
                return True
        elif minutes >= start_at or minutes <= end_at:      # 跨天，例如 23:00~01:00
            return True
    return False


def normalize_mute_state(raw: Any) -> Dict[str, Any]:
    """整理官方禁言状态：全员禁言规则 + 成员级禁言列表。"""
    import datetime as _dt
    data = raw if isinstance(raw, dict) else {}
    global_rule = data.get("global_rule") if isinstance(data.get("global_rule"), dict) else {}
    mode = str(global_rule.get("mode") or "none").strip().lower() or "none"
    schedule_rules = [rule for rule in (global_rule.get("schedule_rules") or [])
                      if isinstance(rule, dict)]
    recurring_rules = [rule for rule in (global_rule.get("recurring_rules") or [])
                       if isinstance(rule, dict)]
    now = _dt.datetime.now(_beijing_tz())
    active_now = mode == "always" or (
        mode == "schedule" and _in_schedule_now(now, schedule_rules, recurring_rules))

    members: List[Dict[str, Any]] = []
    now_stamp = time.time()
    for item in (data.get("members") or []):
        if not isinstance(item, dict):
            continue
        openid = str(item.get("member_openid") or "").strip()
        if not openid:
            continue
        expire_at = str(item.get("mute_expire_at") or "")
        expire_ts = _parse_rfc3339(expire_at)
        members.append({
            "member_openid": openid,
            "username": str(item.get("username") or "").strip(),
            "union_openid": str(item.get("union_openid") or "").strip(),
            "mute_expire_at": expire_at,
            "expire_ts": expire_ts,
            "permanent": expire_ts <= 0,
            "remaining_seconds": max(0, int(expire_ts - now_stamp)) if expire_ts else 0,
            "expire_text": (_dt.datetime.fromtimestamp(expire_ts, _beijing_tz())
                            .strftime("%Y-%m-%d %H:%M:%S") if expire_ts else "永久"),
            "source": "platform",
        })

    texts = [text for text in (_rule_text(rule) for rule in schedule_rules + recurring_rules) if text]
    if mode == "always":
        text = "全员禁言中（始终）"
    elif mode == "schedule":
        text = "定时/周期禁言：" + ("；".join(texts) if texts else "已配置规则")
        if active_now:
            text += "（当前正处于禁言时段）"
        else:
            text += "（当前不在禁言时段）"
    else:
        text = "未开启全员禁言"
    return {
        "mode": mode,
        "mode_text": MUTE_MODE_TEXT.get(mode, mode),
        "text": text,
        "active_now": active_now,
        "schedule_rules": [dict(rule, text=_rule_text(rule)) for rule in schedule_rules],
        "recurring_rules": [dict(rule, text=_rule_text(rule)) for rule in recurring_rules],
        "members": members,
        "member_count": len(members),
        "checked_at": time.time(),
        "raw": data,
    }


class QQApiClient(QQUploadMixin):
    """一个 QQ 机器人的 HTTP API 客户端。"""


    def __init__(self, bot_id: str, app_id: str, app_secret: str, sandbox: bool = False,
                 config=None, logger_obj: logging.Logger = None):
        self.bot_id = bot_id
        self.app_id = app_id
        self.app_secret = app_secret
        self.sandbox = bool(sandbox)
        self.config = config
        self.log = logger_obj or logger

        self.token: Optional[str] = None
        self.token_expires_at = 0.0
        self._token_lock = threading.Lock()
        self._seq_lock = threading.Lock()
        self._seq = 0
        self.session = requests.Session()
        self.last_error = ""

    # ------------------------------------------------------------------ 基础
    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def api_base(self) -> str:
        return "https://sandbox.api.sgroup.qq.com" if self.sandbox else "https://api.bot.qq.com"

    @property
    def ws_gateway(self) -> str:
        return ("wss://sandbox.api.sgroup.qq.com/websocket" if self.sandbox
                else "wss://api.bot.qq.com/websocket")

    def _cfg_int(self, *keys, default=0) -> int:
        if self.config is None:
            return default
        return self.config.int_of(*keys, default=default)

    def _cfg_float(self, *keys, default=0.0) -> float:
        if self.config is None:
            return default
        return self.config.float_of(*keys, default=default)

    def _cfg_str(self, *keys, default="") -> str:
        if self.config is None:
            return default
        return self.config.str_of(*keys, default=default)

    def next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    # ------------------------------------------------------------------ token
    def get_access_token(self, force_refresh: bool = False) -> Optional[str]:
        """获取 access_token（带缓存；同一时刻只允许一个线程刷新）。"""
        buffer_seconds = self._cfg_float("send", "token_refresh_buffer_seconds", default=60)
        with self._token_lock:
            if (not force_refresh and self.token
                    and self.token_expires_at > time.time() + buffer_seconds):
                return self.token
            return self._fetch_token()

    def _fetch_token(self) -> Optional[str]:
        """请求新 token（调用方必须持有 _token_lock）。"""
        if not self.configured:
            self.last_error = "未配置 app_id / app_secret"
            return None
        url = f"{self.api_base}/app/getAppAccessToken"
        payload = {"appId": self.app_id, "clientSecret": self.app_secret}
        retries = max(1, self._cfg_int("send", "max_retries", default=3))
        backoff = self._cfg_float("send", "retry_backoff_factor", default=1.0)
        for attempt in range(retries):
            try:
                response = self.session.post(url, json=payload, timeout=min(10.0, self._cfg_float(
                    "send", "request_timeout_seconds", default=30)))
                if response.status_code in TRANSIENT_HTTP_STATUS:
                    raise TransientError(f"HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise APIError(f"HTTP {response.status_code}: {response.text[:200]}")
                try:
                    result = response.json()
                except ValueError:
                    raise TransientError(f"响应不是合法 JSON: {response.text[:200]}")
                data = result.get("data") if isinstance(result.get("data"), dict) else result
                token = (data or {}).get("access_token")
                if token:
                    expires_in = int((data or {}).get("expires_in", 7200))
                    self.token = token
                    self.token_expires_at = time.time() + expires_in
                    self.last_error = ""
                    self.log.info("[%s] Access Token 获取成功（有效期 %s 秒）", self.bot_id, expires_in)
                    return token
                if result.get("code") in TOKEN_INVALID_CODES:
                    raise APIError(f"凭据无效: {result}")
                raise TransientError(f"返回异常: {result}")
            except APIError as exc:
                self.last_error = str(exc)
                self.log.error("[%s] 获取 Token 失败: %s", self.bot_id, exc)
                return None
            except Exception as exc:
                self.last_error = str(exc)
                self.log.warning("[%s] 获取 Token 异常（%d/%d）: %s", self.bot_id, attempt + 1, retries, exc)
                if attempt < retries - 1:
                    time.sleep(backoff * (2 ** attempt))
        return None

    def token_remaining(self) -> int:
        return max(0, int(self.token_expires_at - time.time()))

    # ------------------------------------------------------------------ 请求
    def request(self, method: str, url: str, json_data: Dict[str, Any] = None,
                params: Dict[str, Any] = None, retries: int = None) -> Dict[str, Any]:
        """带重试与 token 自动刷新的请求。成功返回解析后的 JSON。"""
        max_retries = int(retries or self._cfg_int("send", "max_retries", default=3))
        backoff = self._cfg_float("send", "retry_backoff_factor", default=1.0)
        timeout = self._cfg_float("send", "request_timeout_seconds", default=30)
        last_exception: Optional[Exception] = None

        for attempt in range(max_retries):
            token = self.get_access_token()
            if not token:
                raise APIError(f"无法获取 Access Token（{self.last_error or '凭据未配置'}）")
            headers = {
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json; charset=utf-8",
            }
            try:
                if method.upper() == "GET":
                    response = self.session.get(url, headers=headers, params=params, timeout=timeout)
                elif method.upper() == "DELETE":
                    response = self.session.delete(url, headers=headers, json=json_data, timeout=timeout)
                elif method.upper() == "PUT":
                    response = self.session.put(url, headers=headers, json=json_data, timeout=timeout)
                else:
                    response = self.session.post(url, headers=headers, json=json_data, timeout=timeout)

                if response.status_code == 401:
                    self.log.warning("[%s] 收到 401，强制刷新 Token 后重试", self.bot_id)
                    self.get_access_token(force_refresh=True)
                    raise TransientError("HTTP 401")
                if response.status_code in TRANSIENT_HTTP_STATUS:
                    raise TransientError(f"HTTP {response.status_code}: {response.text[:200]}")
                if response.status_code >= 400:
                    raise APIError(f"HTTP {response.status_code}: {response.text[:300]}")

                try:
                    result = response.json()
                except ValueError:
                    raise TransientError(f"响应不是合法 JSON: {response.text[:200]}")
                if not isinstance(result, dict):
                    return {"data": result}

                code = result.get("code")
                if code == 0 or code is None:
                    return result
                if code in TOKEN_INVALID_CODES:
                    self.get_access_token(force_refresh=True)
                    raise TransientError(f"业务错误码 {code}")
                raise APIError(f"API 错误 {code}: {result.get('message') or result}")
            except APIError:
                raise
            except Exception as exc:
                last_exception = exc
                self.last_error = str(exc)
                self.log.warning("[%s] 请求失败（%d/%d）%s: %s",
                                 self.bot_id, attempt + 1, max_retries, method.upper(), exc)
                if attempt < max_retries - 1:
                    time.sleep(backoff * (2 ** attempt))
        raise APIError(f"请求失败，已重试 {max_retries} 次：{last_exception}")

    # ------------------------------------------------------------------ 发送文本
    def _messages_url(self, target_type: str, openid: str) -> str:
        scope = "groups" if target_type == "group" else "users"
        return f"{self.api_base}/v2/{scope}/{openid}/messages"

    def _files_url(self, target_type: str, openid: str) -> str:
        scope = "groups" if target_type == "group" else "users"
        return f"{self.api_base}/v2/{scope}/{openid}/files"

    def _attach_reply(self, payload: Dict[str, Any], reply_msg_id: str,
                      style: str = ""):
        """按配置附加引用字段。

        ⚠️ 关键区别（很容易踩坑）：
        - `msg_id` + `msg_seq` 是**被动回复**：只有回复"刚收到的那条消息"才有效，
          过期后平台会直接拒绝整个请求；
        - `message_reference` 是**引用**：用它可以引用更早的消息。

        以前两种情况都带 `msg_id`，于是"引用一条两分钟前的消息"会被平台拒绝，
        我们的兜底又把引用一起删掉，看起来就像"超过两分钟不能引用"。
        现在由调用方通过 `style` 指定：老消息只用 `message_reference`。
        """
        if not reply_msg_id:
            return
        style = (style or self._cfg_str("send", "reply_style", default="both")).lower()
        if style not in ("both", "msg_id", "message_reference"):
            style = "both"
        if style in ("both", "msg_id"):
            payload["msg_id"] = reply_msg_id
            payload["msg_seq"] = self.next_seq()
        if style in ("both", "message_reference"):
            payload["message_reference"] = {
                "message_id": reply_msg_id,
                "ignore_get_message_error": True,
            }

    @staticmethod
    def extract_message_id(result: Dict[str, Any]) -> str:
        """从发送接口的响应里取出消息 ID（撤回消息时必须用到它）。

        QQ 的响应结构在不同接口/版本下不完全一致：`id` 可能出现在顶层、
        也可能在 `data` 里，字段名还可能是 `msg_id`。这里全部兼容，
        避免"发出去的消息没有 ID、事后无法撤回"。
        """
        if not isinstance(result, dict):
            return ""
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        for source in (data, result):
            for key in ("id", "msg_id", "message_id"):
                value = source.get(key)
                if value:
                    return str(value)
        return ""

    def send_text(self, target_type: str, openid: str, content: str,
                  reply_msg_id: str = "", msg_type: int = 0,
                  reply_style: str = "") -> Dict[str, Any]:
        """发送文本消息；引用尽量保留（被拒时逐级降级）。

        引用降级顺序：`both`（msg_id + 引用）→ 只留 `message_reference` → 完全不带引用。
        这样"引用一条较早的消息"不会因为 `msg_id` 过期而丢掉引用。

        返回值里总会带上 `id`（能取到时），供"撤回消息"使用。
        """
        payload: Dict[str, Any] = {"content": content, "msg_type": msg_type}
        self._attach_reply(payload, reply_msg_id, reply_style)
        url = self._messages_url(target_type, openid)
        notes: List[str] = []
        try:
            result = self.request("POST", url, json_data=payload)
        except Exception as exc:
            if not (reply_msg_id and is_param_error(exc)):
                raise
            # 第一步：去掉被动回复字段（msg_id 过期最常见），保留引用再试
            if "msg_id" in payload:
                payload.pop("msg_id", None)
                payload.pop("msg_seq", None)
                self.log.warning("[%s] 被动回复字段被拒（可能超过被动回复时限），改用「引用」重发",
                                 self.bot_id)
                try:
                    result = self.request("POST", url, json_data=payload)
                    notes.append("已改为「引用」方式（被动回复字段被平台拒绝）")
                except Exception as exc2:
                    if not is_param_error(exc2):
                        raise
                    exc = exc2
                else:
                    return self._text_result(result, notes)
            # 第二步：引用也不被接受 → 去掉引用，至少把消息发出去
            if "message_reference" in payload:
                payload.pop("message_reference", None)
                self.log.warning("[%s] 引用被平台拒绝，去掉引用后重发", self.bot_id)
                result = self.request("POST", url, json_data=payload)
                notes.append("引用未生效（被平台拒绝），消息已按普通消息发出")
            else:
                raise exc
        return self._text_result(result, notes)

    def _text_result(self, result: Dict[str, Any], notes: List[str]) -> Dict[str, Any]:
        info = result.get("data") if isinstance(result.get("data"), dict) else {}
        if not isinstance(info, dict):
            info = {}
        message_id = self.extract_message_id(result)
        if message_id:
            info = dict(info)
            info["id"] = message_id
        elif self.log:
            self.log.warning("[%s] 发送成功但响应里没有消息 ID，这条消息将无法撤回：%s",
                             self.bot_id, json.dumps(result, ensure_ascii=False)[:200])
        note = result.get("_note") or ""
        if notes:
            note = "；".join(item for item in [note, "；".join(notes)] if item)
        if note:
            info["_note"] = note
        return info

    # ------------------------------------------------------------------ 指令面板
    # 官方接口（按文档核对的请求格式）：
    #   POST /v2/panels             创建
    #   PUT  /v2/panels/{panel_id}  修改
    #   GET  /v2/panels?scope=...   查询列表（scope 必填，分页）
    #   DELETE /v2/panels/{id}      删除
    #   PUT  /v2/panels/{id}/target 修改关联对象（specific 必须单独调此接口）
    # 请求体：{"scope","target_type","panel":{"items":[...],"remark":...}}
    #         target_type=specific 时另带 user_openids / group_openids

    @staticmethod
    def normalize_panel_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按官方要求规范化面板指令项。

        - `command` 的 `name` **不能带开头的 `/`**（用户点击后内容填入输入框）；
        - `name` 最长 14 字符、`desc` 最长 30 字符（超出会被平台判为参数错误）；
        - `link` 类型必须有 `link`，且不能带 `desc`；
        - 丢掉既没有名字也无效的项，避免整个请求被拒绝。
        """
        out: List[Dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "command").strip().lower()
            if kind not in ("command", "link"):
                kind = "command"
            name = str(item.get("name") or "").strip()
            if kind == "command":
                name = name.lstrip("/").strip()
            if not name:
                continue
            name = name[:14]
            if kind == "link":
                link = str(item.get("link") or "").strip()
                if not link:
                    continue
                entry: Dict[str, Any] = {"type": "link", "name": name, "link": link}
            else:
                entry = {"type": "command", "name": name,
                         "desc": str(item.get("desc") or "").strip()[:30]}
            if item.get("only_admin"):
                entry["only_admin"] = True
            out.append(entry)
        return out

    def _panel_payload(self, scope: str, target_type: str, items: List[Dict[str, Any]],
                       remark: str = "", openids: List[str] = None) -> Dict[str, Any]:
        panel: Dict[str, Any] = {"items": self.normalize_panel_items(items)}
        if remark:
            panel["remark"] = str(remark)[:100]
        payload: Dict[str, Any] = {"scope": scope, "target_type": target_type, "panel": panel}
        if target_type == "specific":
            cleaned = [str(x).strip() for x in (openids or []) if str(x).strip()]
            if cleaned:
                payload["user_openids" if scope == "c2c" else "group_openids"] = cleaned
        return payload

    def list_command_panels(self, scope: str = "c2c", with_raw: bool = False):
        """查询指令面板列表（scope 必填；自动翻页）。返回面板列表或 (列表, 原始响应)。"""
        if scope not in ("c2c", "group"):
            return ([], "") if with_raw else []
        url = f"{self.api_base}/v2/panels"
        collected: List[Dict[str, Any]] = []
        raw_text = ""
        next_key = ""
        try:
            for _page in range(1, 21):
                params: Dict[str, Any] = {"scope": scope, "limit": 100}
                if next_key:
                    params["next_key"] = next_key
                result = self.request("GET", url, params=params, retries=2)
                raw_text = json.dumps(result, ensure_ascii=False)
                page = None
                for key in ("records", "panels", "panel_list", "list"):
                    if isinstance(result.get(key), list):
                        page = result[key]
                        break
                if page is None:
                    data = result.get("data")
                    if isinstance(data, list):
                        page = data
                    elif isinstance(data, dict):
                        for key in ("records", "panels", "panel_list", "list"):
                            if isinstance(data.get(key), list):
                                page = data[key]
                                break
                        next_key = data.get("next_key") or ""
                    else:
                        next_key = ""
                else:
                    next_key = result.get("next_key") or ""
                if page:
                    collected.extend(item for item in page if isinstance(item, dict))
                if not next_key or result.get("is_end"):
                    break
        except Exception as exc:
            self.log.warning("[%s] 查询指令面板列表失败(%s)：%s", self.bot_id, scope, exc)
            return (collected, f"{exc}") if with_raw else collected
        return (collected, raw_text) if with_raw else collected

    def create_command_panel(self, scope: str, target_type: str, items: List[Dict[str, Any]],
                             remark: str = "", openids: List[str] = None) -> Optional[str]:
        """创建指令面板，成功返回 panel_id。"""
        payload = self._panel_payload(scope, target_type, items, remark, openids)
        if not payload["panel"]["items"]:
            self.log.warning("[%s] 指令列表为空，跳过创建面板", self.bot_id)
            return None
        if target_type == "specific" and not any(
                key in payload for key in ("user_openids", "group_openids")):
            self.log.warning("[%s] %s 面板设为 specific 但没填关联对象，QQ 里不会展示",
                             self.bot_id, scope)
        try:
            result = self.request("POST", f"{self.api_base}/v2/panels", json_data=payload)
        except Exception as exc:
            self.log.error("[%s] 创建指令面板失败(%s)：%s", self.bot_id, scope, exc)
            self.log.error("[%s] 请求体：%s", self.bot_id,
                           json.dumps(payload, ensure_ascii=False)[:500])
            return None
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        panel_id = (data or {}).get("panel_id") or (data or {}).get("id")
        if panel_id:
            self.log.info("[%s] 指令面板创建成功(%s/%s) panel_id=%s",
                          self.bot_id, scope, target_type, panel_id)
        return panel_id

    def update_command_panel(self, panel_id: str, scope: str, target_type: str,
                             items: List[Dict[str, Any]], remark: str = "",
                             openids: List[str] = None) -> bool:
        """更新已有指令面板。"""
        if not panel_id:
            return False
        payload = self._panel_payload(scope, target_type, items, remark, openids)
        if not payload["panel"]["items"]:
            return False
        try:
            self.request("PUT", f"{self.api_base}/v2/panels/{panel_id}", json_data=payload)
            return True
        except Exception as exc:
            self.log.warning("[%s] 更新指令面板失败(%s/%s)：%s", self.bot_id, scope, panel_id, exc)
            return False

    def delete_command_panel(self, panel_id: str) -> bool:
        if not panel_id:
            return False
        try:
            self.request("DELETE", f"{self.api_base}/v2/panels/{panel_id}")
            self.log.info("[%s] 指令面板已删除：%s", self.bot_id, panel_id)
            return True
        except Exception as exc:
            self.log.error("[%s] 删除指令面板失败：%s", self.bot_id, exc)
            return False

    def set_panel_targets(self, panel_id: str, scope: str, openids: List[str],
                          op: str = "add") -> bool:
        """修改面板关联对象（官方要求 specific 面板必须单独调此接口建立关联）。"""
        if not panel_id:
            return False
        cleaned = [str(x).strip() for x in (openids or []) if str(x).strip()]
        if not cleaned:
            return False
        payload: Dict[str, Any] = {"op": op if op in ("add", "del") else "add"}
        payload["user_openids" if scope == "c2c" else "group_openids"] = cleaned
        try:
            self.request("PUT", f"{self.api_base}/v2/panels/{panel_id}/target", json_data=payload)
            self.log.info("[%s] 指令面板关联%s成功(%s, %d 个对象)",
                          self.bot_id, payload["op"], scope, len(cleaned))
            return True
        except Exception as exc:
            self.log.error("[%s] 指令面板关联%s失败(%s)：%s", self.bot_id, payload["op"], scope, exc)
            return False

    # ------------------------------------------------------------------ 群信息 / 成员
    def group_info(self, group_openid: str) -> Dict[str, Any]:
        result = self.request("GET", f"{self.api_base}/v2/groups/{group_openid}/info")
        return result.get("data") if isinstance(result.get("data"), dict) else result

    def group_members(self, group_openid: str, limit: int = 200) -> List[Dict[str, Any]]:
        """拉取群成员列表（官方游标分页，每页最多 30；该能力可能需平台白名单）。

        平台未开放时抛 APIError（常见 11253 仅白名单机器人可用），由上层提示并可退回
        “从历史消息识别到的成员”。
        """
        url = f"{self.api_base}/v2/groups/{group_openid}/members"
        members: List[Dict[str, Any]] = []
        cursor = ""
        for _page in range(1, 21):
            params: Dict[str, Any] = {"limit": 30}
            if cursor:
                params["cursor"] = cursor
            result = self.request("GET", url, params=params)
            data = result.get("data") if isinstance(result.get("data"), dict) else result
            page = None
            if isinstance(data, dict):
                page = data.get("members")
            if page is None and isinstance(result.get("members"), list):
                page = result["members"]
            if not page:
                break
            members.extend(item for item in page if isinstance(item, dict))
            cursor = (data or {}).get("next_cursor") if isinstance(data, dict) else ""
            if not cursor or len(members) >= limit:
                break
        return members

    def group_member(self, group_openid: str, member_openid: str) -> Dict[str, Any]:
        url = f"{self.api_base}/v2/groups/{group_openid}/members/{member_openid}"
        result = self.request("GET", url)
        return result.get("data") if isinstance(result.get("data"), dict) else result

    # ------------------------------------------------------------------ 禁言（官方接口）
    def bot_state(self, group_openid: str) -> Dict[str, Any]:
        """查询机器人在群里的身份与消息接收设置。

        返回字段（官方）：`member_role`(member/owner/admin)、`allow_proactive_msg`、
        `recv_msg_setting`(all/only_mention/mention_and_context)。
        `member_role != member` 是调用禁言接口的硬前提。
        """
        url = f"{self.api_base}/v2/groups/{group_openid}/bot_state"
        result = self.request("GET", url)
        return result.get("data") if isinstance(result.get("data"), dict) else result

    def restrict_chat_setting(self, group_openid: str,
                              members: List[Dict[str, str]]) -> Dict[str, Any]:
        """群成员级禁言（官方接口）。

        `POST /v2/groups/{group_openid}/restrict_chat_setting`
        请求体：`{"members": [{"op": "add|update|del", "member_openid": "...",
                              "mute_expire_at": "2026-08-05T11:23:05+08:00"}]}`

        官方约束：
        - **机器人必须拥有群管理员身份**，最大禁言时长 30 天；
        - 只能操作普通成员（不能对群主/管理员/机器人操作）；
        - 单次最多 20 个成员。
        """
        if not members:
            raise APIError("禁言成员列表为空")
        if len(members) > 20:
            raise APIError("单次最多操作 20 个成员")
        url = f"{self.api_base}/v2/groups/{group_openid}/restrict_chat_setting"
        result = self.request("POST", url, json_data={"members": members})
        return result.get("data") if isinstance(result.get("data"), dict) else result

    def mute_member(self, group_openid: str, member_openid: str, seconds: int = 600,
                    op: str = "add") -> Dict[str, Any]:
        """禁言一个成员（秒数上限 30 天；op=del 时立即解除）。"""
        seconds = int(seconds or 0)
        max_seconds = 30 * 86400
        if seconds > max_seconds:
            seconds = max_seconds
        if op == "del":
            expire = ""
        else:
            expire = _rfc3339(time.time() + max(1, seconds))
        return self.restrict_chat_setting(group_openid, [{
            "op": op, "member_openid": member_openid, "mute_expire_at": expire,
        }])

    def unmute_member(self, group_openid: str, member_openid: str) -> Dict[str, Any]:
        """解除成员禁言（op=del + 空 mute_expire_at）。"""
        return self.restrict_chat_setting(group_openid, [{
            "op": "del", "member_openid": member_openid, "mute_expire_at": "",
        }])

    def list_muted_members(self, group_openid: str) -> Dict[str, Any]:
        """查询群禁言状态（官方 `GET /v2/groups/{gid}/restrict_chat_setting`）。

        官方返回两段内容，都已整理成页面直接可用的结构：
        - `global_rule`：群级（全员）禁言规则，mode = none / always / schedule，
          schedule 时带定时规则 `schedule_rules` 与周期规则 `recurring_rules`；
        - `members`：**当前真正处于禁言中的成员**（不含已过期），带昵称 `username`
          与到期时间 `mute_expire_at`。

        注意：**机器人必须是群管理员**，否则平台会返回无权限错误（由上层展示原始错误）。
        """
        url = f"{self.api_base}/v2/groups/{group_openid}/restrict_chat_setting"
        result = self.request("GET", url)
        raw = result.get("data") if isinstance(result.get("data"), dict) else result
        state = normalize_mute_state(raw)
        self.log.debug("[%s] 群 %s 禁言状态：%s（成员禁言 %d 人）", self.bot_id, group_openid,
                       state.get("text"), len(state.get("members") or []))
        return state

    # ------------------------------------------------------------------ 撤回消息
    # 官方接口（两个都只允许撤回"2 分钟内"的消息，成功返回 HTTP 200 且无响应体）：
    #   DELETE /v2/groups/{group_openid}/messages/{message_id}
    #   DELETE /v2/users/{user_openid}/messages/{message_id}
    # 错误码：
    #   40061001 请求参数无效（检查参数格式）
    #   40062003 无操作权限（机器人不是群管理员，或撤回的不是自己的消息）
    #   40064004 已超出消息撤回时限（超过 2 分钟）
    #   50065001 撤回失败，请稍后重试（单聊）
    #   306009   用户 openid 无效
    #   40061002 msgid 无效
    def _recall_url(self, scope: str, openid: str, message_id: str) -> str:
        if scope == "group":
            return f"{self.api_base}/v2/groups/{openid}/messages/{message_id}"
        return f"{self.api_base}/v2/users/{openid}/messages/{message_id}"

    def recall_group_message(self, group_openid: str, message_id: str) -> Dict[str, Any]:
        """撤回群消息（机器人是管理员时可撤回成员消息；普通成员只能撤自己的）。"""
        url = self._recall_url("group", group_openid, message_id)
        result = self.request("DELETE", url)
        return result.get("data") if isinstance(result.get("data"), dict) else result

    def recall_private_message(self, user_openid: str, message_id: str) -> Dict[str, Any]:
        """撤回机器人发给该用户的单聊消息。"""
        url = self._recall_url("c2c", user_openid, message_id)
        result = self.request("DELETE", url)
        return result.get("data") if isinstance(result.get("data"), dict) else result

    def recall_message(self, group_openid: str, message_id: str) -> Dict[str, Any]:
        """兼容旧调用：按群消息撤回处理。"""
        return self.recall_group_message(group_openid, message_id)
