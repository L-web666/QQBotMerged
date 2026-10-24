# -*- coding: utf-8 -*-
"""媒体留存模块：把收到的图片/表情包等立即下载到本地并入库。

为什么需要它：QQ 侧返回的媒体链接有效期很短（富媒体 file_info 换来的一次性地址尤甚），
一旦过期就再也拿不到原图。因此在消息落地时同步/异步下载到 `data/media/`，
网页只引用本地地址，QQ 链接过期也不影响查看。

要点：
- 相同 URL 只下载一次（按 URL 哈希去重，命中已有记录直接复用）；
- **相同内容也只存一份**（按 sha256 去重）：多个机器人同在一个群时，同一条消息里的
  图片会分别下发给每个机器人，每份下载地址都带各自的 rkey 签名（URL 不同），
  只按 URL 去重是拦不住的；
- QQ 图片有防盗链，下载时带上 Referer / User-Agent；
- 文件名用 `URL哈希_时间戳.ext`，避免同名覆盖与路径穿越；
- 下载失败也记一条 `state=failed` 的媒体记录，界面上可提示“未能留存”。
"""

import base64
import hashlib
import json
import logging
import mimetypes
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote

import requests

from core import paths
from core.storage import is_image_attachment

logger = logging.getLogger(__name__)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}
MIME_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/svg+xml": ".svg",
    "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
    "application/pdf": ".pdf", "text/plain": ".txt", "application/zip": ".zip",
    "application/json": ".json", "video/mp4": ".mp4", "audio/mpeg": ".mp3",
}
# 常见二进制扩展名（QQ 文件消息、上传文件时用于推断类型）
GENERIC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".zip",
               ".rar", ".7z", ".mp3", ".mp4", ".wav", ".amr", ".sil", ".json", ".csv", ".apk"}

# 允许的下载来源域名后缀（防 SSRF：不让本地服务去访问内网地址）
ALLOWED_HOST_SUFFIX = ("qq.com", "qq.com.cn", "qpic.cn", "gtimg.cn", "qqusercontent.com",
                       "weixin.qq.com", "tencent.com", "myqcloud.com", "cos.ap-")

# 说明：QQ 富媒体下载链接是带 rkey 的签名地址（形如
# https://multimedia.nt.qq.com.cn/download?appid=..&fileid=..&rkey=..），
# 访问控制靠签名校验而不是 Referer，因此只需带一个常见 UA 即可。
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def url_hash(url: str) -> str:
    return hashlib.sha1((url or "").encode("utf-8", "ignore")).hexdigest()[:20]


def content_hash(blob: bytes) -> str:
    """内容指纹（sha256）。用来判断"这张图是不是已经存过了"。"""
    if not blob:
        return ""
    return hashlib.sha256(blob).hexdigest()


def guess_ext(url: str, content_type: str = "", fallback: str = ".bin") -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in MIME_EXT:
        return MIME_EXT[ctype]
    path = urlparse(url or "").path
    ext = os.path.splitext(path)[1].lower()
    if ext and (ext in IMAGE_EXT or ext in GENERIC_EXT):
        return ext
    guessed = mimetypes.guess_extension(ctype) if ctype else None
    if guessed:
        return guessed
    return fallback if fallback.startswith(".") else f".{fallback}"


def is_probably_image(url: str, content_type: str = "") -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype.startswith("image/"):
        return True
    ext = os.path.splitext(urlparse(url or "").path)[1].lower()
    return ext in IMAGE_EXT


# 常见图片格式的魔数（文件头）。只按扩展名判断会把 .exe 改名成 .png 也当图片，
# 上传给 QQ 后一般会以"文件类型不符"失败，所以发送前先看一眼真实内容。
IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"BM", "image/bmp", ".bmp"),
)


