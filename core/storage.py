# -*- coding: utf-8 -*-
"""消息与会话的 SQLite 持久化（合并版）

融合了两套实现的优点：
- 原 `app/storage.py` 的即时通讯式存储：会话列表、按会话取历史、原始事件回查；
- 原 `API_qqbot` 的统计/上下文需求：多机器人区分、媒体落盘记录、群成员与禁言表。

表结构：
  messages      消息（含收到的与发出的）
  conversations 会话汇总（供左侧列表，避免每次全表聚合）
  media         收到的图片/表情包等媒体文件（本地留存，防 QQ 链接过期）
  group_members 群成员缓存
  mutings       禁言记录
  bot_status    机器人最近一次连接状态
  meta          键值杂项
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id        TEXT NOT NULL DEFAULT '',
    conv_key      TEXT NOT NULL,
    type          TEXT NOT NULL,
    direction     TEXT NOT NULL,
    group_openid  TEXT DEFAULT '',
    openid        TEXT DEFAULT '',
    username      TEXT DEFAULT '',
    content       TEXT DEFAULT '',
    image_url     TEXT DEFAULT '',
    media_local   TEXT DEFAULT '',
    attachments   TEXT DEFAULT '',
    quote         TEXT DEFAULT '',
    mentions      TEXT DEFAULT '',
    reply_to      TEXT DEFAULT '',
    msg_id        TEXT DEFAULT '',
    msg_idx       TEXT DEFAULT '',
    raw_event     TEXT DEFAULT '',
    ts            REAL NOT NULL,
    time_text     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_msg_conv   ON messages(conv_key, id);
CREATE INDEX IF NOT EXISTS idx_msg_bot    ON messages(bot_id, id);
CREATE INDEX IF NOT EXISTS idx_msg_ts     ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_msg_msgid  ON messages(msg_id);

CREATE TABLE IF NOT EXISTS conversations (
    conv_key      TEXT PRIMARY KEY,
    bot_id        TEXT DEFAULT '',
    type          TEXT DEFAULT '',
    group_openid  TEXT DEFAULT '',
    openid        TEXT DEFAULT '',
    username      TEXT DEFAULT '',
    last_content  TEXT DEFAULT '',
    last_ts       REAL DEFAULT 0,
    last_time     TEXT DEFAULT '',
    message_count INTEGER DEFAULT 0,
    unread        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS media (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id       TEXT DEFAULT '',
    conv_key     TEXT DEFAULT '',
    msg_id       TEXT DEFAULT '',
    source       TEXT DEFAULT 'received',
    url          TEXT DEFAULT '',
    url_hash     TEXT DEFAULT '',
    local_name   TEXT DEFAULT '',
    file_name    TEXT DEFAULT '',
    content_type TEXT DEFAULT '',
    size         INTEGER DEFAULT 0,
    state        TEXT DEFAULT 'done',
    error        TEXT DEFAULT '',
    created_at   REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_media_hash ON media(url_hash);
CREATE INDEX IF NOT EXISTS idx_media_conv ON media(conv_key, id);
CREATE INDEX IF NOT EXISTS idx_media_msg  ON media(msg_id);

CREATE TABLE IF NOT EXISTS group_members (
    bot_id       TEXT DEFAULT '',
    group_openid TEXT DEFAULT '',
    member_openid TEXT DEFAULT '',
    username     TEXT DEFAULT '',
    role         TEXT DEFAULT 'member',
    joined_at    REAL DEFAULT 0,
    updated_at   REAL DEFAULT 0,
    PRIMARY KEY (bot_id, group_openid, member_openid)
);

CREATE TABLE IF NOT EXISTS mutings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id       TEXT DEFAULT '',
    group_openid TEXT DEFAULT '',
    member_openid TEXT DEFAULT '',
    username     TEXT DEFAULT '',
    until        REAL DEFAULT 0,
    minutes      INTEGER DEFAULT 0,
    reason       TEXT DEFAULT '',
    mode         TEXT DEFAULT 'local',
    state        TEXT DEFAULT 'active',
    created_at   REAL DEFAULT 0,
    created_by   TEXT DEFAULT 'web'
);
CREATE INDEX IF NOT EXISTS idx_mute_lookup ON mutings(bot_id, group_openid, member_openid, state);

CREATE TABLE IF NOT EXISTS bot_status (
    bot_id       TEXT PRIMARY KEY,
    online       INTEGER DEFAULT 0,
    last_error   TEXT DEFAULT '',
    updated_at   REAL DEFAULT 0,
    started_at   REAL DEFAULT 0,
    extra        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);
"""

MAX_CONTENT_CHARS = 8000
MAX_URL_CHARS = 2000
MAX_ATTACHMENTS = 10
MAX_RAW_EVENT_CHARS = 20000
IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp|svg|ico)(\?|$)", re.IGNORECASE)


# ======================================================================================
# 工具函数
# ======================================================================================
def conversation_key(msg_type: str, group_openid: str = "", openid: str = "", bot_id: str = "") -> str:
    """会话唯一键。

    格式：`[bot_id:]scope:openid`。
    **必须带 bot_id**：两个机器人可能同时在一个群里，不加机器人维度会把
    “机器人 A 看到的群消息”和“机器人 B 看到的群消息”混成同一个会话。
    """
    if msg_type == "group":
        base = f"group:{group_openid or openid}"
    elif msg_type == "channel":
        base = f"channel:{openid}"
    else:
        base = f"private:{openid}"
    return f"{bot_id}:{base}" if bot_id else base


