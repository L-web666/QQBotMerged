# -*- coding: utf-8 -*-
"""从历史消息的原始报文里补齐群成员身份。

为什么需要它：官方群消息的 `author.member_role`（owner / admin / member）**每条都带**，
但老版本解析时把它丢了，当时的群成员表又只有"从历史消息猜出来的成员"（身份全是未知）。
好在每条消息的原始报文一直存在 `messages.raw_event` 里，直接解析就能把真实身份补回来：
不联网、不需要机器人是群管理员、不需要平台白名单。

用法（程序里由群管理页「拉取成员」自动触发；也可以单独跑一段做一次性补齐）：
    from core.member_roles import backfill_from_history
    backfill_from_history(store)
"""

import json
import logging
from typing import Any, Dict, List

from core.group_manager import member_role

logger = logging.getLogger(__name__)


def collect_roles(rows: List[Dict[str, Any]]) -> Dict[tuple, Dict[str, Dict[str, str]]]:
    """把报文行解析成 {(bot_id, group_openid): {member_openid: {role, username}}}。

    身份以**更新的消息为准**（`rows` 是倒序返回的，这里用"先到先得"，
    也就是最新的那条先写入、后面的老消息不覆盖它）。昵称同理，但会补空。
    """
    out: Dict[tuple, Dict[str, Dict[str, str]]] = {}
    for row in rows or []:
        bot_id = str(row.get("bot_id") or "")
        group_openid = str(row.get("group_openid") or "")
        if not bot_id or not group_openid:
            continue
        try:
            payload = json.loads(row.get("raw_event") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        bucket = out.setdefault((bot_id, group_openid), {})

        def remember(openid: str, role: str, username: str):
            if not openid:
                return
            entry = bucket.setdefault(openid, {"role": "", "username": ""})
            if role and not entry["role"]:
                entry["role"] = role
            if username and not entry["username"]:
                entry["username"] = username

        author = payload.get("author") or {}
        if isinstance(author, dict):
            remember(str(author.get("member_openid") or author.get("user_openid") or ""),
                     member_role(author.get("member_role")),
                     str(author.get("username") or ""))
        for mention in payload.get("mentions") or []:
            if not isinstance(mention, dict):
                continue
            remember(str(mention.get("member_openid") or mention.get("id") or ""),
                     member_role(mention.get("member_role")),
                     str(mention.get("username") or ""))
    return out


def backfill_from_history(store, limit: int = 5000) -> Dict[str, int]:
    """从历史报文补齐群成员身份，返回 {群数, 写入成员数}。

    只补**身份仍是未知/空的**成员，不覆盖平台成员接口给过的身份
    （`upsert_members` 本身也不会用 unknown/空串覆盖已知身份，这里再保守一层）。
    """
    getter = getattr(store, "member_role_rows", None)
    if not callable(getter):
        return {"groups": 0, "members": 0}
    try:
        rows = getter(limit=limit)
    except Exception as exc:
        logger.debug("读取历史报文失败: %s", exc)
        return {"groups": 0, "members": 0}
    grouped = collect_roles(rows)
    groups = 0
    written = 0
    for (bot_id, group_openid), entries in grouped.items():
        if not entries:
            continue
        try:
            known = {item.get("member_openid"): item
                     for item in store.list_members(group_openid, bot_id)}
        except Exception:
            known = {}
        batch = []
        for openid, entry in entries.items():
            current = str((known.get(openid) or {}).get("role") or "")
            role = str(entry.get("role") or "")
            username = str(entry.get("username") or "")
            # 已经有明确身份的（owner/admin/member）不再改；这时只补昵称
            if current in ("owner", "admin", "member"):
                if not username or (known.get(openid) or {}).get("username"):
                    continue
                batch.append({"member_openid": openid, "username": username, "role": current})
                continue
            if not role:
                continue
            batch.append({"member_openid": openid, "username": username, "role": role})
        if not batch:
            continue
        try:
            store.upsert_members(bot_id, group_openid, batch)
        except Exception as exc:
            logger.debug("补齐群成员身份失败(%s): %s", group_openid, exc)
            continue
        groups += 1
        written += len(batch)
    if written:
        logger.info("已从历史消息补齐群成员身份：%d 个群、%d 位成员", groups, written)
    return {"groups": groups, "members": written}
