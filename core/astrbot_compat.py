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

logger = logging.getLogger(__name__)

# 我们模拟的 AstrBot 版本。插件用 metadata.yaml 里的 astrbot_version 做兼容判断时，
# 低于这个要求会给出明确警告（而不是静默跑挂）。
ASTRBOT_VERSION = "4.9.2"
SHIM_MARK = "_qqbot_astrbot_shim"


# ======================================================================================
# 异步执行器：AstrBot 插件基本都是 async，我们统一在自己的一条事件循环线程里跑
# ======================================================================================
class AsyncRunner:
    """常驻事件循环，把协程提交进去并等待结果（避免每次 asyncio.run 重建事件循环）。"""

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()

            def run():
                asyncio.set_event_loop(loop)
                loop.run_forever()

            thread = threading.Thread(target=run, name="astrbot-loop", daemon=True)
            thread.start()
            self._loop = loop
            self._thread = thread
            return loop

    def run(self, coro, timeout: float = 60.0):
        """同步等待一个协程的结果（超时会抛 TimeoutError）。"""
        loop = self._ensure()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    def collect(self, agen, timeout: float = 60.0) -> List[Any]:
        """把异步生成器的结果全部收集起来。"""
        results: List[Any] = []

        async def drain():
            async for item in agen:
                results.append(item)

        self.run(drain(), timeout=timeout)
        return results

    def stop(self):
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=2.0)


RUNNER = AsyncRunner()


# ======================================================================================
# 消息组件
# ======================================================================================
class BaseMessageComponent:
    type: str = "base"

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type}


class Plain(BaseMessageComponent):
    type = "Plain"

    def __init__(self, text: str = "", convert: bool = True, **kwargs):
        self.text = "" if text is None else str(text)
        self.convert = convert

    def __str__(self):
        return self.text

    def to_dict(self):
        return {"type": "Plain", "text": self.text}


class Image(BaseMessageComponent):
    type = "Image"

    def __init__(self, file: str = "", url: str = "", path: str = "", **kwargs):
        self.file = file or ""
        self.url = url or ""
        self.path = path or ""
        self.file_name = kwargs.get("file_name") or ""

    @classmethod
    def fromURL(cls, url: str, **kwargs):
        return cls(url=url, **kwargs)

    @classmethod
    def fromFileSystem(cls, path: str, **kwargs):
        return cls(path=path, **kwargs)

    @classmethod
    def fromBytes(cls, data: bytes, **kwargs):
        item = cls(**kwargs)
        item._blob = data
        return item

    def _blob_or_none(self) -> Optional[bytes]:
        blob = getattr(self, "_blob", None)
        if blob:
            return blob
        for candidate in (self.path, self.file):
            if candidate and os.path.isfile(candidate):
                try:
                    with open(candidate, "rb") as handle:
                        return handle.read()
                except OSError:
                    return None
        return None

    def to_dict(self):
        return {"type": "Image", "url": self.url, "file": self.file or self.path}


class Record(BaseMessageComponent):
    type = "Record"

    def __init__(self, file: str = "", url: str = "", path: str = "", **kwargs):
        self.file, self.url, self.path = file or "", url or "", path or ""

    @classmethod
    def fromFileSystem(cls, path: str, **kwargs):
        return cls(path=path, **kwargs)

    @classmethod
    def fromURL(cls, url: str, **kwargs):
        return cls(url=url, **kwargs)


class Video(BaseMessageComponent):
    type = "Video"

    def __init__(self, file: str = "", url: str = "", path: str = "", **kwargs):
        self.file, self.url, self.path = file or "", url or "", path or ""

    @classmethod
    def fromFileSystem(cls, path: str, **kwargs):
        return cls(path=path, **kwargs)

    @classmethod
    def fromURL(cls, url: str, **kwargs):
        return cls(url=url, **kwargs)


class File(BaseMessageComponent):
    type = "File"

    def __init__(self, name: str = "", file: str = "", url: str = "", path: str = "", **kwargs):
        self.name = name or kwargs.get("file_name") or ""
        self.file, self.url, self.path = file or "", url or "", path or ""

    @classmethod
    def fromFileSystem(cls, path: str, name: str = "", **kwargs):
        return cls(name=name or os.path.basename(path), path=path, **kwargs)

    @classmethod
    def fromURL(cls, url: str, name: str = "", **kwargs):
        return cls(name=name, url=url, **kwargs)


class At(BaseMessageComponent):
    type = "At"

    def __init__(self, qq: str = "", name: str = "", **kwargs):
        self.qq = "" if qq is None else str(qq)
        self.name = name or ""


