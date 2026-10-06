# -*- coding: utf-8 -*-
"""单个机器人的运行时（BotRuntime）。

本模块是 `core/runtime_pkg/` 包的一部分（由 `core/runtime.py` 拆分而来）；
`core/runtime.py` 只是兼容再导出层。
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

from core.runtime_pkg.base import _mask_app_id




class BotRuntime:
    """单个机器人的运行时（客户端 + 网关 + 状态）。"""

    def __init__(self, bot_id: str, name: str, app_id: str, app_secret: str, sandbox: bool,
                 intents: int, runtime: "Runtime", reconnect_attempts: int = 5,
                 reconnect_interval: int = 10):
        self.id = bot_id
        self.name = name or bot_id
        self.runtime = runtime
        self.log = runtime.log
        self.client = QQApiClient(bot_id, app_id, app_secret, sandbox, config=runtime.config,
                                  logger_obj=self.log)
        self.gateway = QQGateway(
            bot_id=bot_id,
            api_client=self.client,
            intents=intents or DEFAULT_INTENTS,
            on_message=runtime.on_gateway_message,
            on_status=self._on_status,
            reconnect_interval=reconnect_interval,
            max_reconnect_interval=runtime.config.float_of("receiver", "max_reconnect_interval_seconds",
                                                           default=30),
            heartbeat_timeout_factor=runtime.config.float_of("receiver", "heartbeat_timeout_factor",
                                                             default=3),
            logger_obj=self.log,
        )
        self.enabled = True
        self.sent_count = 0
        self.error_count = 0

    def _on_status(self, online: bool, error: str = ""):
        try:
            self.runtime.store.set_bot_status(self.id, online, error, started_at=self.runtime.started_at)
        except Exception:
            pass
        self.runtime.broadcast({"type": "bot_status", "bot_id": self.id, "online": online})

    def start(self):
        if not self.client.configured:
            self.log.warning("[%s] 未填写 app_id / app_secret，已跳过连接", self.id)
            return
        self.gateway.start()

    def stop(self):
        self.gateway.stop()

    def status(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "configured": self.client.configured,
            "sandbox": self.client.sandbox,
            "app_id": _mask_app_id(self.client.app_id),
            "online": self.gateway.ready,
            "state": self.gateway.status_text(),
            "session_id": self.gateway.session_id or "",
            "last_seq": self.gateway.last_seq,
            "heartbeat_interval": self.gateway.heartbeat_interval,
            "intents": self.gateway._effective_intents,
            "token_remaining": self.client.token_remaining(),
            "last_error": self.gateway.last_error,
            "sent": self.sent_count,
            "errors": self.error_count,
            "ready_at": self.gateway.ready_at,
            "last_event": self.gateway.last_event_type,
            "all_message_mode": bool(self.gateway.all_message_mode),
            "events": dict(self.gateway.event_counts),
        }
