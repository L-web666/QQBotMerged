# -*- coding: utf-8 -*-
"""Runtime 本体：把各 mixin 组合成一个类，并保留原来的 `__init__`。

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

from core.runtime_pkg.config import RuntimeConfigMixin
from core.runtime_pkg.messaging import RuntimeMessagingMixin
from core.runtime_pkg.names import RuntimeNameMixin
from core.runtime_pkg.moderation import RuntimeModerationMixin
from core.runtime_pkg.media import RuntimeMediaMixin
from core.runtime_pkg.panels import RuntimePanelMixin
from core.runtime_pkg.scheduler import RuntimeSchedulerMixin
from core.runtime_pkg.lifecycle import RuntimeLifecycleMixin
from core.runtime_pkg.bot import BotRuntime


class Runtime(RuntimeConfigMixin, RuntimeMessagingMixin, RuntimeNameMixin, RuntimeModerationMixin, RuntimeMediaMixin, RuntimePanelMixin, RuntimeSchedulerMixin, RuntimeLifecycleMixin):
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
        # 缓存文件的读写锁：注册流程与网页删除流程会并发做"整份读-改-写"，
        # 不串行化就会丢更新（RLock：删除接口里 load/pop/save 会嵌套取锁）。
        self._panel_ids_lock = threading.RLock()
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
        # 存储降级记录（SQLite 打不开时会填上原因，供日志/状态接口显示）
        self._store_degraded: Optional[Dict[str, Any]] = None
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
        # 这样就不会把测试数据写进真实的 data/ 里）。
        # 必须和 _make_store() 用同一套解析（abs_path），否则配置里两种分隔符写法下
        # 会出现"库在一个目录、群配置落到另一个目录"的不一致。
        data_dir = self._resolve_data_dir()
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
        # 配置内容指纹：忽略只有 active_bot_id 变化的情况（切换机器人不是"改配置"）
        self._config_fingerprint_cached = self._config_fingerprint()
        # 上一次"热更新完成时"的配置快照：用来算出这次到底改了哪些分组
        # （只改了 send 就不要去重启云同步线程，也不要谎报"过滤与安全、云同步"）
        self._config_snapshot: Dict[str, Any] = self._snapshot_config()
        self.stats_thread: Optional[threading.Thread] = None
