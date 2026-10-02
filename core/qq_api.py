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

logger = logging.getLogger(__name__)

TRANSIENT_HTTP_STATUS = {429, 500, 502, 503, 504}
TOKEN_INVALID_CODES = {40001, 40014, 40002}
URL_UPLOAD_ERROR_HINTS = ("上传URL错误", "40093010")
PARAM_ERROR_HINTS = ("请求数据异常", "40011000")

# 富媒体类型（file_type）
FILE_TYPE_IMAGE = 1          # 图片 / 表情包
FILE_TYPE_VIDEO = 2
FILE_TYPE_AUDIO = 3
FILE_TYPE_FILE = 4           # 普通文件（机器人平台对文件消息支持有限，失败会给出说明）

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MIME_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp",
}
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]")

# ------------------------------------------------------------------ 富媒体大小限制（官方文档）
# 1=图片(png/jpg) 软 20MB / 硬 200MB；2=视频(mp4) 软 30MB / 硬 200MB；
# 3=语音(silk) 软 20MB / 硬 200MB；4=文件 软 200MB / 硬 200MB。
# 超过软限制会降级为"文件"类型上传，超过硬限制平台直接报错（850031）。
MEDIA_SOFT_LIMIT_MB = {FILE_TYPE_IMAGE: 20, FILE_TYPE_VIDEO: 30, FILE_TYPE_AUDIO: 20,
                       FILE_TYPE_FILE: 200}
MEDIA_HARD_LIMIT_MB = 200
# 超过这个大小改用官方推荐的"分片上传"（小文件仍走一次上传，省事也更快）
CHUNKED_UPLOAD_THRESHOLD = 4 * 1024 * 1024
CHUNK_FALLBACK_BLOCK_SIZE = 10 * 1024 * 1024      # 服务端没给 block_size 时的兜底
CHUNK_MD5_HEAD_BYTES = 10002432                   # 官方 md5_10m：文件前 10002432 字节
VIDEO_EXTS = {".mp4"}
AUDIO_EXTS = {".silk", ".slk", ".amr", ".mp3", ".m4a", ".wav", ".ogg"}



class TransientError(Exception):
    """临时错误（网络抖动 / 5xx / 429 / token 失效），可重试。"""


class APIError(Exception):
    """永久错误（参数错误、业务错误码），重试无意义。"""


