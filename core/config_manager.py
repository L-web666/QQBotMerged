# -*- coding: utf-8 -*-
"""配置管理器（合并版）

职责：
- `config.json` 首次运行自动生成，缺失项自动补全（相当于自带迁移）；
- 读写、按点号路径 get/set；
- 环境变量覆盖（`QQBOT_WEB_PORT=9000` 之类），密钥不必写进文件；
- 密钥打码（网页只看到 `********`，留空提交=不修改）；
- 生成中文配置说明文件。

与原两个项目的关系：合并了 `API_qqbot/core/config_manager.py`（ConfigManager + 说明文档）
与 `app/config_manager.py`（Config 封装 + field_meta + 环境变量覆盖）的职责。
"""

import copy
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from core import config_schema as schema
from core import paths

logger = logging.getLogger(__name__)

ENV_PREFIX = "QQBOT_"


class ConfigError(Exception):
    """配置读写错误。"""


def _deep_merge_defaults(defaults: Any, data: Any) -> Tuple[Any, List[str]]:
    """把 defaults 中缺失的键补进 data，返回 (合并结果, 新增键路径列表)。"""
    added: List[str] = []
    if not isinstance(data, dict):
        return copy.deepcopy(defaults), []
    out = copy.deepcopy(data)
    for key, default_value in (defaults or {}).items():
        if key not in out:
            out[key] = copy.deepcopy(default_value)
            added.append(key)
        elif isinstance(default_value, dict) and isinstance(out.get(key), dict):
            # ⚠️ 必须把递归结果写回 out[key]：以前这里把返回值丢掉了，
            # 于是"分组的子项"缺默认值时只会记进 added（日志里说补全了），
            # 配置里其实一直是缺的 —— 新增的嵌套配置项在老配置上永远不生效。
            merged_child, sub_added = _deep_merge_defaults(default_value, out[key])
            out[key] = merged_child
            added.extend(f"{key}.{sub_added_item}" for sub_added_item in sub_added)
    return out, added


def _default_for(path: str) -> Any:
    node: Any = schema.DEFAULT_CONFIG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _all_paths(node: Any = None, prefix: str = "") -> List[str]:
    """列出配置树里所有"叶子"路径（用于环境变量解析）。"""
    node = schema.DEFAULT_CONFIG if node is None else node
    out: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).startswith("_") or not isinstance(key, str):
                continue
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                out.extend(_all_paths(value, path))
            else:
                out.append(path)
    return out


_PATH_CACHE: Dict[str, List[str]] = {}


def known_paths() -> List[str]:
    if "paths" not in _PATH_CACHE:
        _PATH_CACHE["paths"] = _all_paths()
    return _PATH_CACHE["paths"]


def resolve_env_path(env_key: str) -> Optional[str]:
    """把环境变量名解析成配置路径。猜不出来返回 None。

    以前这里简单粗暴地把 **所有** 下划线都换成点号：

        QQBOT_AI_API_KEY → ai_api_key → ai.api.key   ← 错，`ai.api_key` 永远匹配不到

    于是 `ai.api_key`、`cloud_sync.*`、`alert_owner_openid` 这些**键名自带下划线**的
    配置根本没法用环境变量覆盖，而且不会留下任何日志。

    现在按优先级依次尝试（都能正确处理键名里的下划线）：
      1. `__` 当层级分隔符：`QQBOT_AI__API_KEY` → `ai.api_key`
      2. 与已知路径逐段匹配（长键优先）：`QQBOT_AI_API_KEY` → `ai.api_key`、
         `QQBOT_CLOUD_SYNC_ENABLED` → `cloud_sync.enabled`
      3. 老写法：所有下划线都当分隔符（`QQBOT_WEB_PORT` → `web.port`）
    """
    raw = env_key[len(ENV_PREFIX):].lower()
    if not raw:
        return None
    paths = known_paths()

    # 1) 双下划线是显式层级分隔符，单下划线保留（键名里的下划线不受影响）
    if "__" in raw:
        candidate = raw.replace("__", ".")
        if candidate in paths:
            return candidate
        # 允许"层级写对、但某一段键名含下划线"的组合：用已知路径做前缀匹配
        matched = _match_known(raw.replace("__", "."), paths, keep_underscore=True)
        if matched:
            return matched

    # 2) 逐段与已知路径匹配（把 "_" 同时当作"层级分隔符"和"键名的一部分"来试）
    matched = _match_known(raw, paths, keep_underscore=True)
    if matched:
        return matched

    # 3) 老写法兜底：所有下划线都当分隔符
    legacy = raw.replace("__", ".").replace("_", ".")
    if legacy in paths:
        return legacy
    return None


