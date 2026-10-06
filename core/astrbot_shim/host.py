# -*- coding: utf-8 -*-
"""插件加载与消息分发（AstrBotPlugin / AstrBotHost）。

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
from core.astrbot_shim.base import ASTRBOT_VERSION, logger
from core.astrbot_shim.base import ASTRBOT_VERSION, logger
from core.astrbot_shim.runner import RUNNER
from core.astrbot_shim.components import AstrBotMessage, AstrMessageEvent, At, BaseMessageComponent, EventMessageType, File, Group, Image, MessageChain, MessageEventResult, MessageMember, MessageType, PermissionType, Plain, PlatformAdapterType, Record, Video
from core.astrbot_shim.filters import FILTERS_ATTR, REGISTRY
from core.astrbot_shim.star import AstrBotConfig, Star, StarTools
from core.astrbot_shim.provider import Context, ProviderRequest
from core.astrbot_shim.compat_utils import _load_yaml, _version_satisfies
from core.astrbot_shim.install import install_shim




class AstrBotPlugin:
    """一个已加载的 AstrBot 插件。"""

    def __init__(self, name: str, path: str, meta: Dict[str, Any],
                 instance: Any, handlers: List[Dict[str, Any]], context: Context,
                 module: Any = None):
        self.name = name
        self.path = path
        self.meta = meta
        self.instance = instance
        self.handlers = handlers
        self.context = context
        self.module = module
        self.unsupported: List[str] = []
        self.load_error = ""

    def info(self) -> Dict[str, Any]:
        kinds = sorted({item["kind"] for item in self.handlers})
        commands = [item["options"].get("name") for handler in self.handlers
                    for item in handler.get("filters") or [] if item["kind"] == "command"]
        return {
            "name": self.meta.get("name") or self.name,
            "module": self.name,
            "title": self.meta.get("display_name") or self.meta.get("name") or self.name,
            "description": self.meta.get("desc") or self.meta.get("short_desc") or "",
            "version": str(self.meta.get("version") or "1.0.0"),
            "author": str(self.meta.get("author") or ""),
            "repo": str(self.meta.get("repo") or ""),
            "astrbot_version": str(self.meta.get("astrbot_version") or ""),
            "support_platforms": list(self.meta.get("support_platforms") or []),
            "tags": list(self.meta.get("tags") or []),
            "file": "main.py",
            "path": self.path,
            "kind": "astrbot",
            "format": "AstrBot",
            "commands": [item for item in commands if item],
            "keywords": [],
            "handler_kinds": kinds,
            "handlers": [{"kind": item["kind"], "name": item["name"]} for item in self.handlers],
            "unsupported": list(self.unsupported),
        }


class AstrBotHost:
    """AstrBot 插件的加载、匹配与执行。"""

    def __init__(self, plugin_manager=None, logger_obj: logging.Logger = None):
        self.manager = plugin_manager
        self.log = logger_obj or logger
        self.plugins: List[AstrBotPlugin] = []
        self.issues: List[Dict[str, str]] = []
        self.runtime = None
        self.bot = None
        self.loaded_hooks: List[Tuple[AstrBotPlugin, Callable]] = []
        self._emit_sink: Optional[Callable[[List[Any]], None]] = None
        self.stop = False
        self.astrbot_version = ASTRBOT_VERSION
        # 插件的 StarTools 数据目录跟随本程序的插件数据目录
        StarTools.set_resolver(self.data_dir_for)

    # ------------------------------------------------------------------ 工具
    def _note(self, name: str, reason: str, level: str = "error"):
        self.issues.append({"name": name, "reason": reason, "level": level})
        (self.log.error if level == "error" else self.log.warning)(
            "AstrBot 插件 %s %s", name, reason)

    @staticmethod
    def is_astrbot_plugin_dir(path: str) -> bool:
        if not os.path.isdir(path):
            return False
        has_meta = any(os.path.isfile(os.path.join(path, name))
                       for name in ("metadata.yaml", "metadata.yml"))
        has_entry = any(os.path.isfile(os.path.join(path, name))
                        for name in ("main.py", "__init__.py"))
        return has_meta and has_entry

    def data_dir_for(self, name: str) -> str:
        if self.manager is not None:
            return os.path.join(self.manager.data_dir, name)
        return StarTools.get_data_dir(name)

    # ------------------------------------------------------------------ 加载
    def load(self, name: str, dir_path: str) -> Optional[AstrBotPlugin]:
        install_shim()
        meta = {}
        for candidate in ("metadata.yaml", "metadata.yml"):
            full = os.path.join(dir_path, candidate)
            if os.path.isfile(full):
                meta = _load_yaml(full)
                break
        entry = os.path.join(dir_path, "main.py")
        if not os.path.isfile(entry):
            entry = os.path.join(dir_path, "__init__.py")
        if not os.path.isfile(entry):
            self._note(name, "AstrBot 插件目录里找不到 main.py")
            return None

        try:
            sys.modules.pop("_qqbot_astrbot_" + name, None)
            spec = importlib.util.spec_from_file_location("_qqbot_astrbot_" + name, entry)
            if spec is None or spec.loader is None:
                raise ImportError("无法创建模块规格")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            data_dir = self.data_dir_for(name)
            try:
                os.makedirs(data_dir, exist_ok=True)
            except OSError:
                pass
            # 插件目录加入 sys.path：插件内部的 `import utils` 之类才找得到
            if dir_path not in sys.path:
                sys.path.insert(0, dir_path)
            before = len(REGISTRY)
            spec.loader.exec_module(module)
        except SyntaxError as exc:
            self._note(name, f"语法错误（第 {exc.lineno} 行）：{exc.msg}  ← {entry}")
            return None
        except Exception as exc:
            self._note(name, f"导入失败：{type(exc).__name__}: {exc}  ← {entry}")
            return None
        finally:
            # 重新加载时清掉同一个模块以前注册的类，避免 REGISTRY 无限增长
            module_name = "_qqbot_astrbot_" + name
            REGISTRY[:] = [item for item in REGISTRY
                           if getattr(item["cls"], "__module__", "") != module_name]

        classes = [item["cls"] for item in REGISTRY[before:]]
        if not classes:
            # 没写 @register 的情况：自己在模块里找 Star 子类
            classes = [value for value in vars(module).values()
                       if inspect.isclass(value) and issubclass(value, Star)
                       and value is not Star and value.__module__ == module.__name__]
        if not classes:
            self._note(name, "没有找到带 @register 的 Star 子类（不是有效的 AstrBot 插件）")
            return None

        # 插件名（优先 metadata.yaml 的 name，其次目录名）。
        # 必须在**实例化之前**确定：官方行为是插件在 __init__ 里就能用 self.name
        # 拼数据目录（Path(get_astrbot_data_path()) / "plugin_data" / self.name），
        # 这里把它写到类属性上，实例化后 Star.__init__ 就不会再被类名覆盖。
        plugin_name = str(meta.get("name") or name or "")
        context = Context(plugin_name=plugin_name, runtime=self.runtime, plugin_dir=dir_path,
                          data_dir=data_dir, logger_obj=self.log, host=self)
        instance = None
        cls_meta = {}
        config = context.load_config()
        for cls in classes:
            cls_meta = dict(getattr(cls, "__astrbot_meta__", {}) or {})
            plugin_name = str(meta.get("name") or cls_meta.get("name") or name or "")
            try:
                setattr(cls, "name", plugin_name)
                instance = self._instantiate(cls, context, config)
            except Exception as exc:
                self._note(name, f"实例化失败：{type(exc).__name__}: {exc}")
                return None
            break

        merged_meta = {"name": plugin_name or name,
                       "display_name": meta.get("display_name") or cls_meta.get("name") or name,
                       "short_desc": meta.get("short_desc") or "",
                       "author": meta.get("author") or cls_meta.get("author") or "",
                       "desc": meta.get("desc") or meta.get("description")
                       or cls_meta.get("desc") or "",
                       "version": meta.get("version") or cls_meta.get("version") or "1.0.0",
                       "repo": meta.get("repo") or cls_meta.get("repo") or "",
                       "astrbot_version": str(meta.get("astrbot_version") or ""),
                       "support_platforms": meta.get("support_platforms") or [],
                       "tags": meta.get("tags") or []}
        # 插件名以 metadata.yaml 为准（AstrBot 用 self.name 定位数据目录）
        try:
            instance.name = str(merged_meta["name"])
        except Exception:
            pass
        self._check_metadata(merged_meta, name)
        handlers = self._collect_handlers(instance)
        plugin = AstrBotPlugin(name=name, path=entry, meta=merged_meta, instance=instance,
                               handlers=handlers, context=context, module=module)
        # 用了暂不支持的能力 → 明确写一条诊断（而不是静默不生效）
        unsupported: List[str] = []
        for handler in handlers:
            for item in handler.get("filters") or []:
                if item["kind"] == "unsupported":
                    unsupported.append(f"{handler['name']}：{item['options'].get('reason', '')}")
        # 只用了"不支持的能力"的方法不会成为消息处理器，但也要让用户看到
        for klass in type(instance).__mro__:
            for attr_name, value in vars(klass).items():
                for item in (getattr(value, FILTERS_ATTR, None) or []):
                    if item["kind"] == "unsupported":
                        reason = f"{attr_name}：{item['options'].get('reason', '')}"
                        if reason not in unsupported:
                            unsupported.append(reason)
        if unsupported:
            self._note(plugin.name,
                       "用到了暂不支持的能力，这些处理器不会生效：" + "；".join(unsupported[:4]),
                       level="warning")
        if context.unsupported:
            for what in context.unsupported:
                self._note(plugin.name, f"{what}（插件已加载，但这些功能不会生效）", level="warning")
        self.plugins.append(plugin)
        self.log.info("AstrBot 插件 %s 已加载（%d 个处理器：%s）", plugin.name, len(handlers),
                      "、".join(sorted({item["kind"] for item in handlers})) or "无")
        if not handlers:
            self._note(plugin.name, "没有解析到任何处理器（没有 @filter.command/regex 等装饰器）",
                       level="warning")
        plugin.unsupported = list(dict.fromkeys(unsupported + list(context.unsupported)))
        return plugin

    # 能真正"触发一次消息处理"的过滤器；只有这些（或与之组合）的方法才算消息处理器
    MESSAGE_FILTER_KINDS = ("command", "regex", "event_message_type",
                            "platform_adapter_type", "permission_type")

    @classmethod
    def _collect_handlers(cls, instance: Any) -> List[Dict[str, Any]]:
        """每个方法收集成**一个**处理器，方法上的多个装饰器按"与"关系判断。

        AstrBot 里
            @filter.event_message_type(GROUP_MESSAGE)
            @filter.permission_type(ADMIN)
            @filter.command("管理测试")
            async def f(self, event): ...
        表示"群聊 + 管理员 + 指令是 管理测试"三个条件都要满足。

        注意：**只有"不支持的能力"过滤器（例如 llm_tool）或只有生命周期钩子的方法不算
        消息处理器**——否则它会匹配所有消息、把消息吞掉（曾经真的发生过）。
        """
        handlers: List[Dict[str, Any]] = []
        seen = set()
        for klass in type(instance).__mro__:
            for attr_name, value in vars(klass).items():
                if attr_name in seen:
                    continue
                filters = getattr(value, FILTERS_ATTR, None)
                if not filters:
                    continue
                seen.add(attr_name)
                bound = getattr(instance, attr_name, None)
                if not callable(bound):
                    continue
                usable = [item for item in filters
                          if item["kind"] in cls.MESSAGE_FILTER_KINDS]
                if not usable:
                    continue
                handlers.append({"filters": usable, "func": bound, "name": attr_name,
                                 "kind": "+".join(item["kind"] for item in usable)})
        return handlers

    @staticmethod
    def _instantiate(cls, context: Context, config: AstrBotConfig):
        """按官方签名实例化插件。

        官方文档里插件是 `def __init__(self, context: Context, config: AstrBotConfig)`，
        也有只写 `(self, context)` 的；两种都要支持，所以先看签名再决定传几个参数。
        """
        try:
            params = [item for item in inspect.signature(cls.__init__).parameters.values()
                      if item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD)
                      and item.name != "self"]
            wanted = len(params)
        except (TypeError, ValueError):
            wanted = 1
        if wanted >= 2:
            return cls(context, config)
        if wanted == 1:
            return cls(context)
        return cls()

    def _check_metadata(self, meta: Dict[str, Any], name: str):
        """按官方 metadata.yaml 约定做兼容性提示（不阻止加载）。"""
        required = str(meta.get("astrbot_version") or "").strip()
        if required and not _version_satisfies(ASTRBOT_VERSION, required):
            self._note(name,
                       f"该插件要求 AstrBot {required}，本程序模拟的是 {ASTRBOT_VERSION}，"
                       "可能缺少较新的 API（能加载，但个别功能可能不生效）",
                       level="warning")
        platforms = [str(item).lower() for item in (meta.get("support_platforms") or [])]
        if platforms and not any(item in ("qq_official", "qqofficial", "all") for item in platforms):
            self._note(name,
                       f"该插件声明的支持平台是 {platforms}，不含 QQ 官方机器人（qq_official），"
                       "在本程序里可能无法正常工作", level="warning")

    def unload(self):
        for plugin in self.plugins:
            try:
                RUNNER.run(plugin.instance.terminate(), timeout=10)
            except Exception as exc:
                self.log.debug("AstrBot 插件 %s terminate 失败：%s", plugin.name, exc)
        self.plugins = []
        self.issues = []

    def call_loaded_hooks(self):
        for plugin in self.plugins:
            for cls in type(plugin.instance).__mro__:
                for attr_name, value in vars(cls).items():
                    filters = [item for item in (getattr(value, FILTERS_ATTR, None) or [])
                               if item["kind"] in ("on_loaded", "on_plugin_loaded")]
                    if not filters:
                        continue
                    func = getattr(plugin.instance, attr_name, None)
                    if not callable(func):
                        continue
                    try:
                        self._invoke(func, None)
                    except Exception as exc:
                        self.log.error("AstrBot 插件 %s 的加载钩子 %s 执行失败：%s",
                                       plugin.name, attr_name, exc)

    def call_initialize(self):
        for plugin in self.plugins:
            func = getattr(plugin.instance, "initialize", None)
            if not callable(func):
                continue
            try:
                result = func()
                if inspect.isawaitable(result):
                    RUNNER.run(result, timeout=30)
            except Exception as exc:
                self._note(plugin.name, f"initialize() 执行失败：{exc}", level="warning")

    # ------------------------------------------------------------------ 执行
    @staticmethod
    def _invoke(func: Callable, event: Optional[AstrMessageEvent], args: List[Any] = None):
        """调用处理器（同步/异步都支持），返回原始结果。"""
        call_args: List[Any] = []
        if event is not None:
            call_args.append(event)
        if args:
            call_args.extend(args)
        result = func(*call_args)
        if inspect.isasyncgen(result):
            return RUNNER.collect(result)
        if inspect.isawaitable(result):
            return RUNNER.run(result)
        if inspect.isgenerator(result):
            return list(result)
        return result

    @staticmethod
    def _parse_args(func: Callable, content: str, command_names: List[str]) -> List[Any]:
        """把 `/add 1 2` 这样的参数按 handler 的类型注解解析出来（官方行为）。

        官方文档：`async def add(self, event, a: int, b: int)` → `/add 1 2` → a=1, b=2。
        参数不够或类型不匹配时返回 None（表示这次不该触发）。
        """
        try:
            params = [item for item in inspect.signature(func).parameters.values()
                      if item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD)
                      and item.name not in ("self", "event")]
        except (TypeError, ValueError):
            return []
        if not params:
            return []
        text = str(content or "").strip()
        first = text.split()[0] if text.split() else ""
        stripped = first[1:] if first.startswith("/") else first
        # 去掉指令名（含多级指令组的每一段）
        rest = text
        for name in sorted(command_names, key=len, reverse=True):
            if not name:
                continue
            for prefix in (name, "/" + name):
                if rest == prefix:
                    rest = ""
                    break
                if rest.startswith(prefix + " ") or rest.startswith(prefix + "\n"):
                    rest = rest[len(prefix):]
                    break
            if rest == "":
                break
        tokens = rest.split()
        out: List[Any] = []
        for index, param in enumerate(params):
            if param.kind == param.VAR_POSITIONAL:
                out.extend(tokens[index:])
                break
            if index >= len(tokens):
                if param.default is not param.empty:
                    out.append(param.default)
                    continue
                return None                      # 参数不够 → 不触发（让用户看到"用法错误"）
            raw = tokens[index]
            annotation = param.annotation
            try:
                if annotation is int:
                    out.append(int(raw))
                elif annotation is float:
                    out.append(float(raw))
                elif annotation is bool:
                    out.append(str(raw).lower() in ("1", "true", "yes", "on", "是", "开"))
                elif annotation is str or annotation is param.empty:
                    out.append(raw)
                else:
                    out.append(raw)
            except (TypeError, ValueError):
                return None
        return out

    def build_event(self, msg: Dict[str, Any], runtime=None) -> AstrMessageEvent:
        is_group = (msg.get("type") or "private") == "group"
        session_id = (msg.get("group_openid") or "") if is_group else (msg.get("user_openid") or "")
        unified = f"qq_official:{'GroupMessage' if is_group else 'FriendMessage'}:{session_id}"
        group_name = msg.get("group_name") or ""
        message_obj = AstrBotMessage(
            type=MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE,
            self_id=(msg.get("bot_id") or ""),
            session_id=session_id,
            message_id=msg.get("msg_id") or "",
            group_id=session_id if is_group else "",
            group=Group(session_id, group_name) if is_group else None,
            sender=MessageMember(msg.get("user_openid") or msg.get("member_openid") or "",
                                 msg.get("user_name") or ""),
            message=[],
            message_str=msg.get("content") or "",
            timestamp=msg.get("ts") or time.time(),
            unified_msg_origin=unified,
        )
        event = AstrMessageEvent(message_obj, msg.get("content") or "",
                                 context=None, bridge=self)
        event.role = "admin" if msg.get("is_admin") else "member"
        event._qqbot_msg = msg          # 方便排查
        return event

    def _matches(self, handler: Dict[str, Any], msg: Dict[str, Any], event) -> bool:
        """一个方法上的所有过滤器都要满足；其中"指令/正则"是触发条件（满足其一即可）。"""
        filters = handler.get("filters") or []
        triggers = [item for item in filters if item["kind"] in ("command", "regex")]
        conditions = [item for item in filters if item["kind"] not in ("command", "regex")]
        if triggers:
            if not any(self._match_one(item, msg, event) for item in triggers):
                return False
        return all(self._match_one(item, msg, event) for item in conditions)

    def _match_one(self, item: Dict[str, Any], msg: Dict[str, Any], event) -> bool:
        kind, options = item["kind"], item["options"]
        content = str(msg.get("content") or "").strip()
        if kind == "command":
            name = str(options.get("name") or "")
            alias = options.get("alias") or set()
            names = {name, *{str(alias_item) for alias_item in alias}}
            first = content.split()[0] if content.split() else ""
            stripped = first[1:] if first.startswith("/") else first
            return (stripped in names or content == name
                    or content.startswith(name + " ") or content.startswith("/" + name + " "))
        if kind == "regex":
            import re
            try:
                return re.search(str(options.get("pattern") or ""), content) is not None
            except re.error:
                return False
        if kind == "event_message_type":
            types_list = options.get("types") or []
            if not types_list or EventMessageType.ALL in types_list:
                return True
            is_group = (msg.get("type") or "private") == "group"
            wanted = EventMessageType.GROUP_MESSAGE if is_group else EventMessageType.PRIVATE_MESSAGE
            return wanted in types_list
        if kind == "platform_adapter_type":
            platforms = options.get("platforms")
            if platforms is None:
                return True
            mask = PlatformAdapterType.QQOFFICIAL
            try:
                if isinstance(platforms, (list, tuple, set)):
                    allowed = PlatformAdapterType(0)
                    for item in platforms:
                        allowed |= PlatformAdapterType(item)
                else:
                    allowed = PlatformAdapterType(platforms)
            except (TypeError, ValueError):
                return True
            return bool(allowed & mask) or bool(allowed & PlatformAdapterType.ALL)
        if kind == "permission_type":
            permission = options.get("permission")
            if permission in (None, PermissionType.ALL):
                return True
            if permission == PermissionType.ADMIN:
                return bool(msg.get("is_admin"))
            if permission == PermissionType.MEMBER:
                return not msg.get("is_admin")
            return True
        if kind in ("on_llm_request", "on_llm_response", "after_message_sent"):
            return False            # 这些是钩子，不作为消息处理器参与匹配
        if kind == "unsupported":
            return True             # 不支持的过滤器不阻塞（加载时已给出诊断）
        if kind == "command_group":
            # 组本身不处理消息，子命令会各自注册
            return False
        return False

    def dispatch(self, msg: Dict[str, Any], runtime=None,
                 sink: Optional[Callable[[List[Any]], None]] = None,
                 allowed: Optional[set] = None) -> Optional[Dict[str, Any]]:
        """把消息交给 AstrBot 插件。返回 {'plugin','text','images','handled'} 或 None。

        `allowed` 非 None 时只分发其中的插件名（按机器人隔离：每个机器人可以启用
        不同的插件，见 `PluginManager.allowed_names`）。
        """
        if not self.plugins:
            return None
        self.runtime = runtime or self.runtime
        self._emit_sink = sink
        self._emit_used = False
        self.stop = False
        event = self.build_event(msg, runtime)
        collected: List[Any] = []
        matched: List[str] = []
        for plugin in self.plugins:
            if self.manager is not None and self.manager.is_disabled(plugin.meta.get("name")
                                                                    or plugin.name):
                continue
            if allowed is not None and (plugin.meta.get("name") or plugin.name) not in allowed:
                self.log.debug("插件 %s 未对机器人 %s 启用，跳过", plugin.name,
                               msg.get("bot_id") or "")
                continue
            for handler in plugin.handlers:
                if self.stop:
                    break
                if not self._matches(handler, msg, event):
                    continue
                command_names = [str(item["options"].get("name") or "")
                                 for item in handler.get("filters") or []
                                 if item["kind"] == "command"]
                call_args = None
                if command_names:
                    call_args = self._parse_args(handler["func"], msg.get("content") or "",
                                                 command_names)
                    if call_args is None:
                        # 参数不满足 handler 的签名：官方行为是提示用法，这里就跳过
                        self.log.debug("AstrBot 插件 %s 的 %s 参数不匹配，已跳过",
                                       plugin.name, handler["name"])
                        continue
                matched.append(f"{plugin.name}.{handler['name']}")
                try:
                    result = self._invoke(handler["func"], event, call_args)
                except Exception as exc:
                    self.log.error("AstrBot 插件 %s 的 %s 执行失败：%s",
                                   plugin.name, handler["name"], exc)
                    continue
                self._collect(result, collected)
        if not matched:
            return None
        text_parts: List[str] = []
        images: List[Any] = []
        sequence: List[Dict[str, Any]] = []
        for item in collected:
            self._split_result(item, text_parts, images)
            sequence.extend(self._result_sequence(item))
        if not text_parts and not images:
            if self._emit_used or event.is_stopped() or self.stop:
                # 插件要么已经自己发过消息（event.send），要么主动叫停（stop_event）：
                # 这条消息到此为止，不再走主链路。
                return {"plugin": ",".join(matched), "handled": True, "text": "", "images": [],
                        "sequence": [], "via_sink": bool(self._emit_used)}
            # 只是"匹配上了但什么都没做"（例如 Ollama 插件决定让给内置 AI）：
            # 按官方语义交回主链路，而不是把消息吞掉。
            self.log.debug("插件 %s 匹配了这条消息但没有任何回复，交回主链路",
                           ",".join(matched))
            return None
        return {"plugin": ",".join(matched), "handled": True,
                "text": "\n".join(part for part in text_parts if part),
                "images": images, "sequence": sequence,
                "via_sink": bool(self._emit_used)}

    @staticmethod
    def _collect(result: Any, sink: List[Any]):
        if result is None:
            return
        if isinstance(result, (list, tuple)):
            sink.extend(result)
        else:
            sink.append(result)

    @staticmethod
    def _result_sequence(item: Any) -> List[Dict[str, Any]]:
        """把一条结果拆成**有序**的发送步骤（文本 / 图片）。

        AstrBot 的消息链是有顺序的（文本、图片、文本…），而本程序的发送接口一次只能发
        文本或图片，所以这里按顺序保留步骤，由上层逐条发出（顺序不会乱）。
        """
        steps: List[Dict[str, Any]] = []

        def add_text(text: str):
            if text and text.strip():
                steps.append({"type": "text", "text": text})

        def add_image(url: str = "", blob: bytes = None, file_name: str = ""):
            if blob or url:
                steps.append({"type": "image", "url": url, "blob": blob,
                              "file_name": file_name or "image.png"})

        if item is None:
            return steps
        if isinstance(item, str):
            add_text(item)
            return steps
        if isinstance(item, MessageEventResult):
            chain = item.chain
        elif isinstance(item, (list, tuple, MessageChain)):
            chain = list(item)
        elif isinstance(item, BaseMessageComponent):
            chain = [item]
        elif isinstance(item, dict):
            if item.get("type") == "text":
                add_text(str(item.get("text") or ""))
            return steps
        else:
            add_text(str(item))
            return steps
        for component in chain:
            if isinstance(component, Plain):
                add_text(component.text)
            elif isinstance(component, str):
                add_text(component)
            elif isinstance(component, Image):
                url = component.url or component.file or component.path
                blob = component._blob_or_none()
                if blob is not None and not str(url).lower().startswith("http"):
                    add_image(blob=blob, file_name=component.file_name)
                else:
                    add_image(url=url, file_name=component.file_name)
            elif isinstance(component, File):
                if component.url or component.path or component.file:
                    add_image(url=component.url or component.path or component.file,
                              file_name=component.name)
            elif isinstance(component, At):
                if component.qq and component.qq != "all":
                    add_text(f"@{component.name or component.qq}")
            elif isinstance(component, (Record, Video)):
                add_text(f"[{'语音' if isinstance(component, Record) else '视频'}]")
        return steps

    @staticmethod
    def _split_result(item: Any, texts: List[str], images: List[Any]):
        """把结果按"文本 / 图片"聚合（顺序由 `_result_sequence` 负责）。"""
        for step in AstrBotHost._result_sequence(item):
            if step["type"] == "text":
                texts.append(step["text"])
            else:
                images.append(step)

    # ------------------------------------------------------------------ 主动发送
    def send_to_umo(self, umo: str, components: List[Any]) -> bool:
        """`context.send_message(unified_msg_origin, chain)` 的实现（保持消息链顺序）。"""
        runtime = self.runtime
        if runtime is None:
            return False
        parts = str(umo or "").split(":")
        if len(parts) < 3:
            self.log.warning("[AstrBot 兼容] unified_msg_origin 格式不对：%r", umo)
            return False
        is_group = parts[1].lower().startswith("group")
        openid = parts[2]
        steps: List[Dict[str, Any]] = []
        for component in components:
            steps.extend(self._result_sequence(component))
        if not steps:
            return False
        target = {"type": "group" if is_group else "private",
                  "group_openid": openid if is_group else "",
                  "user_openid": "" if is_group else openid,
                  "bot_id": ""}
        if self.manager is not None:
            self.manager.send_steps(runtime, target, steps)
            return True
        for step in steps:
            if step["type"] == "text":
                runtime.send_text("group" if is_group else "private", openid, step["text"])
            else:
                runtime.send_image("group" if is_group else "private", openid,
                                   image_url=step.get("url", ""), blob=step.get("blob"),
                                   file_name=step.get("file_name") or "image.png")
        return True

    def emit(self, components: List[Any]):
        """处理器内部 `await event.send(...)` 时把消息立刻发出去。"""
        sink = self._emit_sink
        if sink is not None and components:
            self._emit_used = True
            sink(list(components))

    # ------------------------------------------------------------------ LLM 钩子
    def build_llm_request(self, prompt: str, system_prompt: str,
                          image_urls: List[str] = None, bot_id: str = "") -> ProviderRequest:
        """构造 `ProviderRequest` 并跑一遍插件的 `on_llm_request` 钩子。

        官方签名是 `async def hook(self, event, req: ProviderRequest)`（三个参数），
        所以这里单独调用而不是走消息处理器那套。

        `bot_id` 用来按机器人隔离：没对该机器人启用的插件不会跑钩子。
        """
        request = ProviderRequest(prompt=prompt, system_prompt=system_prompt,
                                  image_urls=list(image_urls or []))
        event = self.build_event({"type": "private", "content": prompt}, self.runtime)
        for func in self.llm_request_hooks(bot_id):
            try:
                result = func(event, request)
                if inspect.isawaitable(result):
                    RUNNER.run(result, timeout=15)
            except Exception as exc:
                self.log.error("AstrBot 插件 on_llm_request 钩子执行失败：%s", exc)
        return request

    def llm_request_hooks(self, bot_id: str = "") -> List[Callable]:
        return self._hooks("on_llm_request", bot_id)

    def llm_response_hooks(self, bot_id: str = "") -> List[Callable]:
        return self._hooks("on_llm_response", bot_id)

    def _allowed_for(self, bot_id: str) -> Optional[set]:
        """按机器人取"允许的插件名集合"（None = 不限制）。"""
        if self.manager is None or not bot_id:
            return None
        try:
            return self.manager.allowed_names(bot_id)
        except Exception as exc:
            self.log.debug("读取机器人 %s 的插件策略失败：%s", bot_id, exc)
            return None

    def _hooks(self, kind: str, bot_id: str = "") -> List[Callable]:
        allowed = self._allowed_for(bot_id)
        out: List[Callable] = []
        for plugin in self.plugins:
            name = plugin.meta.get("name") or plugin.name
            if self.manager is not None and self.manager.is_disabled(name):
                continue
            if allowed is not None and name not in allowed:
                continue
            for cls in type(plugin.instance).__mro__:
                for attr_name, value in vars(cls).items():
                    filters = [item for item in (getattr(value, FILTERS_ATTR, None) or [])
                               if item["kind"] == kind]
                    if not filters:
                        continue
                    func = getattr(plugin.instance, attr_name, None)
                    if callable(func):
                        out.append(func)
        return out

    # ------------------------------------------------------------------ 列表
    def list_plugins(self) -> List[Dict[str, Any]]:
        return [plugin.info() for plugin in self.plugins]
