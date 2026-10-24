# -*- coding: utf-8 -*-
"""上传相关逻辑（`QQUploadMixin`）：媒体/图片/文件的上传与降级。

从 `core/qq_api.py` 拆出；`QQApiClient` 通过继承获得这些方法，
`core/qq_api.py` 继续再导出这里的常量与辅助函数。
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

from core.qq_errors import APIError, is_param_error, is_url_upload_error



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


def _is_loopback(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS or host.startswith("127.")


class QQUploadMixin:
    """上传相关方法（被 `QQApiClient` 继承；不要单独实例化）。"""


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