class AtAll(At):
    type = "AtAll"

    def __init__(self, **kwargs):
        super().__init__(qq="all")


class Face(BaseMessageComponent):
    type = "Face"

    def __init__(self, id: Any = "", **kwargs):
        self.id = id


class Reply(BaseMessageComponent):
    type = "Reply"

    def __init__(self, id: str = "", sender_nickname: str = "", **kwargs):
        self.id = id or ""
        self.sender_nickname = sender_nickname or ""


class Poke(BaseMessageComponent):
    type = "Poke"

    def __init__(self, qq: str = "", **kwargs):
        self.qq = qq


class Node(BaseMessageComponent):
    type = "Node"

    def __init__(self, **kwargs):
        self.self_id = kwargs.get("self_id", "")
        self.name = kwargs.get("name", "")
        self.uin = kwargs.get("uin", "")
        self.content = kwargs.get("content") or kwargs.get("message") or []


class Nodes(BaseMessageComponent):
    type = "Nodes"

    def __init__(self, nodes: Optional[List[Any]] = None, **kwargs):
        self.nodes = nodes or []


class MessageChain(list):
    """AstrBot 里的消息链（本质就是组件列表），带常见构造方法。"""

    def message(self, text: Any):
        self.append(text if isinstance(text, BaseMessageComponent) else Plain(str(text)))
        return self

    def plain(self, text: str):
        return self.message(text)

    def at(self, qq: Any, name: str = ""):
        self.append(At(qq=qq, name=name))
        return self

    def at_all(self):
        self.append(AtAll())
        return self

    def face(self, id: Any):
        self.append(Face(id=id))
        return self

    def image(self, url_or_path: str):
        text = str(url_or_path or "")
        self.append(Image.fromURL(text) if text.lower().startswith(("http://", "https://"))
                    else Image.fromFileSystem(text))
        return self

    def file_image(self, path: str):
        self.append(Image.fromFileSystem(path))
        return self

    def url_image(self, url: str):
        self.append(Image.fromURL(url))
        return self

    def file(self, file: str = "", name: str = ""):
        self.append(File(file=file, name=name or os.path.basename(file or "")))
        return self

    def file_audio(self, path: str):
        self.append(Record(file=path, path=path, url=path))
        return self

    def record(self, path: str):
        return self.file_audio(path)

    def file_video(self, path: str):
        self.append(Video.fromFileSystem(path))
        return self

    def video(self, path: str):
        return self.file_video(path)


# ======================================================================================
# 事件与结果
# ======================================================================================
class MessageEventResult:
    """处理器返回/产出的结果。可以是字符串，也可以是一串消息组件。"""

    def __init__(self):
        self.chain: List[Any] = []
        self.use_t2i = False
        self.result_content_type = "normal"
        self.raw: Any = None

    def message(self, text: Any):
        if isinstance(text, BaseMessageComponent):
            self.chain.append(text)
        else:
            self.chain.append(Plain(str(text)))
        return self

    def message_chain(self, chain: List[Any]):
        for item in chain or []:
            self.chain.append(item)
        return self

    def plain(self, text: str):
        return self.message(text)

    def image(self, url_or_path: str):
        self.chain.append(Image.fromURL(url_or_path) if str(url_or_path).startswith("http")
                          else Image.fromFileSystem(str(url_or_path)))
        return self

    def is_empty(self) -> bool:
        return not self.chain

    def __iter__(self):
        return iter(self.chain)

    def __bool__(self):
        return bool(self.chain)


class AstrBotMessage:
    """事件里的"消息对象"（字段与 AstrBot 官方文档一致）。"""

    def __init__(self, **kwargs):
        self.type: Any = kwargs.get("type")
        self.self_id: str = str(kwargs.get("self_id") or "")
        self.session_id: str = str(kwargs.get("session_id") or "")
        self.message_id: str = str(kwargs.get("message_id") or "")
        # 官方 v4 是 group_id（字符串）；旧版本是 group 对象，两个都提供
        self.group_id: str = str(kwargs.get("group_id") or "")
        self.group: Any = kwargs.get("group")
        self.sender: Any = kwargs.get("sender")
        self.message: List[Any] = kwargs.get("message") or []
        self.raw_message: Any = kwargs.get("raw_message")
        self.message_str: str = kwargs.get("message_str") or ""
        self.timestamp: float = float(kwargs.get("timestamp") or time.time())
        self.unified_msg_origin: str = str(kwargs.get("unified_msg_origin") or "")


class MessageMember:
    def __init__(self, user_id: Any = "", nickname: str = ""):
        self.user_id = str(user_id or "")
        self.nickname = nickname or ""


