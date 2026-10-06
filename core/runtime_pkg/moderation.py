# -*- coding: utf-8 -*-
"""限速、禁言、群内身份与撤回（RuntimeModerationMixin）。

本模块是 `core/runtime_pkg/` 包的一部分（由 `core/runtime.py` 拆分而来）；
由 `core/runtime_pkg/core.py` 里的 `Runtime` 组合使用。
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



class RuntimeModerationMixin:
    """限速、禁言、群内身份与撤回（RuntimeModerationMixin）（被 `Runtime` 继承；不要单独实例化）。"""


    # ================================================================== 限速 / 禁言
    def rate_limited(self, key: str, interval: float) -> bool:
        if interval <= 0:
            return False
        now = time.time()
        last = self._rate_last.get(key, 0.0)
        if now - last < interval:
            return True
        self._rate_last[key] = now
        if len(self._rate_last) > 5000:
            cutoff = now - 3600
            self._rate_last = {k: v for k, v in self._rate_last.items() if v >= cutoff}
        return False

    def mark_replied(self, key: str):
        self._rate_last[key] = time.time()

    def is_muted(self, bot_id: str, group_openid: str, member_openid: str) -> Optional[Dict[str, Any]]:
        if not self.config.bool_of("features", "group_management", default=True):
            return None
        return self.group_manager.is_muted(bot_id, group_openid, member_openid)

    # ================================================================== 撤回消息
    # 官方限制：只能撤回 **2 分钟内** 的消息（群聊与单聊都一样）。
    # 群聊里机器人**是群管理员时，还能撤回普通群成员的消息**（消息 ID 就是群消息事件里的
    # d.id，我们收到消息时就把它存进了 msg_id）；普通成员身份只能撤回自己发的。
    # 失败原因要如实告诉用户，**绝不把失败伪装成"已撤回"**。
    RECALL_WINDOW_SECONDS = 120
    ROLE_CACHE_SECONDS = 600

    def group_bot_role(self, bot_id: str = "", group_openid: str = "",
                       refresh: bool = False) -> str:
        """机器人在群里的身份：owner / admin / member（查不到时返回空串）。

        结果缓存 10 分钟 —— 撤回群成员消息、官方禁言都要判断管理员身份，
        每次都去问接口既慢又浪费额度。
        """
        if not group_openid:
            return ""
        key = f"{bot_id or ''}:{group_openid}"
        now = time.time()
        cached = self._bot_roles.get(key)
        if cached and not refresh and (now - cached[0]) < self.ROLE_CACHE_SECONDS:
            return cached[1]
        client = self.get_client(bot_id)
        if client is None:
            return cached[1] if cached else ""
        try:
            state = client.bot_state(group_openid)
            role = str((state or {}).get("member_role") or "").strip().lower()
        except Exception as exc:
            self.log.debug("查询群内身份失败（%s）：%s", group_openid, exc)
            return cached[1] if cached else ""
        if role:
            self._bot_roles[key] = (now, role)
        return role

    def group_bot_is_admin(self, bot_id: str = "", group_openid: str = "",
                           refresh: bool = False) -> bool:
        """机器人是不是群主/管理员（只有这样才能撤回普通成员的消息）。"""
        return self.group_bot_role(bot_id, group_openid, refresh) in ("owner", "admin")

    def cached_group_role(self, bot_id: str = "", group_openid: str = "") -> str:
        """只读缓存里的群内身份（不发请求）。"""
        entry = self._bot_roles.get(f"{bot_id or ''}:{group_openid}")
        if entry and (time.time() - entry[0]) < self.ROLE_CACHE_SECONDS:
            return entry[1]
        return ""

    ROLE_WAIT_SECONDS = 1.5

    def warm_group_role(self, bot_id: str = "", group_openid: str = "",
                        wait: float = None) -> str:
        """拿群内身份：有缓存直接返回；没有就查一次。

        查询在后台线程里做，但**最多等 `ROLE_WAIT_SECONDS` 秒**（接口通常几百毫秒就回来），
        这样页面第一次打开就能显示正确的身份，又不会因为接口卡住把页面拖死。
        """
        cached = self.cached_group_role(bot_id, group_openid)
        if cached or not group_openid:
            return cached
        key = f"{bot_id or ''}:{group_openid}"
        started = False
        with self._lock:
            if key not in self._role_pending:
                self._role_pending.add(key)
                started = True

        if started:
            def worker():
                try:
                    self.group_bot_role(bot_id, group_openid, refresh=True)
                    self.broadcast({"type": "group_role", "group_openid": group_openid,
                                    "bot_id": bot_id,
                                    "role": self.cached_group_role(bot_id, group_openid)})
                except Exception as exc:
                    self.log.debug("后台查询群内身份失败：%s", exc)
                finally:
                    with self._lock:
                        self._role_pending.discard(key)

            threading.Thread(target=worker, name="group-role", daemon=True).start()

        budget = self.ROLE_WAIT_SECONDS if wait is None else max(0.0, float(wait))
        deadline = time.time() + budget
        while time.time() < deadline:
            role = self.cached_group_role(bot_id, group_openid)
            if role:
                return role
            if not started and key not in self._role_pending:
                break
            time.sleep(0.05)
        return self.cached_group_role(bot_id, group_openid)

    def _find_message_record(self, message_id: int = 0, msg_id: str = ""):
        """按本地 id 或平台消息 ID 找一条消息记录。"""
        record = None
        if message_id:
            try:
                record = self.store.get_message(int(message_id))
            except Exception:
                record = None
        if record is None and msg_id:
            try:
                for item in self.store.get_all(newest_first=True, limit=200):
                    if item.get("msg_id") == msg_id:
                        record = item
                        break
            except Exception:
                record = None
        return record

    def recall_message(self, message_id: int = 0, msg_id: str = "", bot_id: str = "",
                       as_admin: bool = False) -> Dict[str, Any]:
        """撤回消息（官方 DELETE，2 分钟内有效）。

        - 机器人自己发的：群聊 / 私聊都可以；
        - **群聊里普通成员发的**：只有机器人是群管理员才行（`as_admin` 只是页面意图，
          真正的权限以平台返回为准）；
        - 私聊里对方发的：QQ 没有开放该接口，直接如实说明。

        只有平台确认成功（HTTP 200、无响应体）时才把本地记录标记为「已撤回」。
        """
        record = self._find_message_record(message_id, msg_id)
        if record is None:
            return {"success": False, "message": "找不到这条本地记录"}
        direction = record.get("direction") or "out"
        target_type = record.get("type") or "private"
        bot = record.get("bot_id") or bot_id
        target_id = record.get("group_openid") if target_type == "group" else record.get("openid")
        target_msg_id = msg_id or record.get("msg_id") or ""
        is_member_message = direction != "out"

        if is_member_message:
            if target_type != "group":
                return {"success": False,
                        "message": "私聊里只能撤回机器人自己发出的消息"
                                   "（QQ 未开放撤回对方私聊消息的接口）"}
            role = self.group_bot_role(bot, target_id)
            if role == "member":
                return {"success": False, "need_admin": True,
                        "message": "机器人不是该群管理员，只能撤回自己发出的消息；"
                                   "请让群主把机器人设为管理员后再试"}

        if not target_msg_id or not target_id:
            return {"success": False, "no_message_id": True,
                    "message": "这条消息没有记录平台消息 ID（多为升级前的历史消息），无法撤回；"
                               "新收到的消息都带消息 ID，可以撤回"}

        # 先按时间判断：超过 2 分钟平台必然拒绝，省掉一次无用请求
        sent_ts = float(record.get("ts") or 0)
        if sent_ts and (time.time() - sent_ts) > self.RECALL_WINDOW_SECONDS:
            return {"success": False, "expired": True,
                    "message": "这条消息发送已超过 2 分钟，QQ 不允许撤回"}

        client = self.get_client(bot)
        if client is None:
            return {"success": False, "message": "没有可用的机器人（无法调用撤回接口）"}
        try:
            if target_type == "group":
                client.recall_group_message(target_id, target_msg_id)
            else:
                client.recall_private_message(target_id, target_msg_id)
        except Exception as exc:
            hint = self._recall_error_hint(exc)
            self.stats.record("recall_failed")
            self.log.warning("撤回失败（%s，%s）：%s", target_type,
                             "成员消息" if is_member_message else "自己的消息", exc)
            result = {"success": False, "message": hint or f"平台撤回失败：{exc}"}
            if "40062003" in str(exc) and is_member_message:
                result["need_admin"] = True
                # 平台说没权限 → 缓存的身份可能不准，作废它下次重新查
                self._bot_roles.pop(f"{bot or ''}:{target_id}", None)
            return result

        # 平台确认成功后才标记本地记录
        try:
            self.store.update_message(int(record["id"]), content="（已撤回）", image_url="")
        except Exception as exc:
            self.log.warning("撤回成功但标记本地记录失败：%s", exc)
        self.stats.record("recalled_member" if is_member_message else "recalled")
        self.broadcast({"type": "message_recalled", "conversation": record.get("conv_key") or "",
                        "message_id": record.get("id")})
        self.log.info("已撤回%s（群=%s）", "群成员的消息" if is_member_message else "消息", target_id)
        return {"success": True,
                "message": ("已撤回该群成员的消息（2 分钟内有效）" if is_member_message
                            else "已撤回（2 分钟内有效）"),
                "recalled_member": is_member_message}

    def recall_own_message(self, message_id: int = 0, msg_id: str = "",
                           bot_id: str = "") -> Dict[str, Any]:
        """兼容旧调用：撤回机器人自己发出的消息。"""
        return self.recall_message(message_id=message_id, msg_id=msg_id, bot_id=bot_id)

    @staticmethod
    def _recall_error_hint(message: str) -> str:
        """把平台错误码翻译成人话。"""
        text = str(message or "")
        if "40064004" in text:
            return "已超过 2 分钟，QQ 不允许撤回这条消息"
        if "40062003" in text:
            return ("没有撤回权限：机器人不是群管理员（只能撤回自己发的消息），"
                    "或这条消息不属于可撤回的范围")
        if "40061001" in text:
            return "请求参数无效（消息 ID 格式可能不对）"
        if "40061002" in text:
            return "消息 ID 无效"
        if "306009" in text:
            return "用户 openid 无效"
        if "50065001" in text:
            return "平台撤回失败，请稍后重试"
        return ""
