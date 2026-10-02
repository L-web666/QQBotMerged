# -*- coding: utf-8 -*-
"""运行时核心：多机器人调度、消息总线、发送出口、配置热更新

这是合并版的“大脑”：
- 每个启用的机器人一条独立 WebSocket 连接（`gateway.py`）与一个 API 客户端（`qq_api.py`）；
- 收到消息 → 落库 → **立即留存附件** → 广播给网页 → 交给回复链路；
- 网页/插件/定时任务都通过 `Runtime.send_*` 统一出口发消息，并同样落库与广播；
- 配置保存后调用 `Runtime.on_config_changed()` 做热更新（无需重启的项立即生效）。
"""

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
from core.storage import (SQLiteStore, conv_key_for, is_image_attachment,
                          normalize_attachments, preview_text)


class BotRuntime:
    """单个机器人的运行时（客户端 + 网关 + 状态）。"""

    def __init__(self, bot_id: str, name: str, app_id: str, app_secret: str, sandbox: bool,
                 intents: int, runtime: "Runtime", reconnect_attempts: int = 5,
                 reconnect_interval: int = 10):
        self.id = bot_id
        self.name = name or bot_id
        self.runtime = runtime
        self.log = runtime.log
        self.client = QQApiClient(bot_id, app_id, app_secret, sandbox, config=runtime.config,
                                  logger_obj=self.log)
        self.gateway = QQGateway(
            bot_id=bot_id,
            api_client=self.client,
            intents=intents or DEFAULT_INTENTS,
            on_message=runtime.on_gateway_message,
            on_status=self._on_status,
            reconnect_interval=reconnect_interval,
            max_reconnect_interval=runtime.config.float_of("receiver", "max_reconnect_interval_seconds",
                                                           default=30),
            heartbeat_timeout_factor=runtime.config.float_of("receiver", "heartbeat_timeout_factor",
                                                             default=3),
            logger_obj=self.log,
        )
        self.enabled = True
        self.sent_count = 0
        self.error_count = 0

    def _on_status(self, online: bool, error: str = ""):
        try:
            self.runtime.store.set_bot_status(self.id, online, error, started_at=self.runtime.started_at)
        except Exception:
            pass
        self.runtime.broadcast({"type": "bot_status", "bot_id": self.id, "online": online})

    def start(self):
        if not self.client.configured:
            self.log.warning("[%s] 未填写 app_id / app_secret，已跳过连接", self.id)
            return
        self.gateway.start()

    def stop(self):
        self.gateway.stop()

    def status(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "configured": self.client.configured,
            "sandbox": self.client.sandbox,
            "app_id": _mask_app_id(self.client.app_id),
            "online": self.gateway.ready,
            "state": self.gateway.status_text(),
            "session_id": self.gateway.session_id or "",
            "last_seq": self.gateway.last_seq,
            "heartbeat_interval": self.gateway.heartbeat_interval,
            "intents": self.gateway._effective_intents,
            "token_remaining": self.client.token_remaining(),
            "last_error": self.gateway.last_error,
            "sent": self.sent_count,
            "errors": self.error_count,
            "ready_at": self.gateway.ready_at,
            "last_event": self.gateway.last_event_type,
            "all_message_mode": bool(self.gateway.all_message_mode),
            "events": dict(self.gateway.event_counts),
        }


def _mask_app_id(app_id: str) -> str:
    app_id = str(app_id or "")
    if len(app_id) <= 4:
        return app_id
    return app_id[:4] + "****" + app_id[-2:]