def conv_key_for(bot_id: str, msg_type: str, group_openid: str = "", openid: str = "") -> str:
    """带机器人维度的会话键（运行时代码统一用这个）。"""
    return conversation_key(msg_type, group_openid, openid, bot_id)


def is_image_attachment(att: Any) -> bool:
    """附件是不是图片。

    这个判断很关键：只有图片才能被当成 `<img>` 渲染。历史上它只看
    `att["url"]`，于是"发一个 .exe/.py 文件"时，附件的本地地址被当成图片地址，
    页面就会显示"图片加载失败"——所以本地留存地址、文件名也要一起看。
    """
    if not isinstance(att, dict):
        return False
    ctype = str(att.get("content_type") or "").split(";")[0].strip().lower()
    if ctype.startswith("image/"):
        return True
    # 明确说了是别的类型（file / application/pdf ...）就按非图片处理
    if ctype and ctype not in ("application/octet-stream", "binary/octet-stream"):
        return False
    for key in ("local_url", "url", "file_name"):
        value = str(att.get(key) or "").split("?")[0]
        if os.path.splitext(value)[1].lower() in image_ext_set():
            return True
    return False


def image_ext_set() -> set:
    """图片扩展名集合（storage 侧只做判断，真实列表在 media_store）。"""
    return {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}


def normalize_attachments(raw: Any) -> List[Dict[str, str]]:
    out = []
    for item in (raw or [])[:MAX_ATTACHMENTS]:
        if isinstance(item, dict):
            url = str(item.get("url") or "")[:MAX_URL_CHARS]
            entry = {
                "url": url,
                "content_type": str(item.get("content_type") or "")[:100],
                "local_url": str(item.get("local_url") or "")[:MAX_URL_CHARS],
                "file_info": str(item.get("file_info") or "")[:400],
                "file_name": str(item.get("file_name") or item.get("filename") or "")[:200],
            }
            if url or entry["file_info"] or entry["file_name"]:
                out.append({k: v for k, v in entry.items() if v})
        elif isinstance(item, str) and item:
            out.append({"url": item[:MAX_URL_CHARS]})
    return out


def normalize_quote(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        return {}
    quote = {
        "msg_id": str(raw.get("msg_id") or "")[:200],
        "msg_idx": str(raw.get("msg_idx") or "")[:400],
        "content": str(raw.get("content") or "")[:1000],
    }
    attachments = normalize_attachments(raw.get("attachments"))
    if attachments:
        quote["attachments"] = attachments
    return {k: v for k, v in quote.items() if v}


def normalize_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """补全 conv_key / ts / time，裁剪超长字段（内存与 SQLite 行为一致）。"""
    item = dict(msg)
    msg_type = item.get("type") or "private"
    item["type"] = msg_type
    item["bot_id"] = str(item.get("bot_id") or "")
    item["direction"] = item.get("direction") or "in"
    item["group_openid"] = item.get("group_openid") or ""
    item["openid"] = item.get("openid") or ""
    item["conv_key"] = item.get("conv_key") or conv_key_for(
        item["bot_id"], msg_type, item["group_openid"], item["openid"])
    item["ts"] = float(item.get("ts") or time.time())
    item["time"] = item.get("time") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["ts"]))
    item["content"] = str(item.get("content") or "")[:MAX_CONTENT_CHARS]
    item["username"] = str(item.get("username") or "")[:200]
    item["image_url"] = str(item.get("image_url") or "")[:MAX_URL_CHARS]
    item["media_local"] = str(item.get("media_local") or "")[:MAX_URL_CHARS]
    item["reply_to"] = str(item.get("reply_to") or "")[:200]
    item["msg_id"] = str(item.get("msg_id") or "")[:200]
    item["msg_idx"] = str(item.get("msg_idx") or "")[:400]
    item["attachments"] = normalize_attachments(item.get("attachments"))
    item["quote"] = normalize_quote(item.get("quote"))

    mentions = []
    for entry in (item.get("mentions") or [])[:20]:
        if isinstance(entry, dict) and entry.get("openid"):
            mentions.append({
                "openid": str(entry["openid"])[:200],
                "username": str(entry.get("username") or "")[:60],
                "is_you": bool(entry.get("is_you")),
                "is_bot": bool(entry.get("is_bot")),
            })
    item["mentions"] = mentions
    raw = item.get("raw_event")
    item["raw_event"] = str(raw)[:MAX_RAW_EVENT_CHARS] if raw else ""
    return item


def preview_text(msg: Dict[str, Any]) -> str:
    """会话列表里的一行预览。"""
    content = str(msg.get("content") or "").strip()
    has_image = bool(msg.get("image_url")) or any(
        is_image_attachment(att) for att in (msg.get("attachments") or []))
    if has_image:
        return (f"[图片] {content}").strip()
    if not content and msg.get("attachments"):
        return "[文件]"
    return content


