# -*- coding: utf-8 -*-
"""机器人显示名称的自动维护。

**为什么不让用户手填**：机器人在 QQ 开放平台上的名字是唯一的权威来源，
手填既容易写错，也和实际不符（改了平台上的名字，这里不会跟着变）。
所以名称改为程序自动维护：

  1. **保存机器人时**：如果这一条带了 AppSecret（新建，或用户重新填了密钥），
     就用它去问平台（`GET /users/@me`）并把名称写进配置；
  2. **每小时**：拿已保存的凭据再问一次，平台改名后这里会自动跟上。

失败时**保留原名称不动**，只记一条 debug —— 网络抖动不该把名字清空。
"""

import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 刷新间隔（秒）。官方接口 QPS 限制宽松，每小时一次对配额没有影响。
REFRESH_SECONDS = 3600


def fetch_bot_name(app_id: str, app_secret: str, config=None,
                   logger_obj: Optional[logging.Logger] = None) -> str:
    """用 AppID + AppSecret 查询机器人在平台上的名称；失败返回空串。

    **只在内存里使用凭据**：不写配置、不落日志。
    """
    log = logger_obj or logger
    app_id = str(app_id or "").strip()
    app_secret = str(app_secret or "").strip()
    if not app_id or not app_secret:
        return ""
    try:
        from core.qq_api import QQApiClient
        api = QQApiClient(bot_id="name-refresh", app_id=app_id,
                          app_secret=app_secret, config=config, logger_obj=log)
        info = api.bot_info()
        return str((info or {}).get("username") or "").strip()
    except Exception as exc:
        log.debug("获取机器人名称失败（AppID=%s）：%s", app_id, type(exc).__name__)
        return ""


def refresh_names(config_manager, config=None, logger_obj=None,
                  only_bot_id: str = "", force: bool = False) -> Dict[str, str]:
    """把配置里各机器人的名称更新为平台上的最新名称。

    返回 {bot_id: 新名称}（只包含**真的发生了变化**的条目）。

    `only_bot_id` 非空时只刷新那一个（保存单个机器人时用）。
    `force=False` 时只有"距上次刷新超过一小时"才真的去问，
    避免频繁保存把接口打满。
    """
    log = logger_obj or logger
    updated: Dict[str, str] = {}
    try:
        bots = config_manager.config.get("bots") or []
    except Exception:
        return updated
    if not isinstance(bots, list):
        return updated

    changed = False
    for index, bot in enumerate(bots):
        if not isinstance(bot, dict):
            continue
        bot_id = str(bot.get("id") or f"bot{index + 1}")
        if only_bot_id and bot_id != only_bot_id:
            continue
        secret = str(bot.get("app_secret") or "")
        if not secret or secret == "********":
            continue                    # 没有可用密钥（例如从没填过）
        if not force:
            try:
                last = float(bot.get("name_checked_at") or 0)
            except (TypeError, ValueError):
                last = 0.0
            if last and time.time() - last < REFRESH_SECONDS:
                continue                # 一小时内已经查过
        name = fetch_bot_name(bot.get("app_id"), secret,
                              config=config, logger_obj=log)
        # 无论查没查到都记一次时间，避免"失败就每分钟重试"
        fresh = {"name_checked_at": int(time.time())}
        if name and name != str(bot.get("name") or ""):
            fresh["name"] = name
            updated[bot_id] = name
        for key, value in fresh.items():
            if bot.get(key) != value:
                bot[key] = value
                changed = True
        if bot_id in updated:
            log.info("机器人 %s 的名称已更新为「%s」", bot_id, updated[bot_id])

    if changed:
        try:
            config_manager.set_path("bots", bots)
            config_manager.save()
        except Exception as exc:
            log.debug("写回机器人名称失败：%s", exc)
    return updated


class NameRefresher:
    """后台线程：每小时刷新一次机器人名称。"""

    def __init__(self, config_manager, config=None, logger_obj=None,
                 interval: int = REFRESH_SECONDS):
        self.config_manager = config_manager
        self.config = config
        self.log = logger_obj or logger
        self.interval = max(300, int(interval or REFRESH_SECONDS))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="bot-name-refresh",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2)

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                refresh_names(self.config_manager, config=self.config,
                              logger_obj=self.log)
            except Exception as exc:        # 刷新失败绝不能影响主流程
                self.log.debug("定时刷新机器人名称失败：%s", exc)