class Group:
    def __init__(self, group_id: Any = "", group_name: str = ""):
        self.group_id = str(group_id or "")
        self.group_name = group_name or ""


class MessageType(str, Enum):
    """`event.message_obj.type` 的取值（官方定义）。"""

    GROUP_MESSAGE = "GroupMessage"
    FRIEND_MESSAGE = "FriendMessage"
    OTHER_MESSAGE = "OtherMessage"
    PRIVATE_MESSAGE = "FriendMessage"      # 别名，方便按老名字比较


class EventMessageType(Enum):
    """`@filter.event_message_type(...)` 用的枚举（官方定义）。"""

    ALL = 0
    GROUP_MESSAGE = 1
    PRIVATE_MESSAGE = 2
    OTHER_MESSAGE = 3


class PermissionType(Enum):
    """`@filter.permission_type(...)` 用的枚举（官方定义）。"""

    ADMIN = 0
    MEMBER = 1
    ALL = 2


class PlatformAdapterType(IntFlag):
    """`@filter.platform_adapter_type(...)`：官方支持按位或组合。

    本程序就是 QQ 官方机器人平台，所以只声明 QQOFFICIAL / QQOFFICIAL_WEBHOOK / ALL。
    """

    QQOFFICIAL = 1 << 0
    QQOFFICIAL_WEBHOOK = 1 << 1
    ALL = 1 << 2
    # 其它平台（aiocqhttp / telegram / ... ）本程序不支持，但保留常量以免插件导入即报错
    AIOCQHTTP = 1 << 3
    TELEGRAM = 1 << 4
    WECOM = 1 << 5
    WECOM_AI_BOT = 1 << 6
    LARK = 1 << 7
    DINGTALK = 1 << 8
    DISCORD = 1 << 9
    SLACK = 1 << 10
    KOOK = 1 << 11
    VOCECHAT = 1 << 12
    WEIXIN_OFFICIAL_ACCOUNT = 1 << 13
    SATORI = 1 << 14
    MISSKEY = 1 << 15
    LINE = 1 << 16
    MATRIX = 1 << 17
    WEIXIN_OC = 1 << 18
    MATTERMOST = 1 << 19
    WEBCHAT = 1 << 20


class AstrMessageEvent:
    """交给 AstrBot 插件的消息事件（由本程序的消息包装而来）。"""

    def __init__(self, message_obj: AstrBotMessage, message_str: str = "",
                 context=None, bridge=None):
        self.message_obj = message_obj
        self.message_str = message_str or ""
        self.context = context
        self._bridge = bridge
        self.is_wake = True
        self.is_at_or_wake_command = True
        self.role = "member"
        self.platform_meta = types.SimpleNamespace(name="qq_official", id="qq_official")
        self.session_id = message_obj.session_id
        self.unified_msg_origin = message_obj.unified_msg_origin
        self._stopped = False
        self._extras: Dict[str, Any] = {}

    # ---------- 基本信息 ----------
    def get_message_str(self) -> str:
        return self.message_str

    def get_messages(self) -> List[Any]:
        return list(self.message_obj.message)

    def get_self_id(self) -> str:
        return self.message_obj.self_id

    def get_sender_id(self) -> str:
        sender = self.message_obj.sender
        return str(getattr(sender, "user_id", "") or "")

    def get_sender_name(self) -> str:
        sender = self.message_obj.sender
        return str(getattr(sender, "nickname", "") or "")

    def get_group_id(self) -> str:
        group_id = getattr(self.message_obj, "group_id", "") or ""
        if group_id:
            return str(group_id)
        group = self.message_obj.group
        return str(getattr(group, "group_id", "") or "")

    def get_session_id(self) -> str:
        return self.session_id

    def get_platform_name(self) -> str:
        return "qq_official"

    def get_platform_id(self) -> str:
        return "qq_official"

    def get_group(self):
        return self.message_obj.group

    def is_admin(self) -> bool:
        return self.role == "admin"

    # ---------- 结果构造 ----------
    def plain_result(self, text: str) -> MessageEventResult:
        return MessageEventResult().message(text)

    def image_result(self, url_or_path: str) -> MessageEventResult:
        return MessageEventResult().image(url_or_path)

    def chain_result(self, chain: List[Any]) -> MessageEventResult:
        return MessageEventResult().message_chain(chain)

    def make_result(self) -> MessageEventResult:
        return MessageEventResult()

    # ---------- 控制 ----------
    def stop_event(self):
        self._stopped = True
        if self._bridge is not None:
            self._bridge.stop = True

    def is_stopped(self) -> bool:
        return self._stopped

    def set_extra(self, key: str, value: Any):
        self._extras[key] = value

    def get_extra(self, key: str, default=None):
        return self._extras.get(key, default)

    async def send(self, chain: Any):
        """插件里 `await event.send(chain)` 直接回消息。"""
        if self._bridge is None:
            return
        components = chain if isinstance(chain, (list, tuple)) else [chain]
        self._bridge.emit(components)

    async def request_llm(self, *args, **kwargs):
        provider = self.context.get_using_provider() if self.context else None
        if provider is None:
            raise RuntimeError("没有可用的 AI 提供方（本程序未配置 AI）")
        return await provider.text_chat(prompt=self.message_str, session_id=self.session_id)


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


