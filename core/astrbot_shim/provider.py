# -*- coding: utf-8 -*-
"""AI Provider 与 Context 桥接。

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
from core.astrbot_shim.star import AstrBotConfig
from core.astrbot_shim.compat_utils import _deep_update, _schema_defaults




class _ProviderShim:
    """把 AstrBot 的 provider 接口桥接到本程序的 AI 配置。"""

    def __init__(self, runtime=None, provider_config=None):
        self.runtime = runtime
        self.provider_config = provider_config or {}
        self.meta = types.SimpleNamespace(id="qqbot-merged", model=provider_config.get("model", ""),
                                          type="chat_completion")

    def _client(self):
        runtime = self.runtime
        if runtime is None:
            return None
        for attr in ("ai_client",):
            client = getattr(runtime, attr, None)
            if client is not None:
                return client
        return None

    @property
    def usable(self) -> bool:
        """本程序的 AI 是否可用（插件用它决定要不要"让给内置 AI"）。"""
        client = self._client()
        return bool(client is not None and getattr(client, "usable", False))

    async def text_chat(self, prompt: str = "", session_id: str = "", contexts=None,
                        system_prompt: str = "", **kwargs):
        client = self._client()
        if client is None:
            raise RuntimeError("本程序没有配置可用的 AI（请到「设置 → AI 接入」填写）")
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        for item in contexts or []:
            if isinstance(item, dict):
                messages.append(item)
        messages.append({"role": "user", "content": str(prompt or "")})

        def call():
            for name in ("chat", "complete", "ask", "reply"):
                func = getattr(client, name, None)
                if callable(func):
                    return func(messages)
            raise RuntimeError("AI 客户端缺少可用的对话方法")

        text = await asyncio.get_event_loop().run_in_executor(None, call)
        if isinstance(text, dict):
            text = text.get("content") or text.get("text") or ""
        return types.SimpleNamespace(completion_text=str(text or ""), role="assistant")

    async def text_chat_stream(self, *args, **kwargs):
        result = await self.text_chat(*args, **kwargs)
        yield result


class _PlatformManagerShim:
    def get_insts(self):
        return []

    def get_inst(self, *args, **kwargs):
        return None


class ProviderRequest:
    """`@filter.on_llm_request()` 里拿到的请求对象（只实现插件最常用的几个字段）。"""

    def __init__(self, prompt: str = "", system_prompt: str = "", contexts: List[Any] = None,
                 image_urls: List[str] = None):
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.contexts = contexts or []
        self.image_urls = image_urls or []
        self.extra_user_content_parts: List[Any] = []
        self.func_tool = None

    def extra_user_text(self) -> str:
        """把插件通过 extra_user_content_parts 追加的文本拼起来（本程序的做法）。"""
        chunks: List[str] = []
        for part in self.extra_user_content_parts:
            text = getattr(part, "text", None)
            if text:
                chunks.append(str(text))
            elif isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and part.get("text"):
                chunks.append(str(part["text"]))
        return "\n".join(chunks)


class Context:
    """AstrBot 的 `Context`：插件通过它拿配置、发消息、调 AI。"""

    def __init__(self, plugin_name: str = "", runtime=None, plugin_dir: str = "",
                 data_dir: str = "", logger_obj: logging.Logger = None, host=None):
        self.plugin_name = plugin_name
        self.runtime = runtime
        self.plugin_dir = plugin_dir
        self.data_dir = data_dir
        self.log = logger_obj or logger
        self.logger = self.log
        self.host = host
        self.unsupported: List[str] = []

    # ---------- 配置 ----------
    def _config_path(self) -> str:
        """插件配置的存放位置（与 AstrBot 一致：`data/config/<插件名>_config.json`）。"""
        from core import paths
        return os.path.join(paths.DATA_DIR, "config", f"{self.plugin_name}_config.json")

    def schema_defaults(self) -> Dict[str, Any]:
        """按 `_conf_schema.json` 递归生成默认值（object 类型会往下钻 items）。"""
        schema_path = os.path.join(self.plugin_dir or "", "_conf_schema.json")
        try:
            with open(schema_path, "r", encoding="utf-8") as handle:
                schema = json.load(handle) or {}
        except (OSError, ValueError):
            return {}
        return _schema_defaults(schema)

    def load_config(self) -> AstrBotConfig:
        """读取插件配置：schema 默认值 + 用户在 `data/config/...` 里的值。"""
        data = self.schema_defaults()
        path = self._config_path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle) or {}
            if isinstance(stored, dict):
                _deep_update(data, stored)
        except (OSError, ValueError):
            pass
        return AstrBotConfig(data, path)

    def get_config(self, plugin_name: str = "") -> AstrBotConfig:
        if plugin_name and plugin_name != self.plugin_name:
            return AstrBotConfig({}, "")
        return self.load_config()

    # ---------- 发消息 ----------
    async def send_message(self, unified_msg_origin: str, chain: Any):
        if self.host is None:
            return False
        components = chain if isinstance(chain, (list, tuple)) else [chain]
        return self.host.send_to_umo(unified_msg_origin, components)

    # ---------- AI ----------
    def get_using_provider(self, provider_type: str = ""):
        provider_config = {}
        if self.runtime is not None:
            try:
                provider_config = {"model": self.runtime.config.str_of("ai", "model", default="")}
            except Exception:
                provider_config = {}
        return _ProviderShim(self.runtime, provider_config)

    def get_all_providers(self):
        return [self.get_using_provider()]

    def get_platform_manager(self):
        return _PlatformManagerShim()

    def get_event_queue(self):
        return None

    def get_all_stars(self):
        return []

    # ---------- 明确不支持的能力 ----------
    def register_web_api(self, *args, **kwargs):
        self._unsupported("register_web_api（插件自带网页接口）")

    def register_llm_tool(self, *args, **kwargs):
        self._unsupported("register_llm_tool（给 LLM 注册函数工具）")

    def _unsupported(self, what: str):
        if what not in self.unsupported:
            self.unsupported.append(what)
        self.log.warning("[AstrBot 兼容] %s 暂不支持（插件：%s）", what, self.plugin_name)

    def __getattr__(self, item):
        """访问到本程序没实现的能力时，给出明确提示（而不是一个费解的 AttributeError）。"""
        if item.startswith("_"):
            raise AttributeError(item)

        def missing(*_args, **_kwargs):
            self._unsupported(f"context.{item}()")
            raise NotImplementedError(
                f"本程序（QQ 机器人合并版）没有提供 AstrBot 的 context.{item}()，"
                f"插件 {self.plugin_name} 的这部分功能无法使用")

        return missing
