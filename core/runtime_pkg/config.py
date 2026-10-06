# -*- coding: utf-8 -*-
"""配置解析 / 按机器人取配置 / 热更新（RuntimeConfigMixin）。

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

from core.runtime_pkg.base import _bot_change_labels, _json_same, _section_label
from core.runtime_pkg.bot import BotRuntime


class RuntimeConfigMixin:
    """配置解析 / 按机器人取配置 / 热更新（RuntimeConfigMixin）（被 `Runtime` 继承；不要单独实例化）。"""


    # ================================================================== 构建
    def _resolve_data_dir(self) -> str:
        """消息库所在目录（群配置/群名缓存跟它放在一起）。

        必须和 `_make_store()` 用**同一套**解析（`Config.abs_path`）：配置里
        `data/messages.db` 与 `data\\messages.db` 两种写法会解析到同一个目录，
        这样"库在一个目录、群配置落到另一个目录"的不一致就不会发生。

        内存库（`:memory:` / 空）没有目录，沿用默认的 `data`（相对程序目录）。
        """
        db_value = str(self.config.str_of("storage", "db_path",
                                          default="data/messages.db") or "data/messages.db").strip()
        if db_value in (":memory:", ""):
            return "data"                          # 内存库没有目录，沿用默认
        return os.path.dirname(self.config.abs_path(db_value)) or "data"

    def _make_store(self) -> SQLiteStore:
        if not self.config.bool_of("storage", "enabled", default=True):
            from core.storage import MemoryStore
            return MemoryStore(max_messages=self.config.int_of("storage", "max_messages", default=20000))
        db_path = self.config.abs_path(
            self.config.str_of("storage", "db_path", default="data/messages.db") or "data/messages.db")
        try:
            return SQLiteStore(
                db_path,
                max_messages=self.config.int_of("storage", "max_messages", default=20000),
                retention_days=self.config.int_of("storage", "retention_days", default=30),
                trim_every=self.config.int_of("storage", "trim_every_writes", default=100),
                max_conversations=self.config.int_of("storage", "max_conversations", default=500),
                messages_per_page=self.config.int_of("storage", "messages_per_page", default=200),
            )
        except Exception as exc:
            # 绝不"静默"降级：内存库一重启就什么都不剩，必须让用户看得见原因与后果
            self._store_degraded = {"db_path": db_path, "error": str(exc)}
            self.log.error(
                "SQLite 初始化失败，已改用**内存存储**：数据库路径 %s（原因：%s）。"
                "当前所有消息只存在内存里，程序一重启就会全部丢失；"
                "请检查「设置 → 存储 → 数据库路径」是否可写、目录是否存在（相对路径默认相对程序目录）",
                db_path, exc)
            from core.storage import MemoryStore
            return MemoryStore()

    def _load_stats(self):
        from core.stats import StatsCollector
        return StatsCollector(path=os.path.join("data", "stats.json"), keep_days=60, logger_obj=self.log)

    def _max_history(self) -> int:
        value = self.config.int_of("reply", "max_history", default=None)
        if value is None:
            value = self.config.int_of("context", "max_history", default=20)
        return int(value or 0)

    def _filter_config(self) -> Dict[str, Any]:
        return {
            "keywords_enabled": self.config.bool_of("filters", "keywords_enabled", default=True),
            "exact_match_responses": self.config.get("filters", "exact_match_responses", default={}) or {},
            "fuzzy_match_responses": self.config.get("filters", "fuzzy_match_responses", default={}) or {},
            "filter_meaningless": self.config.bool_of("reply", "filter_meaningless", default=True),
            "sensitive_enabled": self.config.bool_of("filters", "sensitive_enabled", default=False),
            "sensitive_list": self.config.list_of("filters", "sensitive_list", default=[]),
            "sensitive_replacement": self.config.str_of("filters", "sensitive_replacement", default="***"),
            "sensitive_block_input": self.config.bool_of("filters", "sensitive_block_input", default=False),
        }

    def _build_bots(self):
        """按配置建立/重建启用的机器人实例。

        **同一个 AppID 只允许一条连接**：两条连接会各收一份同样的消息，
        同一条用户消息就会被回复两次、入库两次。这里按 AppID 去重，
        重复的启用项会被跳过并给出明确日志（配置页也会提示）。
        """
        configured = self.config.get("bots", default=[]) or []
        seen = set()
        seen_app_ids: Dict[str, str] = {}
        for index, item in enumerate(configured):
            if not isinstance(item, dict):
                continue
            bot_id = str(item.get("id") or f"bot{index + 1}").strip() or f"bot{index + 1}"
            if bot_id in seen:
                bot_id = f"{bot_id}_{index}"
            seen.add(bot_id)
            enabled = bool(item.get("enabled"))
            app_id = str(item.get("app_id") or "").strip()
            if enabled and app_id:
                owner = seen_app_ids.get(app_id)
                if owner:
                    if enabled:
                        self.log.error(
                            "机器人 %s（%s）与 %s 使用了同一个 AppID，已跳过它："
                            "同一个 AppID 只能有一条连接，否则同一条消息会被回复两次。",
                            bot_id, item.get("name") or bot_id, owner)
                    enabled = False
                else:
                    seen_app_ids[app_id] = f"{bot_id}（{item.get('name') or bot_id}）"
            existing = self.bots.get(bot_id)
            if existing is not None:
                # 保留已连接实例，仅更新启用状态
                existing.enabled = enabled
                existing.name = str(item.get("name") or bot_id)
                if not enabled and existing.gateway._running:
                    existing.stop()
                continue
            if not enabled:
                continue
            bot = BotRuntime(
                bot_id=bot_id,
                name=str(item.get("name") or bot_id),
                app_id=app_id,
                app_secret=str(item.get("app_secret") or "").strip(),
                sandbox=bool(item.get("sandbox")),
                intents=int(item.get("intents") or DEFAULT_INTENTS),
                runtime=self,
                reconnect_attempts=int(item.get("reconnect_attempts") or 5),
                reconnect_interval=int(item.get("reconnect_interval") or 10),
            )
            self.bots[bot_id] = bot

    def set_bots_disabled(self, disabled: bool = True):
        """调试模式：`--no-bots` 启动时调用，之后任何配置变化都不连接 QQ。"""
        self.bots_disabled = bool(disabled)

    # ================================================================== 按机器人取配置
    def bot_config(self, bot_id: str = ""):
        """取某个机器人的**生效配置**（全局配置 + 该机器人的覆盖）。

        聊天、回复、发送等链路都用它，保证"在设置里选中的机器人"和
        "实际生效的机器人设置"是同一个。
        """
        if not bot_id or bot_id not in self.bots:
            return self.config
        with self._lock:
            cached = self._bot_configs.get(bot_id)
        if cached is not None:
            return cached
        try:
            resolved = self.config_manager.resolve_for_bot(bot_id)
        except Exception as exc:
            self.log.warning("[%s] 解析机器人配置失败，改用全局配置：%s", bot_id, exc)
            resolved = self.config
        with self._lock:
            self._bot_configs[bot_id] = resolved
        return resolved

    def bot_ai_client(self, bot_id: str) -> AIClient:
        """按机器人取 AI 客户端（每个机器人可以有自己的模型/密钥/人设）。

        优先用已经注入/缓存过的客户端（测试会注入桩），否则按该机器人的配置创建。
        """
        with self._lock:
            if bot_id in self._ai_clients:
                return self._ai_clients[bot_id]
        config = self.bot_config(bot_id)
        api_key = config.str_of("ai", "api_key", default="")
        base_url = config.str_of("ai", "base_url", default="")
        model = config.str_of("ai", "model", default="")
        # 该机器人没有单独配置 AI 时，沿用全局的 AI 客户端（避免每个机器人都要重填一遍）
        if (not api_key or not base_url or not model) and getattr(self, "ai_client", None) is not None:
            if not (config.get("ai") or {}).get("overrides"):
                with self._lock:
                    self._ai_clients[bot_id] = self.ai_client
                return self.ai_client
        client = AIClient(load_ai_config(config), logger_obj=self.log)
        with self._lock:
            self._ai_clients.setdefault(bot_id, client)
        return client

    def bot_message_filter(self, bot_id: str) -> MessageFilter:
        """按机器人取消息过滤器（关键词回复/敏感词可以各自配置）。"""
        with self._lock:
            if bot_id in self._message_filters:
                return self._message_filters[bot_id]
        cfg = self.bot_config(bot_id)
        message_filter = MessageFilter({
            "keywords_enabled": cfg.bool_of("filters", "keywords_enabled", default=True),
            "exact_match_responses": cfg.get("filters", "exact_match_responses", default={}) or {},
            "fuzzy_match_responses": cfg.get("filters", "fuzzy_match_responses", default={}) or {},
            "filter_meaningless": cfg.bool_of("reply", "filter_meaningless", default=True),
            "sensitive_enabled": cfg.bool_of("filters", "sensitive_enabled", default=False),
            "sensitive_list": cfg.list_of("filters", "sensitive_list", default=[]),
            "sensitive_replacement": cfg.str_of("filters", "sensitive_replacement", default="***"),
            "sensitive_block_input": cfg.bool_of("filters", "sensitive_block_input", default=False),
        }, logger_obj=self.log)
        with self._lock:
            self._message_filters.setdefault(bot_id, message_filter)
        return message_filter

    def invalidate_bot_configs(self):
        """配置变化后让按机器人解析的缓存失效。"""
        with self._lock:
            self._bot_configs.clear()
            self._ai_clients.clear()
            self._message_filters.clear()

    # ================================================================== 发送出口
    def get_client(self, bot_id: str = "") -> Optional[QQApiClient]:
        """按 id 取客户端；bot_id 为空时返回第一个可用的（优先在线的）。"""
        with self._lock:
            bots = [bot for bot in self.bots.values() if bot.enabled]
        if bot_id:
            bot = self.bots.get(bot_id)
            return bot.client if bot and bot.enabled else None
        for bot in bots:
            if bot.gateway.ready:
                return bot.client
        for bot in bots:
            if bot.client.configured:
                return bot.client
        return None

    def pick_bot_id(self, bot_id: str = "") -> str:
        if bot_id and bot_id in self.bots and self.bots[bot_id].enabled:
            return bot_id
        client = self.get_client("")
        for candidate_id, bot in self.bots.items():
            if bot.enabled and bot.client is client:
                return candidate_id
        return ""

    # ================================================================== 配置热更新
    def _snapshot_config(self) -> Dict[str, Any]:
        """当前配置的深拷贝（快照）：热更新时用来判断"这次到底改了什么"。"""
        data = getattr(self.config, "data", None)
        try:
            return copy.deepcopy(data) if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _changed_sections(before: Dict[str, Any], after: Dict[str, Any]) -> set:
        """比较两份配置数据，返回**顶层分组**里真正变了的那些（例：{"filters"}）。

        用途：决定哪些组件需要跟着热更新。以前是不管改了什么，都把过滤器重设一遍、
        把云同步线程重启一遍，日志里就冒出"云同步已停止 / 云同步已启动（每 300 秒…）"
        和"配置已热更新：过滤与安全、云同步"——看起来就像用户动了云同步设置，其实没有。
        """
        if not isinstance(before, dict) or not isinstance(after, dict):
            return set(before or {}) | set(after or {})
        changed = set()
        for key in set(before) | set(after):
            if not _json_same(before.get(key), after.get(key)):
                changed.add(str(key))
        return changed

    @staticmethod
    def _describe_changes(sections: set, before: Dict[str, Any], after: Dict[str, Any]) -> str:
        """把"改了哪些分组"翻成人话，供日志使用。"""
        labels: List[str] = []
        for key in sorted(sections):
            if key == "active_bot_id":
                continue
            if key == "bots":
                labels.extend(_bot_change_labels(before, after))
            else:
                labels.append(_section_label(key))
        return "、".join(dict.fromkeys(labels)) or "无实质变化"

    def _config_mtime(self) -> float:
        try:
            return os.path.getmtime(self.config_manager.config_path)
        except OSError:
            return 0.0

    def _config_watch_tick(self) -> bool:
        """监视循环的"一拍"：真的发生了设置变化（已热更新）返回 True，否则 False。

        抽成独立方法只为了能离线测试这个分支，逻辑与原来完全一致。
        """
        mtime = self._config_mtime()
        if mtime == self._last_config_mtime:
            return False
        # mtime 变了不代表"设置"变了：切换机器人只重写 active_bot_id，
        # 它只是"后台当前选中哪个机器人"，不是设置项，不该白跑一整轮热更新。
        self._last_config_mtime = mtime
        fingerprint = self._config_fingerprint()
        if fingerprint == self._config_fingerprint_cached:
            self.log.debug("config.json 只有 active_bot_id 变化，跳过热更新")
            return False
        self.log.info("检测到 config.json 变化，自动热更新")
        self.on_config_changed(source="文件监视")
        return True

    def _start_config_watcher(self):
        """监视 config.json 的修改时间，改动后自动热更新（与动画设置无关）。"""
        self._watching = True

        def loop():
            while self._watching:
                time.sleep(2.0)
                try:
                    self._config_watch_tick()
                except Exception as exc:
                    self.log.debug("配置监视异常: %s", exc)

        self._watch_thread = threading.Thread(target=loop, name="config-watch", daemon=True)
        self._watch_thread.start()

    def _config_fingerprint(self) -> str:
        """配置指纹：忽略 active_bot_id（它只是"后台当前选中哪个机器人"，不是设置项）。"""
        try:
            with open(self.config_manager.config_path, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
        except (OSError, ValueError):
            return ""
        if isinstance(data, dict):
            data.pop("active_bot_id", None)
        return hashlib.sha256(
            json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _start_stats_saver(self):
        def loop():
            while self._watching:
                time.sleep(60)
                try:
                    self.stats.save()
                except Exception:
                    pass
        self.stats_thread = threading.Thread(target=loop, name="stats-save", daemon=True)
        self.stats_thread.start()

    def on_config_changed(self, source: str = "web") -> List[str]:
        """配置保存后热更新运行中的组件。"""
        # 先记下"上一次热更新完成时"的配置快照，再重新读盘：
        # · 网页保存：内存里的 config.data 早就被改过了，所以只能跟**快照**比；
        # · 外部直接改 config.json：磁盘是新的、内存还是旧的，重新读盘后才比得出来。
        # 两种情况都必须"读完盘再比"，否则文件监视这条路会算出"什么都没变"。
        before_data = self._config_snapshot if isinstance(self._config_snapshot, dict) else {}
        self.config_manager.reload()
        self.config = self.config_manager.config
        sections = self._changed_sections(before_data, self.config.data)
        self.invalidate_bot_configs()          # 按机器人解析的缓存作废
        changed: List[str] = []

        # AI
        ai_config = load_ai_config(self.config)
        before = self.ai_client.status()
        self.ai_client.update_config(ai_config)
        if before.get("model") != self.ai_client.model or before.get("usable") != self.ai_client.usable:
            changed.append("AI 配置")

        # 过滤器：只有「过滤与安全 / 回复与消息」真的改了才重设并上报
        if sections & {"filters", "reply"}:
            self.message_filter.update(self._filter_config())
            if "filters" in sections:
                changed.append("过滤与安全")
            if "reply" in sections:
                changed.append("回复与消息")

        # 上下文条数
        if sections & {"reply", "context"}:
            try:
                self.context_manager.MAX_HISTORY = self._max_history()
            except Exception:
                pass

        # 存储限制
        if "storage" in sections:
            try:
                self.store.apply_limits(
                    max_messages=self.config.int_of("storage", "max_messages", default=20000),
                    retention_days=self.config.int_of("storage", "retention_days", default=30),
                    max_conversations=self.config.int_of("storage", "max_conversations", default=500),
                    messages_per_page=self.config.int_of("storage", "messages_per_page", default=200),
                    trim_every=self.config.int_of("storage", "trim_every_writes", default=100),
                )
            except Exception as exc:
                self.log.debug("应用存储限制失败: %s", exc)

        # 插件开关与配置
        if sections & {"plugins", "bots"}:
            if not self.config.bool_of("plugins", "enabled", default=True):
                self.plugin_manager.plugins = []
            elif not self.plugin_manager.plugins:
                self.plugin_manager.load_plugins()
            self.plugin_manager.refresh_bot_config(self.config.data)

        # 机器人增删/凭据变更
        self._reconcile_bots()

        # 云同步：**只有云同步自己的设置变了**才重启同步线程
        # （以前无条件重启，导致改任何设置都打印"云同步已停止/已启动"）
        if "cloud_sync" in sections and self.cloud_sync is not None:
            try:
                self.cloud_sync.stop(final_sync=False)
                if self.cloud_sync.enabled:
                    self.cloud_sync.start(immediate=False)
                    changed.append("云同步")
            except Exception as exc:
                self.log.warning("重启云同步失败：%s", exc)

        # 日志级别
        if "logging" in sections:
            level = self.config.str_of("logging", "level", default="INFO")
            self.log.setLevel(getattr(logging, level.upper(), logging.INFO))

        self._last_config_mtime = self._config_mtime()
        # 同步刷新指纹：否则 2 秒后监视线程会对同一次改动再热更新一遍
        self._config_fingerprint_cached = self._config_fingerprint()
        self._config_snapshot = self._snapshot_config()
        self.broadcast({"type": "config_changed", "source": source})
        # 日志要说实话：只列**真的重载过**的组件；没有组件需要重载时也讲清楚
        # （按机器人读取的设置本来就是即时生效的，不需要"热更新"这一步）
        detail = "、".join(changed)
        if not detail:
            detail = ("改动：%s；无需重载组件，按机器人读取的设置已即时生效"
                      % self._describe_changes(sections, before_data,
                                               self.config.data)) if sections else "无实质变化"
        self.log.info("配置已热更新（来源：%s）：%s", source, detail)
        return changed

    def _reconcile_bots(self):
        """按最新配置新建/停止机器人（**只在凭据真的变了时才重连**）。

        注意：以前这里是"配置一变就把所有机器人重启一遍"，导致
        · 每次保存设置都会断线重连一次（正在处理的消息被打断）；
        · 没填凭据的机器人反复打印"网关已停止"；
        现在只有当 app_id / app_secret / 沙箱 / intents 真的变化时才重启。

        另外：`python run.py --no-bots`（调试模式）下**永远不连接 QQ**。
        以前保存一次设置就会把机器人连上，调试时很容易造成重复回复。
        """
        if self.bots_disabled:
            for bot_id, bot in list(self.bots.items()):
                if bot.gateway._running:
                    bot.stop()
                bot.enabled = False
            return
        configured = {str(item.get("id")): item for item in (self.config.get("bots", default=[]) or [])
                      if isinstance(item, dict)}
        # 停用已被删除或禁用的
        for bot_id, bot in list(self.bots.items()):
            item = configured.get(bot_id)
            if item is None or not item.get("enabled"):
                if bot.gateway._running:
                    bot.stop()
                bot.enabled = False
        # 新建/重启凭据变化的
        for bot_id, item in configured.items():
            if not item.get("enabled"):
                continue
            app_id = str(item.get("app_id") or "").strip()
            app_secret = str(item.get("app_secret") or "").strip()
            sandbox = bool(item.get("sandbox"))
            intents = int(item.get("intents") or DEFAULT_INTENTS)
            existing = self.bots.get(bot_id)
            if existing is None:
                self._build_bots()
                existing = self.bots.get(bot_id)
                if existing and existing.client.configured:
                    existing.start()
                continue
            existing.enabled = True
            existing.name = str(item.get("name") or bot_id)
            credentials_changed = (
                existing.client.app_id != app_id
                or existing.client.app_secret != app_secret
                or existing.client.sandbox != sandbox
                or existing.gateway.intents != intents)
            if credentials_changed:
                existing.client.app_id = app_id
                existing.client.app_secret = app_secret
                existing.client.sandbox = sandbox
                existing.client.token = None
                existing.client.token_expires_at = 0
                existing.gateway.intents = intents
                existing.gateway._effective_intents = intents
                if not app_id or not app_secret:
                    # 凭据被清空：只断开，不重连（否则会不停地重试）
                    if existing.gateway._running:
                        existing.gateway.stop()
                    self.log.warning("[%s] 凭据已被清空，已断开连接（不再重试）", bot_id)
                    continue
                self.log.info("[%s] 凭据已变更，正在重连", bot_id)
                existing.gateway.restart()
            elif existing.client.configured and not existing.gateway._running:
                # 之前因为缺凭据没连上，现在配置齐了 → 补一次连接
                existing.start()