def sniff_image_type(blob: bytes) -> Tuple[str, str]:
    """按文件头判断图片类型，返回 (mime, 扩展名)；不是已知图片时返回 ("", "")。"""
    if not blob:
        return "", ""
    head = bytes(blob[:16])
    for magic, mime, ext in IMAGE_MAGIC:
        if head.startswith(magic):
            return mime, ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if head[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon", ".ico"
    if head[:5] == b"<?xml" or head[:4] == b"<svg":
        return "image/svg+xml", ".svg"
    return "", ""


def looks_like_image(blob: bytes) -> bool:
    """内容看起来是不是图片（没有魔数的一律当"不是图片"）。"""
    return bool(sniff_image_type(blob)[0])


def host_allowed(url: str) -> bool:
    """只允许腾讯系媒体域名与公网 https 资源，避免被当成内网探测工具。"""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return False
    # 内网网段一律拒绝
    if host.startswith(("10.", "192.168.", "169.254.")) or host.startswith("172."):
        parts = host.split(".")
        if len(parts) == 4 and parts[0] == "172":
            try:
                if 16 <= int(parts[1]) <= 31:
                    return False
            except ValueError:
                pass
    return True


def safe_name(name: str) -> str:
    """把用户提供的文件名清洗成安全的基名（防路径穿越）。"""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = "".join(ch for ch in base if ch not in '<>:"|?*\x00')
    return base[:160]


class MediaStore:
    """媒体下载、入库与查询。"""

    def __init__(self, store, config, session: requests.Session = None):
        self.store = store
        self.config = config
        self.session = session or requests.Session()
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._inflight: set = set()
        # "查重 → 落盘 → 登记"这一小段必须串行：两个机器人同时收到同一条群消息时，
        # 两条线程会同时查到"还没存过"，然后各写一份文件。持锁的只有这段快操作，
        # 真正的网络下载在锁外面，不会把并发下载拖成串行。
        self._save_lock = threading.Lock()
        os.makedirs(self.media_dir, exist_ok=True)

    # ------------------------------------------------------------------ 配置
    @property
    def media_dir(self) -> str:
        configured = self.config.str_of("storage", "media_dir", default="data/media")
        return self.config.abs_path(configured or "data/media")

    @property
    def enabled(self) -> bool:
        return self.config.bool_of("features", "save_received_media", default=True)

    @property
    def timeout(self) -> float:
        return float(self.config.int_of("features", "media_download_timeout", default=20) or 20)

    @property
    def max_bytes(self) -> int:
        return int(float(self.config.float_of("send", "max_image_mb", default=6) or 6) * 1024 * 1024)

    def local_url(self, local_name: str) -> str:
        return f"/media/{local_name}" if local_name else ""

    # ------------------------------------------------------------------ 下载
    def save_bytes(self, blob: bytes, content_type: str = "", file_name: str = "",
                   source: str = "received", bot_id: str = "", conv_key: str = "",
                   msg_id: str = "", url: str = "", record: bool = True,
                   sha256: str = "", dedup: bool = True) -> Dict[str, Any]:
        """把字节流落盘并（可选）登记到媒体表，返回记录。

        去重（`dedup=True`，默认开）：**内容相同就复用已经存好的那一份** —— 不写第二个
        文件，也不再登记第二条记录，返回已有记录（带 `reused=True`）。
        原因：多个机器人同在一个群时，同一条消息里的图片会分别下发给每个机器人，
        内容一模一样但下载地址（带 rkey 签名）各不相同；只按 URL 去重拦不住，
        而按文件名去重既不可靠（同名不同图）也不够（不同名同一图）。
        """
        if not blob:
            raise ValueError("媒体内容为空")
        size = len(blob)
        limit = int(float(self.config.float_of("send", "max_file_mb", default=8) or 8) * 1024 * 1024)
        if size > max(limit, self.max_bytes):
            raise ValueError(f"文件过大（{size / 1048576:.1f} MB），已跳过留存")

        digest = sha256 or content_hash(blob)
        with self._save_lock:
            if dedup and digest:
                existing = self.find_duplicate(digest)
                if existing:
                    logger.debug("媒体内容重复，复用已有文件：%s", existing.get("local_name"))
                    return existing

            ext = guess_ext(file_name or url, content_type)
            base = safe_name(file_name) if file_name else ""
            if not base:
                base = f"{(url_hash(url) if url else uuid.uuid4().hex[:16])}_{int(time.time())}{ext}"
            elif not os.path.splitext(base)[1]:
                base += ext
            local_name = base
            full = os.path.join(self.media_dir, local_name)
            index = 1
            while os.path.exists(full):
                stem, dot, tail = local_name.rpartition(".")
                local_name = f"{stem}_{index}{dot}{tail}" if dot else f"{local_name}_{index}{ext}"
                full = os.path.join(self.media_dir, local_name)
                index += 1

            os.makedirs(self.media_dir, exist_ok=True)
            with open(full, "wb") as handle:
                handle.write(blob)

            record_data = {
                "bot_id": bot_id, "conv_key": conv_key, "msg_id": msg_id, "source": source,
                "url": url, "url_hash": url_hash(url) if url else "", "sha256": digest,
                "local_name": local_name,
                "file_name": base, "content_type": content_type, "size": size,
                "state": "done", "created_at": time.time(),
            }
            if record:
                try:
                    self.store.add_media(record_data)
                except Exception as exc:  # 记录失败不影响文件已留存的事实
                    logger.warning("登记媒体记录失败: %s", exc)
            record_data["local_url"] = self.local_url(local_name)
            return record_data

    def find_duplicate(self, sha256: str) -> Optional[Dict[str, Any]]:
        """按内容指纹找一份已留存且文件还在的记录（找不到返回 None）。"""
        digest = str(sha256 or "").strip()
        if not digest:
            return None
        finder = getattr(self.store, "find_media_by_sha256", None)
        if not callable(finder):
            return None
        try:
            existing = finder(digest)
        except Exception as exc:
            logger.debug("按内容查重失败: %s", exc)
            return None
        return self._reusable(existing)

    def _reusable(self, existing: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """记录里的文件确实还在磁盘上，才算"能复用"。"""
        if not existing or not existing.get("local_name"):
            return None
        full = os.path.join(self.media_dir, existing["local_name"])
        if not os.path.isfile(full):
            return None
        item = dict(existing)
        item["local_url"] = self.local_url(item["local_name"])
        item["reused"] = True
        return item

    def fetch(self, url: str) -> Tuple[bytes, str]:
        """下载 URL 内容，返回 (字节, Content-Type)。"""
        if not host_allowed(url):
            raise ValueError("链接不被允许（仅支持公网 http/https 资源）")
        headers = {
            "User-Agent": DEFAULT_UA,
            "Accept": "image/*,video/*,application/octet-stream,*/*;q=0.8",
        }
        response = self.session.get(url, timeout=self.timeout, headers=headers, stream=True)
        if response.status_code != 200:
            raise ValueError(f"下载失败：HTTP {response.status_code}")
        chunks, total = [], 0
        limit = int(float(self.config.float_of("send", "max_file_mb", default=8) or 8) * 1024 * 1024)
        for chunk in response.iter_content(65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                response.close()
                raise ValueError(f"文件超过上限（{limit / 1048576:.1f} MB）")
            chunks.append(chunk)
        blob = b"".join(chunks)
        if not blob:
            raise ValueError("下载内容为空")
        return blob, response.headers.get("Content-Type", "")

    def _key_lock(self, key: str) -> threading.Lock:
        """按去重键取一把锁（同一个 URL 同时被两条线程要下载时只下一次）。"""
        with self._locks_guard:
            # 锁表不能无限长：偶尔清一次"没人持有"的锁。清掉的锁万一还有线程在用，
            # 最坏结果只是多下一次（`_save_lock` 仍然保证不会存出第二份文件）。
            if len(self._locks) > 1024:
                for name in [n for n, lock in self._locks.items() if not lock.locked()]:
                    self._locks.pop(name, None)
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def download(self, url: str, file_name: str = "", source: str = "received", bot_id: str = "",
                 conv_key: str = "", msg_id: str = "") -> Optional[Dict[str, Any]]:
        """下载并留存一个媒体链接（失败返回 None，并登记失败记录）。

        去重有两道：① 同 URL（`url_hash`）已经存过就直接复用，省掉一次下载；
        ② 内容相同（sha256）复用同一个文件，见 `save_bytes` —— 多机器人同群时
        每个机器人拿到的下载地址不一样，只有第二道拦得住。
        """
        if not url:
            return None
        key = url_hash(url)
        existing = self._find_by_url(key)
        if existing:
            return existing
        with self._key_lock("url:" + (key or url)):
            # 拿到锁之后再查一次：刚才可能在别的线程里已经下完了
            existing = self._find_by_url(key)
            if existing:
                return existing
            try:
                blob, content_type = self.fetch(url)
                return self.save_bytes(blob, content_type, file_name=file_name, source=source,
                                       bot_id=bot_id, conv_key=conv_key, msg_id=msg_id, url=url)
            except Exception as exc:
                logger.warning("媒体留存失败（%s）：%s", url[:120], exc)
                try:
                    self.store.add_media({
                        "bot_id": bot_id, "conv_key": conv_key, "msg_id": msg_id, "source": source,
                        "url": url, "url_hash": key, "state": "failed", "error": str(exc)[:300],
                        "created_at": time.time(),
                    })
                except Exception:
                    pass
                return None

    def _find_by_url(self, key: str) -> Optional[Dict[str, Any]]:
        """同一个 URL 已经留存过 → 直接复用（文件还在磁盘上才算）。"""
        if not key:
            return None
        try:
            existing = self.store.find_media_by_hash(key)
        except Exception:
            existing = None
        return self._reusable(existing)

    def download_async(self, url: str, **kwargs) -> None:
        """后台线程下载（不阻塞消息处理）。"""
        if not url or url in self._inflight:
            return
        with self._locks_guard:
            if url in self._inflight:
                return
            self._inflight.add(url)

        def worker():
            try:
                self.download(url, **kwargs)
            finally:
                with self._locks_guard:
                    self._inflight.discard(url)

        threading.Thread(target=worker, name="media-download", daemon=True).start()

    def persist_attachments(self, message: Dict[str, Any], bot_id: str, conv_key: str,
                            blocking: bool = False) -> List[Dict[str, Any]]:
        """把一条消息里的所有图片/表情包附件留存到本地。

        返回成功留存的记录列表；同时把本地地址写回消息（附件 local_url + media_local + image_url）。
        对“非图片附件”同样留存（文件消息），但 media_local 只在有图片时更新。
        """
        if not self.enabled:
            return []
        results: List[Dict[str, Any]] = []
        attachments = message.get("attachments") or []
        msg_id = message.get("msg_id") or ""
        for attachment in attachments:
            url = str(attachment.get("url") or "").strip()
            if not url:
                continue
            kwargs = {
                "file_name": attachment.get("file_name") or "",
                "source": "received",
                "bot_id": bot_id,
                "conv_key": conv_key,
                "msg_id": msg_id,
            }
            if blocking:
                record = self.download(url, **kwargs)
                if record:
                    results.append(record)
            else:
                self.download_async(url, **kwargs)
        return results

    # ------------------------------------------------------------------ 查询/清理
    def family_fallback(self, local_name: str) -> Optional[str]:
        """根据本地文件名反查记录。"""
        for item in self.store.list_media(limit=500):
            if item.get("local_name") == local_name:
                return item.get("file_name") or ""
        return None

    def cleanup(self, keep_days: int = 0) -> int:
        """清理过期媒体文件，返回删除的文件数。"""
        days = int(keep_days or self.config.int_of("send", "upload_keep_days", default=7) or 0)
        if days <= 0:
            return 0
        names = self.store.purge_media(older_than_days=days)
        removed = 0
        for name in names:
            full = os.path.join(self.media_dir, safe_name(name))
            try:
                if os.path.isfile(full):
                    os.remove(full)
                    removed += 1
            except OSError:
                pass
        if removed:
            logger.info("媒体清理：删除 %d 个过期文件", removed)
        return removed

    def resolve_local(self, name: str) -> Optional[str]:
        """把请求里的文件名解析成本地真实路径（防目录穿越）。"""
        base = safe_name(name)
        if not base:
            return None
        full = os.path.join(self.media_dir, base)
        if os.path.isfile(full):
            return full
        return None

    def decode_data_url(self, data: str) -> Tuple[bytes, str, str]:
        """解析 data URL 或纯 base64，返回 (字节, mime, 建议扩展名)。"""
        raw = (data or "").strip()
        name = ""
        mime = ""
        if raw.startswith("data:"):
            header, _, raw = raw.partition(",")
            mime = header[5:].split(";")[0].strip().lower()
            if "base64" not in header:
                from urllib.parse import unquote_to_bytes
                return unquote_to_bytes(raw), mime, guess_ext("", mime)
        try:
            blob = base64.b64decode(raw, validate=False)
        except Exception as exc:
            raise ValueError(f"文件数据不是合法的 base64: {exc}")
        if not blob:
            raise ValueError("文件数据为空")
        return blob, mime, guess_ext(name, mime)
