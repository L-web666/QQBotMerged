# -*- coding: utf-8 -*-
"""消息组件、消息对象与事件（AstrMessageEvent 等）。

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
