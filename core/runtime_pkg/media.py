# -*- coding: utf-8 -*-
"""对外地址与本机媒体读取（RuntimeMediaMixin）。

本模块是 `core/runtime_pkg/` 包的一部分（由 `core/runtime.py` 拆分而来）；
由 `core/runtime_pkg/core.py` 里的 `Runtime` 组合使用。
"""

import copy
import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from core import media_store as media_module
from core import message_text
from core import paths
from core.ai_client import AIClient, load_ai_config
from core.context_manager import ContextManager
from core.gateway import DEFAULT_INTENTS, QQGateway
from core.group_manager import GroupManager
from core.message_filter import MessageFilter
from core.plugin_manager import PluginBot, PluginManager
from core.qq_api import QQApiClient
from core.storage import (MemoryStore, SQLiteStore, conv_key_for, is_image_attachment,
                          normalize_attachments, preview_text)



class RuntimeMediaMixin:
    """对外地址与本机媒体读取（RuntimeMediaMixin）（被 `Runtime` 继承；不要单独实例化）。"""


    def public_base_url(self) -> str:
        """对外可访问的站点根地址（给"发下载链接"用）。

        顺序：显式配置 `web.public_base_url` → 实际监听地址（非 0.0.0.0 时）→
        本机第一个非回环 IPv4 兜底。**绝不能**把 0.0.0.0 直接写进链接
        （0.0.0.0 不是可访问地址），也尽量不用 127.0.0.1
        （那只有服务器自己能打开，QQ 群里的人点了必然打不开）。
        """
        explicit = (self.config.str_of("web", "public_base_url", default="") or "").strip()
        if explicit:
            return explicit.rstrip("/")
        host = (self.bind_host or self.config.str_of("web", "host", default="127.0.0.1")).strip()
        port = self.bind_port or self.config.int_of("web", "port", default=8666)
        if host in ("", "0.0.0.0", "::"):
            host = self.lan_ip() or "127.0.0.1"
        return f"http://{host}:{port}"

    @staticmethod
    def lan_ip() -> str:
        """猜一个本机在局域网里的 IPv4（拿不到返回空串）。"""
        import socket
        candidates: List[str] = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # 只是选路由，不会真的发包，也不会连上 8.8.8.8
                sock.connect(("8.8.8.8", 80))
                address = sock.getsockname()[0] or ""
                if address and not address.startswith("127."):
                    return address
            finally:
                sock.close()
        except OSError:
            pass
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                address = info[4][0]
                if address and not address.startswith("127."):
                    candidates.append(address)
        except OSError:
            pass
        if not candidates:
            try:
                for address in socket.gethostbyname_ex(socket.gethostname())[2]:
                    if address and not address.startswith("127."):
                        candidates.append(address)
            except OSError:
                pass
        return candidates[0] if candidates else ""

    def _public_file_link(self, local_url: str) -> Tuple[str, str]:
        """把本地文件路径拼成可访问链接，返回 (链接, 给用户看的说明)。"""
        base = self.public_base_url()
        link = f"{base}{local_url}"
        explicit = (self.config.str_of("web", "public_base_url", default="") or "").strip()
        host = self.bind_host or self.config.str_of("web", "host", default="127.0.0.1")
        if explicit:
            note = "链接用的是「设置 → 网页后台 → 对外地址」(web.public_base_url)"
        elif host in ("", "0.0.0.0", "::"):
            note = ("配置里监听的是 0.0.0.0（所有网卡），已自动换成局域网地址；"
                    "如果外面访问不到，请在设置里填 web.public_base_url")
        elif host in ("127.0.0.1", "localhost", "::1"):
            note = ("当前只监听本机，群里的人点不开这个链接；"
                    "请在设置里填 web.public_base_url（例如 http://你的域名或公网IP:8666）")
        else:
            note = "链接用的是当前监听地址"
        if "127.0.0.1" in link or "localhost" in link:
            note += " ⚠️ 该地址只有服务器自己能访问，建议改成对外地址"
        return link, note

    def _load_local_media(self, path: str):
        """读取本地留存媒体（供链接发送时改用上传）。"""
        name = os.path.basename(path or "")
        full = self.media.resolve_local(name)
        if not full:
            raise ValueError("本地图片不存在，可能已被清理")
        with open(full, "rb") as handle:
            return handle.read(), name

    def _load_remote_media(self, url: str):
        """下载远端媒体（供本机链接/防盗链链接改用上传）。"""
        blob, content_type = self.media.fetch(url)
        ext = media_module.guess_ext(url, content_type, ".png")
        return blob, content_type, f"image{ext}"