class Runtime:
    """全局运行时：共享服务 + 多机器人管理。"""

    def __init__(self, config_manager, logger_obj: logging.Logger):
        self.config_manager = config_manager
        self.config = config_manager.config
        self.log = logger_obj
        self.started_at = time.time()
        self._lock = threading.RLock()
        self._subscribers: List[Callable[[Dict[str, Any]], None]] = []
        self._sub_lock = threading.Lock()
        self._rate_last: Dict[str, float] = {}
        self._group_name_tried: Dict[str, float] = {}
        # 机器人在群里的身份缓存（撤回群成员消息 / 官方禁言要判断管理员）
        self._bot_roles: Dict[str, Any] = {}
        self._role_pending: set = set()
        # 网页服务的实际监听地址/端口（启动时由 run.py 写入）。
        # 用户改了 web.port 后进程不会自动换端口，靠这两个值给出明确提示。
        self.bind_host = ""
        self.bind_port = 0
        self.port_hint_logged = ""
        # 已注册的指令面板 id 缓存（key = "bot_id:scope"）
        self.panel_ids_file = os.path.join(paths.DATA_DIR, "command_panel.json")
        self._port_thread: Optional[threading.Thread] = None
        self._name_thread: Optional[threading.Thread] = None
        # 关闭程序（网页按钮）
        self._shutdown_requested = False
        self._shutdown_callback = None
        # 调试模式（`--no-bots`）：为 True 时任何配置变化都不会连接 QQ
        self.bots_disabled = False
        # 按机器人解析出来的配置 / AI 客户端 / 过滤器（配置变化时清空）
        self._bot_configs: Dict[str, Any] = {}
        self._ai_clients: Dict[str, AIClient] = {}
        self._message_filters: Dict[str, MessageFilter] = {}

        # ---------- 存储 ----------
        from core import media_store as media_mod
        self.media = media_mod.MediaStore(self._make_store(), self.config)
        self.store = self.media.store
        # 修复历史版本留下的坏数据（幽灵会话、错误昵称），幂等
        try:
            self.store.repair_legacy_data()
            # 注意：这里**不做**"群名等于最后发言者昵称就清掉"的猜测式修复
            # （会误伤"群名恰好是某人昵称"的正常数据），只按群名缓存补名字；
            # 那条启发式改成了网页上手动触发的维护操作。
            suspects = self.store.group_name_suspects()
        except Exception as exc:
            self.log.warning("历史数据修复失败（不影响运行）：%s", exc)
            suspects = 0

        # ---------- 共享服务 ----------
        self.context_manager = ContextManager(
            base_dir=os.path.join("data", "user_context"),
            max_history=self._max_history(), logger_obj=self.log)
        self.message_filter = MessageFilter(self._filter_config(), logger_obj=self.log)
        self.stats = self._load_stats()
        self.ai_client = AIClient(load_ai_config(self.config), logger_obj=self.log)
        # 插件目录：必须解析成**绝对路径**。
        # 原先直接用配置里的 "plugins" 相对路径，一旦程序不是从自己的目录启动
        # （例如在父目录执行 python QQBotMerged/run.py），就会扫描到别的目录，
        # 表现就是"我明明放了插件，后台却一个都不显示"。
        plugin_dir = self.config.str_of("plugins", "dir", default="plugins") or "plugins"
        plugin_dir = self.config.abs_path(plugin_dir)
        self.log.info("插件目录：%s", plugin_dir)
        if not os.path.isdir(plugin_dir):
            try:
                os.makedirs(plugin_dir, exist_ok=True)
            except OSError:
                pass
        self.plugin_manager = PluginManager(
            plugin_dir=plugin_dir,
            data_dir=os.path.join(paths.DATA_DIR, "plugin_data"),
            disabled_file=os.path.join("data", "plugins_disabled.json"),
            logger_obj=self.log,
            enabled=self.config.bool_of("plugins", "enabled", default=True))
        # 旧版把插件数据放在 data/plugins_data/，按 AstrBot 官方约定搬到 data/plugin_data/
        self.plugin_manager.migrate_legacy_data()
        # 必须显式加载一次：即使插件系统被关闭也先扫描，
        # 这样后台「插件管理」页能显示"目录里有什么、为什么没生效"
        loaded_plugins = self.plugin_manager.load_plugins()
        if not self.config.bool_of("plugins", "enabled", default=True):
            self.log.warning("插件系统已禁用（设置 → 插件系统 → 启用插件系统），目录里的插件不会生效")
        else:
            self.log.info("已加载 %d 个插件", len(loaded_plugins))
        # 群配置/群名缓存放在消息库同一个目录（测试里 db_path 指向临时目录，
        # 这样就不会把测试数据写进真实的 data/ 里）
        data_dir = os.path.dirname(self.config.str_of("storage", "db_path",
                                                      default="data/messages.db")) or "data"
        self.group_manager = GroupManager(self.store, self.config, runtime=self,
                                          settings_path=os.path.join(data_dir,
                                                                     "group_settings.json"),
                                          logger_obj=self.log)
        # 群名以 QQ 返回的为准：把缓存里的群名回写到会话表，
        # 顺便把"群名 = 最后发言者昵称"的旧数据彻底修正
        try:
            self.group_manager.resync_conversation_names()
        except Exception as exc:
            self.log.debug("回写群名失败：%s", exc)

        # ---------- 机器人 ----------
        self.bots: Dict[str, BotRuntime] = {}
        self._build_bots()
        self.plugin_manager.set_bot(PluginBot(runtime=self, config=self.config.data, logger_obj=self.log))

        # ---------- 回复链路 ----------
        from core.message_processor import MessageProcessor
        self.processor = MessageProcessor(self)

        # ---------- 云同步（可选，默认关闭） ----------
        self.cloud_sync = None
        try:
            from core.cloud_sync import CloudSync
            if self.config.bool_of("cloud_sync", "enabled", default=False):
                self.cloud_sync = CloudSync(self.config_manager, logger_obj=self.log)
            else:
                self.cloud_sync = CloudSync(self.config_manager, logger_obj=self.log)
        except Exception as exc:
            self.log.warning("云同步模块初始化失败（不影响运行）：%s", exc)

        # ---------- 定时任务 ----------
        self._scheduler_thread: Optional[threading.Thread] = None
        self._scheduler_running = False
        self._schedule_last: Dict[int, str] = {}
        self._watch_thread: Optional[threading.Thread] = None
        self._watching = False
        self._last_config_mtime = self._config_mtime()
        self.stats_thread: Optional[threading.Thread] = None

    # ================================================================== 构建
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
            self.log.error("SQLite 初始化失败（%s），改用内存存储", exc)
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

    def set_bind(self, host: str, port: int):
        """记录网页服务**实际**监听在哪里（由 run.py 启动时调用）。"""
        self.bind_host = host or ""
        self.bind_port = int(port or 0)

    @property
    def web_url(self) -> str:
        """当前实际可访问的网页地址（用户最容易搞错的就是这个）。"""
        if not self.bind_port:
            return ""
        host = self.bind_host or "127.0.0.1"
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        token = self.config.str_of("web", "token", default="")
        return f"http://{host}:{self.bind_port}/" + (f"?token={token}" if token else "")

    NAME_REFRESH_FIRST_DELAY = 12        # 启动后多久开始第一轮（等机器人连接就绪）
    NAME_REFRESH_TICK = 60               # 之后每隔多久检查一次

    def start_name_refresher(self):
        """后台自动刷新群名：群名变了、或还没拿到群名的群，不需要用户手动点。

        每轮最多刷 `limit` 个（默认 6），群信息接口有频率限制，剩下的下一轮继续，
        最终所有群都会拿到名字；刷新后页面轮询会自然看到新名字。
        """
        if self._name_thread is not None and self._name_thread.is_alive():
            return

        def loop():
            time.sleep(self.NAME_REFRESH_FIRST_DELAY)
            while self._watching:
                try:
                    if not self.bots_disabled and any(
                            bot.client.configured for bot in self.bots.values()):
                        result = self.group_manager.refresh_names(limit=6)
                        if result.get("refreshed"):
                            self.log.info("群名自动刷新：%s（%s）", result.get("message"),
                                          "、".join(item["name"] for item in result["refreshed"])[:200])
                            self.broadcast({"type": "groups_changed", "reason": "names",
                                            "detail": result})
                except Exception as exc:
                    self.log.debug("群名自动刷新异常：%s", exc)
                for _ in range(self.NAME_REFRESH_TICK * 2):
                    if not self._watching:
                        return
                    time.sleep(0.5)

        self._name_thread = threading.Thread(target=loop, name="group-names", daemon=True)
        self._name_thread.start()

    def start_port_watcher(self):
        """监视配置里的 web.port 是否和实际监听端口不一致（改了端口要重启才生效）。"""
        if self._port_thread is not None and self._port_thread.is_alive():
            return

        def loop():
            while self._watching:
                time.sleep(4.0)
                try:
                    configured_host = self.config.str_of("web", "host", default="127.0.0.1")
                    configured_port = self.config.int_of("web", "port", default=8666)
                    want = f"{configured_host}:{configured_port}"
                    have = f"{self.bind_host}:{self.bind_port}"
                    if want == have or not self.bind_port:
                        continue
                    if self.port_hint_logged == want:
                        continue
                    self.port_hint_logged = want
                    self.log.warning(
                        "⚠️  配置里的网页地址已改成 %s，但程序当前仍监听在 %s（端口/地址改动需要重启才生效）。"
                        "请用 %s 访问，或重启程序；也可以临时用 python run.py --host %s --port %s 指定。",
                        want, have, self.web_url, configured_host, configured_port)
                    self.broadcast({
                        "type": "port_changed",
                        "message": (f"配置里的网页地址已改为 {want}，但当前仍监听在 {have}。"
                                    f"请访问 {self.web_url}，或重启程序让新配置生效。"),
                        "current_url": self.web_url,
                        "configured": want,
                    })
                except Exception as exc:
                    self.log.debug("端口监视异常: %s", exc)

        self._port_thread = threading.Thread(target=loop, name="port-watch", daemon=True)
        self._port_thread.start()

    # ================================================================== 生命周期
    def start(self):
        message_text.set_name_resolver(self.lookup_name)
        started = 0
        for bot in self.bots.values():
            if not bot.enabled:
                continue
            if not bot.client.configured:
                # 没有凭据的机器人：不进入重连循环（否则会反复打印"未配置凭据"）
                self.log.info("[%s] 未填写 AppID/AppSecret，已跳过连接"
                              "（可在「设置 → 机器人账号」补齐）", bot.id)
                continue
            bot.start()
            started += 1
        self.processor.start()
        self._start_scheduler()
        self._start_config_watcher()
        self._start_stats_saver()
        self.start_port_watcher()
        self.start_name_refresher()
        if self.cloud_sync is not None and self.cloud_sync.enabled:
            self.cloud_sync.start(immediate=self.config.bool_of("cloud_sync", "pull_on_start",
                                                               default=True))
        self.log.info("运行时已启动：%d 个机器人、%d 个插件",
                      sum(1 for bot in self.bots.values() if bot.enabled),
                      len(self.plugin_manager.list_plugins()))
        threading.Thread(target=self._register_panels_safe, name="panels",
                         daemon=True).start()

    def _register_panels_safe(self):
        """启动后延迟注册指令面板（等连接建立；失败只记日志，不影响运行）。"""
        try:
            time.sleep(8)
            result = self.register_command_panels()
            if result:
                self.log.info("指令面板注册结果：%s", json.dumps(result, ensure_ascii=False)[:400])
        except Exception as exc:
            self.log.warning("指令面板注册失败（不影响运行）：%s", exc)

    def stop(self):
        self._scheduler_running = False
        self._watching = False
        try:
            if self.cloud_sync is not None and self.cloud_sync.enabled:
                self.cloud_sync.stop(final_sync=True)
        except Exception as exc:
            self.log.debug("停止云同步失败: %s", exc)
        for bot in self.bots.values():
            try:
                bot.stop()
            except Exception:
                pass
        try:
            self.processor.stop()
        except Exception:
            pass
        try:
            self.stats.save()
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass
        self.log.info("运行时已停止")

    # ================================================================== 订阅（网页实时推送）
    def subscribe(self, callback: Callable[[Dict[str, Any]], None]):
        with self._sub_lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Dict[str, Any]], None]):
        with self._sub_lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def broadcast(self, event: Dict[str, Any]):
        with self._sub_lock:
            targets = list(self._subscribers)
        for callback in targets:
            try:
                callback(event)
            except Exception as exc:
                self.log.debug("推送事件失败: %s", exc)

    # ================================================================== 消息入口
    def on_gateway_message(self, message: Dict[str, Any]):
        """网关回调入口：先落库与留存，再分发到回复链路。"""
        try:
            self._handle_incoming(message)
        except Exception as exc:
            self.log.error("处理入站消息异常: %s", exc, exc_info=True)

    def _handle_incoming(self, message: Dict[str, Any]):
        if message.get("type") == "system":
            self.broadcast({"type": "system_event", "bot_id": message.get("bot_id"),
                            "event": message.get("event")})
            return

        bot_id = message.get("bot_id") or ""
        msg_type = message.get("type") or "private"
        group_openid = message.get("group_openid") or ""
        openid = message.get("openid") or ""
        conv_key = conv_key_for(bot_id, msg_type, group_openid, openid)
        message["conv_key"] = conv_key

        # 群消息里通常带昵称 → 记下来，私聊里就能显示同一个人的名字
        # （同一机器人在群聊与私聊看到的是同一个 openid）
        username = (message.get("username") or "").strip()
        if username and openid and msg_type in ("group", "channel"):
            try:
                self.store.set_user_name(openid, username)
            except Exception as exc:
                self.log.debug("记录群成员昵称失败: %s", exc)

        # 去重：同一条消息（同一机器人 + 同一 msg_id）只处理一次。
        # 触发场景：断线重连/会话恢复时平台重推、或（异常情况下）有多个实例在跑。
        msg_id = message.get("msg_id") or ""
        if msg_id and self.store.has_message(bot_id, msg_id):
            self.log.warning("忽略重复消息（bot=%s msg_id=%s），避免重复回复", bot_id, msg_id)
            self.stats.record("duplicates")
            return

        # 附件：先把 file_info 换成可下载地址，再立即留存
        attachments = self._resolve_attachment_urls(message, bot_id, msg_type, group_openid, openid)
        message["attachments"] = attachments

        media_urls = []
        if self.media.enabled:
            for attachment in attachments:
                url = attachment.get("url")
                if not url:
                    continue
                record = self.media.download(
                    url, file_name=attachment.get("file_name") or "",
                    source="received", bot_id=bot_id, conv_key=conv_key,
                    msg_id=message.get("msg_id") or "")
                if record and record.get("local_url"):
                    attachment["local_url"] = record["local_url"]
                    media_urls.append(record["local_url"])
                    self.stats.record("media_saved")
                elif record is None:
                    self.stats.record("media_failed")
        image_urls = [a.get("local_url") or a.get("url") for a in attachments
                      if media_module.is_probably_image(a.get("url") or "", a.get("content_type") or "")]
        message["media_local"] = media_urls[0] if media_urls else ""
        message["image_url"] = ""

        # 落库
        try:
            saved = self.store.add({
                "bot_id": bot_id,
                "direction": "in",
                "type": msg_type,
                "group_openid": group_openid,
                "openid": openid,
                "username": message.get("username") or "",
                "content": message.get("content") or "",
                "attachments": attachments,
                "quote": message.get("quote") or {},
                "mentions": message.get("mentions") or [],
                "msg_id": message.get("msg_id") or "",
                "msg_idx": message.get("msg_idx") or "",
                "raw_event": message.get("raw_event") or "",
            })
        except Exception as exc:
            self.log.error("消息落库失败: %s", exc)
            saved = None

        self.stats.record("messages")
        self.stats.record("messages_group" if msg_type == "group" else "messages_c2c")
        if attachments:
            self.stats.record("messages_with_file")

        # 附上本地留存地址，供网页显示
        message["_saved"] = saved
        message["_image_urls"] = image_urls
        self.broadcast({"type": "message", "conversation": conv_key,
                        "message": self.public_message(message, bot_id)})

        # 交给回复链路（AI 回复 / 关键词 / 插件）
        self.processor.submit(message)

    def _resolve_attachment_urls(self, message: Dict[str, Any], bot_id: str, msg_type: str,
                                 group_openid: str, openid: str) -> List[Dict[str, Any]]:
        """把只有 file_info 的附件换成真实下载地址（QQ 富媒体规范）。"""
        attachments = normalize_attachments(message.get("attachments"))
        client = self.get_client(bot_id)
        scope_openid = group_openid if msg_type == "group" else openid
        for attachment in attachments:
            if attachment.get("url") or not attachment.get("file_info"):
                continue
            if client is None:
                continue
            url = client.file_download_url("group" if msg_type == "group" else "c2c",
                                           scope_openid, attachment["file_info"])
            if url:
                attachment["url"] = url
        return attachments

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

    # 引用（quote）与撤回（recall）是两回事：
    #   · 撤回：官方限制 **2 分钟** 内（RECALL_WINDOW_SECONDS）；
    #   · 引用：没有 2 分钟限制 —— `msg_id` 只是"被动回复"字段，过期后平台会拒，
    #           但 `message_reference`（引用字段）仍可用于较早的消息。
    # 所以引用时按"被引用消息有多旧"选择字段，绝不能因为超过 2 分钟就不给引用。
    PASSIVE_REPLY_WINDOW_SECONDS = 300

    def _quote_reply_style(self, target_type: str, openid: str, bot_id: str,
                           reply_msg_id: str) -> str:
        """决定这次引用用哪种字段：both / message_reference / msg_id。

        - 找不到本地记录（不知新旧）→ 只用 `message_reference`（最稳，不受被动回复时限影响）
        - 被引用的是 5 分钟内的入站消息 → `both`（被动回复 + 引用，最贴近平台语义）
        - 否则 → 只用 `message_reference`
        """
        if not reply_msg_id:
            return ""
        record = None
        try:
            record = self.store.find_by_msg_id(reply_msg_id, bot_id=bot_id)
        except Exception:
            record = None
        if not record:
            return "message_reference"
        age = time.time() - float(record.get("ts") or 0)
        if (record.get("direction") or "") == "in" and age <= self.PASSIVE_REPLY_WINDOW_SECONDS:
            return "both"
        return "message_reference"

    def send_text(self, target_type: str, openid: str, content: str, bot_id: str = "",
                  reply_msg_id: str = "", direction: str = "out", extra: Dict[str, Any] = None) -> Dict[str, Any]:
        """发送文本并落库/广播。返回 API 返回的数据（失败抛异常）。"""
        client = self.get_client(bot_id)
        bot_id = self.pick_bot_id(bot_id)
        if client is None:
            raise RuntimeError("没有可用的机器人（请在「设置 → 机器人账号」里配置并启用）")
        cfg = self.bot_config(bot_id)
        limit = cfg.int_of("send", "message_max_length", default=4000)
        if len(content or "") > limit:
            raise ValueError(f"文本过长（最多 {limit} 字）")
        info = client.send_text("group" if target_type == "group" else "private",
                                openid, content, reply_msg_id,
                                reply_style=self._quote_reply_style(target_type, openid, bot_id,
                                                                    reply_msg_id))
        self._record_outgoing(bot_id, target_type, openid, content=content,
                              msg_id=(info or {}).get("id") or (info or {}).get("msg_id") or "",
                              msg_idx=(info or {}).get("msg_idx") or (info or {}).get("msgIdx") or "",
                              reply_to=reply_msg_id, extra=extra)
        self.stats.record("sent_text")
        return info or {}

    def send_image(self, target_type: str, openid: str, image_url: str = "", blob: bytes = None,
                   file_name: str = "image.png", content: str = "", bot_id: str = "",
                   reply_msg_id: str = "") -> Dict[str, Any]:
        """发送图片（支持本地字节流或链接）。"""
        client = self.get_client(bot_id)
        bot_id = self.pick_bot_id(bot_id)
        if client is None:
            raise RuntimeError("没有可用的机器人（请在「设置 → 机器人账号」里配置并启用）")
        scope = "group" if target_type == "group" else "private"
        reply_style = self._quote_reply_style(target_type, openid, bot_id, reply_msg_id)
        display_url = image_url
        if blob:
            record = self.media.save_bytes(blob, file_name=file_name, source="sent",
                                           bot_id=bot_id,
                                           conv_key=conv_key_for(bot_id, scope, openid, openid))
            display_url = record.get("local_url") or ""
            info = client.send_image_by_data(scope, openid, blob, file_name or "image.png",
                                             content, reply_msg_id, reply_style=reply_style)
        else:
            info, display_url = client.send_image_by_url(
                scope, openid, image_url, content, reply_msg_id, reply_style=reply_style,
                local_loader=self._load_local_media, remote_loader=self._load_remote_media)
        self._record_outgoing(bot_id, target_type, openid, content=content, image_url=display_url,
                              msg_id=(info or {}).get("id") or "", reply_to=reply_msg_id)
        self.stats.record("sent_image")
        return {"info": info, "image_url": display_url, "note": (info or {}).get("_note", "")}

    def send_file(self, target_type: str, openid: str, blob: bytes, file_name: str,
                  content: str = "", bot_id: str = "", reply_msg_id: str = "",
                  fallback_link: bool = True) -> Dict[str, Any]:
        """发送文件。

        QQ 机器人平台对“文件消息”的支持有限：先尝试平台接口，失败时（默认）退化为
        “把文件存到本地 + 发一条下载链接”的可读方案，并把失败原因一并返回。
        """
        client = self.get_client(bot_id)
        bot_id = self.pick_bot_id(bot_id)
        if client is None:
            raise RuntimeError("没有可用的机器人（请在「设置 → 机器人账号」里配置并启用）")
        conv_key = conv_key_for(bot_id, "group" if target_type == "group" else "private",
                                openid if target_type == "group" else "", 
                                "" if target_type == "group" else openid)
        record = self.media.save_bytes(blob, file_name=file_name, source="sent",
                                       bot_id=bot_id, conv_key=conv_key)
        local_url = record.get("local_url") or ""
        error_text = ""
        try:
            info = client.send_file("group" if target_type == "group" else "private",
                                    openid, blob, file_name, content, reply_msg_id,
                                    reply_style=self._quote_reply_style(
                                        target_type, openid, bot_id, reply_msg_id))
            # 文件内容不在文本里，若不补一句说明，聊天界面就会出现一个"空消息"
            # （用户反馈过：发文件时显示空消息）。这里记录成"📎 文件名"，
            # 并把文件作为附件一起存下来，界面就能显示出这个文件。
            shown = content or ""
            label = f"📎 {file_name}"
            record_text = (shown + "\n" + label) if shown else label
            self._record_outgoing(
                bot_id, target_type, openid, content=record_text,
                msg_id=(info or {}).get("id") or "", reply_to=reply_msg_id,
                extra={"file_name": file_name},
                attachments=[{
                    "url": "", "local_url": local_url, "file_name": file_name,
                    "content_type": record.get("content_type") or "",
                }])
            self.stats.record("sent_file")
            return {"info": info, "image_url": "", "file_url": local_url, "mode": "file",
                    "note": (info or {}).get("_note", "")}
        except Exception as exc:
            error_text = str(exc)
            self.log.warning("文件消息发送失败，将改用链接方式：%s", exc)
            if not fallback_link:
                raise
        # 退化：发文本链接（QQ 内可点开下载）
        link, link_note = self._public_file_link(local_url)
        text = (content + "\n" if content else "") + f"📎 文件：{file_name}\n{link}"
        client.send_text("group" if target_type == "group" else "private", openid, text, reply_msg_id)
        self._record_outgoing(bot_id, target_type, openid, content=text, reply_to=reply_msg_id,
                              extra={"file_name": file_name, "fallback": True})
        self.stats.record("sent_file")
        return {"info": {}, "image_url": "", "file_url": local_url, "link": link, "mode": "link",
                "note": f"平台不支持文件消息，已改为发送下载链接（{link_note}；"
                        f"原因：{error_text[:160]}）"}

    def public_base_url(self) -> str:
        """对外可访问的站点根地址（给"发下载链接"用）。

        顺序：显式配置 `web.public_base_url` → 实际监听地址（非 0.0.0.0 时）→
        本机第一个非回环 IPv4 兜底。**绝不能**把 0.0.0.0 直接写进链接
        （0.0.0.0 不是可访问地址），也尽量不用 127.0.0.1
        （那只有服务器自己能打开，QQ 群里的人点了必然打不开）。
        """
        explicit = (self.config.str_of("web", "public_base_url", default="") or "").strip()
        if explicit:
            return explicit.rstrip("/")
        host = (self.bind_host or self.config.str_of("web", "host", default="127.0.0.1")).strip()
        port = self.bind_port or self.config.int_of("web", "port", default=8666)
        if host in ("", "0.0.0.0", "::"):
            host = self.lan_ip() or "127.0.0.1"
        return f"http://{host}:{port}"

    @staticmethod
    def lan_ip() -> str:
        """猜一个本机在局域网里的 IPv4（拿不到返回空串）。"""
        import socket
        candidates: List[str] = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # 只是选路由，不会真的发包，也不会连上 8.8.8.8
                sock.connect(("8.8.8.8", 80))
                address = sock.getsockname()[0] or ""
                if address and not address.startswith("127."):
                    return address
            finally:
                sock.close()
        except OSError:
            pass
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                address = info[4][0]
                if address and not address.startswith("127."):
                    candidates.append(address)
        except OSError:
            pass
        if not candidates:
            try:
                for address in socket.gethostbyname_ex(socket.gethostname())[2]:
                    if address and not address.startswith("127."):
                        candidates.append(address)
            except OSError:
                pass
        return candidates[0] if candidates else ""

    def _public_file_link(self, local_url: str) -> Tuple[str, str]:
        """把本地文件路径拼成可访问链接，返回 (链接, 给用户看的说明)。"""
        base = self.public_base_url()
        link = f"{base}{local_url}"
        explicit = (self.config.str_of("web", "public_base_url", default="") or "").strip()
        host = self.bind_host or self.config.str_of("web", "host", default="127.0.0.1")
        if explicit:
            note = "链接用的是「设置 → 网页后台 → 对外地址」(web.public_base_url)"
        elif host in ("", "0.0.0.0", "::"):
            note = ("配置里监听的是 0.0.0.0（所有网卡），已自动换成局域网地址；"
                    "如果外面访问不到，请在设置里填 web.public_base_url")
        elif host in ("127.0.0.1", "localhost", "::1"):
            note = ("当前只监听本机，群里的人点不开这个链接；"
                    "请在设置里填 web.public_base_url（例如 http://你的域名或公网IP:8666）")
        else:
            note = "链接用的是当前监听地址"
        if "127.0.0.1" in link or "localhost" in link:
            note += " ⚠️ 该地址只有服务器自己能访问，建议改成对外地址"
        return link, note

    def _load_local_media(self, path: str):
        """读取本地留存媒体（供链接发送时改用上传）。"""
        name = os.path.basename(path or "")
        full = self.media.resolve_local(name)
        if not full:
            raise ValueError("本地图片不存在，可能已被清理")
        with open(full, "rb") as handle:
            return handle.read(), name

    def _load_remote_media(self, url: str):
        """下载远端媒体（供本机链接/防盗链链接改用上传）。"""
        blob, content_type = self.media.fetch(url)
        ext = media_module.guess_ext(url, content_type, ".png")
        return blob, content_type, f"image{ext}"

    def _record_outgoing(self, bot_id: str, target_type: str, openid: str, content: str = "",
                         image_url: str = "", msg_id: str = "", msg_idx: str = "",
                         reply_to: str = "", extra: Dict[str, Any] = None,
                         attachments: List[Dict[str, Any]] = None):
        is_group = target_type == "group"
        conversation = conv_key_for(bot_id, "group" if is_group else "private",
                                    openid if is_group else "", "" if is_group else openid)
        message = {
            "bot_id": bot_id,
            "direction": "out",
            "type": "group" if is_group else "private",
            # 关键：私聊也要写 openid、群聊写 group_openid，
            # 否则机器人发的消息会落到一个 openid 为空的"幽灵会话"里，
            # 会话名会显示成"我"，和真实用户的会话分家。
            "conv_key": conversation,
            "group_openid": openid if is_group else "",
            "openid": "" if is_group else openid,
            "username": "我",
            "content": content,
            "image_url": image_url,
            "attachments": attachments or [],
            "msg_id": msg_id,
            "msg_idx": msg_idx,
            "reply_to": reply_to,
        }
        try:
            saved = self.store.add(message)
        except Exception as exc:
            self.log.error("发送记录落库失败: %s", exc)
            saved = None
        self.broadcast({"type": "message", "conversation": saved["conv_key"] if saved else "",
                        "message": self.public_message(message, bot_id)})
        return saved

    # ================================================================== 对外数据加工
    def lookup_name(self, openid: str) -> str:
        """openid → 显示名。

        优先级：**手动别名** → QQ 群名 → **在群聊里学到的用户昵称** → 历史消息昵称。

        关键点：同一个机器人在群聊和私聊里看到的 openid 是**同一个**，
        而群消息事件通常会带 `author.username`（私聊事件经常是空串）。
        因此收到群消息时会把昵称记下来（`meta` 里的 `uname:<openid>`），
        私聊里就能直接显示出这个人的名字 —— 不需要手动命名。
        """
        if not openid:
            return ""
        try:
            alias = self.store.get_alias(openid)
        except Exception:
            alias = ""
        if alias:
            return alias
        names = self.group_manager.group_names()
        if openid in names:
            return names[openid]
        try:
            learned = self.store.get_meta(f"uname:{openid}", "")
        except Exception:
            learned = ""
        if learned and learned not in ("我", "机器人"):
            return learned
        try:
            index = self.store.name_index(limit=1000)
            name = index.get(openid, "")
        except Exception:
            name = ""
        if name in ("我", "机器人", "bot", "Bot"):
            return ""
        return name

    @staticmethod
    def short_label(openid: str) -> str:
        """QQ 没给昵称时的占位显示：用 openid 尾部做可区分的短标签。"""
        text = str(openid or "").strip()
        if not text:
            return "未知用户"
        return "用户 " + (text[-6:] if len(text) > 6 else text)

    @staticmethod
    def short_group_label(group_openid: str) -> str:
        """群名还没拿到时的占位显示（避免把发言者的昵称当成群名）。"""
        text = str(group_openid or "").strip()
        if not text:
            return "未知群"
        return "群 " + (text[-6:] if len(text) > 6 else text)

    def public_message(self, message: Dict[str, Any], bot_id: str = "") -> Dict[str, Any]:
        """给网页的消息结构（补上渲染需要的一切字段）。"""
        content = message.get("content") or ""
        display, face_images = message_text.extract_face_info(content)
        display = message_text.mention_markup_to_text(display, message.get("mentions"))
        attachments = normalize_attachments(message.get("attachments"))
        # 只有**图片附件**的地址才能当 image_url：否则发一个 .exe/.py 文件时，
        # 前端会把文件地址当图片渲染，然后显示"图片加载失败"（用户反馈过这个错误提示）
        local_images = [item.get("local_url") for item in attachments
                        if item.get("local_url") and is_image_attachment(item)]
        image_url = message.get("image_url") or ""
        if not image_url and local_images:
            image_url = local_images[0]
        quote = message.get("quote") or {}
        if isinstance(quote, dict) and quote.get("content"):
            quote = dict(quote)
            quote["content_display"] = message_text.face_markup_to_text(quote["content"])
        if message_text.is_pure_face_markup(content) and (face_images or image_url):
            display = ""
        direction = message.get("direction") or "in"
        username = message.get("username") or ""
        # 收到的消息里，机器人自己的昵称"我"绝不能当作用户名显示
        if direction != "out" and username == "我":
            username = ""
        sender_name = ""
        if direction != "out":
            sender_name = (self.lookup_name(message.get("openid") or "")
                           or username or self.short_label(message.get("openid") or ""))
        return {
            "id": message.get("id"),
            "bot_id": bot_id or message.get("bot_id") or "",
            "conv_key": message.get("conv_key") or "",
            "type": message.get("type") or "private",
            "direction": direction,
            "group_openid": message.get("group_openid") or "",
            "openid": message.get("openid") or "",
            "username": username,
            "sender_name": sender_name,
            "content": content,
            "content_display": display,
            "content_images": face_images,
            "image_url": image_url,
            "media_local": message.get("media_local") or "",
            "attachments": attachments,
            "quote": quote,
            "mentions": message.get("mentions") or [],
            "msg_id": message.get("msg_id") or "",
            "msg_idx": message.get("msg_idx") or "",
            "reply_to": message.get("reply_to") or "",
            "time": message.get("time") or time.strftime("%Y-%m-%d %H:%M:%S",
                                                         time.localtime(message.get("ts") or time.time())),
            "ts": message.get("ts") or time.time(),
            "preview": preview_text(message),
        }

    def public_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self.public_message(item, item.get("bot_id") or "") for item in messages]

    def public_conversations(self, bot_id: str = "") -> List[Dict[str, Any]]:
        out = []
        groups = self.group_manager.group_names()
        for conv in self.store.conversations(bot_id=bot_id):
            item = dict(conv)
            group_openid = conv.get("group_openid") or ""
            openid = conv.get("openid") or ""
            is_group = conv.get("type") == "group"
            # 跳过"幽灵会话"：既不是群、也没有用户 openid 的会话无法发送，
            # 通常是历史版本把机器人自己的消息记错了地方留下的
            if not is_group and not openid:
                continue
            if is_group and not group_openid:
                continue
            name = ""
            if is_group:
                name = groups.get(group_openid) or ""
                if not name:
                    # 群名以 QQ 为准；拿不到时先用会话表里保存的群名兜底
                    # （绝不用"最后发言者的昵称"，那会让群名乱变）
                    name = conv.get("username") or ""
                if not name:
                    name = self.short_group_label(group_openid)
                if not groups.get(group_openid):
                    self._refresh_group_name_async(group_openid, conv.get("bot_id") or "")
            else:
                # 私聊名一律以"用户自己发的消息/手动别名"为准，
                # 不能直接用会话表里的 username（可能是机器人自己的"我"）
                name = self.lookup_name(openid) or ""
                # QQ 私聊事件经常不返回昵称，这时给一个可区分的占位名，
                # 方便用户在会话列表里认出是谁（可在页面上手动命名）
                if not name:
                    name = self.short_label(openid)
            item["name"] = name
            item["named"] = bool(self.lookup_name(openid)) if not is_group else bool(
                groups.get(group_openid))
            item["display"] = name or conv.get("conv_key") or ""
            item["key"] = conv.get("conv_key") or ""
            out.append(item)
        return out

    def _refresh_group_name_async(self, group_openid: str, bot_id: str):
        """后台补群名（每群至少间隔 60 秒，避免频繁打接口）。"""
        now = time.time()
        last = self._group_name_tried.get(group_openid, 0)
        if now - last < 60:
            return
        self._group_name_tried[group_openid] = now

        def worker():
            try:
                self.group_manager.refresh_group_name(group_openid, bot_id)
                self.broadcast({"type": "conversations_changed", "bot_id": bot_id})
            except Exception as exc:
                self.log.debug("自动获取群名失败: %s", exc)

        threading.Thread(target=worker, name="group-name", daemon=True).start()

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

    # ================================================================== 关闭程序
    def request_shutdown(self, reason: str = "网页操作") -> bool:
        """请求关闭整个程序（网页上的"关闭程序"按钮）。

        真正退出由 run.py 在后台完成：先停机器人/网页服务，再结束进程。
        """
        if self._shutdown_requested:
            return True
        self._shutdown_requested = True
        self.log.warning("收到关闭程序的请求（%s），正在停止…", reason)
        try:
            self._shutdown_callback(reason)
        except Exception as exc:
            self.log.error("执行关闭回调失败：%s", exc)
        return True

    def is_shutdown_requested(self) -> bool:
        return self._shutdown_requested

    def set_shutdown_callback(self, callback):
        self._shutdown_callback = callback

    # ================================================================== 配置热更新
    def _config_mtime(self) -> float:
        try:
            return os.path.getmtime(self.config_manager.config_path)
        except OSError:
            return 0.0

    def _start_config_watcher(self):
        """监视 config.json 的修改时间，改动后自动热更新（与动画设置无关）。"""
        self._watching = True

        def loop():
            while self._watching:
                time.sleep(2.0)
                try:
                    mtime = self._config_mtime()
                    if mtime != self._last_config_mtime:
                        self.log.info("检测到 config.json 变化，自动热更新")
                        self.on_config_changed(source="文件监视")
                except Exception as exc:
                    self.log.debug("配置监视异常: %s", exc)

        self._watch_thread = threading.Thread(target=loop, name="config-watch", daemon=True)
        self._watch_thread.start()

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
        self.config_manager.reload()
        self.config = self.config_manager.config
        self.invalidate_bot_configs()          # 按机器人解析的缓存作废
        changed: List[str] = []

        # AI
        ai_config = load_ai_config(self.config)
        before = self.ai_client.status()
        self.ai_client.update_config(ai_config)
        if before.get("model") != self.ai_client.model or before.get("usable") != self.ai_client.usable:
            changed.append("AI 配置")

        # 过滤器
        self.message_filter.update(self._filter_config())
        changed.append("过滤与安全")

        # 上下文条数
        try:
            self.context_manager.MAX_HISTORY = self._max_history()
        except Exception:
            pass

        # 存储限制
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
        if not self.config.bool_of("plugins", "enabled", default=True):
            self.plugin_manager.plugins = []
        else:
            if not self.plugin_manager.plugins:
                self.plugin_manager.load_plugins()
        self.plugin_manager.refresh_bot_config(self.config.data)

        # 机器人增删/凭据变更
        self._reconcile_bots()

        # 云同步：配置变化后重启同步线程
        if self.cloud_sync is not None:
            try:
                self.cloud_sync.stop(final_sync=False)
                if self.cloud_sync.enabled:
                    self.cloud_sync.start(immediate=False)
                    changed.append("云同步")
            except Exception as exc:
                self.log.warning("重启云同步失败：%s", exc)

        # 日志级别
        level = self.config.str_of("logging", "level", default="INFO")
        self.log.setLevel(getattr(logging, level.upper(), logging.INFO))

        self._last_config_mtime = self._config_mtime()
        self.broadcast({"type": "config_changed", "source": source})
        self.log.info("配置已热更新（来源：%s）：%s", source, "、".join(changed) or "无实质变化")
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

    # ================================================================== 定时任务
    def _start_scheduler(self):
        if not self.config.bool_of("scheduler", "enabled", default=False):
            return
        self._scheduler_running = True

        def loop():
            while self._scheduler_running:
                try:
                    self._scheduler_tick()
                except Exception as exc:
                    self.log.error("定时任务异常: %s", exc)
                time.sleep(20)

        self._scheduler_thread = threading.Thread(target=loop, name="scheduler", daemon=True)
        self._scheduler_thread.start()
        self.log.info("定时任务已启动")

    def _scheduler_tick(self):
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
        today = now.strftime("%Y-%m-%d")
        current = now.strftime("%H:%M")
        tasks = self.config.get("scheduler", "tasks", default=[]) or []
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                continue
            if str(task.get("time") or "").strip() != current:
                continue
            if self._schedule_last.get(index) == today:
                continue
            self._schedule_last[index] = today
            self._run_scheduled_task(task)

    def _run_scheduled_task(self, task: Dict[str, Any]):
        target_type = str(task.get("target_type") or "group")
        target_id = str(task.get("target_id") or "").strip()
        content = str(task.get("content") or "").strip()
        bot_id = str(task.get("bot_id") or "").strip()
        if not target_id or not content:
            self.log.warning("定时任务缺少 target_id 或 content，已跳过")
            return
        try:
            kind = "group" if target_type in ("group", "群聊") else "private"
            self.send_text(kind, target_id, content, bot_id=bot_id)
            self.log.info("定时任务已发送：%s → %s", content[:30], target_id)
        except Exception as exc:
            self.log.error("定时任务发送失败: %s", exc)

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
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(panel_ids, handle, ensure_ascii=False, indent=1)
        except OSError as exc:
            self.log.debug("保存面板 ID 失败: %s", exc)

    # ================================================================== 维护
    def cleanup_media(self, older_than_days: int = 0) -> int:
        return self.media.cleanup(older_than_days)

    def clear_messages(self, conv_key: str = "", bot_id: str = "") -> int:
        """清空消息（指定会话或全部），同时清掉对应上下文。"""
        removed = self.store.clear(conv_key or None, bot_id=bot_id)
        if conv_key:
            parts = conv_key.split(":")
            if len(parts) >= 3:
                conv_bot = parts[0]
                conv_type = "group" if parts[1] == "group" else "private"
                openid = ":".join(parts[2:])
                self.context_manager.clear(conv_bot, conv_type, openid)
        elif bot_id:
            self.context_manager.clear(bot_id=bot_id)
        self.broadcast({"type": "messages_cleared", "conversation": conv_key, "bot_id": bot_id})
        return removed

    def status(self) -> Dict[str, Any]:
        stats = self.stats.summary()
        return {
            "uptime_seconds": int(time.time() - self.started_at),
            "bots": [bot.status() for bot in self.bots.values()],
            "bots_disabled": bool(self.bots_disabled),
            "bot_count": len(self.bots),
            "online_count": sum(1 for bot in self.bots.values() if bot.gateway.ready),
            "ai": self.ai_client.status(),
            "storage": {
                "kind": "sqlite" if isinstance(self.store, SQLiteStore) else "memory",
                "path": self.config.str_of("storage", "db_path", default="data/messages.db"),
                "messages": self.store.count(),
                "conversations": len(self.store.conversations()),
            },
            "media": {
                "dir": self.media.media_dir,
                "stats": self.store.media_stats(),
                "save_enabled": self.media.enabled,
            },
            "plugins": len(self.plugin_manager.list_plugins()),
            "queue": self.processor.queue_size(),
            "stats": stats,
            "beijing_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        }
