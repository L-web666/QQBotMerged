# -*- coding: utf-8 -*-
"""公共常量与小工具（Runtime 与外部的共享部分）。

本模块是 `core/runtime_pkg/` 包的一部分（由 `core/runtime.py` 拆分而来）；
`core/runtime.py` 只是兼容再导出层。
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





_BOT_ACCOUNT_FIELDS = ("id", "name", "enabled", "app_id", "app_secret", "sandbox",
                       "intents", "reconnect_attempts", "reconnect_interval")


def _json_same(left: Any, right: Any) -> bool:
    """两份配置值是否一样（用 JSON 归一化，避免 dict 顺序影响判断）。"""
    try:
        return (json.dumps(left, ensure_ascii=False, sort_keys=True)
                == json.dumps(right, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):
        return left == right


def _section_label(key: str) -> str:
    """分组 key → 中文名（取不到就用 key 本身）。"""
    try:
        from core import config_schema as schema
        return str((schema.SECTION_LABELS.get(key) or {}).get("label") or key)
    except Exception:
        return str(key or "")


def _bot_change_labels(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """`bots` 分组变了时，展开成「回复与消息（bot3）」这种更好读的说法。

    按机器人的设置都存在 `bots[i].overrides.<分组>` 里，只报一个「机器人账号」
    会让用户以为改的是账号（日志里根本看不出到底改了什么）。
    """
    def _index(data):
        out = {}
        for item in ((data or {}).get("bots") or []):
            if isinstance(item, dict):
                out[str(item.get("id") or "")] = item
        return out

    old, new = _index(before), _index(after)
    labels: List[str] = []
    account: List[str] = []
    for bot_id in sorted(set(old) | set(new)):
        first, second = old.get(bot_id), new.get(bot_id)
        if first is None or second is None:
            account.append(bot_id or "?")
            continue
        if any(not _json_same(first.get(key), second.get(key)) for key in _BOT_ACCOUNT_FIELDS):
            account.append(bot_id or "?")
        ov_first = first.get("overrides") if isinstance(first.get("overrides"), dict) else {}
        ov_second = second.get("overrides") if isinstance(second.get("overrides"), dict) else {}
        for key in sorted(set(ov_first) | set(ov_second)):
            if _json_same(ov_first.get(key), ov_second.get(key)):
                continue
            labels.append(f"{_section_label(str(key))}（{bot_id}）")
    if account:
        labels.append("机器人账号（%s）" % "、".join(account))
    return labels


def _mask_app_id(app_id: str) -> str:
    app_id = str(app_id or "")
    if len(app_id) <= 4:
        return app_id
    return app_id[:4] + "****" + app_id[-2:]
