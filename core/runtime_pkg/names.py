# -*- coding: utf-8 -*-
"""昵称与群名解析（RuntimeNameMixin）。

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



class RuntimeNameMixin:
    """昵称与群名解析（RuntimeNameMixin）（被 `Runtime` 继承；不要单独实例化）。"""


    # ================================================================== 对外数据加工
    def lookup_name(self, openid: str) -> str:
        """openid → 显示名。

        优先级：**手动别名** → QQ 群名 → **在群聊里学到的用户昵称** → 历史消息昵称。

        关键点：同一个机器人在群聊和私聊里看到的 openid 是**同一个**，
        而群消息事件通常会带 `author.username`（私聊事件经常是空串）。
        因此收到群消息时会把昵称记下来（`meta` 里的 `uname:<openid>`），
        私聊里就能直接显示出这个人的名字 —— 不需要手动命名。
        """
        if not openid:
            return ""
        try:
            alias = self.store.get_alias(openid)
        except Exception:
            alias = ""
        if alias:
            return alias
        names = self.group_manager.group_names()
        if openid in names:
            return names[openid]
        try:
            learned = self.store.get_meta(f"uname:{openid}", "")
        except Exception:
            learned = ""
        if learned and learned not in ("我", "机器人"):
            return learned
        try:
            index = self.store.name_index(limit=1000)
            name = index.get(openid, "")
        except Exception:
            name = ""
        if name in ("我", "机器人", "bot", "Bot"):
            return ""
        return name

    @staticmethod
    def short_label(openid: str) -> str:
        """QQ 没给昵称时的占位显示：用 openid 尾部做可区分的短标签。"""
        text = str(openid or "").strip()
        if not text:
            return "未知用户"
        return "用户 " + (text[-6:] if len(text) > 6 else text)

    @staticmethod
    def short_group_label(group_openid: str) -> str:
        """群名还没拿到时的占位显示（避免把发言者的昵称当成群名）。"""
        text = str(group_openid or "").strip()
        if not text:
            return "未知群"
        return "群 " + (text[-6:] if len(text) > 6 else text)

    def _refresh_group_name_async(self, group_openid: str, bot_id: str):
        """后台补群名（每群至少间隔 60 秒，避免频繁打接口）。"""
        now = time.time()
        last = self._group_name_tried.get(group_openid, 0)
        if now - last < 60:
            return
        self._group_name_tried[group_openid] = now

        def worker():
            try:
                self.group_manager.refresh_group_name(group_openid, bot_id)
                self.broadcast({"type": "conversations_changed", "bot_id": bot_id})
            except Exception as exc:
                self.log.debug("自动获取群名失败: %s", exc)

        threading.Thread(target=worker, name="group-name", daemon=True).start()
