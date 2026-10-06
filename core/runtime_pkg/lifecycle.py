# -*- coding: utf-8 -*-
"""生命周期、后台线程、状态与维护（RuntimeLifecycleMixin）。

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



class RuntimeLifecycleMixin:
    """生命周期、后台线程、状态与维护（RuntimeLifecycleMixin）（被 `Runtime` 继承；不要单独实例化）。"""


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
                # 注意 MemoryStore 是 SQLiteStore 的子类：必须先判 MemoryStore，
                # 否则内存库也会被报成 "sqlite"（网页上就看不出"重启即丢消息"）。
                "kind": "memory" if isinstance(self.store, MemoryStore) else "sqlite",
                "path": self.config.str_of("storage", "db_path", default="data/messages.db"),
                "messages": self.store.count(),
                "conversations": len(self.store.conversations()),
                # 非空表示"SQLite 打不开、当前用的是内存库（重启即丢消息）"
                "degraded": dict(self._store_degraded) if self._store_degraded else None,
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
