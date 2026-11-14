# -*- coding: utf-8 -*-
"""出错告警：把严重问题私聊通知主人。

**为什么不是"填一个 openid 就完事"**：
QQ 的 openid 是**按机器人隔离**的 —— 同一个人（同一个 QQ 号）在机器人 A 下
拿到的 openid 和在机器人 B 下拿到的**完全不同**。所以"发给谁"必须和"谁来发"
配对，不能只存一个全局 openid 就指望所有机器人都能用。

本模块的解决办法：
  · 全局选一个「发送告警的机器人」（`security.alert_bot_id`），
    再填该机器人下看到的 openid（`security.alert_owner_openid`）；
  · 渲染时把提示写清楚：这两个值必须来自同一个机器人，混用会发不出去；
  · 发送失败时把真实原因写进日志与审计（常见原因是 openid 与机器人不匹配），
    而不是静默失败 —— 否则用户只会看到"告警没收到"却不知道为什么。

**只发严重错误**：普通日志、警告一律不发，避免变成刷屏机器。
同一类问题按 `alert_cooldown_seconds` 冷却，避免同一个错误反复骚扰。
"""

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 哪些问题算"严重到要私聊通知"（用稳定的 key 做冷却分组）
SEVERITY_ERROR = "error"


class Alerter:
    """按配置把严重问题私聊发给主人。

    用法：`alerter.notify("gateway_dead", "机器人 bot1 连续 10 次连不上网关")`
    """

    def __init__(self, config_manager, runtime_getter=None, logger_obj=None):
        self.config_manager = config_manager
        self._runtime = runtime_getter          # 取运行时（延迟获取，避免循环依赖）
        self.log = logger_obj or logger
        self._last_sent: Dict[str, float] = {}  # key -> 上次发送时间

    # ---------------------------------------------------------------- 配置
    def _cfg(self, section: str, key: str, default: str = "") -> str:
        """读字符串配置；读不到时返回 default。"""
        try:
            return str(self.config_manager.config.str_of(section, key, default=default))
        except Exception:
            return default

    def target(self) -> Dict[str, str]:
        """当前生效的告警目标：`{"bot_id": ..., "openid": ...}`（不可用时为空串）。"""
        bot_id = self._cfg("security", "alert_bot_id").strip()
        openid = self._cfg("security", "alert_owner_openid").strip()
        return {"bot_id": bot_id, "openid": openid}

    def available(self) -> bool:
        """告警是否可以真的发出去（开关打开、选了机器人、填了 openid）。"""
        if not self._cfg_bool("security", "alert_enabled", True):
            return False
        got = self.target()
        return bool(got["bot_id"] and got["openid"])

    def _cfg_bool(self, section: str, key: str, default: bool = True) -> bool:
        try:
            return bool(self.config_manager.config.bool_of(section, key, default=default))
        except Exception:
            return default

    def _cooldown(self) -> int:
        try:
            return max(60, int(self.config_manager.config.int_of(
                "security", "alert_cooldown_seconds", default=600)))
        except Exception:
            return 600

    # ---------------------------------------------------------------- 发送
    def notify(self, key: str, message: str, detail: str = "") -> bool:
        """发一条告警。`key` 用来做冷却分组（同类问题冷却期内只发一次）。

        返回是否真的发出去了。任何异常都不会往外抛 —— 告警本身不能影响主流程。
        """
        if not self.available():
            return False
        now = time.time()
        last = self._last_sent.get(key, 0)
        if last and now - last < self._cooldown():
            self.log.debug("告警「%s」在冷却期内，跳过（%.0f 秒前发过）",
                           key, now - last)
            return False

        target = self.target()
        text = "⚠️ 【QQBotMerged 告警】\n%s" % message
        if detail:
            text += "\n\n详情：%s" % detail

        try:
            api = self._api_for(target["bot_id"])
            if api is None:
                self.log.warning("告警未发送：机器人 %s 当前不可用（没连上或没配置凭据）",
                                 target["bot_id"])
                return False
            api.send_text("c2c", target["openid"], text)
            self._last_sent[key] = now
            self.log.info("已发送告警给主人（机器人 %s）：%s", target["bot_id"], message[:80])
            return True
        except Exception as exc:
            # **把真实原因写出来**：最常见的就是 openid 与所选机器人不匹配
            self.log.warning("告警发送失败（机器人 %s）：%s。"
                             "常见原因：告警 openid 不是在这个机器人下获取的 —— "
                             "openid 按机器人隔离，各机器人的不能混用",
                             target["bot_id"], exc)
            return False

    def _api_for(self, bot_id: str):
        """取出某个机器人的 API 客户端（用它自己凭据发消息）。"""
        try:
            rt = self._runtime() if self._runtime else None
            if rt is None:
                return None
            bot = (getattr(rt, "bots", {}) or {}).get(bot_id)
            if bot is None:
                return None
            client = getattr(bot, "client", None)
            if client is None or not getattr(client, "configured", False):
                return None
            return client
        except Exception as exc:
            self.log.debug("取告警机器人 %s 失败：%s", bot_id, exc)
            return None
