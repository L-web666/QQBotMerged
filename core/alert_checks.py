# -*- coding: utf-8 -*-
"""把告警接进运行时：定期检查严重问题，需要时私聊通知主人。

**为什么用"定期体检"而不是在每个错误点插 `notify()`**：
  · 错误点很分散（网关、插件、存储、云同步），逐个加容易漏；
  · 有些问题是"持续状态"（比如机器人一直连不上），只在出错那一刻发一次
    反而会漏掉"当时还没配置告警"的情况。
定期检查能覆盖"当前是否处于异常状态"，配合冷却时间也不会刷屏。

检查项（只报严重问题）：
  · 机器人在启用且配了凭据，却长时间没连上（网关断了 / 凭据失效）
  · 插件加载失败
  · 存储降级成内存库（重启就丢数据）
"""

import logging
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 连续多少秒没连上算"严重"（避免刚启动就误报）
OFFLINE_ALERT_AFTER = 180
CHECK_INTERVAL = 60


def collect_problems(rt) -> List[Dict[str, str]]:
    """检查运行时当前有哪些严重问题。返回 [{key, message, detail}]。"""
    problems: List[Dict[str, str]] = []
    now = time.time()

    # ---- 机器人长时间离线 ----
    try:
        for bot_id, bot in (getattr(rt, "bots", {}) or {}).items():
            if not getattr(bot, "enabled", False):
                continue
            client = getattr(bot, "client", None)
            if client is None or not getattr(client, "configured", False):
                continue                    # 没配凭据的不算"故障"
            info: Dict[str, Any] = {}
            try:
                info = bot.status() or {}
            except Exception:
                info = {}
            if info.get("online"):
                bot._offline_since = None   # 连上了就清掉计时
                continue
            state = str(info.get("state") or "未知")
            since = getattr(bot, "_offline_since", None)
            if since is None:
                bot._offline_since = now    # 第一次看到它离线，从此刻开始计时
                continue
            if now - since < OFFLINE_ALERT_AFTER:
                continue
            problems.append({
                "key": "bot_offline:%s" % bot_id,
                "message": "机器人「%s」已超过 %d 分钟没有连上 QQ" % (
                    info.get("name") or bot_id, (now - since) // 60),
                "detail": "当前状态：%s。请检查 AppID/AppSecret 是否有效、"
                          "服务器能否访问 QQ 网关" % state,
            })
    except Exception as exc:
        logger.debug("检查机器人在线状态失败：%s", exc)

    # ---- 插件加载失败 ----
    try:
        manager = getattr(rt, "plugin_manager", None)
        failed = []
        for info in (manager.list_plugins() if manager is not None else []):
            if isinstance(info, dict) and info.get("error"):
                failed.append("%s（%s）" % (info.get("name"), str(info["error"])[:80]))
        if failed:
            problems.append({
                "key": "plugin_load_failed",
                "message": "有 %d 个插件没能加载" % len(failed),
                "detail": "；".join(failed[:5]),
            })
    except Exception as exc:
        logger.debug("检查插件状态失败：%s", exc)

    # ---- 存储降级 ----
    try:
        store = getattr(rt, "store", None)
        # 与状态接口同一套判定：不是 SQLiteStore 就是降级成了内存库
        kind = "sqlite" if (store is not None
                            and store.__class__.__name__ == "SQLiteStore") else "memory"
        if kind == "memory":
            problems.append({
                "key": "storage_degraded",
                "message": "数据库未能初始化，当前用的是内存存储",
                "detail": "所有消息只存在内存里，程序重启就会全部丢失。"
                          "请检查「设置 → 存储 → 数据库路径」是否可写。",
            })
    except Exception as exc:
        logger.debug("检查存储状态失败：%s", exc)

    return problems


def check_and_notify(rt, alerter) -> int:
    """跑一次检查并把新问题发出去；返回本次发送的条数。"""
    sent = 0
    for item in collect_problems(rt):
        try:
            if alerter.notify(item["key"], item["message"], item.get("detail", "")):
                sent += 1
        except Exception as exc:
            logger.debug("发送告警失败：%s", exc)
    return sent
