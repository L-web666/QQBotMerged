# -*- coding: utf-8 -*-
"""出错告警：把严重问题私聊通知主人。

**为什么整组配置按机器人隔离**：
QQ 的 openid 是**按机器人隔离**的 —— 同一个人（同一个 QQ 号）在机器人 A 下
拿到的 openid 和在机器人 B 下拿到的**完全不同**。所以"谁的管理员""告警发给谁"
天然就该跟着机器人走，用一份全局值根本表达不了。

本模块的做法：
  · `security` 分组整体按机器人隔离（见 `PER_BOT_SECTIONS`），
    每个机器人各有一套「启用告警 / 接收人 openid / 冷却 / 管理员 openid」；
  · 告警由**出事的那个机器人自己**发（用它自己的凭据与 openid 配对），
    不会出现"用 A 的凭据发 B 的 openid"这种必然失败的情况；
  · 出事的机器人如果本身就断了，那就发不出去 —— 此时把原因写进日志，
    而不是假装成功（这是这套设计的已知限制，写在这里避免以后误解）。

**只发严重错误**：普通日志、警告一律不发，避免变成刷屏机器。
同一类问题按 `alert_cooldown_seconds` 冷却，避免反复骚扰。
"""

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 哪些问题算"严重到要私聊通知"（用稳定的 key 做冷却分组）
SEVERITY_ERROR = "error"


class Alerter:
    """按机器人把严重问题私聊发给该机器人的管理员。

    用法：`alerter.notify("bot1", "gateway_dead", "连续 10 次连不上网关")`
    """

    def __init__(self, config_manager, runtime_getter=None, logger_obj=None):
        self.config_manager = config_manager
        self._runtime = runtime_getter          # 取运行时（延迟获取，避免循环依赖）
        self.log = logger_obj or logger
        self._last_sent: Dict[str, float] = {}  # "bot_id|key" -> 上次发送时间

    # ---------------------------------------------------------------- 配置
    def _resolved(self, bot_id: str):
        """取某个机器人**生效的** security 配置（per-bot 覆盖合并后的）。"""
        try:
            resolved = self.config_manager.resolve_for_bot(bot_id)
            return resolved or {}
        except Exception:
            try:
                return self.config_manager.config.data.get("security") or {}
            except Exception:
                return {}

    def target(self, bot_id: str) -> Dict[str, str]:
        """该机器人的告警目标：`{"bot_id": ..., "openid": ...}`（不可用时 openid 为空）。"""
        sec = self._resolved(bot_id).get("security") or {}
        return {"bot_id": bot_id,
                "openid": str(sec.get("alert_owner_openid") or "").strip()}

    def available(self, bot_id: str) -> bool:
        """该机器人的告警是否可以真的发出去（开关打开 + 填了 openid）。"""
        sec = self._resolved(bot_id).get("security") or {}
        if not bool(sec.get("alert_enabled", True)):
            return False
        return bool(str(sec.get("alert_owner_openid") or "").strip())

    def _cooldown(self, bot_id: str) -> int:
        sec = self._resolved(bot_id).get("security") or {}
        try:
            return max(60, int(sec.get("alert_cooldown_seconds") or 600))
        except (TypeError, ValueError):
            return 600

    # ---------------------------------------------------------------- 发送
    def notify(self, bot_id: str, key: str, message: str, detail: str = "") -> bool:
        """用 `bot_id` 这个机器人发一条告警。`key` 用来做冷却分组。

        返回是否真的发出去了。任何异常都不会往外抛 —— 告警本身不能影响主流程。
        """
        bot_id = str(bot_id or "").strip()
        if not bot_id or not self.available(bot_id):
            return False
        cache_key = "%s|%s" % (bot_id, key)
        now = time.time()
        last = self._last_sent.get(cache_key, 0)
        if last and now - last < self._cooldown(bot_id):
            self.log.debug("告警「%s」（%s）在冷却期内，跳过", key, bot_id)
            return False

        openid = self.target(bot_id)["openid"]
        text = "⚠️ 【QQBotMerged 告警】\n%s" % message
        if detail:
            text += "\n\n详情：%s" % detail

        try:
            api = self._api_for(bot_id)
            if api is None:
                self.log.warning("告警未发送：机器人 %s 当前不可用（没连上或没配置凭据）。"
                                 "这是已知限制 —— 出事的机器人自己断了就发不出告警",
                                 bot_id)
                return False
            api.send_text("c2c", openid, text)
            self._last_sent[cache_key] = now
            self.log.info("已发送告警（机器人 %s）：%s", bot_id, message[:80])
            return True
        except Exception as exc:
            # 最常见的原因：openid 不是这个机器人下的
            self.log.warning("告警发送失败（机器人 %s）：%s。"
                             "常见原因：告警 openid 不是在这个机器人下获取的 —— "
                             "openid 按机器人隔离，各机器人的不能混用",
                             bot_id, exc)
            return False

    def _api_for(self, bot_id: str):
        """取出该机器人的 API 客户端（用它自己的凭据发）。"""
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
