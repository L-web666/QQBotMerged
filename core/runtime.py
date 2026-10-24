# -*- coding: utf-8 -*-
"""运行时核心：多机器人调度、消息总线、发送出口、配置热更新

这是合并版的“大脑”：
- 每个启用的机器人一条独立 WebSocket 连接（`gateway.py`）与一个 API 客户端（`qq_api.py`）；
- 收到消息 → 落库 → **立即留存附件** → 广播给网页 → 交给回复链路；
- 网页/插件/定时任务都通过 `Runtime.send_*` 统一出口发消息，并同样落库与广播；
- 配置保存后调用 `Runtime.on_config_changed()` 做热更新（无需重启的项立即生效）。


**拆分说明（2026-10-06）**：实现已拆到 `core/runtime_pkg/` 包
（base / bot / config / messaging / names / moderation / media / panels / scheduler / lifecycle / core），
本文件保留为**兼容再导出层**：`from core.runtime import Runtime` 照旧可用，
而且是**同一个类对象**（split 后 `Runtime` 由各 mixin 组合而成，行为不变）。
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

from core.runtime_pkg import (  # noqa: F401
    _BOT_ACCOUNT_FIELDS,
    _json_same,
    _section_label,
    _bot_change_labels,
    _mask_app_id,
    BotRuntime,
    RuntimeConfigMixin,
    RuntimeMessagingMixin,
    RuntimeNameMixin,
    RuntimeModerationMixin,
    RuntimeMediaMixin,
    RuntimePanelMixin,
    RuntimeSchedulerMixin,
    RuntimeLifecycleMixin,
    Runtime,
)

__all__ = [
    "_BOT_ACCOUNT_FIELDS",
    "_json_same",
    "_section_label",
    "_bot_change_labels",
    "_mask_app_id",
    "BotRuntime",
    "RuntimeConfigMixin",
    "RuntimeMessagingMixin",
    "RuntimeNameMixin",
    "RuntimeModerationMixin",
    "RuntimeMediaMixin",
    "RuntimePanelMixin",
    "RuntimeSchedulerMixin",
    "RuntimeLifecycleMixin",
    "Runtime",
]