def _match_known(raw: str, paths: List[str], keep_underscore: bool = True) -> Optional[str]:
    """把 `ai_api_key` 这样的串匹配到已知路径 `ai.api_key`。

    做法：对每条已知路径，把它转成环境变量形式（点→"_"、键名里的下划线保留），
    与 `raw` 比较；再按"段数最多/最长匹配"排序，避免 `ai.api` 抢了 `ai.api_key`。
    """
    candidates: List[Tuple[int, str]] = []
    for path in paths:
        env_form = path.replace(".", "_")
        if env_form == raw:
            candidates.append((len(path), path))
            continue
        if keep_underscore:
            # 也允许把层级分隔符写成双下划线
            if path.replace(".", "__") == raw:
                candidates.append((len(path), path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][1]


def coerce_like(default_value: Any, raw: Any) -> Any:
    """按默认值类型解析用户输入（网页表单传来的都是字符串/原始 JSON）。"""
    if isinstance(default_value, bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "y", "t", "是", "开")
    if isinstance(default_value, int) and not isinstance(default_value, bool):
        return int(float(str(raw).strip()))
    if isinstance(default_value, float):
        return float(str(raw).strip())
    if isinstance(default_value, list):
        if isinstance(raw, list):
            return raw
        text = str(raw)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
        except (ValueError, TypeError):
            pass
        return [item.strip() for item in text.split(",") if item.strip()]
    if isinstance(default_value, dict):
        if isinstance(raw, dict):
            return raw
        return json.loads(str(raw))
    return str(raw)


# 敏感字段的“显式清空”魔法值（网页复选框用）：空串=不修改，本值=清空
CLEAR_MARKER = "__CLEAR__"


class Config:
    """运行期配置对象（与原 app/config_manager.py 的 Config 接口保持兼容）。"""

    def __init__(self, data: Dict[str, Any], file_data: Dict[str, Any], path: str):
        self.data = data
        self.file_data = file_data
        self.path = path

    # ---- 取值 ----
    def get(self, *keys, default=None):
        node: Any = self.data
        for key in keys:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return node

    def str_of(self, *keys, default="") -> str:
        value = self.get(*keys, default=default)
        return default if value is None else str(value)

    def int_of(self, *keys, default=0) -> int:
        try:
            return int(self.get(*keys, default=default))
        except (TypeError, ValueError):
            return default

    def float_of(self, *keys, default=0.0) -> float:
        try:
            return float(self.get(*keys, default=default))
        except (TypeError, ValueError):
            return default

    def bool_of(self, *keys, default=False) -> bool:
        value = self.get(*keys, default=default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "t")

    def list_of(self, *keys, default=None) -> list:
        value = self.get(*keys, default=None)
        if isinstance(value, list):
            return value
        if value in (None, ""):
            return list(default or [])
        return [str(value)]

    def abs_path(self, path_value: str) -> str:
        path_value = str(path_value or "").strip()
        if not path_value:
            return paths.BASE_DIR
        if os.path.isabs(path_value):
            return path_value
        return os.path.join(paths.BASE_DIR, path_value)


def _migrate_legacy_media_limits(file_data: Dict[str, Any]) -> List[str]:
    """把旧版本的媒体大小上限升级到 QQ 官方允许的值。

    旧默认：发送文件 8MB、图片 6MB（都远低于官方软限制 200MB / 20MB）。
    只有等于**旧默认值**的才升级 —— 用户自己改过的值保持不动。
    """
    legacy = {("send", "max_file_mb"): (8, 200), ("send", "max_image_mb"): (6, 20)}
    changed: List[str] = []
    for (section, key), (old, new) in legacy.items():
        block = file_data.get(section)
        if not isinstance(block, dict):
            continue
        try:
            value = float(block.get(key))
        except (TypeError, ValueError):
            continue
        if abs(value - float(old)) < 1e-6:
            block[key] = new
            changed.append(f"send.{key} {old:g}MB → {new:g}MB")
    return changed


class ConfigManager:
    """配置文件读写 + 迁移 + 环境变量覆盖。"""

    # 已经就"机器人 id 重复"告警过的 id（避免每次读配置都刷一遍日志）
    _duplicate_warned: set = set()

    def __init__(self, config_path: str = None):
        self.config_path = config_path or paths.CONFIG_FILE
        self.config: Config = None  # type: ignore
        self._lock = threading.RLock()
        self.env_overrides: Dict[str, Any] = {}
        self.env_unknown: List[str] = []
        self.missing: List[str] = []
        self._load()

    # ---------------------------------------------------------------- 读写
    def _load(self):
        with self._lock:
            file_data: Dict[str, Any] = {}
            created = False
            if os.path.exists(self.config_path):
                try:
                    with open(self.config_path, "r", encoding="utf-8") as handle:
                        file_data = json.load(handle) or {}
                except (OSError, ValueError) as exc:
                    logger.error("读取 %s 失败（%s），已改用默认配置", self.config_path, exc)
                    file_data = {}
            else:
                created = True
            if not isinstance(file_data, dict):
                file_data = {}

            # 旧版本把媒体上限设成了 8MB / 6MB，远低于 QQ 官方允许的值，
            # 这里把"没改过的旧默认值"按新默认值升级（用户自己改过的值不动）。
            migrated = _migrate_legacy_media_limits(file_data)
            merged, added = _deep_merge_defaults(schema.DEFAULT_CONFIG, file_data)
            if created or added or migrated:
                try:
                    self._write(merged)
                except OSError as exc:
                    logger.error("写入配置文件失败: %s", exc)
                if added:
                    logger.info("配置已补全 %d 个缺失项", len(added))
                if migrated:
                    logger.info("已按 QQ 官方限制放宽媒体大小上限：%s（可在「设置 → 发送策略」调整）",
                                "、".join(migrated))

            # 文件侧（不含环境变量覆盖）与运行侧（含覆盖）分开保存
            self._file_data = copy.deepcopy(merged)
            runtime = copy.deepcopy(merged)
            self.env_overrides = self._apply_env(runtime)
            self.config = Config(runtime, self._file_data, self.config_path)
            self._write_doc()

    def _write(self, data: Dict[str, Any]):
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.config_path)

    def reload(self) -> Config:
        """重新从磁盘加载（热更新用）。"""
        self._load()
        return self.config

    def save(self):
        """把文件侧配置写回磁盘。"""
        with self._lock:
            self._write(self._file_data)

    def set_path(self, path: str, value: Any):
        """按点号路径同时更新“文件侧”和“运行侧”配置（不落盘）。

        注意：两边各写一份**深拷贝**，不能共用同一个对象。
        否则之后任何一边的改动都会同时改到另一边，出现
        "内存里恢复了密钥、磁盘上却还是空"这类诡异问题。
        """
        with self._lock:
            for target in (self._file_data, self.config.data):
                node = target
                parts = path.split(".")
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                node[parts[-1]] = copy.deepcopy(value)

    # ---------------------------------------------------------------- 按机器人读取
    @staticmethod
    def _split(path: str):
        parts = str(path or "").split(".")
        return parts[:-1], parts[-1]

    def get_for_bot(self, bot_id: str, path: str, default: Any = None) -> Any:
        """读取某个机器人的某项配置：该机器人有覆盖就用覆盖，否则用全局值。"""
        if not schema.is_per_bot(path):
            return self.get_path(path, default)
        node: Any = self.bot_overrides(bot_id)
        for key in path.split("."):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return self.get_path(path, default)
        return node

    def get_path(self, path: str, default: Any = None) -> Any:
        """按点号路径从运行配置里取值。"""
        node: Any = self.config.data
        for key in str(path or "").split("."):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return node

    def set_for_bot(self, bot_id: str, path: str, value: Any):
        """写入某个机器人的配置：按机器人的项写进该机器人的覆盖，其余写全局。"""
        if not schema.is_per_bot(path):
            self.set_path(path, value)
            return
        with self._lock:
            for target in (self._file_data, self.config.data):
                entry = self._find_bot(target, bot_id)
                if entry is None:
                    continue
                node = entry.setdefault("overrides", {})
                parts = path.split(".")
                for key in parts[:-1]:
                    node = node.setdefault(key, {})
                node[parts[-1]] = copy.deepcopy(value)

    def bot_overrides(self, bot_id: str) -> Dict[str, Any]:
        entry = self._find_bot(self.config.data, bot_id)
        overrides = (entry or {}).get("overrides")
        return copy.deepcopy(overrides) if isinstance(overrides, dict) else {}

    @classmethod
    def _find_bot(cls, data: Dict[str, Any], bot_id: str) -> Optional[Dict[str, Any]]:
        """按 id 找机器人配置。**id 重复时会告警**（复制配置很容易撞 id）。

        以前这里直接返回第一个匹配项：如果用户不小心把两个机器人的 id 写成一样，
        改设置只会写进第一个，页面上看起来"改了没生效 / 改一个另一个也变了"，
        而且没有任何提示。现在会把重复的 id 记下来（`duplicate_bot_ids`），
        页面与诊断都能看到。
        """
        matches = [item for item in ((data or {}).get("bots") or [])
                   if isinstance(item, dict) and str(item.get("id")) == str(bot_id)]
        if len(matches) > 1:
            cls._note_duplicate_bot(bot_id, len(matches))
        return matches[0] if matches else None

    @classmethod
    def _note_duplicate_bot(cls, bot_id: str, count: int):
        if bot_id in cls._duplicate_warned:
            return
        cls._duplicate_warned.add(bot_id)
        logger.warning("发现 %d 个机器人都用了同一个 id「%s」：改设置只会写进第一个，"
                       "请到「设置 → 机器人账号」把 id 改成互不相同", count, bot_id)

    def duplicate_bot_ids(self) -> List[str]:
        """当前配置里出现重复的机器人 id（用于页面提示）。"""
        seen: Dict[str, int] = {}
        for item in (self.config.data.get("bots") or []):
            if isinstance(item, dict):
                key = str(item.get("id") or "")
                if key:
                    seen[key] = seen.get(key, 0) + 1
        return sorted(key for key, count in seen.items() if count > 1)

    def resolve_for_bot(self, bot_id: str) -> Config:
        """把"全局配置 + 该机器人的覆盖"合成一份完整配置，供运行时使用。"""
        merged = copy.deepcopy(self.config.data)
        entry = self._find_bot(merged, bot_id) or {}
        overrides = entry.get("overrides") if isinstance(entry.get("overrides"), dict) else {}
        # 只把 per-bot 的键覆盖进去，避免误伤全局设置
        for path in schema.per_bot_paths():
            key = path.split(".")[0]
            if key in overrides:
                node = merged
                parts = path.split(".")
                value_node: Any = overrides
                ok = True
                for part in parts:
                    if isinstance(value_node, dict) and part in value_node:
                        value_node = value_node[part]
                    else:
                        ok = False
                        break
                if not ok:
                    continue
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                node[parts[-1]] = copy.deepcopy(value_node)
        merged["active_bot_id"] = bot_id
        return Config(merged, self._file_data, self.config_path)

    def copy_bot_settings_to_all(self, from_bot: str) -> Dict[str, Any]:
        """把某个机器人的 per-bot 设置复制给所有其它机器人（一键配置）。"""
        overrides = self.bot_overrides(from_bot)
        if not overrides:
            return {"copied": 0, "message": "当前机器人没有可复制的自定义设置"}
        count = 0
        with self._lock:
            # 两个副本都要写，但只统计一次（机器人数量）
            for index, target in enumerate((self._file_data, self.config.data)):
                for item in (target.get("bots") or []):
                    if not isinstance(item, dict) or str(item.get("id")) == str(from_bot):
                        continue
                    item["overrides"] = copy.deepcopy(overrides)
                    if index == 0:
                        count += 1
            self.save()
        return {"copied": count,
                "message": f"已把当前机器人的设置复制到其它 {count} 个机器人"}

    def clear_bot_overrides(self, bot_id: str) -> bool:
        """清空某个机器人的覆盖，让它回到全局默认。"""
        with self._lock:
            changed = False
            for target in (self._file_data, self.config.data):
                entry = self._find_bot(target, bot_id)
                if entry is not None and entry.pop("overrides", None) is not None:
                    changed = True
            if changed:
                self.save()
        return changed

    # ---------------------------------------------------------------- 环境变量
    def _apply_env(self, runtime: Dict[str, Any]) -> Dict[str, Any]:
        """把 QQBOT_XXX_YYY 环境变量覆盖到对应配置项，返回 {路径: 原值} 供页面提示。

        支持三种写法（都能正确处理键名里的下划线，例如 `ai.api_key`）：
          QQBOT_AI_API_KEY            ← 最常用
          QQBOT_AI__API_KEY           ← 用双下划线显式表示层级
          QQBOT_CLOUD_SYNC_ENABLED    ← 键名本身就是 cloud_sync
        认不出来的名字会**打一条 warning**（以前是静默忽略，用户根本查不出为什么没生效）。
        """
        overridden: Dict[str, Any] = {}
        unknown: List[str] = []
        for env_key, raw in os.environ.items():
            if not env_key.startswith(ENV_PREFIX):
                continue
            path = resolve_env_path(env_key)
            if not path:
                unknown.append(env_key)
                continue
            default_value = _default_for(path)
            if default_value is None:
                unknown.append(env_key)
                continue
            try:
                value = coerce_like(default_value, raw)
            except (TypeError, ValueError):
                logger.warning("环境变量 %s=%r 解析失败，已忽略", env_key, raw)
                continue
            node = runtime
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            previous = node.get(parts[-1])
            node[parts[-1]] = value
            overridden[path] = previous
            logger.info("环境变量 %s 覆盖配置项 %s", env_key, path)
        if unknown:
            logger.warning(
                "以下环境变量没匹配到任何配置项，已忽略：%s（示例：QQBOT_WEB_PORT / "
                "QQBOT_AI_API_KEY / QQBOT_CLOUD_SYNC_ENABLED，层级分隔符也可写成双下划线）",
                "、".join(sorted(unknown)[:10]))
            self.env_unknown = sorted(unknown)
        else:
            self.env_unknown = []
        return overridden

    def reapply_env(self) -> Dict[str, Any]:
        """保存配置后重新应用环境变量（环境变量始终优先）。"""
        runtime = copy.deepcopy(self._file_data)
        self.env_overrides = self._apply_env(runtime)
        self.config.data = runtime
        return self.env_overrides

    # ---------------------------------------------------------------- 网页支持
    def mask_secrets(self, data: Dict[str, Any]) -> Dict[str, Any]:
        out = copy.deepcopy(data)
        for path in schema.MASKED_FIELDS:
            node = out
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.get(part) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, dict) and node.get(parts[-1]):
                node[parts[-1]] = schema.SECRET_MASK
        for bot in out.get("bots", []) or []:
            if isinstance(bot, dict) and bot.get("app_secret"):
                bot["app_secret"] = schema.SECRET_MASK
        return out

    def payload(self) -> Dict[str, Any]:
        """给设置页的完整数据：当前值（密钥打码）、默认值、元信息与分组结构。

        每个分组会标注 `per_bot`：为真的分组作用于**当前选中的机器人**。
        """
        payload = {
            "path": self.config_path,
            "config": self.mask_secrets(self.config.data),
            "defaults": schema.default_config(),
            "fields": schema.field_meta(),
            "sections": schema.sections_payload(),
            "section_labels": schema.section_labels(),
            "restart_required": sorted(schema.RESTART_REQUIRED),
            "env_overrides": sorted(self.env_overrides.keys()),
            "env_unknown": list(self.env_unknown or []),
            "duplicate_bot_ids": self.duplicate_bot_ids(),
            "editable": sorted(schema.FIELDS.keys()),
            "config_doc": paths.CONFIG_DOC_FILE,
        }
        bot_id = self.active_bot_id()
        if bot_id:
            effective = self.resolve_for_bot(bot_id)
            payload["active_bot"] = bot_id
            payload["bot_config"] = self.mask_secrets(effective.data)
            payload["bot_overrides"] = self.bot_overrides(bot_id)
        return payload

    # ---------------------------------------------------------------- 当前选中的机器人
    def active_bot_id(self) -> str:
        """后台当前选中的机器人（用于"设置作用于哪个机器人"）。"""
        with self._lock:
            bot_id = str((self.config.data or {}).get("active_bot_id") or "").strip()
            if bot_id and self._find_bot(self.config.data, bot_id):
                return bot_id
            # 没设置过 / 已被删除 → 退回第一个启用的机器人
            for item in (self.config.data or {}).get("bots") or []:
                if isinstance(item, dict) and item.get("enabled"):
                    return str(item.get("id") or "")
            for item in (self.config.data or {}).get("bots") or []:
                if isinstance(item, dict):
                    return str(item.get("id") or "")
            return ""

    def set_active_bot(self, bot_id: str) -> str:
        """切换当前选中的机器人（只改运行侧与文件侧的一个字段）。"""
        bot_id = str(bot_id or "").strip()
        with self._lock:
            if not bot_id or not self._find_bot(self.config.data, bot_id):
                return self.active_bot_id()
            for target in (self._file_data, self.config.data):
                target["active_bot_id"] = bot_id
            self.save()
        return bot_id

    def apply_incoming(self, incoming: Dict[str, Any], bot_id: str = "") -> Tuple[List[str], List[str], List[str]]:
        """把网页提交的配置写回。返回 (已热更新的路径, 需重启的路径, 全部变更路径)。

        - `bot_id` 非空时，**按机器人的分组**（AI / 回复 / 过滤 / 功能 / 发送 / 定时 / 面板）
          写入该机器人的覆盖，全局分组照旧写全局；
        - 只接受 `FIELDS` 里登记过的路径（未知键忽略）；
        - 密钥字段收到空串/掩码时视为"不修改"，收到清空标记则真的清空；
        - `bots` 列表以"磁盘上已有的那条"为底再更新账号字段（网页只提交账号字段），
          这样既支持删除机器人，又不会把该机器人的 per-bot 覆盖弄丢。
        """
        changed: List[str] = []
        need_restart: List[str] = []
        applied: List[str] = []
        bot_id = str(bot_id or "").strip()

        with self._lock:
            flat = schema.flatten(incoming)
            for path, raw in flat.items():
                if path not in schema.FIELDS:
                    continue
                if path in schema.SECRET_FIELDS:
                    if raw is None or raw == "" or raw == schema.SECRET_MASK:
                        continue                      # 留空=不修改
                    if raw == CLEAR_MARKER:
                        raw = ""                      # 勾选“清空此值”
                if path == schema.BOTS_FIELD:
                    continue          # bots 列表单独处理（见下）
                default_value = _default_for(path)
                try:
                    value = coerce_like(default_value, raw)
                except (TypeError, ValueError):
                    raise ConfigError(f"配置项「{schema.FIELDS[path]['label']}」的值不合法：{raw!r}")

                meta = schema.FIELDS[path]
                if meta.get("type") in ("int", "float"):
                    low, high = meta.get("min"), meta.get("max")
                    if low is not None and value < low:
                        raise ConfigError(f"配置项「{meta['label']}」不能小于 {low}")
                    if high is not None and value > high:
                        raise ConfigError(f"配置项「{meta['label']}」不能大于 {high}")

                if bot_id and schema.is_per_bot(path):
                    old = self.get_for_bot(bot_id, path)
                    if old != value:
                        changed.append(f"{path}（{bot_id}）")
                    self.set_for_bot(bot_id, path, value)
                else:
                    old = self._get_from(self._file_data, path)
                    if old != value:
                        changed.append(path)
                    self.set_path(path, value)

            # 机器人账号列表：单独处理（含 app_secret 掩码）
            bots = incoming.get("bots")
            if isinstance(bots, list):
                existing_entries = [item for item in (self._file_data.get("bots") or [])
                                    if isinstance(item, dict)]
                cleaned = []
                for index, bot in enumerate(bots):
                    if not isinstance(bot, dict):
                        continue
                    incoming_id = str(bot.get("id") or f"bot{index + 1}").strip() or f"bot{index + 1}"
                    # 以磁盘上已有的那条记录为底：网页只提交账号字段，
                    # 若在这里整条重建，会把该机器人的 per-bot 覆盖
                    # （AI / 回复 / 过滤 / 功能 / 发送 / 定时任务 / 指令面板）
                    # 连带其它未知字段一起丢掉 —— 表现就是"设置保存了却不生效"，
                    # 而且每次都误报"bots 有改动，需要重启"。
                    base: Dict[str, Any] = {}
                    for existing in existing_entries:
                        if str(existing.get("id") or "") == incoming_id:
                            base = copy.deepcopy(existing)
                            break
                    item = dict(base)
                    item.update({
                        "id": incoming_id,
                        "name": str(bot.get("name") or f"机器人 {index + 1}").strip(),
                        "enabled": bool(bot.get("enabled")),
                        "app_id": str(bot.get("app_id") or "").strip(),
                        "app_secret": str(bot.get("app_secret") or "").strip(),
                        "sandbox": bool(bot.get("sandbox")),
                        "intents": int(bot.get("intents") or 0),
                        "reconnect_attempts": int(bot.get("reconnect_attempts") or 5),
                        "reconnect_interval": int(bot.get("reconnect_interval") or 10),
                    })
                    # app_secret：空/掩码 = 不修改；显式清空标记 = 真的清掉
                    old_secret = str(base.get("app_secret") or "")
                    if item["app_secret"] == CLEAR_MARKER:
                        item["app_secret"] = ""
                    elif item["app_secret"] in ("", schema.SECRET_MASK):
                        item["app_secret"] = old_secret
                    cleaned.append(item)
                if cleaned:
                    if self._file_data.get("bots") != cleaned:
                        changed.append("bots")
                    self.set_path("bots", cleaned)

            if changed:
                self.save()
            self.reapply_env()

        for path in changed:
            (need_restart if path in schema.RESTART_REQUIRED else applied).append(path)
        return applied, need_restart, changed

    @staticmethod
    def _get_from(data: Dict[str, Any], path: str) -> Any:
        node: Any = data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    # ---------------------------------------------------------------- 说明文档
    def _write_doc(self):
        """生成中文配置说明文件（首次运行或在配置文件补全后刷新）。"""
        try:
            lines = [
                "═" * 66,
                "              QQBotMerged 配置文件说明（config.json）",
                "═" * 66,
                "",
                "本文件由程序自动生成，仅供参考；配置请直接编辑 config.json 或使用网页后台「设置」页。",
                f"程序根目录：{paths.BASE_DIR}",
                "",
                "环境变量覆盖：任何配置项都可用 QQBOT_<路径> 覆盖（全大写）。",
                "  键名里的下划线会原样保留，层级用下划线连接：",
                "  例：QQBOT_WEB_PORT=9000            → web.port",
                "      QQBOT_AI_API_KEY=sk-xxx        → ai.api_key",
                "      QQBOT_CLOUD_SYNC_ENABLED=1     → cloud_sync.enabled",
                "      QQBOT_SECURITY_ALERT_OWNER_OPENID=xxx → security.alert_owner_openid",
                "  也可以用双下划线显式表示层级（更不容易产生歧义）：",
                "      QQBOT_AI__API_KEY=sk-xxx       → ai.api_key",
                "      QQBOT_CLOUD_SYNC__ENABLED=1    → cloud_sync.enabled",
                "  名字没匹配到任何配置项时会写一条 warning 日志，不会静默忽略。",
                "",
            ]
            current_section = None
            for path in schema.field_paths():
                section = path.split(".")[0]
                if section != current_section:
                    current_section = section
                    info = schema.SECTION_LABELS.get(section, {})
                    lines += ["", "─" * 66,
                              f"【{info.get('label', section)}】{info.get('desc', '')}",
                              "─" * 66]
                meta = schema.FIELDS[path]
                default_value = _default_for(path)
                lines.append(f"  {path}")
                lines.append(f"      名称：{meta['label']}")
                if meta.get("hint"):
                    lines.append(f"      说明：{meta['hint']}")
                lines.append(f"      类型：{meta['type']}    默认值：{json.dumps(default_value, ensure_ascii=False)}")
                if meta.get("options"):
                    opts = "、".join(f"{value}={label}" for value, label in meta["options"])
                    lines.append(f"      可选值：{opts}")
            lines += ["", "─" * 66,
                      "提示：机器人账号（bots）是列表，网页「设置 → 机器人账号」里增删更方便。",
                      "      修改 app_id / app_secret / 端口 / 存储路径后需要重启程序；其余多数项保存即生效。",
                      "═" * 66, ""]
            with open(paths.CONFIG_DOC_FILE, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
        except OSError as exc:
            logger.debug("写入配置说明文件失败: %s", exc)


def load_config(config_path: str = None) -> ConfigManager:
    return ConfigManager(config_path)
