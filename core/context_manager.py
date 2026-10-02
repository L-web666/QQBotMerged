# -*- coding: utf-8 -*-
"""对话上下文管理（按 机器人 + 会话 隔离，落地为 JSON 文件，支持云同步）。

来源：`API_qqbot/core/context_manager.py` 的存取模型，改为按 `bot_id` 隔离
（两个机器人同在一个群时互不串话），并加入体积上限与线程安全。
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ContextManager:
    """每个会话保存最近 N 轮对话（user/assistant）。"""

    def __init__(self, base_dir: str = "data/user_context", max_history: int = 20,
                 logger_obj: logging.Logger = None):
        self.base_dir = base_dir
        self.MAX_HISTORY = max(0, int(max_history or 0))
        self.log = logger_obj or logger
        self._lock = threading.RLock()
        self._cache: Dict[str, List[Dict[str, str]]] = {}
        os.makedirs(os.path.join(base_dir, "private"), exist_ok=True)
        os.makedirs(os.path.join(base_dir, "group"), exist_ok=True)

    # ------------------------------------------------------------------ 路径
    def _path(self, bot_id: str, conv_type: str, openid: str) -> str:
        scope = "group" if conv_type == "group" else "private"
        safe_bot = "".join(ch for ch in (bot_id or "bot") if ch.isalnum() or ch in "-_")
        safe_id = "".join(ch for ch in (openid or "") if ch.isalnum() or ch in "-_")
        return os.path.join(self.base_dir, scope, f"{safe_bot}__{safe_id}.json")

    @staticmethod
    def key(bot_id: str, conv_type: str, openid: str) -> str:
        return f"{bot_id or 'bot'}:{'group' if conv_type == 'group' else 'private'}:{openid or ''}"

    # ------------------------------------------------------------------ 读写
    def _limit(self, max_history: Optional[int] = None) -> int:
        """本次读取/写入用多少条上下文。

        `max_history` 用来支持"某个群单独设置了上下文条数"——以前是直接改
        `self.MAX_HISTORY`（全局实例属性），两个机器人同时跑会互相覆盖，
        现在改成按调用传参。
        """
        if max_history is None:
            return self.MAX_HISTORY
        try:
            return max(0, int(max_history))
        except (TypeError, ValueError):
            return self.MAX_HISTORY

    def get(self, bot_id: str, conv_type: str, openid: str,
            max_history: Optional[int] = None) -> List[Dict[str, str]]:
        limit = self._limit(max_history)
        if not openid or limit <= 0:
            return []
        cache_key = self.key(bot_id, conv_type, openid)
        with self._lock:
            if cache_key in self._cache:
                data = list(self._cache[cache_key])
                # 传了 max_history 就只取最近 N 条（群级设置只影响自己这次调用）
                return data[-limit:] if max_history is not None else data
            path = self._path(bot_id, conv_type, openid)
            data: List[Dict[str, str]] = []
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        raw = json.load(handle)
                    if isinstance(raw, list):
                        data = [item for item in raw if isinstance(item, dict)]
                    elif isinstance(raw, dict) and isinstance(raw.get("history"), list):
                        data = [item for item in raw["history"] if isinstance(item, dict)]
                except (OSError, ValueError) as exc:
                    self.log.warning("读取上下文失败（%s）：%s", path, exc)
                    data = []
            self._cache[cache_key] = data
            return data[-limit:] if max_history is not None else list(data)

    def append(self, bot_id: str, conv_type: str, openid: str, role: str, content: str,
               name: str = "", max_history: Optional[int] = None):
        """追加一条对话记录（自动裁剪到上限条数）。"""
        limit = self._limit(max_history)
        if not openid or limit <= 0 or not content:
            return
        cache_key = self.key(bot_id, conv_type, openid)
        item = {"role": role, "content": str(content)[:4000], "ts": time.time()}
        if name:
            item["name"] = str(name)[:60]
        with self._lock:
            history = self._cache.get(cache_key)
            if history is None:
                history = self.get(bot_id, conv_type, openid)
            history.append(item)
            if len(history) > limit:
                history = history[-limit:]
            self._cache[cache_key] = history
            self._save(bot_id, conv_type, openid, history)

    def set_history(self, bot_id: str, conv_type: str, openid: str, history: List[Dict[str, str]]):
        cache_key = self.key(bot_id, conv_type, openid)
        with self._lock:
            trimmed = list(history or [])[-self.MAX_HISTORY:] if self.MAX_HISTORY else []
            self._cache[cache_key] = trimmed
            self._save(bot_id, conv_type, openid, trimmed)

    def _save(self, bot_id: str, conv_type: str, openid: str, history: List[Dict[str, str]]):
        path = self._path(bot_id, conv_type, openid)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(history, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except OSError as exc:
            self.log.warning("保存上下文失败（%s）：%s", path, exc)

    def clear(self, bot_id: str = "", conv_type: str = "", openid: str = "") -> int:
        """清空上下文：指定会话则只清该会话，否则清空全部（可按机器人限定）。"""
        removed = 0
        with self._lock:
            if openid and conv_type:
                cache_key = self.key(bot_id, conv_type, openid)
                self._cache.pop(cache_key, None)
                path = self._path(bot_id, conv_type, openid)
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                        removed += 1
                    except OSError:
                        pass
                return removed
            for scope in ("private", "group"):
                directory = os.path.join(self.base_dir, scope)
                if not os.path.isdir(directory):
                    continue
                for name in os.listdir(directory):
                    if not name.endswith(".json"):
                        continue
                    if bot_id and not name.startswith(f"{bot_id}__"):
                        continue
                    try:
                        os.remove(os.path.join(directory, name))
                        removed += 1
                    except OSError:
                        pass
            self._cache = {}
        return removed

    # ------------------------------------------------------------------ 概览
    def summary(self, bot_id: str = "") -> Dict[str, Any]:
        """后台「上下文」页：列出上下文文件。

        `bot_id` 非空时**只列该机器人的文件**（上下文按机器人隔离，
        免得在页面上看到别的机器人的文件、点删除删错对象）。
        """
        out: Dict[str, List[Dict[str, Any]]] = {"private": [], "group": []}
        for scope in ("private", "group"):
            directory = os.path.join(self.base_dir, scope)
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".json"):
                    continue
                if bot_id and not name.startswith(f"{bot_id}__"):
                    continue
                full = os.path.join(directory, name)
                entries = 0
                try:
                    with open(full, "r", encoding="utf-8") as handle:
                        raw = json.load(handle)
                    entries = len(raw) if isinstance(raw, list) else 0
                except (OSError, ValueError):
                    entries = 0
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                # 注意：这里不能再用变量名 bot_id —— 会覆盖函数参数（筛选条件就失效了）
                row_bot, _, row_openid = name[:-5].partition("__")
                out[scope].append({
                    "name": name,
                    "bot_id": row_bot,
                    "openid": row_openid,
                    "entries": entries,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "mtime_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                })
        return out

    def delete_file(self, scope: str, name: str, bot_id: str = "") -> bool:
        """删除指定的上下文文件（后台手动清理用）。

        `bot_id` 非空时只允许删除**属于该机器人**的文件（文件名以 `<bot_id>__` 开头），
        否则拒绝——避免误删其它机器人的上下文。
        """
        scope = "group" if scope == "group" else "private"
        safe = os.path.basename(name)
        if not safe.endswith(".json"):
            return False
        if bot_id and not safe.startswith(f"{bot_id}__"):
            return False
        full = os.path.join(self.base_dir, scope, safe)
        if not os.path.isfile(full):
            return False
        # 文件名格式：<bot_id>__<openid>.json —— 两个字段都可能含 "__"，
        # 所以只按第一个 "__" 切一刀，剩下的全部当作 openid。
        stem = safe[:-len(".json")]
        bot_id, _, openid = stem.partition("__")
        try:
            os.remove(full)
        except OSError:
            return False
        with self._lock:
            # 把对应的缓存项一起清掉（用同样的规则重建 key）
            self._cache.pop(self.key(bot_id, scope, openid), None)
        return True

    def format_for_prompt(self, bot_id: str, conv_type: str, openid: str,
                          system_prompt: str = "",
                          max_history: Optional[int] = None) -> List[Dict[str, str]]:
        """拼成 OpenAI 兼容的 messages 数组（`max_history` 可覆盖本次的条数）。"""
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        for item in self.get(bot_id, conv_type, openid, max_history=max_history):
            role = item.get("role")
            content = item.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        return messages
