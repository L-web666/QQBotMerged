# -*- coding: utf-8 -*-
"""AstrBot 插件兼容层（包）：实际实现在这里，`core/astrbot_compat.py` 只是兼容层。

模块划分：`base`（常量/日志）、`runner`（事件循环）、`components`（消息组件与事件）、
`filters`（过滤器与 @register）、`star`（插件侧 API）、`provider`（AI Provider/Context）、
`compat_utils`（小工具）、`install`（astrbot.* 垫片）、`host`（加载与分发）。
"""

from core.astrbot_shim.runner import AsyncRunner, RUNNER  # noqa: F401
from core.astrbot_shim.components import BaseMessageComponent, Plain, Image, Record, Video, File, At, AtAll, Face, Reply, Poke, Node, Nodes, MessageChain, MessageEventResult, AstrBotMessage, MessageMember, Group, MessageType, EventMessageType, PermissionType, PlatformAdapterType, AstrMessageEvent  # noqa: F401
from core.astrbot_shim.filters import _CommandGroup, _FilterNamespace, FILTERS_ATTR, _mark, REGISTRY, register  # noqa: F401
from core.astrbot_shim.star import PluginRuntime, Star, StarTools, AstrBotConfig  # noqa: F401
from core.astrbot_shim.provider import _ProviderShim, _PlatformManagerShim, ProviderRequest, Context  # noqa: F401
from core.astrbot_shim.compat_utils import _module, _deep_update, _schema_defaults, _version_tuple, _version_satisfies, _load_yaml  # noqa: F401
from core.astrbot_shim.install import install_shim  # noqa: F401
from core.astrbot_shim.host import AstrBotPlugin, AstrBotHost  # noqa: F401
from core.astrbot_shim.base import ASTRBOT_VERSION, SHIM_MARK, logger  # noqa: F401

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