class QQApiClient:
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

    # ------------------------------------------------------------------ 富媒体上传/发送
    def upload_media(self, target_type: str, openid: str, file_type: int = FILE_TYPE_IMAGE,
                     url: str = "", file_data: str = "", file_name: str = "",
                     srv_send_msg: bool = False) -> Tuple[str, Dict[str, Any]]:
        """上传富媒体，返回 (file_info, 原始数据)。"""
        payload: Dict[str, Any] = {"file_type": int(file_type), "srv_send_msg": bool(srv_send_msg)}
        if url:
            payload["url"] = url
        if file_data:
            payload["file_data"] = file_data
            if file_name:
                payload["file_name"] = file_name
        result = self.request("POST", self._files_url(target_type, openid), json_data=payload)
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        file_info = (data or {}).get("file_info") or ""
        if not file_info:
            raise APIError(f"上传富媒体失败，未返回 file_info: {str(result)[:300]}")
        return file_info, data or {}

    def send_media(self, target_type: str, openid: str, file_info: str, content: str = "",
                   reply_msg_id: str = "", reply_style: str = "") -> Dict[str, Any]:
        """发送富媒体消息（msg_type=7）；附带文字/引用被拒时逐级降级重发。"""
        payload: Dict[str, Any] = {"msg_type": 7, "media": {"file_info": file_info}}
        if content:
            payload["content"] = content
        self._attach_reply(payload, reply_msg_id, reply_style)
        url = self._messages_url(target_type, openid)
        notes: List[str] = []
        try:
            result = self.request("POST", url, json_data=payload)
        except Exception as exc:
            if not is_param_error(exc):
                raise
            if "content" in payload:
                payload.pop("content", None)
                notes.append("附带文字未生效")
                try:
                    result = self.request("POST", url, json_data=payload)
                except Exception as exc2:
                    exc = exc2
                    if not is_param_error(exc2):
                        raise
                else:
                    return self._media_result(result, notes)
            # 先只去掉被动回复字段（保留引用），再考虑去掉引用
            if "msg_id" in payload:
                payload.pop("msg_id", None)
                payload.pop("msg_seq", None)
                notes.append("已改为「引用」方式（被动回复字段被拒）")
                try:
                    result = self.request("POST", url, json_data=payload)
                    return self._media_result(result, notes)
                except Exception as exc3:
                    if not is_param_error(exc3):
                        raise
            if "message_reference" in payload:
                payload.pop("message_reference", None)
                notes.append("引用未生效")
                result = self.request("POST", url, json_data=payload)
                return self._media_result(result, notes)
            raise
        return self._media_result(result, notes)

    def _media_result(self, result: Dict[str, Any], notes: List[str]) -> Dict[str, Any]:
        """整理富媒体发送结果（统一带上消息 ID，便于事后撤回）。"""
        info = result.get("data") if isinstance(result.get("data"), dict) else {}
        info = dict(info) if isinstance(info, dict) else {}
        message_id = self.extract_message_id(result)
        if message_id:
            info["id"] = message_id
        if notes:
            info["_note"] = "；".join(notes)
        return info

    def upload_with_fallback(self, target_type: str, openid: str, file_type: int,
                             file_data: str = "", url: str = "", file_name: str = "") -> Tuple[str, Dict[str, Any]]:
        """上传并在参数被拒时去掉 file_name 重试（兼容部分接口版本）。"""
        try:
            return self.upload_media(target_type, openid, file_type, url=url,
                                     file_data=file_data, file_name=file_name)
        except Exception as exc:
            if file_name and is_param_error(exc):
                self.log.warning("[%s] 上传带 file_name 被拒，去掉该字段重试", self.bot_id)
                return self.upload_media(target_type, openid, file_type, url=url, file_data=file_data)
            raise

    # ------------------------------ 大文件：官方分片上传 ------------------------------
    def _upload_scope_url(self, target_type: str, openid: str, action: str) -> str:
        scope = "groups" if target_type == "group" else "users"
        return f"{self.api_base}/v2/{scope}/{openid}/{action}"

    @staticmethod
    def guess_file_type(file_name: str, default: int = FILE_TYPE_FILE) -> int:
        """按扩展名猜官方 file_type（图片 1 / 视频 2 / 语音 3 / 文件 4）。"""
        ext = os.path.splitext(str(file_name or ""))[1].lower()
        if ext in IMAGE_EXTS:
            return FILE_TYPE_IMAGE
        if ext in VIDEO_EXTS:
            return FILE_TYPE_VIDEO
        if ext in AUDIO_EXTS:
            return FILE_TYPE_AUDIO
        return default

    @staticmethod
    def _hash_summary(blob: bytes) -> Dict[str, str]:
        """官方分片上传需要的校验值：整文件 MD5/SHA1 + 前 10MB 的 MD5。"""
        import hashlib
        return {
            "md5": hashlib.md5(blob).hexdigest(),
            "sha1": hashlib.sha1(blob).hexdigest(),
            "md5_10m": hashlib.md5(blob[:CHUNK_MD5_HEAD_BYTES]).hexdigest(),
        }

    def upload_prepare(self, target_type: str, openid: str, file_type: int,
                       blob: bytes, file_name: str) -> Dict[str, Any]:
        """分片上传第一步：拿 upload_id 与各分片的预签名 URL。"""
        sums = self._hash_summary(blob)
        payload = {
            "file_type": int(file_type),
            "file_size": str(len(blob)),
            "file_name": file_name or "file",
            "md5": sums["md5"],
            "sha1": sums["sha1"],
            "md5_10m": sums["md5_10m"],
        }
        result = self.request("POST", self._upload_scope_url(target_type, openid, "upload_prepare"),
                              json_data=payload)
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        if not isinstance(data, dict) or not data.get("upload_id"):
            raise APIError(f"申请分片上传失败：{str(result)[:300]}")
        return data

    def _put_chunk(self, presigned_url: str, chunk: bytes, retries: int = 3,
                   delay: float = 1.0, timeout: float = 120.0) -> None:
        """把一片数据 PUT 到预签名地址（这个地址不能用带 token 的请求头）。"""
        last_error = ""
        for attempt in range(max(1, int(retries))):
            try:
                response = requests.put(presigned_url, data=chunk, timeout=timeout,
                                        headers={"Content-Type": "application/octet-stream"})
                if response.status_code in (200, 201, 204):
                    return
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            except Exception as exc:                     # 网络异常也要重试
                last_error = str(exc)
            if attempt < max(1, int(retries)) - 1:
                time.sleep(max(0.0, delay) * (attempt + 1))
        raise APIError(f"分片上传失败：{last_error}")

    def upload_part_finish(self, target_type: str, openid: str, upload_id: str, part_index: int,
                           block_size: int, md5: str) -> Dict[str, Any]:
        """分片上传第二步：通知服务端某一片已完成。"""
        payload = {"upload_id": upload_id, "part_index": int(part_index),
                   "block_size": str(int(block_size)), "md5": md5}
        return self.request("POST", self._upload_scope_url(target_type, openid, "upload_part_finish"),
                            json_data=payload)

    def upload_by_parts(self, target_type: str, openid: str, blob: bytes, file_type: int,
                        file_name: str) -> Tuple[str, Dict[str, Any]]:
        """官方推荐的大文件上传：upload_prepare → 逐片 PUT → upload_part_finish → 合并。

        支持到官方硬限制 200MB；返回 (file_info, 服务端原始数据)。
        """
        import hashlib
        from concurrent.futures import ThreadPoolExecutor
        if not blob:
            raise APIError("文件内容为空")
        hard = MEDIA_HARD_LIMIT_MB * 1024 * 1024
        if len(blob) > hard:
            raise APIError(f"文件过大（{len(blob) / 1048576:.1f} MB），"
                           f"QQ 平台硬限制为 {MEDIA_HARD_LIMIT_MB} MB")

        prepared = self.upload_prepare(target_type, openid, file_type, blob, file_name)
        upload_id = str(prepared.get("upload_id") or "")
        try:
            block_size = int(str(prepared.get("block_size") or CHUNK_FALLBACK_BLOCK_SIZE))
        except (TypeError, ValueError):
            block_size = CHUNK_FALLBACK_BLOCK_SIZE
        if block_size <= 0:
            block_size = CHUNK_FALLBACK_BLOCK_SIZE
        config = prepared.get("upload_config") if isinstance(prepared.get("upload_config"), dict) else {}
        try:
            concurrency = max(1, min(4, int(config.get("concurrency") or 1)))
        except (TypeError, ValueError):
            concurrency = 1
        try:
            retry_timeout = max(5, int(config.get("retry_timeout") or 300))
        except (TypeError, ValueError):
            retry_timeout = 300
        try:
            retry_delay = max(0.2, float(config.get("retry_delay") or 1))
        except (TypeError, ValueError):
            retry_delay = 1.0
        retries = max(1, min(6, int(retry_timeout / 60) + 1))

        # ⚠️ 陷阱：官方文档写"index 从 0 开始"，但**真实接口返回的是 1 起的分片号**
        # （实测 6MB 文件返回 parts=[{index:1, block_size:"6291456"}]）。
        # 所以这里完全以平台返回的 parts 为准：按 index 排序、按各片自己的 block_size
        # 依次切片，并把 index 原样回传给 upload_part_finish。
        raw_parts = [item for item in (prepared.get("parts") or []) if isinstance(item, dict)]
        try:
            raw_parts.sort(key=lambda item: int(item.get("index") or 0))
        except (TypeError, ValueError):
            pass
        units: List[Dict[str, Any]] = []
        offset = 0
        for item in raw_parts:
            url = str(item.get("presigned_url") or "")
            if not url:
                continue
            try:
                size = int(str(item.get("block_size") or 0))
            except (TypeError, ValueError):
                size = 0
            chunk = blob[offset:offset + (size or max(1, len(blob) - offset))]
            if not chunk:
                continue
            units.append({"index": item.get("index"), "url": url, "data": chunk})
            offset += len(chunk)
        if not units:
            raise APIError("平台没有返回分片上传地址（upload_prepare 的 parts 为空）")
        if offset < len(blob):
            raise APIError(f"平台返回的分片信息不完整：只覆盖了 {offset}/{len(blob)} 字节")

        total_parts = len(units)
        self.log.info("[%s] 分片上传：%s（%.1f MB，%d 片，并发 %d）", self.bot_id, file_name or "文件",
                      len(blob) / 1048576, total_parts, concurrency)

        def send_one(unit: Dict[str, Any]) -> None:
            chunk = unit["data"]
            self._put_chunk(unit["url"], chunk, retries=retries, delay=retry_delay)
            self.upload_part_finish(target_type, openid, upload_id, unit["index"], len(chunk),
                                    hashlib.md5(chunk).hexdigest())

        if concurrency > 1 and total_parts > 1:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                list(pool.map(send_one, units))
        else:
            for unit in units:
                send_one(unit)

        # 全部分片完成后，带 upload_id 调一次上传接口完成合并
        result = self.request("POST", self._files_url(target_type, openid), json_data={
            "file_type": int(file_type), "srv_send_msg": False,
            "file_name": file_name or "file", "upload_id": upload_id})
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        file_info = (data or {}).get("file_info") or ""
        if not file_info:
            raise APIError(f"分片上传合并失败，未返回 file_info: {str(result)[:300]}")
        info = dict(data or {})
        info["_parts"] = total_parts
        info["_chunked"] = True
        return file_info, info


    # ------------------------------------------------------------------ 文件（非图片）
    def _file_size_limit_bytes(self) -> float:
        """发送文件的大小上限（字节）。

        QQ 官方对"文件(4)"的软/硬限制都是 200MB，所以默认取 200；
        配置项 `send.max_file_mb` 用来**再往下收**（例如想省流量），
        这里把它封顶到官方硬限制，避免填一个超过平台上限的值却没人拦。
        """
        configured = self._cfg_float("send", "max_file_mb", default=MEDIA_HARD_LIMIT_MB)
        return max(0.1, min(float(MEDIA_HARD_LIMIT_MB), configured)) * 1024 * 1024

    def send_file(self, target_type: str, openid: str, blob: bytes, file_name: str,
                  content: str = "", reply_msg_id: str = "",
                  reply_style: str = "") -> Dict[str, Any]:
        """发送文件（图片以外的附件）。

        大文件走官方的**分片上传**（upload_prepare → 逐片 PUT → upload_part_finish → 合并），
        小文件仍用一次上传；单次上传被拒时再退回分片上传，保证兼容性。
        """
        limit = self._file_size_limit_bytes()
        if len(blob) > limit:
            raise APIError(f"文件过大（{len(blob) / 1048576:.1f} MB），当前上限 {limit / 1048576:g} MB"
                           "（QQ 平台硬限制 200MB，可在「设置 → 发送」里调整）")
        if not blob:
            raise APIError("文件内容为空")

        preferred = self.guess_file_type(file_name)
        last_error: Optional[Exception] = None

        # 大文件：直接用官方推荐的分片上传
        if len(blob) > CHUNKED_UPLOAD_THRESHOLD:
            for file_type in self._file_type_candidates(preferred):
                try:
                    file_info, extra = self.upload_by_parts(target_type, openid, blob, file_type,
                                                            file_name)
                    info = self.send_media(target_type, openid, file_info, content, reply_msg_id, reply_style)
                    info["_file_type"] = file_type
                    info["_chunked"] = True
                    if extra.get("_parts"):
                        info["_note"] = ((info.get("_note") + "；") if info.get("_note") else "") + \
                            f"分片上传 {extra['_parts']} 片"
                    return info
                except Exception as exc:
                    last_error = exc
                    self.log.warning("[%s] 分片上传 file_type=%s 失败：%s", self.bot_id, file_type, exc)
            raise APIError(f"发送文件失败：{last_error}。可稍后重试，或改用「图片」发送、发送文件链接。")

        # 小文件：先按原来的"一次上传"走，失败再退回分片上传
        data_b64 = base64.b64encode(blob).decode()
        for file_type in self._file_type_candidates(preferred):
            try:
                file_info, _ = self.upload_with_fallback(
                    target_type, openid, file_type, file_data=data_b64, file_name=file_name)
                info = self.send_media(target_type, openid, file_info, content, reply_msg_id, reply_style)
                info["_file_type"] = file_type
                return info
            except Exception as exc:
                last_error = exc
                self.log.warning("[%s] 文件一次上传 file_type=%s 失败：%s", self.bot_id, file_type, exc)
        try:
            file_info, extra = self.upload_by_parts(target_type, openid, blob,
                                                   self._file_type_candidates(preferred)[0], file_name)
            info = self.send_media(target_type, openid, file_info, content, reply_msg_id, reply_style)
            info["_chunked"] = True
            return info
        except Exception as exc:
            last_error = exc
        raise APIError(
            f"发送文件失败：{last_error}。QQ 机器人平台对文件消息支持有限（可能缺少权限），"
            "可改用「图片 / 表情包」发送，或改为发送文件链接。")

    @staticmethod
    def _file_type_candidates(preferred: int) -> List[int]:
        """优先用识别出来的类型，再退回"文件(4)"（官方：超出软限制会降级为文件）。"""
        order = [preferred, FILE_TYPE_FILE]
        return [item for index, item in enumerate(order) if item not in order[:index]]

    # ------------------------------------------------------------------ 图片
    def send_image_by_data(self, target_type: str, openid: str, blob: bytes, file_name: str = "image.png",
                           content: str = "", reply_msg_id: str = "",
                           reply_style: str = "") -> Dict[str, Any]:
        """用本地字节流发送图片。

        小图走一次上传；超过阈值（4MB）或一次上传被拒时改用官方分片上传。
        大小上限**同时遵守**两个约束：
        - 官方图片软限制 20MB（超过就降级成"文件"类型发）；
        - 用户设置的 `send.max_image_mb`（超过直接拒绝并说明，不再"前端拦、后端放行"）。
        """
        if not blob:
            raise APIError("图片内容为空")
        configured_mb = self._cfg_float("send", "max_image_mb",
                                        default=MEDIA_SOFT_LIMIT_MB[FILE_TYPE_IMAGE])
        configured_mb = max(0.1, float(configured_mb))
        configured = configured_mb * 1024 * 1024
        if len(blob) > configured:
            raise APIError(f"图片过大（{len(blob) / 1048576:.1f} MB），"
                           f"当前设置的上限是 {configured_mb:g} MB"
                           "（可在「设置 → 发送策略 → 图片大小上限」里调整）")
        soft = min(MEDIA_SOFT_LIMIT_MB[FILE_TYPE_IMAGE] * 1024 * 1024, configured)
        if len(blob) > soft:
            self.log.info("[%s] 图片 %.1f MB 超过图片软限制 %d MB，将按文件类型上传",
                          self.bot_id, len(blob) / 1048576, MEDIA_SOFT_LIMIT_MB[FILE_TYPE_IMAGE])
            return self.send_file(target_type, openid, blob, file_name or "image.png",
                                  content, reply_msg_id, reply_style)
        if len(blob) > CHUNKED_UPLOAD_THRESHOLD:
            file_info, _ = self.upload_by_parts(target_type, openid, blob, FILE_TYPE_IMAGE,
                                                file_name or "image.png")
            return self.send_media(target_type, openid, file_info, content, reply_msg_id, reply_style)
        return self.send_image_by_data_once(target_type, openid, blob, file_name, content,
                                            reply_msg_id, reply_style)

    def send_image_by_data_once(self, target_type: str, openid: str, blob: bytes, file_name: str,
                                content: str = "", reply_msg_id: str = "",
                                reply_style: str = "") -> Dict[str, Any]:
        """一次上传（base64）发送图片；被拒时退回分片上传。"""
        data_b64 = base64.b64encode(blob).decode()
        try:
            file_info, _ = self.upload_with_fallback(
                target_type, openid, FILE_TYPE_IMAGE, file_data=data_b64,
                file_name=file_name or "image.png")
        except Exception as exc:
            self.log.warning("[%s] 图片一次上传失败（%s），改用分片上传", self.bot_id, exc)
            file_info, _ = self.upload_by_parts(target_type, openid, blob, FILE_TYPE_IMAGE,
                                                file_name or "image.png")
        return self.send_media(target_type, openid, file_info, content, reply_msg_id, reply_style)

    def send_image_by_url(self, target_type: str, openid: str, image_url: str,
                          content: str = "", reply_msg_id: str = "", reply_style: str = "",
                          local_loader=None, remote_loader=None) -> Tuple[Dict[str, Any], str]:
        """通过图片链接发送，返回 (发送结果, 页面展示用的本地/原始地址)。

        策略：
          1) 本服务图片代理地址（/api/media?url=xxx）→ 取出内层原始链接；
          2) 本地已留存文件（/media/xxx）→ 直接读取字节上传；
          3) 本机/回环地址 → 由本服务下载后上传（QQ 服务器拉不到本机）；
          4) 公网链接 → 先交给 QQ 拉取；报"上传URL错误"时改由本服务下载后再上传。
        """
        src = (image_url or "").strip()
        inner = self._inner_media_url(src)
        if inner and (inner.lower().startswith(("http://", "https://")) or inner.startswith("/media/")):
            src = inner

        parsed = urlparse(src)
        if src.startswith("/media/") or (parsed.path.startswith("/media/") and _is_loopback(src)):
            blob, name = (local_loader or (lambda _p: (b"", "")))(parsed.path or src)
            if not blob:
                raise APIError("本地图片不存在，可能已被清理")
            info = self.send_image_by_data(target_type, openid, blob, name or "image.png",
                                           content, reply_msg_id, reply_style)
            return info, (parsed.path or src)

        if not src.lower().startswith(("http://", "https://")):
            raise ValueError("图片链接无法识别：需要 http/https 地址，或本服务的 /media/... 路径")

        if _is_loopback(src):
            self.log.info("[%s] 图片链接指向本机，改由本服务下载后上传", self.bot_id)
            blob, content_type, name = (remote_loader or (lambda _u: (b"", "", "")))(src)
            if not blob:
                raise APIError("本机无法访问该图片链接")
            info = self.send_image_by_data(target_type, openid, blob, name, content, reply_msg_id,
                                           reply_style)
            return info, src

        try:
            file_info, _ = self.upload_media(target_type, openid, FILE_TYPE_IMAGE, url=src)
            info = self.send_media(target_type, openid, file_info, content, reply_msg_id, reply_style)
            return info, src
        except Exception as exc:
            if not is_url_upload_error(exc):
                raise
            self.log.warning("[%s] QQ 无法拉取该链接，改由本服务下载后上传: %s", self.bot_id, exc)
            blob, content_type, name = (remote_loader or (lambda _u: (b"", "", "")))(src)
            if not blob:
                raise APIError(f"QQ 无法拉取该链接，本服务也下载失败：{exc}")
            info = self.send_image_by_data(target_type, openid, blob, name, content, reply_msg_id,
                                           reply_style)
            return info, src

    @staticmethod
    def _inner_media_url(url: str) -> str:
        try:
            parsed = urlparse(url or "")
        except ValueError:
            return ""
        if parsed.path.rstrip("/").endswith("/api/media"):
            return (parse_qs(parsed.query).get("url") or [""])[0]
        return ""

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

    # ------------------------------------------------------------------ 富媒体下载链接
    def file_download_url(self, target_type: str, openid: str, file_info: str) -> Optional[str]:
        """用 file_info 换取富媒体文件的下载地址（有效期很短，需及时留存）。"""
        if not file_info or not openid:
            return None
        scope = "groups" if target_type == "group" else "users"
        url = f"{self.api_base}/v2/{scope}/{openid}/files/{file_info}"
        try:
            result = self.request("GET", url, retries=2)
            data = result.get("data") if isinstance(result.get("data"), dict) else result
            return (data or {}).get("url") or result.get("url")
        except Exception as exc:
            self.log.warning("[%s] 换取文件下载链接失败: %s", self.bot_id, exc)
            return None


# ======================================================================================
# 判定工具
# ======================================================================================
def is_param_error(exc: Exception) -> bool:
    text = str(exc)
    return any(hint in text for hint in PARAM_ERROR_HINTS)


def is_url_upload_error(exc: Exception) -> bool:
    text = str(exc)
    return any(hint in text for hint in URL_UPLOAD_ERROR_HINTS)


def _is_loopback(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS or host.startswith("127.")


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

