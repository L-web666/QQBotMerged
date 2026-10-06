# -*- coding: utf-8 -*-
"""群聊管理：群列表、群信息、成员列表、禁言、群级配置

关于「禁言」的重要说明
----------------------
QQ 机器人开放平台**并未在公开文档中提供“禁言群成员”接口**（原两个项目里也完全没有相关代码）。
因此本模块提供两条路径，并由配置项 `features.mute_mode` 决定：

- `local`（默认）：**本地禁言**——记录在 `mutings` 表里，机器人侧立刻停止回复/处理该成员的消息，
  到点自动解禁。这是唯一 100% 可靠、无需平台权限的方式。
- `api`：尝试调用平台禁言接口（`POST /v2/groups/{gid}/members/{mid}/mute`）。
  平台未开放时会返回错误，本模块会把真实错误原样回报给页面。
- `both`：先记本地禁言（保证立即生效），同时尝试调用平台接口，把平台结果一并返回。

群级配置（每群独立覆盖全局设置）保存在 `data/group_settings.json`。
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 官方「查询群禁言状态」接口限 30 QPM，缓存一小段时间，避免页面反复刷新把额度打满
MUTE_STATE_TTL_SECONDS = 20
# 群名多久重新拉一次（页面上的群名会自动更新，不用手动点"刷新群名"）
NAME_TTL_SECONDS = 30 * 60
# 群名拉取失败后的退避时间（避免打不通的群每 15 秒重试一次，把接口额度耗光）
NAME_FAIL_BACKOFF_SECONDS = 10 * 60
# 群信息接口频率有限，批量刷群名时每个之间稍微等一等
NAME_REFRESH_PACE_SECONDS = 1.2


class GroupManager:
    """群列表 / 成员 / 禁言 / 群配置。"""

    def __init__(self, store, config, runtime=None, settings_path: str = "data/group_settings.json",
                 logger_obj: logging.Logger = None):
        self.store = store
        self.config = config
        self.runtime = runtime
        self.settings_path = settings_path
        self.log = logger_obj or logger
        self._lock = threading.RLock()
        self._settings: Dict[str, Dict[str, Any]] = {}
        self._member_refreshing: set = set()
        self._name_refreshing: set = set()
        self._mute_state_cache: Dict[str, Dict[str, Any]] = {}
        self._name_updated: Dict[str, float] = {}
        self._name_failed: Dict[str, float] = {}
        self._load_settings()

    # ================================================================== 群列表
    def _groups(self, bot_id: str = "") -> List[Dict[str, Any]]:
        """从会话记录里汇总群。

        `bot_id` 非空时**只统计该机器人看到过的群**（并按该机器人的 mute 记录计数）。
        这一点很重要：两个机器人可能在不同群里，混在一起会让
        "用哪个机器人的身份去禁言/拉成员"变得不确定，进而请求失败。
        """
        groups: Dict[str, Dict[str, Any]] = {}
        for conv in self.store.conversations(limit=2000, bot_id=bot_id) or []:
            if conv.get("type") != "group":
                continue
            openid = conv.get("group_openid") or ""
            if not openid:
                continue
            item = groups.setdefault(openid, {
                "group_openid": openid,
                "name": "",
                "bots": [],
                "message_count": 0,
                "last_ts": 0,
                "last_time": "",
                "last_content": "",
                "conv_keys": {},
            })
            item["message_count"] += int(conv.get("message_count") or 0)
            if float(conv.get("last_ts") or 0) > item["last_ts"]:
                item["last_ts"] = float(conv.get("last_ts") or 0)
                item["last_time"] = conv.get("last_time") or ""
                item["last_content"] = conv.get("last_content") or ""
                if conv.get("username"):
                    item["name"] = conv["username"]
            conv_bot = conv.get("bot_id") or ""
            if conv_bot and conv_bot not in item["bots"]:
                item["bots"].append(conv_bot)
            if conv_bot:
                item["conv_keys"][conv_bot] = conv.get("conv_key") or ""

        group_names = self.group_names()
        now = time.time()
        for openid, item in groups.items():
            # 群名以 QQ 为准（group_info 缓存）。会话表里的 username 只在
            # 完全没有群名时兜底 —— 历史版本会把"群里最后发言者"的昵称写进去，
            # 直接用会让群名跟着最后发言的人乱变。
            cached_name = (group_names.get(openid) or "").strip()
            fallback_name = (item.get("name") or "").strip()
            item["name"] = cached_name or fallback_name
            item["name_from"] = "platform" if cached_name else ("history" if fallback_name else "")
            item["muted_count"] = len(self.store.list_mutings(
                group_openid=openid, bot_id=bot_id, active_only=True))
            item["settings"] = dict(self._settings.get(openid) or {})
            item["settings_active"] = bool(item["settings"])
            # 没有指定机器人时，标出"这个群有多个机器人看到过"，提醒用户先选机器人
            item["multi_bot"] = len(item["bots"]) > 1
            # 群名的新鲜度：页面据此显示"群名更新于 …"，并自动触发后台刷新
            updated = float(self._name_updated.get(openid) or 0)
            item["name_updated"] = updated
            item["name_updated_text"] = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated))
                                         if updated else "")
            item["name_stale"] = bool(
                not item["name"] or not updated or (now - updated) > NAME_TTL_SECONDS)
            item["name_refreshing"] = openid in self._name_refreshing
            failed_at = float(self._name_failed.get(openid) or 0)
            item["name_error"] = bool(failed_at and (now - failed_at) < NAME_FAIL_BACKOFF_SECONDS)
        return sorted(groups.values(), key=lambda entry: entry["last_ts"], reverse=True)

    def groups(self, bot_id: str = "") -> List[Dict[str, Any]]:
        return self._groups(bot_id)

    def group_bot_ids(self, group_openid: str) -> List[str]:
        """哪些机器人看到过这个群（用于在详情页显示，防止用错机器人）。"""
        out: List[str] = []
        for conv in self.store.conversations(limit=2000) or []:
            if conv.get("type") != "group" or conv.get("group_openid") != group_openid:
                continue
            bot_id = conv.get("bot_id") or ""
            if bot_id and bot_id not in out:
                out.append(bot_id)
        return out

    def group_names(self) -> Dict[str, str]:
        """群名缓存（`data/group_names.json`，以 QQ 返回的群名为准）。"""
        with self._lock:
            return dict(self._names)

    def resync_conversation_names(self) -> int:
        """把缓存的群名写回会话表（启动时用）。

        只"补名字"，**不会**因为猜测去清掉任何名字——清名字属于手动维护操作
        （见 `storage.repair_group_conversation_names(force=True)`）。
        """
        names = self.group_names()
        stats = {"named": 0, "cleared": 0}
        try:
            # 即使缓存为空也要跑一遍：force=False 时它只做安全修正
            stats = self.store.repair_group_conversation_names(force=False, names=names)
        except Exception as exc:
            self.log.debug("回写群名到会话表失败: %s", exc)
        fixed = int(stats.get("named") or 0)
        if fixed:
            self.log.info("已把 %d 个群会话的名字同步成群名", fixed)
        return fixed

    def repair_suspect_names(self) -> Dict[str, Any]:
        """手动维护：清理"群名被写成最后发言者昵称"的历史坏数据。

        只按群名缓存修正 + 启发式清空，返回处理结果供页面提示。
        """
        names = self.group_names()
        before = self.store.group_name_suspects()
        try:
            stats = self.store.repair_group_conversation_names(force=True, names=names)
        except Exception as exc:
            return {"success": False, "message": f"修正失败：{exc}"}
        after = self.store.group_name_suspects()
        self.log.info("手动修正群会话名：按群名补 %d 个、清掉可疑的 %d 个（剩余可疑 %d）",
                      stats.get("named", 0), stats.get("cleared", 0), after)
        return {"success": True, "named": stats.get("named", 0),
                "cleared": stats.get("cleared", 0), "before": before, "after": after,
                "message": f"已按群名修正 {stats.get('named', 0)} 个；"
                           f"另有 {stats.get('cleared', 0)} 个「名字=最后发言者昵称」的已清空，"
                           f"剩余可疑 {after} 个（会自动重新获取群名）"}

    def _load_settings(self):
        try:
            if os.path.isfile(self.settings_path):
                with open(self.settings_path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle) or {}
                if isinstance(raw, dict):
                    self._settings = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError) as exc:
            self.log.warning("读取群配置失败: %s", exc)
        self._names = {}
        try:
            names_path = os.path.join(os.path.dirname(self.settings_path) or ".", "group_names.json")
            if os.path.isfile(names_path):
                with open(names_path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle) or {}
                if isinstance(raw, dict):
                    self._names = {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError):
            pass
        # 群名最后更新时间（自动刷新用）：存在同目录的 group_names_meta.json
        try:
            meta_path = self._names_meta_path()
            if os.path.isfile(meta_path):
                with open(meta_path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle) or {}
                if isinstance(raw, dict):
                    self._name_updated = {str(k): float(v or 0) for k, v in raw.items()}
        except (OSError, ValueError):
            self._name_updated = {}

    def _names_meta_path(self) -> str:
        return os.path.join(os.path.dirname(self.settings_path) or ".", "group_names_meta.json")

    def _save_name_meta(self):
        try:
            meta_path = self._names_meta_path()
            tmp = meta_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._name_updated, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, meta_path)
        except OSError:
            pass

    def _save_settings(self):
        try:
            directory = os.path.dirname(self.settings_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self.settings_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._settings, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self.settings_path)
        except OSError as exc:
            self.log.warning("保存群配置失败: %s", exc)

    def _save_names(self):
        try:
            names_path = os.path.join(os.path.dirname(self.settings_path) or ".", "group_names.json")
            tmp = names_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._names, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, names_path)
        except OSError:
            pass

    # ================================================================== 群信息
    def name_updated_at(self, group_openid: str) -> float:
        return float(self._name_updated.get(group_openid) or 0)

    def _fetch_group_name(self, group_openid: str, bot_id: str = "") -> Dict[str, Any]:
        """真正去平台拉一次群名并写入缓存/会话表（不做限速，供单个与批量刷新共用）。"""
        with self._lock:
            if group_openid in self._name_refreshing:
                return {"success": False, "message": "该群的群名正在刷新中"}
            self._name_refreshing.add(group_openid)
        try:
            client = self._client(bot_id)
            if client is None and bot_id:
                # 记录里"看到过这个群"的机器人已被删除/禁用：换一个可用的
                # （群名是全局信息，任何还在这群里的机器人都能查）
                client = self._client("")
            if client is None:
                return {"success": False,
                        "message": "没有可用的机器人（请先在设置里配置并启用机器人）"}
            try:
                info = client.group_info(group_openid)
            except Exception as exc:
                self._name_failed[group_openid] = time.time()
                return {"success": False, "message": f"获取群信息失败：{exc}"}
            name = self._pick_name(info)
            if not name:
                self._name_failed[group_openid] = time.time()
                return {"success": False, "message": "接口有返回，但识别不出群名称",
                        "raw": json.dumps(info, ensure_ascii=False)[:500]}
            with self._lock:
                self._names[group_openid] = name
                self._name_updated[group_openid] = time.time()
                self._name_failed.pop(group_openid, None)
                self._save_names()
                self._save_name_meta()
            # 写入会话名（所有机器人对应的会话都更新，聊天列表立即显示）
            try:
                for conv in self.store.conversations(limit=2000):
                    if conv.get("group_openid") == group_openid:
                        self.store.rename_conversation(conv["conv_key"], name)
            except Exception as exc:
                self.log.debug("更新会话群名失败: %s", exc)
            return {"success": True, "name": name, "bot_id": bot_id,
                    "message": f"已获取群名：{name}"}
        finally:
            with self._lock:
                self._name_refreshing.discard(group_openid)

    def refresh_group_name(self, group_openid: str, bot_id: str = "") -> Dict[str, Any]:
        """向 QQ 拉取群名称并缓存（记录同时写入会话表，聊天列表立即显示）。"""
        return self._fetch_group_name(group_openid, bot_id)

    def stale_name_groups(self, bot_id: str = "", ttl: int = NAME_TTL_SECONDS,
                          ignore_backoff: bool = False) -> List[Dict[str, Any]]:
        """哪些群需要刷新群名：还没有名字，或名字已经超过 ttl 没更新过。

        刚失败过的群在退避期内会被跳过（除非 ignore_backoff，例如用户手动点刷新）。
        """
        now = time.time()
        pending: List[Dict[str, Any]] = []
        for group in self._groups(bot_id):
            openid = group.get("group_openid") or ""
            if not openid or openid in self._name_refreshing:
                continue
            if not ignore_backoff and group.get("name_error"):
                continue
            name = group.get("name") or ""
            updated = self.name_updated_at(openid)
            if name and updated and (now - updated) < max(0, int(ttl)):
                continue
            pending.append({
                "group_openid": openid,
                "bot_id": (group.get("bots") or [""])[0],
                "name": name,
                "reason": "missing" if not name else "stale",
            })
        return pending

    NAME_REFRESH_MIN_GAP = 10

    def trigger_name_refresh(self, bot_id: str = "", limit: int = 4) -> bool:
        """页面打开群管理时在后台补刷一次群名（节流，不阻塞接口）。"""
        now = time.time()
        with self._lock:
            if now - float(getattr(self, "_last_name_trigger", 0) or 0) < self.NAME_REFRESH_MIN_GAP:
                return False
            self._last_name_trigger = now

        def run():
            try:
                self.refresh_names(bot_id, limit=limit)
            except Exception as exc:
                self.log.debug("后台刷新群名失败：%s", exc)

        threading.Thread(target=run, name="group-names-once", daemon=True).start()
        return True

    def refresh_names(self, bot_id: str = "", force: bool = False, limit: int = 6,
                      pace: float = NAME_REFRESH_PACE_SECONDS) -> Dict[str, Any]:
        """批量自动刷新群名。

        只处理"没名字"或"超过 NAME_TTL_SECONDS 没更新"的群，并且每次最多 limit 个，
        以免把平台的群信息接口刷爆（后台线程会一遍遍来，最终都会刷新到）。
        """
        pending = self.stale_name_groups(bot_id, ttl=0 if force else NAME_TTL_SECONDS,
                                         ignore_backoff=force)
        targets = pending[:max(1, int(limit))]
        refreshed, failed = [], []
        for index, target in enumerate(targets):
            if index:
                time.sleep(max(0.0, pace))
            result = self._fetch_group_name(target["group_openid"], target.get("bot_id") or "")
            if result.get("success"):
                refreshed.append({"group_openid": target["group_openid"],
                                  "name": result.get("name") or ""})
            else:
                failed.append({"group_openid": target["group_openid"],
                               "message": result.get("message") or "失败"})
        remaining = max(0, len(pending) - len(targets))
        if refreshed:
            message = f"已刷新 {len(refreshed)} 个群名" + (f"，还有 {remaining} 个待刷新" if remaining
                                                          else "")
        elif failed:
            message = failed[0]["message"]
        else:
            message = "所有群名都是最新的"
        return {"success": bool(refreshed), "refreshed": refreshed, "failed": failed,
                "pending": remaining, "checked": len(pending), "message": message}

    @staticmethod
    def _pick_name(info: Dict[str, Any]) -> str:
        if not isinstance(info, dict):
            return ""
        for key in ("group_name", "groupName", "name", "group_nick", "nickname"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        data = info.get("data")
        if isinstance(data, dict):
            return GroupManager._pick_name(data)
        return ""

    # ================================================================== 成员
    def members(self, group_openid: str, bot_id: str = "", refresh: bool = False) -> Dict[str, Any]:
        """群成员列表（优先读缓存；缓存过期或 refresh=True 时尝试从平台刷新）。"""
        ttl_hours = self.config.int_of("storage", "group_members_refresh_hours", default=6)
        cached = self.store.list_members(group_openid, bot_id)
        updated_at = self.store.members_updated_at(group_openid, bot_id)
        stale = (time.time() - updated_at) > max(1, ttl_hours) * 3600
        result: Dict[str, Any] = {
            "members": cached,
            "source": "cache" if cached else "none",
            "updated_at": updated_at,
            "updated_text": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated_at))
                             if updated_at else ""),
            "stale": stale,
            "api_error": "",
        }
        if refresh or (stale and not cached):
            refreshed = self.refresh_members(group_openid, bot_id)
            if refreshed.get("success"):
                result["members"] = self.store.list_members(group_openid, bot_id)
                result["source"] = "api"
                result["updated_at"] = self.store.members_updated_at(group_openid, bot_id)
                result["updated_text"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(result["updated_at"] or time.time()))
                result["stale"] = False
            else:
                result["api_error"] = refreshed.get("message", "")
        # 官方「查询群禁言状态」：全员禁言模式 + QQ 群里真实被禁言的成员
        platform = self.platform_mute_state(group_openid, bot_id, refresh=refresh)
        result["platform"] = platform
        result["platform_mutings"] = platform.get("members") or []
        result["global_mute"] = {
            "ok": bool(platform.get("ok")),
            "mode": platform.get("mode") or "unknown",
            "text": platform.get("text") or "",
            "active_now": bool(platform.get("active_now")),
            "rules": (platform.get("schedule_rules") or []) + (platform.get("recurring_rules") or []),
            "error": platform.get("error") or "",
            "checked_at": platform.get("checked_at") or 0,
        }
        # 合并本地已知成员（从历史消息里补昵称）+ 官方禁言名单里的成员
        result["members"] = self._merge_known_members(
            group_openid, bot_id, result["members"], result["platform_mutings"])
        # 本地禁言记录同样按机器人区分，否则 bot2 的页面会看到 bot1 禁言的人
        result["mutings"] = self.mutings(group_openid, bot_id)
        result["mute_mode"] = self.mute_mode()
        return result

    def _merge_known_members(self, group_openid: str, bot_id: str,
                             members: List[Dict[str, Any]],
                             platform_members: Optional[List[Dict[str, Any]]] = None
                             ) -> List[Dict[str, Any]]:
        """把历史消息里出现过的群成员补进列表（平台不给成员列表时也有对象可管理）。"""
        known: Dict[str, Dict[str, Any]] = {row.get("member_openid"): dict(row) for row in members}
        platform_map: Dict[str, Dict[str, Any]] = {}
        for item in (platform_members or []):
            openid = str(item.get("member_openid") or "")
            if not openid:
                continue
            platform_map[openid] = item
            entry = known.setdefault(openid, {
                "member_openid": openid, "group_openid": group_openid, "username": "",
                "role": "member", "bot_id": bot_id, "source": "platform",
            })
            # 官方禁言名单带昵称，正好补上"从历史消息里识别不到名字"的成员
            if item.get("username") and not entry.get("username"):
                entry["username"] = item["username"]
        try:
            # 只从**这个机器人**的会话里补成员：另一个机器人看到的群/成员不算数
            for conv in self.store.conversations(limit=2000, bot_id=bot_id):
                if conv.get("group_openid") != group_openid:
                    continue
                key = conv.get("conv_key")
                if not key:
                    continue
                for msg in self.store.get_conversation(key, limit=200):
                    if msg.get("direction") != "in":
                        continue
                    openid = msg.get("openid") or ""
                    if not openid:
                        continue
                    entry = known.setdefault(openid, {
                        "member_openid": openid, "group_openid": group_openid,
                        "username": "", "role": "member", "bot_id": bot_id, "source": "history",
                    })
                    if msg.get("username"):
                        entry["username"] = msg["username"]
                    entry["message_count"] = int(entry.get("message_count") or 0) + 1
                    entry["last_ts"] = max(float(entry.get("last_ts") or 0), float(msg.get("ts") or 0))
        except Exception as exc:
            self.log.debug("补充历史成员失败: %s", exc)
        out = list(known.values())
        for item in out:
            item.setdefault("source", "cache")
            item["muted"] = self.store.is_muted(bot_id, group_openid, item.get("member_openid") or "")
            official = platform_map.get(item.get("member_openid") or "")
            item["platform_muted"] = bool(official)
            item["platform_expire_text"] = (official or {}).get("expire_text") or ""
            item["platform_expire_at"] = (official or {}).get("mute_expire_at") or ""
            item["platform_remaining_seconds"] = (official or {}).get("remaining_seconds") or 0
            item["platform_remaining_text"] = (
                _human_remaining(item["platform_remaining_seconds"])
                if item["platform_muted"] and not (official or {}).get("permanent") else
                ("永久" if item["platform_muted"] else ""))
        out.sort(key=lambda entry: (-(entry.get("message_count") or 0), entry.get("username") or ""))
        return out

    def refresh_members(self, group_openid: str, bot_id: str = "") -> Dict[str, Any]:
        """从平台拉取群成员列表（平台未开放该能力时返回失败说明）。"""
        key = f"{bot_id}:{group_openid}"
        with self._lock:
            if key in self._member_refreshing:
                return {"success": False, "message": "该群的成员列表正在刷新中"}
            self._member_refreshing.add(key)
        try:
            client = self._client(bot_id)
            if client is None:
                return {"success": False, "message": "没有可用的机器人"}
            try:
                raw_members = client.group_members(group_openid)
            except Exception as exc:
                return {"success": False,
                        "message": f"平台拒绝或未开放群成员接口：{exc}（可继续使用历史消息里识别到的成员）"}
            members = []
            for item in raw_members:
                if not isinstance(item, dict):
                    continue
                openid = item.get("member_openid") or item.get("openid") or item.get("user_openid") or ""
                if not openid:
                    continue
                members.append({
                    "member_openid": openid,
                    "username": item.get("username") or item.get("nickname") or "",
                    "role": item.get("role") or ("owner" if item.get("is_owner") else "member"),
                })
            if members:
                self.store.upsert_members(bot_id, group_openid, members)
            return {"success": True, "count": len(members),
                    "message": f"已刷新 {len(members)} 位群成员"}
        finally:
            with self._lock:
                self._member_refreshing.discard(key)

    # ================================================================== 禁言
    def platform_mute_state(self, group_openid: str, bot_id: str = "",
                            refresh: bool = False) -> Dict[str, Any]:
        """查询 QQ 群里**真实**的禁言状态（官方 GET restrict_chat_setting）。

        返回：全员禁言模式（none / always / schedule + 规则）、当前处于禁言中的成员列表
        （含昵称与到期时间）。结果带短缓存（官方接口 30 QPM），需要机器人是群管理员，
        否则把平台的原始错误原样带回来给页面显示。
        """
        key = f"{bot_id}:{group_openid}"
        now = time.time()
        cached = self._mute_state_cache.get(key)
        if cached and not refresh and (now - float(cached.get("_cached_at") or 0)) < MUTE_STATE_TTL_SECONDS:
            return cached
        client = self._client(bot_id)
        if client is None and bot_id:
            client = self._client("")
        if client is None:
            state: Dict[str, Any] = {
                "ok": False, "mode": "unknown", "text": "无法查询（没有可用的机器人）",
                "members": [], "schedule_rules": [], "recurring_rules": [],
                "error": "没有可用的机器人（请先在设置里配置并启用机器人）",
            }
        else:
            try:
                state = dict(client.list_muted_members(group_openid) or {})
                state.update({"ok": True, "error": ""})
                # 官方禁言名单带昵称：顺手记下来，私聊/群成员显示都能用
                for item in state.get("members") or []:
                    name = str(item.get("username") or "").strip()
                    openid = str(item.get("member_openid") or "")
                    if name and openid:
                        try:
                            self.store.set_user_name(openid, name)
                        except Exception:
                            pass
            except Exception as exc:
                state = {
                    "ok": False, "mode": "unknown", "members": [],
                    "schedule_rules": [], "recurring_rules": [],
                    "text": "查询失败",
                    "error": f"平台拒绝或未开放群禁言查询接口：{exc}"
                             "（该接口要求机器人是群管理员）",
                }
        state["_cached_at"] = now
        state["checked_at"] = now
        self._mute_state_cache[key] = state
        return state

    def invalidate_mute_state(self, group_openid: str = "", bot_id: str = ""):
        """禁言/解禁之后立刻让官方状态缓存失效，页面刷新就能看到真实结果。"""
        with self._lock:
            if group_openid:
                for key in [k for k in self._mute_state_cache
                            if k.endswith(":" + group_openid)
                            and (not bot_id or k.startswith(bot_id + ":"))]:
                    self._mute_state_cache.pop(key, None)
            else:
                self._mute_state_cache.clear()

    def mute_mode(self, mode: str = "") -> str:
        """禁言方式：由「群管理」页在操作时选择，默认本地禁言。

        - `local`：只做本地禁言（机器人不再回复该成员），不需要任何平台权限；
        - `api`  ：只调用官方接口 `POST /v2/groups/{gid}/restrict_chat_setting`
                   （要求机器人是群管理员，最长 30 天，只能操作普通成员）；
        - `both` ：先本地禁言保证立即生效，同时调用官方接口做真实禁言。
        页面传什么就用什么；没传则用本地（不读全局配置，避免"设置了却在群里找不到"）。
        """
        mode = str(mode or "").strip().lower()
        return mode if mode in ("local", "api", "both") else "local"

    def mute(self, group_openid: str, member_openid: str, minutes: int = 0, reason: str = "",
             bot_id: str = "", username: str = "", mode: str = "") -> Dict[str, Any]:
        """禁言一个群成员。返回 {success, mode, local, api, message...}。"""
        if not group_openid or not member_openid:
            return {"success": False, "message": "缺少群 openid 或成员 openid"}
        mode = self.mute_mode(mode)
        if minutes is None or int(minutes) < 0:
            minutes = 10
        minutes = int(minutes)
        # 官方上限 30 天
        if minutes > 30 * 24 * 60:
            minutes = 30 * 24 * 60
        until = time.time() + minutes * 60 if minutes else 0

        result: Dict[str, Any] = {"success": False, "mode": mode, "local": None, "api": None,
                                  "bot_role": ""}
        # 0) 官方模式先探身份（判断能否真实禁言，并把结论回给页面）
        if mode in ("api", "both"):
            client = self._client(bot_id)
            if client is None:
                result["api"] = {"success": False, "message": "没有可用的机器人"}
            else:
                try:
                    state = client.bot_state(group_openid)
                    result["bot_role"] = str((state or {}).get("member_role") or "")
                    if result["bot_role"] == "member":
                        result["api"] = {
                            "success": False,
                            "message": "机器人在该群不是管理员，官方禁言接口会被拒绝",
                            "hint": "请让群主把机器人设为管理员；在此之前可用「本地禁言」阻止机器人回复该成员。",
                        }
                    else:
                        client.mute_member(group_openid, member_openid, minutes * 60 if minutes else 0,
                                           op="add" if until else "add")
                        expire_text = ("30 天" if minutes >= 30 * 24 * 60
                                       else (f"{minutes} 分钟" if minutes else "平台默认时长"))
                        result["api"] = {"success": True,
                                         "message": f"官方禁言已生效（{expire_text}）"}
                        result["success"] = True
                except Exception as exc:
                    result["api"] = {
                        "success": False,
                        "message": f"官方禁言接口调用失败：{exc}",
                        "hint": "常见原因：机器人非群管理员、群成员管理能力未获平台白名单（11253）、"
                                "或对群主/管理员执行了禁言。",
                    }

        # 1) 本地禁言（机器人侧立即生效，不依赖平台权限）
        if mode in ("local", "both"):
            mute_id = self.store.add_muting({
                "bot_id": bot_id, "group_openid": group_openid, "member_openid": member_openid,
                "username": username, "until": until, "minutes": minutes, "reason": reason,
                "mode": "local", "state": "active", "created_at": time.time(), "created_by": "web",
            })
            result["local"] = {"mute_id": mute_id, "until": until, "minutes": minutes}
            result["success"] = True
            self.log.info("本地禁言：群=%s 成员=%s 时长=%s 分钟", group_openid, member_openid,
                          minutes or "平台默认")

        # 2) 汇总提示
        if mode == "local":
            result["message"] = (f"已本地禁言 {minutes} 分钟（机器人不再回复该成员）"
                                 if minutes else "已本地禁言（机器人不再回复该成员）")
        elif mode == "api":
            api_result = result.get("api") or {}
            result["message"] = api_result.get("message", "")
        else:
            api_result = result.get("api") or {}
            if api_result.get("success"):
                result["message"] = "已本地禁言，且官方禁言已生效（该成员在 QQ 群里无法发言）"
            else:
                result["message"] = ("已本地禁言（机器人不再回复该成员）；"
                                     "官方禁言未生效：" + str(api_result.get("message") or ""))
        # 官方状态变了，缓存立刻作废，页面刷新就能看到真实名单
        self.invalidate_mute_state(group_openid, bot_id)
        return result

    def unmute(self, group_openid: str, member_openid: str, bot_id: str = "",
               mode: str = "") -> Dict[str, Any]:
        """解除禁言。

        **默认只做本地解禁**（mode=local）：把本地禁言记录释放掉，机器人立刻恢复响应该成员。
        只有明确选择 official/both 时才会调用官方接口。
        （之前的实现无论什么模式都去调官方接口，导致"本地解禁一直失败"——已修正。）
        """
        mode = self.mute_mode(mode)
        released = self.store.release_member_muting(bot_id, group_openid, member_openid)
        result: Dict[str, Any] = {
            "success": bool(released),
            "mode": mode,
            "released": released,
            "message": (f"已解除本地禁言（{released} 条记录），机器人会重新响应该成员" if released
                        else "该成员当前没有本地禁言记录"),
        }
        if mode in ("api", "both"):
            client = self._client(bot_id)
            if client is None:
                result["api"] = {"success": False, "message": "没有可用的机器人"}
            else:
                try:
                    client.unmute_member(group_openid, member_openid)
                    result["api"] = {"success": True, "message": "官方解禁接口调用成功"}
                    result["success"] = True
                    if not released:
                        result["message"] = "官方解禁接口调用成功"
                except Exception as exc:
                    result["api"] = {"success": False,
                                     "message": f"官方解禁接口调用失败：{exc}"}
                    if not released:
                        result["message"] = (f"本地没有该成员的禁言记录；官方解禁也未成功：{exc}")
        self.invalidate_mute_state(group_openid, bot_id)
        return result

    def is_muted(self, bot_id: str, group_openid: str, member_openid: str) -> Optional[Dict[str, Any]]:
        self.store.expire_mutings()
        return self.store.is_muted(bot_id, group_openid, member_openid)

    def release_by_id(self, mute_id: int) -> bool:
        return self.store.release_muting(int(mute_id))

    def mutings(self, group_openid: str = "", bot_id: str = "") -> List[Dict[str, Any]]:
        """当前生效的禁言名单。

        `bot_id` 非空时只返回该机器人的禁言记录：两个机器人各禁各的，
        页面上不会出现「另一个机器人禁言的人」。
        """
        self.store.expire_mutings()
        items = self.store.list_mutings(group_openid=group_openid, bot_id=bot_id, active_only=True)
        now = time.time()
        for item in items:
            until = float(item.get("until") or 0)
            item["permanent"] = until <= 0
            item["remaining_seconds"] = max(0, int(until - now)) if until else 0
            item["remaining_text"] = ("永久" if not until else _human_remaining(item["remaining_seconds"]))
        return items

    # ================================================================== 群配置
    GROUP_SETTING_FIELDS = ("require_mention", "auto_reply", "keywords_enabled",
                            "rate_limit_seconds", "save_media", "max_history")

    def get_settings(self, group_openid: str) -> Dict[str, Any]:
        stored = dict(self._settings.get(group_openid) or {})
        effective = {
            "require_mention": self.config.bool_of("reply", "require_mention", default=True),
            "auto_reply": self.config.bool_of("features", "auto_reply_in_group", default=True),
            "keywords_enabled": self.config.bool_of("filters", "keywords_enabled", default=True),
            "rate_limit_seconds": self.config.float_of("filters", "rate_limit_seconds", default=3),
            "save_media": self.config.bool_of("features", "save_received_media", default=True),
            "max_history": self.config.int_of("reply", "max_history", default=20),
        }
        effective.update({k: v for k, v in stored.items() if k in self.GROUP_SETTING_FIELDS})
        return {"stored": stored, "effective": effective}

    def set_settings(self, group_openid: str, settings: Dict[str, Any]) -> Dict[str, Any]:
        clean: Dict[str, Any] = {}
        for key in self.GROUP_SETTING_FIELDS:
            if key not in (settings or {}):
                continue
            value = settings[key]
            if key in ("require_mention", "auto_reply", "keywords_enabled", "save_media"):
                clean[key] = bool(value)
            elif key == "rate_limit_seconds":
                try:
                    clean[key] = max(0.5, float(value))
                except (TypeError, ValueError):
                    continue
            elif key == "max_history":
                try:
                    clean[key] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
        with self._lock:
            if clean:
                self._settings[group_openid] = clean
            else:
                self._settings.pop(group_openid, None)
            self._save_settings()
        return {"success": True, "settings": self.get_settings(group_openid)}

    def clear_settings(self, group_openid: str) -> bool:
        with self._lock:
            existed = self._settings.pop(group_openid, None) is not None
            self._save_settings()
        return existed

    def effective(self, group_openid: str, key: str, default=None):
        """运行时读取某群的生效配置（群配置优先，其次全局）。"""
        settings = self._settings.get(group_openid) or {}
        if key in settings:
            return settings[key]
        return default

    # ================================================================== 内部
    def _client(self, bot_id: str = ""):
        if self.runtime is None:
            return None
        return self.runtime.get_client(bot_id)


def _human_remaining(seconds: int) -> str:
    if seconds <= 0:
        return "即将到期"
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    return f"{seconds // 3600} 小时{(seconds % 3600) // 60} 分钟"
