# -*- coding: utf-8 -*-
"""指令面板注册与 panel_id 缓存（RuntimePanelMixin）。

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



class RuntimePanelMixin:
    """指令面板注册与 panel_id 缓存（RuntimePanelMixin）（被 `Runtime` 继承；不要单独实例化）。"""


    # ================================================================== 指令面板
    def register_command_panels(self) -> Dict[str, Any]:
        """按配置向 QQ 注册/更新指令面板（每个启用的机器人各用自己的生效配置）。

        `panels` 属于「每个机器人自己的设置」：设置页保存时会写进该机器人的覆盖，
        所以这里必须取 `bot_config(bot_id)`（全局 + 该机器人覆盖），
        不然在页面上改指令列表永远注册不上去。
        """
        panel_ids = self._load_panel_ids()
        results: Dict[str, Any] = {}
        for bot_id, bot in self.bots.items():
            if not bot.enabled or not bot.client.configured:
                continue
            config = self.bot_config(bot_id)
            if not config.bool_of("panels", "enabled", default=True):
                # 关掉面板时把 QQ 上已经注册的删掉，否则"关闭"看起来没生效
                for scope in ("c2c", "group"):
                    key = f"{bot_id}:{scope}"
                    panel_id = panel_ids.pop(key, "")
                    if panel_id:
                        try:
                            bot.client.delete_command_panel(panel_id)
                        except Exception as exc:
                            self.log.warning("[%s] 删除 %s 指令面板失败：%s", bot_id, scope, exc)
                self._save_panel_ids(panel_ids)
                results[bot_id] = {"skipped": "该机器人的指令面板已关闭（已删除线上面板）"}
                continue
            raw_commands = config.get("panels", "commands", default=[]) or []
            items = []
            for item in raw_commands:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                if str(item.get("type") or "command") == "link":
                    items.append({"type": "link", "name": str(item["name"]),
                                  "link": str(item.get("link") or "")})
                else:
                    items.append({"type": "command", "name": str(item["name"]),
                                  "desc": str(item.get("desc") or "")})
            if not items:
                results[bot_id] = {"skipped": "该机器人的指令列表为空"}
                continue

            remark = config.str_of("panels", "remark", default="QQBotMerged 指令面板")
            c2c_section = {
                "target_type": config.str_of("panels", "c2c_target_type", default="all"),
                "openids": config.list_of("panels", "c2c_openids", default=[]),
            }
            group_section = {
                "target_type": config.str_of("panels", "group_target_type", default="all"),
                "openids": config.list_of("panels", "group_openids", default=[]),
            }
            client = bot.client
            bot_result: Dict[str, Any] = {}
            for scope, section in (("c2c", c2c_section), ("group", group_section)):
                key = f"{bot_id}:{scope}"
                target_type = section["target_type"]
                openids = section["openids"]
                panel_id = panel_ids.get(key)
                if not panel_id:
                    try:
                        existing = client.list_command_panels(scope)
                        if existing:
                            panel_id = existing[0].get("panel_id") or existing[0].get("id")
                            if panel_id:
                                panel_ids[key] = panel_id
                                self._save_panel_ids(panel_ids)
                                self.log.info("[%s] 复用已有 %s 面板：%s", bot_id, scope, panel_id)
                    except Exception as exc:
                        self.log.warning("[%s] 查询已有 %s 面板失败：%s", bot_id, scope, exc)
                ok = False
                if panel_id:
                    ok = client.update_command_panel(panel_id, scope, target_type, items, remark, openids)
                    if ok:
                        self.log.info("[%s] 指令面板已更新(%s/%s) panel_id=%s，指令 %d 条",
                                      bot_id, scope, target_type, panel_id, len(items))
                    if not ok:
                        panel_ids.pop(key, None)
                        self._save_panel_ids(panel_ids)
                        panel_id = None
                if not panel_id:
                    panel_id = client.create_command_panel(scope, target_type, items, remark, openids)
                    if panel_id:
                        panel_ids[key] = panel_id
                        self._save_panel_ids(panel_ids)
                        ok = True
                if ok and target_type == "specific" and openids:
                    try:
                        client.set_panel_targets(panel_id, scope, openids, op="add")
                    except Exception as exc:
                        self.log.warning("[%s] 关联面板对象失败：%s", bot_id, exc)
                bot_result[scope] = {"ok": bool(ok), "panel_id": panel_id or ""}
            results[bot_id] = bot_result
        if not results:
            return {"skipped": "没有已启用且填好凭据的机器人"}
        return results

    def _load_panel_ids(self) -> Dict[str, str]:
        path = self.panel_ids_file
        with self._panel_ids_lock:
            try:
                if os.path.isfile(path):
                    with open(path, "r", encoding="utf-8") as handle:
                        raw = json.load(handle) or {}
                    if isinstance(raw, dict):
                        return {str(k): str(v) for k, v in raw.items()}
            except (OSError, ValueError):
                pass
            return {}

    def _save_panel_ids(self, panel_ids: Dict[str, str]):
        path = self.panel_ids_file
        tmp = path + ".tmp"
        # 先写临时文件再 os.replace：写到一半崩溃也不会留下半截 JSON。
        # （坏文件会被 _load_panel_ids 静默当成 {}，下次保存就会把所有机器人的缓存清空）
        with self._panel_ids_lock:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(panel_ids, handle, ensure_ascii=False, indent=1)
                os.replace(tmp, path)
            except OSError as exc:
                try:
                    if os.path.isfile(tmp):
                        os.remove(tmp)          # 别把半截临时文件留在数据目录里
                except OSError:
                    pass
                self.log.debug("保存面板 ID 失败: %s", exc)
