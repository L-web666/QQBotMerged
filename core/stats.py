# -*- coding: utf-8 -*-
"""运行统计（今日 / 7 天 / 累计）

来源：原 `API_qqbot/core/stats.py`，扩充了合并版需要的指标
（消息收发、AI 调用、插件回复、媒体留存、禁言拦截等），并落地为 `data/stats.json`。
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

STAT_KEYS = (
    "messages", "messages_group", "messages_c2c", "messages_with_file",
    "ai_calls", "ai_errors", "keyword_hits", "commands", "plugin_replies",
    "filtered", "rate_limited", "sensitive_blocked", "sensitive_masked",
    "replies", "busy_replies", "process_errors",
    "media_saved", "media_failed", "muted_blocked",
    "sent_text", "sent_image", "sent_file", "duplicates", "recalled", "recall_failed",
    "recalled_member",
)


class StatsCollector:
    """按天累计的运行统计。"""

    def __init__(self, path: str = "data/stats.json", keep_days: int = 30,
                 logger_obj: logging.Logger = None):
        self.path = path
        self.keep_days = max(1, int(keep_days or 30))
        self.log = logger_obj or logger
        self._lock = threading.Lock()
        self.data: Dict[str, Dict[str, int]] = {}
        self.totals: Dict[str, int] = {}
        self.started_at = time.time()
        self._load()

    # ------------------------------------------------------------------ 持久化
    def _load(self):
        try:
            if os.path.isfile(self.path):
                with open(self.path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle) or {}
                self.data = {day: {k: int(v) for k, v in (values or {}).items()}
                             for day, values in (raw.get("daily") or {}).items()}
                self.totals = {k: int(v) for k, v in (raw.get("totals") or {}).items()}
        except (OSError, ValueError) as exc:
            self.log.warning("读取统计失败: %s", exc)
            self.data, self.totals = {}, {}

    def save(self):
        with self._lock:
            payload = {"daily": self.data, "totals": self.totals,
                       "updated_at": time.time(),
                       "updated_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            try:
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except OSError as exc:
                self.log.warning("保存统计失败: %s", exc)

    # ------------------------------------------------------------------ 记录
    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def record(self, key: str, count: int = 1):
        if not key:
            return
        day = self._today()
        with self._lock:
            bucket = self.data.setdefault(day, {})
            bucket[key] = bucket.get(key, 0) + int(count)
            self.totals[key] = self.totals.get(key, 0) + int(count)
            self._prune_locked()

    def record_many(self, **counts: int):
        for key, value in counts.items():
            self.record(key, value)

    def _prune_locked(self):
        cutoff = (datetime.now() - timedelta(days=self.keep_days)).strftime("%Y-%m-%d")
        for day in [d for d in self.data if d < cutoff]:
            self.data.pop(day, None)

    # ------------------------------------------------------------------ 读取
    def today(self) -> Dict[str, int]:
        return dict(self.data.get(self._today(), {}))

    def recent(self, days: int = 7) -> List[Dict[str, Any]]:
        out = []
        for offset in range(days - 1, -1, -1):
            day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
            values = self.data.get(day, {})
            out.append({
                "day": day,
                "label": day[5:],
                "messages": values.get("messages", 0),
                "ai_calls": values.get("ai_calls", 0),
                "replies": values.get("replies", 0),
                "errors": values.get("process_errors", 0) + values.get("ai_errors", 0),
                "media_saved": values.get("media_saved", 0),
            })
        return out

    def summary(self) -> Dict[str, Any]:
        today = self.today()
        return {
            "date": self._today(),
            "uptime_seconds": int(time.time() - self.started_at),
            "today": today,
            "today_total": sum(today.values()),
            "totals": dict(self.totals),
            "total_all": sum(self.totals.values()),
            "recent": self.recent(7),
            "keys": list(STAT_KEYS),
        }
