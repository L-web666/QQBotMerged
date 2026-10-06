# -*- coding: utf-8 -*-
"""过滤器命名空间与 @register（filter / _FilterNamespace）。

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
from core.astrbot_shim.components import EventMessageType, PermissionType, PlatformAdapterType




# ======================================================================================
# 过滤器（装饰器）
# ======================================================================================
class _CommandGroup:
    """`@filter.command_group("组名")` 的返回值。

    官方写法是：`@filter.command_group("math")` 装饰一个**函数**，随后用
    `@math.command("add")` 注册子指令、`@math.group("calc")` 继续嵌套。
    所以装饰器必须返回**组对象本身**（保持 `.command()` / `.group()` 可用），
    而不是那个函数。
    """

    def __init__(self, name: str, **options):
        self.name = name
        self.options = options
        self.func = None

    def command(self, name: str = "", alias=None, **options):
        full = f"{self.name} {name}".strip()

        def decorator(func):
            _mark(func, "command", {"name": full, "alias": alias or set(), **options})
            return func

        return decorator

    def group(self, name: str = "", **options):
        """嵌套指令组（官方支持无限嵌套）。"""
        return _CommandGroup(f"{self.name} {name}".strip(), **options)

    def command_group(self, name: str = "", **options):
        return self.group(name, **options)

    def __call__(self, func):
        # 记录组函数本身（便于诊断），并把组对象返回给外部继续注册子指令
        self.func = func
        _mark(func, "command_group", {"name": self.name, **self.options})
        return self

    def __repr__(self):
        return f"<command_group {self.name!r}>"


class _FilterNamespace:
    """`astrbot.api.event.filter`：各类装饰器都在这里。"""

    EventMessageType = EventMessageType
    PermissionType = PermissionType
    PlatformAdapterType = PlatformAdapterType

    # ---------- 指令 ----------
    def command(self, name: str, alias=None, priority: int = 0, **options):
        def decorator(func):
            _mark(func, "command", {"name": name, "alias": alias or set(),
                                    "priority": priority, **options})
            return func

        return decorator

    def command_group(self, name: str, **options):
        return _CommandGroup(name, **options)

    def regex(self, pattern: str, priority: int = 0, **options):
        def decorator(func):
            _mark(func, "regex", {"pattern": pattern, "priority": priority, **options})
            return func

        return decorator

    def event_message_type(self, event_types=None, **options):
        types_list = event_types if isinstance(event_types, (list, tuple, set)) else [event_types]

        def decorator(func):
            _mark(func, "event_message_type", {"types": list(types_list), **options})
            return func

        return decorator

    def permission_type(self, permission=None, **options):
        def decorator(func):
            _mark(func, "permission_type", {"permission": permission, **options})
            return func

        return decorator

    def platform_adapter_type(self, platform_types=None, **options):
        """按消息平台过滤。本程序是 QQ 官方机器人平台（QQOFFICIAL）。"""

        def decorator(func):
            _mark(func, "platform_adapter_type", {"platforms": platform_types, **options})
            return func

        return decorator

    # ---------- 生命周期 / LLM 钩子 ----------
    def on_astrbot_loaded(self):
        def decorator(func):
            _mark(func, "on_loaded", {})
            return func

        return decorator

    def on_plugin_loaded(self):
        def decorator(func):
            _mark(func, "on_plugin_loaded", {})
            return decorator

    def on_llm_request(self):
        def decorator(func):
            _mark(func, "on_llm_request", {})
            return func

        return decorator

    def on_llm_response(self):
        def decorator(func):
            _mark(func, "on_llm_response", {})
            return func

        return decorator

    def after_message_sent(self):
        def decorator(func):
            _mark(func, "after_message_sent", {})
            return func

        return decorator

    # ---------- 暂不支持 ----------
    def llm_tool(self, name: str = "", **options):
        def decorator(func):
            _mark(func, "unsupported", {
                "reason": f"llm_tool({name or func.__name__})：把函数注册成 LLM 工具，本程序暂不支持"})
            return func

        return decorator

    def __getattr__(self, item):
        """没实现的过滤器：不要直接崩，标记成"不支持"并让加载器给出诊断。"""
        if item.startswith("_"):
            raise AttributeError(item)

        def decorator(*_args, **_kwargs):
            def wrapper(func):
                _mark(func, "unsupported", {"reason": f"filter.{item}() 暂不支持"})
                return func

            return wrapper

        return decorator


FILTERS_ATTR = "__astrbot_filters__"


def _mark(func, kind: str, options: Dict[str, Any]):
    items = getattr(func, FILTERS_ATTR, None)
    if items is None:
        items = []
        try:
            setattr(func, FILTERS_ATTR, items)
        except AttributeError:
            return func
    items.append({"kind": kind, "options": options})
    return func


REGISTRY: List[Dict[str, Any]] = []


def register(name: str, author: str = "", desc: str = "", version: str = "",
             repo: str = "", **kwargs):
    """`@register("名字", "作者", "简介", "1.0.0")` —— AstrBot 插件的注册装饰器。"""

    def decorator(cls):
        cls.__astrbot_meta__ = {
            "name": name or cls.__name__,
            "author": author or "",
            "desc": desc or "",
            "version": version or "1.0.0",
            "repo": repo or "",
        }
        REGISTRY.append({"cls": cls, "meta": dict(cls.__astrbot_meta__)})
        return cls

    return decorator
