# -*- coding: utf-8 -*-
"""把 astrbot.* 垫片装进 sys.modules（install_shim）。

本模块是 `core/astrbot_shim/` 包的一部分（由 `core/astrbot_compat.py` 拆分而来）；
`core/astrbot_compat.py` 只是兼容再导出层，`from core.astrbot_compat import X` 照旧可用。
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
from core.astrbot_shim.base import ASTRBOT_VERSION, SHIM_MARK
from core.astrbot_shim.base import ASTRBOT_VERSION, SHIM_MARK
from core.astrbot_shim.components import AstrBotMessage, AstrMessageEvent, At, AtAll, BaseMessageComponent, EventMessageType, Face, File, Group, Image, MessageChain, MessageEventResult, MessageMember, MessageType, Node, Nodes, PermissionType, Plain, PlatformAdapterType, Poke, Record, Reply, Video
from core.astrbot_shim.filters import _FilterNamespace, register
from core.astrbot_shim.star import AstrBotConfig, Star, StarTools
from core.astrbot_shim.provider import Context, ProviderRequest, _ProviderShim
from core.astrbot_shim.compat_utils import _module




def install_shim(force: bool = False):
    """把 `astrbot.*` 垫片装进 sys.modules（幂等）。"""
    if not force and getattr(sys.modules.get("astrbot"), SHIM_MARK, False):
        return
    filter_ns = _FilterNamespace()
    filter_ns.filter = filter_ns                      # 兼容 `filter.filter.x`
    filter_ns.EventMessageType = EventMessageType
    filter_ns.PermissionType = PermissionType
    filter_ns.PlatformAdapterType = PlatformAdapterType

    star_mod = _module("astrbot.api.star", Star=Star, Context=Context, register=register,
                       StarTools=StarTools, AstrBotConfig=AstrBotConfig)
    event_mod = _module("astrbot.api.event", filter=filter_ns, Filter=filter_ns,
                        AstrMessageEvent=AstrMessageEvent, MessageEventResult=MessageEventResult,
                        AstrBotMessage=AstrBotMessage, MessageMember=MessageMember, Group=Group,
                        MessageType=MessageType, EventMessageType=EventMessageType,
                        PermissionType=PermissionType, PlatformAdapterType=PlatformAdapterType,
                        MessageChain=MessageChain)
    component_mod = _module(
        "astrbot.api.message_components", BaseMessageComponent=BaseMessageComponent,
        Plain=Plain, Image=Image, At=At, AtAll=AtAll, Face=Face, Reply=Reply, Poke=Poke,
        Node=Node, Nodes=Nodes, Record=Record, Video=Video, File=File,
        MessageChain=MessageChain)
    provider_mod = _module("astrbot.api.provider", ProviderRequest=ProviderRequest,
                           LLMResponse=object, Provider=_ProviderShim)
    message_mod = _module("astrbot.api.message", AstrBotMessage=AstrBotMessage,
                          MessageMember=MessageMember, Group=Group, MessageType=MessageType)
    log_mod = _module("astrbot.api.log", logger=logging.getLogger("astrbot"))
    api_mod = _module("astrbot.api", star=star_mod, event=event_mod,
                      message_components=component_mod, provider=provider_mod,
                      message=message_mod, log=log_mod, logger=logging.getLogger("astrbot"),
                      Star=Star, Context=Context, register=register, StarTools=StarTools,
                      AstrBotConfig=AstrBotConfig, filter=filter_ns)

    all_attrs = dict(vars(component_mod))
    all_attrs.update({
        "Star": Star, "Context": Context, "register": register, "StarTools": StarTools,
        "AstrBotConfig": AstrBotConfig, "filter": filter_ns, "AstrMessageEvent": AstrMessageEvent,
        "MessageEventResult": MessageEventResult, "AstrBotMessage": AstrBotMessage,
        "MessageMember": MessageMember, "Group": Group, "MessageType": MessageType,
        "EventMessageType": EventMessageType, "PermissionType": PermissionType,
        "PlatformAdapterType": PlatformAdapterType, "MessageChain": MessageChain,
        "logger": logging.getLogger("astrbot"), "ProviderRequest": object, "LLMResponse": object,
    })
    all_attrs = {key: value for key, value in all_attrs.items() if not key.startswith("__")}
    all_mod = _module("astrbot.api.all", **all_attrs)

    root = _module("astrbot", api=api_mod, core=None, api_all=all_mod)
    setattr(root, SHIM_MARK, True)
    setattr(root, "__version__", ASTRBOT_VERSION)
    setattr(api_mod, "__version__", ASTRBOT_VERSION)
    setattr(all_mod, "__version__", ASTRBOT_VERSION)

    # 官方推荐的大文件存储路径写法：
    #   Path(get_astrbot_data_path()) / "plugin_data" / self.name
    def _get_astrbot_data_path():
        from core import paths
        return paths.DATA_DIR

    utils_mod = _module("astrbot.core.utils")
    path_mod = _module("astrbot.core.utils.astrbot_path",
                       get_astrbot_data_path=_get_astrbot_data_path)
    utils_mod.astrbot_path = path_mod

    # 常见的老写法：from astrbot.core import ... / astrbot.core.star
    core_mod = _module("astrbot.core", star=star_mod, event=event_mod, utils=utils_mod,
                       astrbot_path=path_mod)
    sys.modules["astrbot.core.star"] = star_mod
    root.core = core_mod
    # 少数插件会 import astrbot.core.config / astrbot.core.message（导不到就报 ImportError）
    sys.modules["astrbot.core.config"] = _module("astrbot.core.config",
                                                 AstrBotConfig=AstrBotConfig)