# ======================================================================================
# SQLite 存储
# ======================================================================================
class SQLiteStore:
    """线程安全的消息存储（WAL 模式，写操作串行化）。"""

    def __init__(self, db_path: str, max_messages: int = 20000, retention_days: int = 30,
                 trim_every: int = 100, max_conversations: int = 500,
                 messages_per_page: int = 200):
        self.db_path = db_path
        self.max_messages = int(max_messages or 0)
        self.retention_days = int(retention_days or 0)
        self.trim_every = max(10, int(trim_every or 100))
        self.max_conversations = int(max_conversations or 0)
        self.messages_per_page = max(20, int(messages_per_page or 200))
        self._lock = threading.RLock()
        self._writes = 0

        directory = os.path.dirname(os.path.abspath(db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        logger.info("消息库已就绪: %s", db_path)

    # ------------------------------------------------------------------ 基础
    def _execute(self, sql: str, params: tuple = ()):
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def close(self):
        with self._lock:
            try:
                self._conn.commit()
            except sqlite3.Error:
                pass
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------ 消息
    def add(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """写入一条消息（自动归一化 + 更新会话汇总 + 定期裁剪）。"""
        item = normalize_message(msg)
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO messages
                   (bot_id, conv_key, type, direction, group_openid, openid, username, content,
                    image_url, media_local, attachments, quote, mentions, reply_to, msg_id,
                    msg_idx, raw_event, ts, time_text)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item["bot_id"], item["conv_key"], item["type"], item["direction"],
                 item["group_openid"], item["openid"], item["username"], item["content"],
                 item["image_url"], item["media_local"],
                 json.dumps(item["attachments"], ensure_ascii=False),
                 json.dumps(item["quote"], ensure_ascii=False),
                 json.dumps(item["mentions"], ensure_ascii=False),
                 item["reply_to"], item["msg_id"], item["msg_idx"], item["raw_event"],
                 item["ts"], item["time"]))
            item["id"] = cursor.lastrowid
            self._touch_conversation(item)
            self._conn.commit()
            self._writes += 1
            if self._writes % self.trim_every == 0:
                self._trim_locked()
        return item

    def _touch_conversation(self, item: Dict[str, Any]):
        row = self._conn.execute(
            "SELECT conv_key, username FROM conversations WHERE conv_key=?", (item["conv_key"],)).fetchone()
        username = item["username"]
        if item.get("type") == "group":
            # 群会话的名字只能是**群名**（由群信息接口写进来），
            # 绝不能用发消息那个人的昵称：否则群列表里的群名会随着
            # "群里最后一个发言的人"变来变去（历史版本的问题）。
            username = (row["username"] if row else "") or ""
        elif row is not None and not username:
            username = row["username"] or ""
        count_delta = 1
        if row is None:
            self._conn.execute(
                """INSERT INTO conversations
                   (conv_key, bot_id, type, group_openid, openid, username, last_content,
                    last_ts, last_time, message_count, unread)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
                (item["conv_key"], item["bot_id"], item["type"], item["group_openid"],
                 item["openid"], username, preview_text(item), item["ts"], item["time"], 1))
        else:
            if not username:
                username = row["username"] or ""
            unread = 0 if item["direction"] == "out" else 1
            self._conn.execute(
                """UPDATE conversations
                   SET bot_id=?, type=?, group_openid=?, openid=?, username=?,
                       last_content=?, last_ts=?, last_time=?,
                       message_count=message_count+?, unread=unread+?
                   WHERE conv_key=?""",
                (item["bot_id"] or "", item["type"], item["group_openid"], item["openid"],
                 username, preview_text(item), item["ts"], item["time"], count_delta, unread,
                 item["conv_key"]))

    def update_media_local(self, msg_id: str, local_url: str, bot_id: str = ""):
        """把某条消息的本地留存地址写回（媒体下载完成后调用）。"""
        if not msg_id:
            return
        with self._lock:
            if bot_id:
                self._conn.execute(
                    "UPDATE messages SET media_local=? WHERE msg_id=? AND bot_id=?",
                    (local_url, msg_id, bot_id))
            else:
                self._conn.execute("UPDATE messages SET media_local=? WHERE msg_id=?", (local_url, msg_id))
            self._conn.commit()

    def get_all(self, newest_first: bool = True, limit: Optional[int] = None,
                bot_id: str = "") -> List[Dict[str, Any]]:
        order = "DESC" if newest_first else "ASC"
        sql = f"SELECT * FROM messages {{where}} ORDER BY id {order}"
        params: List[Any] = []
        where = ""
        if bot_id:
            where = "WHERE bot_id=?"
            params.append(bot_id)
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._query(sql.format(where=where), tuple(params))
        return [self._row_to_msg(row) for row in rows]

    def get_conversation(self, conv_key: str, limit: Optional[int] = None,
                         newest_first: bool = False, before_id: Optional[int] = None) -> List[Dict[str, Any]]:
        limit = int(limit or self.messages_per_page)
        params: List[Any] = [conv_key]
        sql = "SELECT * FROM messages WHERE conv_key=?"
        if before_id:
            sql += " AND id < ?"
            params.append(int(before_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._query(sql, tuple(params))
        messages = [self._row_to_msg(row) for row in rows]
        messages.reverse()
        return messages

    def conversations(self, limit: Optional[int] = None, bot_id: str = "") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM conversations {where} ORDER BY last_ts DESC"
        params: List[Any] = []
        where = ""
        if bot_id:
            where = "WHERE bot_id=?"
            params.append(bot_id)
        sql += " LIMIT ?"
        params.append(int(limit or self.max_conversations or 500))
        rows = self._query(sql.format(where=where), tuple(params))
        return [dict(row) for row in rows]

    def conversation(self, conv_key: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM conversations WHERE conv_key=?", (conv_key,))
        return dict(rows[0]) if rows else None

    def rename_conversation(self, conv_key: str, username: str):
        self._execute("UPDATE conversations SET username=? WHERE conv_key=?", (username, conv_key))

    def unread(self, bot_id: str = "") -> Dict[str, int]:
        if bot_id:
            rows = self._query(
                "SELECT conv_key, unread FROM conversations WHERE bot_id=? AND unread>0", (bot_id,))
        else:
            rows = self._query("SELECT conv_key, unread FROM conversations WHERE unread>0")
        return {row["conv_key"]: row["unread"] for row in rows}

    def mark_read(self, conv_key: str):
        if conv_key:
            self._execute("UPDATE conversations SET unread=0 WHERE conv_key=?", (conv_key,))
        else:
            self._execute("UPDATE conversations SET unread=0")

    def count(self, bot_id: str = "") -> int:
        if bot_id:
            rows = self._query("SELECT COUNT(*) AS n FROM messages WHERE bot_id=?", (bot_id,))
        else:
            rows = self._query("SELECT COUNT(*) AS n FROM messages")
        return int(rows[0]["n"]) if rows else 0

    def has_message(self, bot_id: str, msg_id: str) -> bool:
        """是否已经处理过这条消息（用于防重复处理/重复回复）。

        QQ 在断线重连、会话恢复（RESUME）等场景可能重复推送同一个事件，
        多个实例同时运行时也会各收一份，这里按 `机器人 + 消息 id` 去重。
        """
        if not msg_id:
            return False
        if bot_id:
            rows = self._query(
                "SELECT 1 FROM messages WHERE msg_id=? AND bot_id=? LIMIT 1", (msg_id, bot_id))
        else:
            rows = self._query("SELECT 1 FROM messages WHERE msg_id=? LIMIT 1", (msg_id,))
        return bool(rows)

    def repair_legacy_data(self) -> Dict[str, int]:
        """修复历史版本留下的两类坏数据（幂等，可重复调用）。

        1. **幽灵会话**：早期版本记录机器人发出的私聊消息时没有写 `openid`，
           于是那些消息落进了 `bot:private:`（openid 为空）这个无法发送的会话里，
           会话名还会显示成"我"。这里把它们并回该机器人最近的真实用户会话；
           **找不到对应会话时不动它们**（宁可不显示，也不删用户数据），
           这一类会话会被 `conversations()` 之外的展示层过滤掉。
        2. **收到的消息昵称是"我"**：把这些 `username` 清空，让网页改用
           用户自己发来的消息里的昵称。
        """
        stats = {"merged": 0, "removed": 0, "cleared_names": 0, "orphan": 0}
        with self._lock:
            # ---- 1. 幽灵会话 ----
            rows = self._conn.execute(
                """SELECT conv_key, bot_id, type FROM conversations
                   WHERE (type<>'group' AND (openid IS NULL OR openid=''))
                      OR (type='group' AND (group_openid IS NULL OR group_openid=''))"""
            ).fetchall()
            for row in rows:
                conv_key, bot_id, conv_type = row["conv_key"], row["bot_id"] or "", row["type"]
                if conv_type == "group":
                    # 该机器人下真正有 group_openid 的群会话（取最近活跃的一个）
                    target = self._conn.execute(
                        """SELECT conv_key FROM conversations
                           WHERE bot_id=? AND type='group' AND group_openid<>''
                           ORDER BY last_ts DESC LIMIT 1""", (bot_id,)).fetchone()
                else:
                    target = self._conn.execute(
                        """SELECT conv_key FROM conversations
                           WHERE bot_id=? AND type<>'group' AND openid<>''
                           ORDER BY last_ts DESC LIMIT 1""", (bot_id,)).fetchone()
                if target and target["conv_key"] != conv_key:
                    cursor = self._conn.execute(
                        "UPDATE messages SET conv_key=?, bot_id=? WHERE conv_key=?",
                        (target["conv_key"], bot_id, conv_key))
                    stats["merged"] += cursor.rowcount or 0
                    self._conn.execute("DELETE FROM conversations WHERE conv_key=?", (conv_key,))
                else:
                    # 没有可归属的真实会话：保留消息（不删数据），仅标记该会话不再展示
                    stats["orphan"] = stats.get("orphan", 0) + 1
            # ---- 2. 收到的消息不该带"我"这个昵称 ----
            cursor = self._conn.execute(
                "UPDATE messages SET username='' WHERE direction='in' AND username='我'")
            stats["cleared_names"] += cursor.rowcount or 0
            self._conn.commit()
        if any(stats.values()):
            logger.info("已修复历史数据：合并 %d 条、清除错误昵称 %d 条、保留无法归属的旧会话 %d 个",
                        stats["merged"], stats["cleared_names"], stats["orphan"])
        return stats

    def repair_group_conversation_names(self, force: bool = False,
                                        names: Optional[Dict[str, str]] = None) -> Dict[str, int]:
        """修复群会话名（幂等）。

        **默认（force=False）只做安全修复**：把"群名缓存（QQ 返回的群名）"写回会话表，
        绝不会因为"猜"而清掉一个名字。

        force=True 时（网页上手动点「修正群会话名」才用）才启用旧版那条启发式：
        会话名**正好等于**该会话最后一条入站消息的发送者昵称 → 视为"历史版本写坏的发送者名"并清空。
        这条启发式有误伤风险（群里只有一个人说话、而群名恰好是他的昵称时，正确的群名会被清掉），
        所以它只允许由用户主动触发。

        返回 {"named": 按群名缓存修正的数量, "cleared": 清空的数量}。
        """
        names = names or {}
        stats = {"named": 0, "cleared": 0}
        with self._lock:
            rows = self._conn.execute(
                "SELECT conv_key, group_openid, username FROM conversations WHERE type='group'"
            ).fetchall()
            for row in rows:
                current = row["username"] or ""
                cached = (names.get(row["group_openid"] or "") or "").strip()
                if cached and current != cached:
                    # 有权威群名 → 直接修正（这是"补名字"，不是"删名字"）
                    self._conn.execute("UPDATE conversations SET username=? WHERE conv_key=?",
                                       (cached, row["conv_key"]))
                    stats["named"] += 1
                    continue
                if not force or not current or cached:
                    continue
                last = self._conn.execute(
                    """SELECT username FROM messages
                       WHERE conv_key=? AND direction='in' AND username<>''
                       ORDER BY ts DESC, id DESC LIMIT 1""", (row["conv_key"],)).fetchone()
                if last and last["username"] and last["username"] == current:
                    self._conn.execute(
                        "UPDATE conversations SET username='' WHERE conv_key=?", (row["conv_key"],))
                    stats["cleared"] += 1
            if stats["named"] or stats["cleared"]:
                self._conn.commit()
        if stats["named"]:
            logger.info("已按群名缓存修正 %d 个群会话名", stats["named"])
        if stats["cleared"]:
            logger.info("已清理 %d 个被写成「发送者昵称」的群会话名（手动修正）", stats["cleared"])
        return stats

    def group_name_suspects(self) -> int:
        """只统计、不修改：有多少个群会话名"看起来像最后发言者的昵称"。"""
        count = 0
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.conv_key, c.username FROM conversations c
                   WHERE c.type='group' AND c.username<>''""").fetchall()
            for row in rows:
                last = self._conn.execute(
                    """SELECT username FROM messages
                       WHERE conv_key=? AND direction='in' AND username<>''
                       ORDER BY ts DESC, id DESC LIMIT 1""", (row["conv_key"],)).fetchone()
                if last and last["username"] and last["username"] == row["username"]:
                    count += 1
        return count

    def update_message(self, message_id: int, **fields) -> bool:
        """更新一条消息的少数字段（撤回标记、媒体本地地址等）。"""
        allowed = ("content", "image_url", "media_local", "attachments")
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            params.append(json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict))
                          else value)
        if not sets:
            return False
        params.append(int(message_id))
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE messages SET {', '.join(sets)} WHERE id=?", tuple(params))
            self._conn.commit()
            return (cursor.rowcount or 0) > 0

    def delete_message(self, message_id: int) -> bool:
        """删除一条消息（当前仅用于测试与手动清理）。"""
        with self._lock:
            cursor = self._conn.execute("DELETE FROM messages WHERE id=?", (int(message_id),))
            self._conn.commit()
            return (cursor.rowcount or 0) > 0

    def get_message(self, message_id: int) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM messages WHERE id=?", (int(message_id),))
        return self._row_to_msg(rows[0]) if rows else None

    def find_by_msg_id(self, msg_id: str, bot_id: str = "") -> Optional[Dict[str, Any]]:
        """按**平台消息 ID** 找一条消息（引用时要用它判断消息新旧）。"""
        msg_id = str(msg_id or "").strip()
        if not msg_id:
            return None
        if bot_id:
            rows = self._query(
                "SELECT * FROM messages WHERE msg_id=? AND bot_id=? ORDER BY id DESC LIMIT 1",
                (msg_id, bot_id))
        else:
            rows = self._query(
                "SELECT * FROM messages WHERE msg_id=? ORDER BY id DESC LIMIT 1", (msg_id,))
        return self._row_to_msg(rows[0]) if rows else None

    # ------------------------------------------------------------------ 用户昵称（跨会话共享）
    def set_user_name(self, openid: str, name: str) -> bool:
        """记录某个 openid 的昵称（群里拿到的昵称可以给私聊用）。"""
        openid = (openid or "").strip()
        name = (name or "").strip()[:60]
        if not openid or not name or name == "我":
            return False
        with self._lock:
            self._conn.execute(
                """INSERT INTO meta (key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (f"uname:{openid}", name))
            self._conn.commit()
        return True

    def user_names(self) -> Dict[str, str]:
        rows = self._query("SELECT key, value FROM meta WHERE key LIKE 'uname:%'")
        out = {}
        for row in rows:
            openid = str(row["key"])[len("uname:"):]
            if openid and row["value"]:
                out[openid] = str(row["value"])[:60]
        return out

    # ------------------------------------------------------------------ 别名（手动命名）
    def set_alias(self, openid: str, name: str) -> bool:
        """给 openid 设置/清除手动别名（QQ 不返回昵称时的替代方案）。"""
        openid = (openid or "").strip()
        if not openid:
            return False
        name = (name or "").strip()[:60]
        with self._lock:
            if name:
                self._conn.execute(
                    """INSERT INTO meta (key, value) VALUES (?,?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (f"alias:{openid}", name))
            else:
                self._conn.execute("DELETE FROM meta WHERE key=?", (f"alias:{openid}",))
            self._conn.commit()
        return True

    def aliases(self) -> Dict[str, str]:
        rows = self._query("SELECT key, value FROM meta WHERE key LIKE 'alias:%'")
        out = {}
        for row in rows:
            openid = str(row["key"])[len("alias:"):]
            if openid and row["value"]:
                out[openid] = str(row["value"])[:60]
        return out

    def get_alias(self, openid: str) -> str:
        if not openid:
            return ""
        return self.get_meta(f"alias:{openid}", "")

    def get_raw_event(self, conv_key: str, msg_id: str) -> str:
        rows = self._query(
            "SELECT raw_event FROM messages WHERE conv_key=? AND msg_id=? ORDER BY id DESC LIMIT 1",
            (conv_key, msg_id))
        return rows[0]["raw_event"] if rows else ""

    def name_index(self, limit: int = 1000) -> Dict[str, str]:
        """openid → 昵称（取自**收到的**消息，机器人自己的消息不算）。

        只统计 `direction='in'`：机器人发出去的消息昵称是“我”，
        如果把它算进来，用户在网页上就会被显示成“我”。
        """
        rows = self._query(
            """SELECT openid, username, MAX(id) AS mid FROM messages
               WHERE direction='in' AND openid<>'' AND username<>''
                 AND username<>'我'
               GROUP BY openid ORDER BY mid DESC LIMIT ?""",
            (int(limit),))
        return {row["openid"]: row["username"] for row in rows}

    def clear(self, key: Optional[str] = None, bot_id: str = "") -> int:
        """清空消息：key 为空则清空全部（可按 bot 限定），否则只清该会话。"""
        with self._lock:
            if key:
                cursor = self._conn.execute("DELETE FROM messages WHERE conv_key=?", (key,))
                self._conn.execute("DELETE FROM conversations WHERE conv_key=?", (key,))
            elif bot_id:
                cursor = self._conn.execute("DELETE FROM messages WHERE bot_id=?", (bot_id,))
                self._conn.execute("DELETE FROM conversations WHERE bot_id=?", (bot_id,))
            else:
                cursor = self._conn.execute("DELETE FROM messages")
                self._conn.execute("DELETE FROM conversations")
            self._conn.commit()
            return cursor.rowcount or 0

    def _trim_locked(self):
        """按条数 / 天数 / 会话数裁剪（写锁内调用）。"""
        if self.max_messages:
            self._conn.execute(
                """DELETE FROM messages WHERE id IN (
                       SELECT id FROM messages ORDER BY id DESC LIMIT -1 OFFSET ?)""",
                (self.max_messages,))
        if self.retention_days:
            cutoff = time.time() - self.retention_days * 86400
            self._conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
        if self.max_conversations:
            self._conn.execute(
                """DELETE FROM conversations WHERE conv_key IN (
                       SELECT conv_key FROM conversations ORDER BY last_ts DESC LIMIT -1 OFFSET ?)""",
                (self.max_conversations,))
        self._conn.commit()

    def trim(self):
        with self._lock:
            self._trim_locked()

    def apply_limits(self, max_messages: Optional[int] = None, retention_days: Optional[int] = None,
                     max_conversations: Optional[int] = None, messages_per_page: Optional[int] = None,
                     trim_every: Optional[int] = None):
        """配置变更后立即生效（并顺手裁剪一次）。"""
        if max_messages is not None:
            self.max_messages = int(max_messages or 0)
        if retention_days is not None:
            self.retention_days = int(retention_days or 0)
        if max_conversations is not None:
            self.max_conversations = int(max_conversations or 0)
        if messages_per_page is not None:
            self.messages_per_page = max(20, int(messages_per_page or 200))
        if trim_every is not None:
            self.trim_every = max(10, int(trim_every or 100))
        self.trim()

    @staticmethod
    def _row_to_msg(row: sqlite3.Row) -> Dict[str, Any]:
        def _json(text: str, fallback):
            try:
                value = json.loads(text) if text else fallback
                return value if isinstance(value, type(fallback)) else fallback
            except (ValueError, TypeError):
                return fallback

        return {
            "id": row["id"],
            "bot_id": row["bot_id"],
            "conv_key": row["conv_key"],
            "type": row["type"],
            "direction": row["direction"],
            "group_openid": row["group_openid"],
            "openid": row["openid"],
            "username": row["username"],
            "content": row["content"],
            "image_url": row["image_url"],
            "media_local": row["media_local"],
            "attachments": _json(row["attachments"], []),
            "quote": _json(row["quote"], {}),
            "mentions": _json(row["mentions"], []),
            "reply_to": row["reply_to"],
            "msg_id": row["msg_id"],
            "msg_idx": row["msg_idx"],
            "raw_event": row["raw_event"],
            "ts": row["ts"],
            "time": row["time_text"] or datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ------------------------------------------------------------------ 统计
    def stats_summary(self) -> Dict[str, Any]:
        """今日 / 累计统计（消息、收到、发出、会话数、媒体数）。"""
        midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        week_ago = time.time() - 7 * 86400
        rows = self._query(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS today,
                 SUM(CASE WHEN direction='in' THEN 1 ELSE 0 END) AS received,
                 SUM(CASE WHEN direction='out' THEN 1 ELSE 0 END) AS sent,
                 SUM(CASE WHEN type='group' THEN 1 ELSE 0 END) AS group_msgs,
                 SUM(CASE WHEN type='private' THEN 1 ELSE 0 END) AS private_msgs
               FROM messages""", (midnight,))
        base = dict(rows[0]) if rows else {}
        media_rows = self._query(
            """SELECT COUNT(*) AS total, COALESCE(SUM(size),0) AS bytes,
                      SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS today
               FROM media WHERE state='done'""", (midnight,))
        media = dict(media_rows[0]) if media_rows else {}
        conv_rows = self._query("SELECT COUNT(*) AS n FROM conversations")
        trend_rows = self._query(
            "SELECT ts FROM messages WHERE ts >= ?", (week_ago,))
        buckets: Dict[str, int] = {}
        for row in trend_rows:
            day = time.strftime("%m-%d", time.localtime(row["ts"]))
            buckets[day] = buckets.get(day, 0) + 1
        trend = []
        for offset in range(6, -1, -1):
            day_ts = time.time() - offset * 86400
            day = time.strftime("%m-%d", time.localtime(day_ts))
            trend.append({"day": day, "count": buckets.get(day, 0)})
        return {
            "total": base.get("total") or 0,
            "today": base.get("today") or 0,
            "received": base.get("received") or 0,
            "sent": base.get("sent") or 0,
            "group_messages": base.get("group_msgs") or 0,
            "private_messages": base.get("private_msgs") or 0,
            "conversations": (dict(conv_rows[0]).get("n") if conv_rows else 0) or 0,
            "media_count": media.get("total") or 0,
            "media_bytes": media.get("bytes") or 0,
            "media_today": media.get("today") or 0,
            "trend": trend,
        }

    # ------------------------------------------------------------------ 媒体
    def add_media(self, record: Dict[str, Any]) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO media
                   (bot_id, conv_key, msg_id, source, url, url_hash, local_name, file_name,
                    content_type, size, state, error, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record.get("bot_id", ""), record.get("conv_key", ""), record.get("msg_id", ""),
                 record.get("source", "received"), record.get("url", ""), record.get("url_hash", ""),
                 record.get("local_name", ""), record.get("file_name", ""),
                 record.get("content_type", ""), int(record.get("size") or 0),
                 record.get("state", "done"), record.get("error", ""),
                 float(record.get("created_at") or time.time())))
            self._conn.commit()
            return cursor.lastrowid

    def find_media_by_hash(self, url_hash: str) -> Optional[Dict[str, Any]]:
        if not url_hash:
            return None
        rows = self._query(
            "SELECT * FROM media WHERE url_hash=? AND state='done' ORDER BY id DESC LIMIT 1", (url_hash,))
        return dict(rows[0]) if rows else None

    def list_media(self, limit: int = 200, conv_key: str = "", source: str = "",
                   state: str = "") -> List[Dict[str, Any]]:
        conditions, params = [], []
        if conv_key:
            conditions.append("conv_key=?")
            params.append(conv_key)
        if source:
            conditions.append("source=?")
            params.append(source)
        if state:
            conditions.append("state=?")
            params.append(state)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(int(limit))
        rows = self._query(f"SELECT * FROM media {where} ORDER BY id DESC LIMIT ?", tuple(params))
        return [dict(row) for row in rows]

    def media_stats(self) -> Dict[str, Any]:
        rows = self._query(
            """SELECT state, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes
               FROM media GROUP BY state""")
        return {row["state"]: {"count": row["n"], "bytes": row["bytes"]} for row in rows}

    def purge_media(self, older_than_days: int = 0, state: str = "") -> List[str]:
        """删除媒体记录，返回要一并删除的本地文件名。"""
        conditions, params = [], []
        if state:
            conditions.append("state=?")
            params.append(state)
        if older_than_days > 0:
            conditions.append("created_at < ?")
            params.append(time.time() - older_than_days * 86400)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._query(f"SELECT local_name FROM media {where}", tuple(params))
        names = [row["local_name"] for row in rows if row["local_name"]]
        with self._lock:
            self._conn.execute(f"DELETE FROM media {where}", tuple(params))
            self._conn.commit()
        return names

    # ------------------------------------------------------------------ 群成员 / 禁言
    def upsert_members(self, bot_id: str, group_openid: str, members: List[Dict[str, Any]]):
        now = time.time()
        with self._lock:
            for member in members:
                openid = str(member.get("member_openid") or member.get("openid") or "").strip()
                if not openid:
                    continue
                existing = self._conn.execute(
                    """SELECT joined_at FROM group_members
                       WHERE bot_id=? AND group_openid=? AND member_openid=?""",
                    (bot_id, group_openid, openid)).fetchone()
                joined = existing["joined_at"] if existing else now
                self._conn.execute(
                    """INSERT INTO group_members
                       (bot_id, group_openid, member_openid, username, role, joined_at, updated_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(bot_id, group_openid, member_openid) DO UPDATE SET
                         username=excluded.username, role=excluded.role, updated_at=excluded.updated_at""",
                    (bot_id, group_openid, openid, str(member.get("username") or "")[:200],
                     str(member.get("role") or "member"), joined, now))
            self._conn.commit()

    def list_members(self, group_openid: str, bot_id: str = "") -> List[Dict[str, Any]]:
        if bot_id:
            rows = self._query(
                """SELECT * FROM group_members WHERE group_openid=? AND bot_id=?
                   ORDER BY role DESC, username""", (group_openid, bot_id))
        else:
            rows = self._query(
                "SELECT * FROM group_members WHERE group_openid=? ORDER BY role DESC, username",
                (group_openid,))
        return [dict(row) for row in rows]

    def members_updated_at(self, group_openid: str, bot_id: str = "") -> float:
        if bot_id:
            rows = self._query(
                "SELECT MAX(updated_at) AS ts FROM group_members WHERE group_openid=? AND bot_id=?",
                (group_openid, bot_id))
        else:
            rows = self._query(
                "SELECT MAX(updated_at) AS ts FROM group_members WHERE group_openid=?", (group_openid,))
        return float(rows[0]["ts"] or 0) if rows and rows[0]["ts"] is not None else 0.0

    def delete_members(self, group_openid: str, bot_id: str = ""):
        if bot_id:
            self._execute("DELETE FROM group_members WHERE group_openid=? AND bot_id=?",
                          (group_openid, bot_id))
        else:
            self._execute("DELETE FROM group_members WHERE group_openid=?", (group_openid,))

    def add_muting(self, record: Dict[str, Any]) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO mutings
                   (bot_id, group_openid, member_openid, username, until, minutes, reason,
                    mode, state, created_at, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (record.get("bot_id", ""), record.get("group_openid", ""),
                 record.get("member_openid", ""), record.get("username", ""),
                 float(record.get("until") or 0), int(record.get("minutes") or 0),
                 record.get("reason", ""), record.get("mode", "local"),
                 record.get("state", "active"), float(record.get("created_at") or time.time()),
                 record.get("created_by", "web")))
            self._conn.commit()
            return cursor.lastrowid

    def list_mutings(self, group_openid: str = "", bot_id: str = "",
                     active_only: bool = True) -> List[Dict[str, Any]]:
        conditions, params = [], []
        if group_openid:
            conditions.append("group_openid=?")
            params.append(group_openid)
        if bot_id:
            conditions.append("bot_id=?")
            params.append(bot_id)
        if active_only:
            conditions.append("state='active'")
            conditions.append("(until=0 OR until > ?)")
            params.append(time.time())
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._query(f"SELECT * FROM mutings {where} ORDER BY id DESC", tuple(params))
        return [dict(row) for row in rows]

    def is_muted(self, bot_id: str, group_openid: str, member_openid: str) -> Optional[Dict[str, Any]]:
        """查询某成员当前是否被禁言（本地禁言表）。"""
        if not member_openid:
            return None
        rows = self._query(
            """SELECT * FROM mutings
               WHERE group_openid=? AND member_openid=? AND state='active'
                 AND (bot_id=? OR bot_id='') AND (until=0 OR until > ?)
               ORDER BY id DESC LIMIT 1""",
            (group_openid, member_openid, bot_id, time.time()))
        return dict(rows[0]) if rows else None

    def release_muting(self, mute_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE mutings SET state='released' WHERE id=? AND state='active'", (int(mute_id),))
            self._conn.commit()
            return (cursor.rowcount or 0) > 0

    def release_member_muting(self, bot_id: str, group_openid: str, member_openid: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE mutings SET state='released'
                   WHERE group_openid=? AND member_openid=? AND state='active' AND (bot_id=? OR bot_id='')""",
                (group_openid, member_openid, bot_id))
            self._conn.commit()
            return cursor.rowcount or 0

    def expire_mutings(self) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE mutings SET state='expired' WHERE state='active' AND until>0 AND until<=?",
                (time.time(),))
            self._conn.commit()
            return cursor.rowcount or 0

    # ------------------------------------------------------------------ 机器人状态
    def set_bot_status(self, bot_id: str, online: bool, last_error: str = "",
                       started_at: Optional[float] = None, extra: Optional[Dict[str, Any]] = None):
        with self._lock:
            existing = self._conn.execute(
                "SELECT started_at FROM bot_status WHERE bot_id=?", (bot_id,)).fetchone()
            start = started_at if started_at is not None else (
                existing["started_at"] if existing else time.time())
            self._conn.execute(
                """INSERT INTO bot_status (bot_id, online, last_error, updated_at, started_at, extra)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(bot_id) DO UPDATE SET
                     online=excluded.online, last_error=excluded.last_error,
                     updated_at=excluded.updated_at, started_at=excluded.started_at,
                     extra=excluded.extra""",
                (bot_id, 1 if online else 0, last_error[:500], time.time(), float(start or 0),
                 json.dumps(extra or {}, ensure_ascii=False)))
            self._conn.commit()

    def get_bot_status(self) -> Dict[str, Dict[str, Any]]:
        rows = self._query("SELECT * FROM bot_status")
        out = {}
        for row in rows:
            item = dict(row)
            try:
                item["extra"] = json.loads(item.get("extra") or "{}")
            except ValueError:
                item["extra"] = {}
            out[row["bot_id"]] = item
        return out

    # ------------------------------------------------------------------ 杂项
    def set_meta(self, key: str, value: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
            self._conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        rows = self._query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def vacuum(self):
        with self._lock:
            self._conn.execute("VACUUM")
            self._conn.commit()


class MemoryStore(SQLiteStore):
    """内存存储（storage.enabled=false 时使用）：接口与 SQLiteStore 完全一致。"""

    def __init__(self, *args, **kwargs):
        kwargs["db_path"] = ":memory:"
        kwargs.pop("retention_days", None)
        super().__init__(*args, retention_days=0, **kwargs)
        logger.info("使用内存存储（重启后消息清空）")
