# -*- coding: utf-8 -*-
"""定时任务（RuntimeSchedulerMixin）。

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



class RuntimeSchedulerMixin:
    """定时任务（RuntimeSchedulerMixin）（被 `Runtime` 继承；不要单独实例化）。"""


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