# ======================================================================================
# 垫片安装
# ======================================================================================
def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


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


# ======================================================================================
# 插件加载器
# ======================================================================================
def _deep_update(base: Dict[str, Any], extra: Dict[str, Any]):
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def _schema_defaults(schema: Dict[str, Any]) -> Dict[str, Any]:
    """按官方 `_conf_schema.json` 递归取默认值（object 类型钻进 items）。"""
    out: Dict[str, Any] = {}
    for key, spec in (schema or {}).items():
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("type") or "")
        if kind == "object" and isinstance(spec.get("items"), dict):
            out[key] = _schema_defaults(spec["items"])
        elif "default" in spec:
            out[key] = copy.deepcopy(spec["default"])
        elif kind == "int":
            out[key] = 0
        elif kind == "float":
            out[key] = 0.0
        elif kind == "bool":
            out[key] = False
        elif kind == "list" or kind == "file":
            out[key] = []
        elif kind == "dict" or kind == "template_list":
            out[key] = {} if kind == "dict" else []
        else:
            out[key] = ""
    return out


def _version_tuple(text: str) -> Tuple[int, ...]:
    parts = []
    for chunk in str(text or "").replace("v", "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or [0])


def _version_satisfies(current: str, requirement: str) -> bool:
    """极简的版本范围判断：支持 `>=4.17.0`、`>4.0`、`<=x`、`<x`、`==x`、`4.17.0`。"""
    req = str(requirement or "").strip()
    if not req:
        return True
    for op in (">=", "<=", "==", ">", "<"):
        if req.startswith(op):
            target = _version_tuple(req[len(op):])
            now = _version_tuple(current)
            size = max(len(target), len(now))
            now = now + (0,) * (size - len(now))
            target = target + (0,) * (size - len(target))
            if op == ">=":
                return now >= target
            if op == "<=":
                return now <= target
            if op == "==":
                return now == target
            if op == ">":
                return now > target
            return now < target
    # 没写运算符时按"最低版本"理解
    now, target = _version_tuple(current), _version_tuple(req)
    size = max(len(target), len(now))
    return now + (0,) * (size - len(now)) >= target + (0,) * (size - len(target))


def _load_yaml(path: str) -> Dict[str, Any]:
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}
    except ImportError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning("解析 %s 失败：%s", path, exc)
        return {}
    # 没有 PyYAML 时的极简兜底：只认 "key: value"
    out: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                out[key.strip()] = value.strip().strip("'\"")
    except OSError:
        return {}
    return out


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
                 sink: Optional[Callable[[List[Any]], None]] = None) -> Optional[Dict[str, Any]]:
        """把消息交给 AstrBot 插件。返回 {'plugin','text','images','handled'} 或 None。"""
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
                          image_urls: List[str] = None) -> ProviderRequest:
        """构造 `ProviderRequest` 并跑一遍插件的 `on_llm_request` 钩子。

        官方签名是 `async def hook(self, event, req: ProviderRequest)`（三个参数），
        所以这里单独调用而不是走消息处理器那套。
        """
        request = ProviderRequest(prompt=prompt, system_prompt=system_prompt,
                                  image_urls=list(image_urls or []))
        event = self.build_event({"type": "private", "content": prompt}, self.runtime)
        for func in self.llm_request_hooks():
            try:
                result = func(event, request)
                if inspect.isawaitable(result):
                    RUNNER.run(result, timeout=15)
            except Exception as exc:
                self.log.error("AstrBot 插件 on_llm_request 钩子执行失败：%s", exc)
        return request

    def llm_request_hooks(self) -> List[Callable]:
        return self._hooks("on_llm_request")

    def llm_response_hooks(self) -> List[Callable]:
        return self._hooks("on_llm_response")

    def _hooks(self, kind: str) -> List[Callable]:
        out: List[Callable] = []
        for plugin in self.plugins:
            if self.manager is not None and self.manager.is_disabled(plugin.meta.get("name")
                                                                    or plugin.name):
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
