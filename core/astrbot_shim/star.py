# -*- coding: utf-8 -*-
"""插件侧 API（Star / PluginRuntime / StarTools / AstrBotConfig）。

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
from core.astrbot_shim.base import logger
from core.astrbot_shim.base import logger




# ======================================================================================
# Star / StarTools / Context / Provider
# ======================================================================================
class PluginRuntime:
    """暴露给插件的"运行时"白名单。

    插件拿到的是这个门面，而不是本程序的 `Runtime`：
    拿不到 `runtime.config`（全部密钥）、`runtime.store`（消息库）、`runtime.media`（文件）等。
    只保留发送与只读查询能力。
    """

    def __init__(self, runtime=None, logger_obj: logging.Logger = None):
        self._runtime = runtime          # 私有引用（Python 拦不住越权，但不再是"顺手可得"）
        self._log = logger_obj or logger

    @property
    def available(self) -> bool:
        return self._runtime is not None

    def send_text(self, target_type: str, openid: str, content: str, bot_id: str = "",
                  **kwargs) -> bool:
        runtime = self._runtime
        if runtime is None:
            return False
        return bool(runtime.send_text(target_type, openid, content, bot_id=bot_id, **kwargs))

    def send_image(self, target_type: str, openid: str, image_url: str = "", blob: bytes = None,
                   file_name: str = "image.png", content: str = "", bot_id: str = "") -> bool:
        runtime = self._runtime
        if runtime is None:
            return False
        return bool(runtime.send_image(target_type, openid, image_url=image_url, blob=blob,
                                       file_name=file_name, content=content, bot_id=bot_id))

    def send_file(self, target_type: str, openid: str, blob: bytes, file_name: str,
                  content: str = "", bot_id: str = "", reply_msg_id: str = ""):
        runtime = self._runtime
        if runtime is None:
            return False
        return runtime.send_file(target_type, openid, blob, file_name, content=content,
                                 bot_id=bot_id, reply_msg_id=reply_msg_id)

    def bot_ids(self) -> List[str]:
        runtime = self._runtime
        if runtime is None:
            return []
        ids = getattr(runtime, "bot_ids", None)
        if callable(ids):
            try:
                return list(ids())
            except Exception:
                return []
        return list(getattr(runtime, "bots", {}) or {})

    def lookup_name(self, openid: str) -> str:
        runtime = self._runtime
        func = getattr(runtime, "lookup_name", None)
        try:
            return str(func(openid) or "") if callable(func) else ""
        except Exception:
            return ""

    def short_label(self, openid: str) -> str:
        runtime = self._runtime
        func = getattr(runtime, "short_label", None)
        try:
            return str(func(openid) or "") if callable(func) else ""
        except Exception:
            return ""

    def group_names(self) -> Dict[str, str]:
        runtime = self._runtime
        manager = getattr(runtime, "group_manager", None)
        func = getattr(manager, "group_names", None)
        try:
            return dict(func() or {}) if callable(func) else {}
        except Exception:
            return {}

    def stats_record(self, key: str, count: int = 1):
        runtime = self._runtime
        stats = getattr(runtime, "stats", None)
        func = getattr(stats, "record", None)
        if callable(func):
            try:
                return func(key, count)
            except Exception:
                return None
        return None

    def log(self, message: str, *args):
        self._log.info("[插件] " + str(message), *args)

    def info(self, message: str, *args):
        self._log.info("[插件] " + str(message), *args)

    def error(self, message: str, *args):
        self._log.error("[插件] " + str(message), *args)


class Star:
    """AstrBot 插件基类。

    提供官方文档里写在基类上的能力：`self.context`、`self.name`、
    以及简单 KV 存储 `put_kv_data / get_kv_data / delete_kv_data`（>= v4.9.2）。

    `name` 在**实例化之前**就会由宿主写到类上（官方也是这个行为），
    这样插件在 `__init__` 里就能用它拼数据目录：
    `Path(get_astrbot_data_path()) / "plugin_data" / self.name`。
    """

    # 宿主在实例化前写入真实插件名（来自 metadata.yaml / 目录名）
    name = ""

    def __init__(self, context=None):
        self.context = context
        if not getattr(self, "name", ""):
            self.name = self.__class__.__name__
        self._kv_path = ""

    # ---- KV 存储（官方 >= v4.9.2）----
    def _kv_file(self) -> str:
        base = ""
        if self.context is not None and getattr(self.context, "data_dir", ""):
            base = self.context.data_dir
        else:
            base = StarTools.get_data_dir(self.name)
        if not self._kv_path:
            self._kv_path = os.path.join(base, "kv.json")
        return self._kv_path

    def _kv_load(self) -> Dict[str, Any]:
        try:
            with open(self._kv_file(), "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _kv_save(self, data: Dict[str, Any]):
        path = self._kv_file()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    async def put_kv_data(self, key: str, value: Any):
        data = self._kv_load()
        data[str(key)] = value
        self._kv_save(data)

    async def get_kv_data(self, key: str, default: Any = None):
        return self._kv_load().get(str(key), default)

    async def delete_kv_data(self, key: str):
        data = self._kv_load()
        data.pop(str(key), None)
        self._kv_save(data)

    async def initialize(self):
        """可选的生命周期钩子（AstrBot 会在加载后调用）。"""
        return None

    async def terminate(self):
        """可选的生命周期钩子（AstrBot 会在停用/退出时调用）。"""
        return None


class StarTools:
    """AstrBot 提供的数据目录与 JSON 读写工具。

    目录按 AstrBot 官方约定：`data/plugin_data/<插件名>/`。
    宿主加载插件时会用 `set_resolver()` 把目录解析到**本程序插件管理器**的数据目录，
    保证和 `plugins.dir` / 云端同步看到的是同一个位置。
    """

    data_root = os.path.join("data", "plugin_data")
    _resolver: Optional[Callable[[str], str]] = None

    @classmethod
    def set_resolver(cls, resolver: Optional[Callable[[str], str]]):
        cls._resolver = resolver

    @classmethod
    def _dir(cls, name: str = "") -> str:
        base = ""
        if cls._resolver is not None:
            try:
                base = cls._resolver(name or "") or ""
            except Exception:
                base = ""
        if not base:
            base = os.path.join(cls.data_root, name or "")
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            pass
        return base

    @classmethod
    def get_data_dir(cls, name: str = "") -> str:
        return cls._dir(name)

    @classmethod
    def get_data_path(cls, name: str = "") -> str:
        return cls._dir(name)

    @classmethod
    def save_json(cls, path: str, data: Any):
        target = path if os.path.isabs(path) else os.path.join(cls.data_root, path)
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)

    @classmethod
    def load_json(cls, path: str, default=None):
        target = path if os.path.isabs(path) else os.path.join(cls.data_root, path)
        try:
            with open(target, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return default


class AstrBotConfig(dict):
    """插件配置：`self.context.get_config("插件名")` 的返回。"""

    def __init__(self, data: Dict[str, Any] = None, path: str = ""):
        super().__init__(data or {})
        self._path = path

    def save_config(self):
        if not self._path:
            return
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(dict(self), handle, ensure_ascii=False, indent=2)
