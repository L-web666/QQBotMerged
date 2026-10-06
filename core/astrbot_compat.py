# -*- coding: utf-8 -*-
"""AstrBot 插件兼容层

让本程序能直接加载 **AstrBot 格式的插件**（`plugins/<插件名>/` 里放
`metadata.yaml` + `main.py`，类继承 `Star`，用 `@filter.command(...)` 之类装饰器注册）。

实现方式：在 `sys.modules` 里装一套 `astrbot.*` 垫片模块（星号、事件、过滤器、
消息组件、Provider、StarTools……），把 AstrBot 的插件直接 `exec` 进来，然后把
本程序收到的消息包装成 `AstrMessageEvent` 交给它们，最后把插件产出的
`MessageEventResult`（文本 / 图片 / @ 等）发回 QQ。

支持范围（够跑绝大多数常见插件）：
- `@filter.command(...)` / 别名 / `@filter.command_group(...)` 子命令
- `@filter.regex(...)`、`@filter.event_message_type(...)`、`@filter.permission_type(...)`
- `@filter.on_astrbot_loaded()` / `on_plugin_loaded()` / `on_llm_request()` / `on_llm_response()`
- 同步/异步处理器、`async def` 异步生成器（`yield event.plain_result(...)`）
- `event.plain_result / image_result / chain_result / make_result`、`event.stop_event()`
- `self.context.send_message(unified_msg_origin, chain)` 主动发消息
- `self.context.get_using_provider().text_chat(...)`（桥接到本程序的 AI 配置）
- `StarTools.get_data_dir()` / `save_json` / `load_json`
- 插件 `_conf_schema.json` 的默认值 + `data/config/<插件名>_config.json` 里的用户值

**不支持**（会记一条明确的诊断，而不是静默失败）：`llm_tool`（给 LLM 注册函数工具）、
`register_web_api`（插件自带网页接口）、平台适配器相关过滤器。


**拆分说明（2026-10-06）**：实现已拆到 `core/astrbot_shim/` 包（base / runner /
components / filters / star / provider / compat_utils / install / host），
本文件保留为**兼容再导出层**：所有老的 `from core.astrbot_compat import X` 照旧可用，
而且拿到的是**同一个对象**（不是复制），`isinstance` / 单例判断都不受影响。
"""

import asyncio
import copy
import importlib
import importlib.util
import inspect
import json
import logging
import os
import sys
import threading
import time
import types
from enum import Enum, IntFlag
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.astrbot_shim import (  # noqa: F401
    logger,
    ASTRBOT_VERSION,
    SHIM_MARK,
    AsyncRunner,
    RUNNER,
    BaseMessageComponent,
    Plain,
    Image,
    Record,
    Video,
    File,
    At,
    AtAll,
    Face,
    Reply,
    Poke,
    Node,
    Nodes,
    MessageChain,
    MessageEventResult,
    AstrBotMessage,
    MessageMember,
    Group,
    MessageType,
    EventMessageType,
    PermissionType,
    PlatformAdapterType,
    AstrMessageEvent,
    _CommandGroup,
    _FilterNamespace,
    FILTERS_ATTR,
    _mark,
    REGISTRY,
    register,
    PluginRuntime,
    Star,
    StarTools,
    AstrBotConfig,
    _ProviderShim,
    _PlatformManagerShim,
    ProviderRequest,
    Context,
    _module,
    _deep_update,
    _schema_defaults,
    _version_tuple,
    _version_satisfies,
    _load_yaml,
    install_shim,
    AstrBotPlugin,
    AstrBotHost,
)

__all__ = [
    "logger",
    "ASTRBOT_VERSION",
    "SHIM_MARK",
    "AsyncRunner",
    "RUNNER",
    "BaseMessageComponent",
    "Plain",
    "Image",
    "Record",
    "Video",
    "File",
    "At",
    "AtAll",
    "Face",
    "Reply",
    "Poke",
    "Node",
    "Nodes",
    "MessageChain",
    "MessageEventResult",
    "AstrBotMessage",
    "MessageMember",
    "Group",
    "MessageType",
    "EventMessageType",
    "PermissionType",
    "PlatformAdapterType",
    "AstrMessageEvent",
    "_CommandGroup",
    "_FilterNamespace",
    "FILTERS_ATTR",
    "_mark",
    "REGISTRY",
    "register",
    "PluginRuntime",
    "Star",
    "StarTools",
    "AstrBotConfig",
    "_ProviderShim",
    "_PlatformManagerShim",
    "ProviderRequest",
    "Context",
    "_module",
    "_deep_update",
    "_schema_defaults",
    "_version_tuple",
    "_version_satisfies",
    "_load_yaml",
    "install_shim",
    "AstrBotPlugin",
    "AstrBotHost",
]
