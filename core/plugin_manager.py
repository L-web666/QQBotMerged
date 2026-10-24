# -*- coding: utf-8 -*-
"""插件系统（只支持 **AstrBot 插件格式**）

插件目录（默认 `plugins/`）里放 **AstrBot 插件目录**即可，形如：

    plugins/astrbot_plugin_xxx/
    ├── metadata.yaml     ← name / display_name / desc / version / author / repo ...
    ├── main.py           ← 必须叫 main.py，里面写继承 Star 的插件类
    └── _conf_schema.json ← 可选：插件配置的 Schema

`main.py` 的标准写法（与 AstrBot 官方文档一致）：

    from astrbot.api.event import filter, AstrMessageEvent
    from astrbot.api.star import Context, Star

    class MyPlugin(Star):
        def __init__(self, context: Context):
            super().__init__(context)

        @filter.command("helloworld")
        async def helloworld(self, event: AstrMessageEvent):
            yield event.plain_result("Hello!")

`astrbot.*` 的接口由 `core/astrbot_compat.py` 提供（垫片 + 事件桥接），
支持范围、已知差异都写在该文件顶部。

**目录约定（与 AstrBot 官方一致）**：
- 插件本体：`plugins/<插件名>/{metadata.yaml, main.py, _conf_schema.json}`；
- 插件配置：`data/config/<插件名>_config.json`（由 `_conf_schema.json` 自动生成实体）；
- 插件数据：`data/plugin_data/<插件名>/`（官方文档里插件存大文件的位置）。

本程序不再支持任何"原生插件"格式：`plugins/` 下不是 AstrBot 插件目录的东西一律忽略。
"""

import copy
import json
import logging
import os
import shutil
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core import config_schema as schema
from core import paths
from core.astrbot_compat import AstrBotHost, PluginRuntime

logger = logging.getLogger(__name__)

# 官方目录：插件数据放 data/plugin_data/<插件名>/
OFFICIAL_DATA_DIR = "plugin_data"
# 旧版本用的是 data/plugins_data/<插件名>/，启动时自动迁移一次（不覆盖已有文件）
LEGACY_DATA_DIR = "plugins_data"
# 旧程序/旧版本里按中文名存的数据，迁移时改成 AstrBot 插件目录名
LEGACY_PLUGIN_ALIASES = {
    "每日签到": "astrbot_plugin_daily_checkin",
    "骰子": "astrbot_plugin_dice",
    "ollama": "astrbot_plugin_ollama",
}

# 从插件能看到的配置里剔除的敏感项（插件不需要密钥；避免"顺手读一下配置"泄密）。
# 除了这里列的，还会带上 config_schema.SECRET_FIELDS 里声明的所有密钥字段。
EXTRA_SECRET_CONFIG_PATHS = ("cloud_sync.account_id", "security.alert_owner_openid")


def _strip_secrets(config: Dict[str, Any]) -> Dict[str, Any]:
    """给插件的配置副本：去掉密钥类字段（含每个机器人的 AppSecret）。"""
    out = copy.deepcopy(config or {})
    paths = list(EXTRA_SECRET_CONFIG_PATHS)
    try:
        paths.extend(schema.SECRET_FIELDS)
    except Exception:
        pass
    for path in paths:
        parts = str(path).split(".")
        node: Any = out
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and parts[-1] in node:
            node[parts[-1]] = ""
    for bot in out.get("bots") or []:
        if isinstance(bot, dict) and bot.get("app_secret"):
            bot["app_secret"] = ""
    return out


class PluginBot:
    """给插件的"机器人能力"（供 AstrBot 兼容层内部使用）。

    **不**直接暴露 `Runtime` 与完整配置：
    - `bot.runtime` 是白名单门面（见 `PluginRuntime`）：只能发消息、查名字、记统计、写日志；
    - `bot.config` 是去掉密钥后的副本。
    """

    def __init__(self, runtime=None, config: Dict[str, Any] = None,
                 logger_obj: logging.Logger = None):
        self._runtime = runtime                 # 私有：门面内部用
        self._log = logger_obj or logger
        self._raw_config = config or {}         # 私有：内部用（含密钥）
        self.runtime = PluginRuntime(runtime, self._log)
        self.config = _strip_secrets(self._raw_config)

    def set_config(self, config: Dict[str, Any]):
        self._raw_config = config or {}
        self.config = _strip_secrets(self._raw_config)

    # ---- 发送（供兼容层调用） ----
    def send_message(self, openid: str, content: str, bot_id: str = "") -> bool:
        return self.runtime.send_text("private", openid, content, bot_id=bot_id)

    def send_group_message(self, group_openid: str, content: str, bot_id: str = "") -> bool:
        return self.runtime.send_text("group", group_openid, content, bot_id=bot_id)

    def send_image(self, target_type: str, openid: str, image_url: str = "", blob: bytes = None,
                   file_name: str = "image.png") -> bool:
        return self.runtime.send_image(target_type, openid, image_url=image_url, blob=blob,
                                       file_name=file_name)

    def bot_ids(self) -> List[str]:
        return self.runtime.bot_ids()

    def log(self, message: str):
        self._log.info("[插件] %s", message)


class PluginManager:
    """AstrBot 插件的加载、启停与分发。"""

    def __init__(self, plugin_dir: str = "plugins", data_dir: str = "",
                 disabled_file: str = "data/plugins_disabled.json",
                 logger_obj: logging.Logger = None, enabled: bool = True):
        # 插件目录必须是绝对路径：相对路径会跟着"启动时的工作目录"变，
        # 用户在程序目录放好插件、却从别处启动，就会一个插件都看不到。
        if plugin_dir and not os.path.isabs(plugin_dir):
            plugin_dir = os.path.join(paths.BASE_DIR, plugin_dir)
        self.plugin_dir = plugin_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins")
        # 官方约定：插件数据放 data/plugin_data/<插件名>/
        self.data_dir = data_dir or os.path.join(paths.DATA_DIR, OFFICIAL_DATA_DIR)
        if not os.path.isabs(self.data_dir):
            self.data_dir = os.path.join(paths.BASE_DIR, self.data_dir)
        self.disabled_file = disabled_file
        self.log = logger_obj or logger
        self.plugins: List[Dict[str, Any]] = []
        self.disabled: Dict[str, bool] = {}
        self.bot: Optional[PluginBot] = None
        self._lock = threading.RLock()
        self.issues: List[Dict[str, str]] = []
        self.discovered: List[Dict[str, str]] = []
        self.enabled = bool(enabled)
        self.loaded_at = 0.0
        self.host: Optional[AstrBotHost] = None
        self._load_disabled()

    # ------------------------------------------------------------------ 旧数据迁移
    def migrate_legacy_data(self) -> List[str]:
        """把旧版 `data/plugins_data/<插件名>/` 里的插件数据搬到官方目录。

        只在数据目录就是官方目录时执行（测试用临时目录不会碰到真实数据）。
        规则：只搬目标里不存在的文件，全部搬完才删掉旧目录；有失败就原样保留并告警。
        """
        official = os.path.abspath(self.data_dir)
        legacy = os.path.join(paths.DATA_DIR, LEGACY_DATA_DIR)
        if official != os.path.abspath(os.path.join(paths.DATA_DIR, OFFICIAL_DATA_DIR)):
            return []
        if not os.path.isdir(legacy):
            return []
        moved: List[str] = []
        failed: List[str] = []
        for root, _dirs, files in os.walk(legacy):
            rel = os.path.relpath(root, legacy)
            parts = [] if rel == "." else rel.split(os.sep)
            if parts and parts[0] in LEGACY_PLUGIN_ALIASES:
                parts[0] = LEGACY_PLUGIN_ALIASES[parts[0]]
            target_dir = os.path.join(official, *parts) if parts else official
            for name in files:
                src = os.path.join(root, name)
                if not parts and name.endswith(".json"):
                    # 旧程序里 <插件名>.json 这种"插件名即文件名"的写法
                    # （每日签到.json 就是签到数据 → 交给新插件的 legacy 迁移入口）
                    stem = os.path.splitext(name)[0]
                    mapped = LEGACY_PLUGIN_ALIASES.get(stem)
                    if mapped == "astrbot_plugin_daily_checkin":
                        dst = os.path.join(official, mapped, "checkin_legacy.json")
                    elif mapped:
                        dst = os.path.join(official, mapped, name)
                    else:
                        dst = os.path.join(target_dir, name)
                else:
                    dst = os.path.join(target_dir, name)
                if os.path.exists(dst):
                    continue
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    moved.append(os.path.relpath(dst, paths.BASE_DIR))
                except OSError as exc:
                    failed.append(f"{src}: {exc}")
        if failed:
            self.log.warning("旧插件数据迁移有 %d 个文件失败，旧目录保留：%s",
                             len(failed), "；".join(failed[:3]))
            return moved
        if moved:
            try:
                shutil.rmtree(legacy, ignore_errors=True)
            except OSError:
                pass
            self.log.info("已把旧插件数据迁到官方目录 data/%s/：%d 个文件", OFFICIAL_DATA_DIR, len(moved))
        else:
            try:
                shutil.rmtree(legacy, ignore_errors=True)
            except OSError:
                pass
        return moved

    # ------------------------------------------------------------------ 诊断基线
    def _note_issue(self, name: str, reason: str, level: str = "error"):
        self.issues.append({"name": name, "reason": reason, "level": level})
        (self.log.error if level == "error" else self.log.warning)(
            "插件 %s %s", name, reason)

    @staticmethod
    def _is_astrbot_dir(path: str) -> bool:
        try:
            return AstrBotHost.is_astrbot_plugin_dir(path)
        except Exception:
            return False

    def scan(self) -> List[Dict[str, str]]:
        """扫描目录：只列出 AstrBot 插件目录（含 metadata.yaml + main.py）。"""
        found: List[Dict[str, str]] = []
        try:
            if not os.path.isdir(self.plugin_dir):
                return found
            entries = sorted(os.listdir(self.plugin_dir))
        except OSError as exc:
            self.log.warning("读取插件目录失败：%s", exc)
            return found
        for name in entries:
            if name.startswith((".", "__", "_")):
                continue
            full = os.path.join(self.plugin_dir, name)
            if os.path.isdir(full) and self._is_astrbot_dir(full):
                found.append({"file": name + "/", "kind": "AstrBot 插件", "name": name})
        return found

    def diagnostics(self) -> Dict[str, Any]:
        """给后台的加载诊断信息（含一次实时目录扫描）。"""
        live = self.scan()
        return {
            "dir": self.plugin_dir,
            "dir_exists": os.path.isdir(self.plugin_dir),
            "enabled": self.enabled,
            "loaded_at": self.loaded_at,
            "discovered": list(self.discovered) or live,
            "live_scan": live,
            "issues": list(self.issues),
            "format": "AstrBot",
            "astrbot_version": getattr(self.host, "astrbot_version", "") or "",
            "unsupported": sorted({item for info in self.plugins
                                   for item in (info.get("unsupported") or [])}),
            # 插件安全扫描汇总（high/medium/info 各几条），插件页用来做整体提示
            "safety": self._safety_summary(),
        }

    def _safety_summary(self) -> Dict[str, Any]:
        """把所有插件的安全扫描结果汇总成一份（给插件页顶部提示用）。"""
        findings: List[Dict[str, Any]] = []
        for info in self.plugins:
            items = ((info.get("safety") or {}).get("findings") or [])
            for item in items:
                merged = dict(item)
                merged["plugin"] = str(info.get("name") or "")
                findings.append(merged)
        try:
            from core import plugin_safety
            return plugin_safety.summarize(findings,
                                           [str(info.get("name") or "") for info in self.plugins])
        except Exception:
            return {"level": "ok", "counts": {}, "findings": findings, "files": [],
                    "summary": "未扫描"}

    # ------------------------------------------------------------------ 启停清单
    def _load_disabled(self):
        try:
            if os.path.isfile(self.disabled_file):
                with open(self.disabled_file, "r", encoding="utf-8") as handle:
                    raw = json.load(handle) or {}
                if isinstance(raw, dict):
                    self.disabled = {str(k): bool(v) for k, v in raw.items()}
        except (OSError, ValueError) as exc:
            self.log.warning("读取插件停用清单失败: %s", exc)

    def _save_disabled(self):
        try:
            directory = os.path.dirname(self.disabled_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.disabled_file, "w", encoding="utf-8") as handle:
                json.dump(self.disabled, handle, ensure_ascii=False, indent=1)
        except OSError as exc:
            self.log.warning("保存插件停用清单失败: %s", exc)

    def set_disabled(self, name: str, disabled: bool) -> bool:
        with self._lock:
            if name not in {item["name"] for item in self.plugins}:
                return False
            self.disabled[name] = bool(disabled)
            return True

    def is_disabled(self, name: str) -> bool:
        return bool(self.disabled.get(name))

    def apply_changes(self) -> int:
        """落盘启停状态并重新加载插件，返回当前插件数量。"""
        self._save_disabled()
        return len(self.reload())

    # ------------------------------------------------------------------ 加载
    def set_bot(self, bot: PluginBot):
        with self._lock:
            self.bot = bot
            if self.host is not None:
                self.host.bot = bot
                self.host.runtime = bot._runtime

    def refresh_bot_config(self, config: Dict[str, Any]):
        if self.bot is not None:
            self.bot.set_config(config)

    # ------------------------------------------------------------------ 按机器人隔离
    def bot_policy(self, bot_id: str) -> Dict[str, List[str]]:
        """该机器人的插件策略（读的是"全局配置 + 该机器人覆盖"里的生效值）。

        - `allow`（`plugins.enabled_names`）：白名单，空 = 不限制；
        - `deny`（`plugins.disabled_names`）：黑名单，优先级高于白名单。
        两个都为空 = 沿用旧行为（所有已启用的插件对该机器人都生效）。
        """
        allow: List[str] = []
        deny: List[str] = []
        if not bot_id:
            return {"allow": allow, "deny": deny}
        runtime = getattr(self.bot, "_runtime", None)
        if runtime is None or not hasattr(runtime, "bot_config"):
            return {"allow": allow, "deny": deny}
        try:
            config = runtime.bot_config(bot_id)
            allow = [str(item).strip() for item in (config.list_of("plugins", "enabled_names",
                                                                    default=[]) or [])
                     if str(item).strip()]
            deny = [str(item).strip() for item in (config.list_of("plugins", "disabled_names",
                                                                   default=[]) or [])
                    if str(item).strip()]
        except Exception as exc:
            self.log.debug("[%s] 读取插件策略失败，按「全部插件可用」处理：%s", bot_id, exc)
        return {"allow": allow, "deny": deny}

    def allowed_names(self, bot_id: str) -> Optional[set]:
        """该机器人可用的插件名集合；返回 None 表示"不限制"（全部插件）。"""
        policy = self.bot_policy(bot_id)
        allow, deny = policy["allow"], policy["deny"]
        if not allow and not deny:
            return None
        names = {str(item.get("name") or "") for item in self.plugins}
        return {name for name in names if name and name not in deny
                and (not allow or name in allow)}

    def reload(self) -> List[Dict[str, Any]]:
        with self._lock:
            self.plugins = []
            self.load_plugins()
            return self.list_plugins()

    def load_plugins(self, force: bool = False) -> List[Dict[str, Any]]:
        """扫描并加载 AstrBot 插件。

        `force=True` 时即使 `plugins.enabled=false` 也照常加载（供后台"刷新"用）。
        """
        with self._lock:
            self.plugins = []
            self.issues = []
            self.discovered = []
            self.loaded_at = time.time()
            if not os.path.isdir(self.plugin_dir):
                self._note_issue(self.plugin_dir, "目录不存在，无法加载任何插件")
                return self.plugins

            found = self.scan()
            self.discovered = [{"file": item["file"], "kind": item["kind"]} for item in found]
            if not any(item["kind"] == "AstrBot 插件" for item in found):
                self._note_issue(self.plugin_dir,
                                 "目录里没有 AstrBot 插件（需要 <插件名>/metadata.yaml + main.py）",
                                 level="warning")
                return self.plugins

            if not force and not self.enabled:
                self._note_issue("插件系统", "已在配置里关闭（plugins.enabled=false），"
                                            "目录里的插件未被加载", level="warning")
                return self.plugins

            host = self._astrbot_host()
            if host is None:
                self._note_issue("AstrBot 兼容层", "初始化失败，插件未加载")
                return self.plugins
            host.runtime = getattr(self.bot, "_runtime", None)
            host.bot = self.bot
            host.unload()
            for item in found:
                if item["kind"] != "AstrBot 插件":
                    continue
                host.load(item["name"], os.path.join(self.plugin_dir, item["name"]))
            for info in host.list_plugins():
                info["disabled"] = bool(self.disabled.get(info["name"]))
                # 安全扫描（AST 静态分析，不执行插件代码）：只做提示，绝不阻断加载
                info["safety"] = self._scan_safety(info)
                self.plugins.append(info)
            host.call_initialize()
            host.call_loaded_hooks()
            self.issues.extend(host.issues)
            return self.plugins

    def _scan_safety(self, info: Dict[str, Any]) -> Dict[str, Any]:
        """扫一个插件的源码，返回给插件页展示的风险摘要（失败也不影响加载）。"""
        try:
            from core import plugin_safety
            module = str(info.get("module") or info.get("name") or "")
            path = str(info.get("path") or "")
            if path and os.path.isfile(path):
                path = os.path.dirname(path)          # info["path"] 是入口文件时取它所在目录
            if not path or not os.path.isdir(path):
                path = os.path.join(self.plugin_dir, module)
            return plugin_safety.scan_plugin_dir(path)
        except Exception as exc:                       # 扫描只是提示，不能拖垮加载
            self.log.debug("插件 %s 的安全扫描失败：%s", info.get("name"), exc)
            return {"level": "ok", "counts": {}, "findings": [], "files": [],
                    "summary": "扫描失败（不影响使用）"}

    def _astrbot_host(self) -> Optional[AstrBotHost]:
        if self.host is None:
            try:
                self.host = AstrBotHost(self, logger_obj=self.log)
                self.host.runtime = getattr(self.bot, "_runtime", None)
                self.host.bot = self.bot
            except Exception as exc:
                self._note_issue("AstrBot 兼容层", f"初始化失败：{exc}", level="warning")
                self.host = None
        return self.host

    # ------------------------------------------------------------------ 分发
    def dispatch_message(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """把消息交给 AstrBot 插件。

        返回 `{"source", "text", "images", "handled"}`；`images` 里是
        `{"url"|"blob", "file_name"}`。没有插件接管时返回 None。
        """
        host = self.host
        if host is None or not host.plugins:
            return None
        runtime = getattr(self.bot, "_runtime", None)

        def sink(components):
            """`await event.send(...)`：立刻把消息发出去（不参与引用/分段）。"""
            self._send_components(runtime, msg, components)

        try:
            # 按机器人隔离：该机器人没勾选的插件不参与这条消息的分发
            result = host.dispatch(msg, runtime=runtime, sink=sink,
                                   allowed=self.allowed_names(msg.get("bot_id") or ""))
        except Exception as exc:
            self.log.error("AstrBot 插件分发失败: %s", exc)
            return None
        if not result or not result.get("handled"):
            return None
        return {"source": "astrbot:" + str(result.get("plugin") or ""),
                "text": result.get("text") or "",
                "images": result.get("images") or [],
                "sequence": result.get("sequence") or [],
                "via_sink": bool(result.get("via_sink")),
                "handled": True}

    def _send_components(self, runtime, msg: Dict[str, Any], components: List[Any]):
        """按消息链的顺序发送（文本/图片会拆成多条，但顺序不变）。"""
        if runtime is None:
            return
        from core.astrbot_compat import AstrBotHost
        steps: List[Dict[str, Any]] = []
        for component in components:
            steps.extend(AstrBotHost._result_sequence(component))
        self.send_steps(runtime, msg, steps)

    def send_steps(self, runtime, msg: Dict[str, Any], steps: List[Dict[str, Any]]):
        target_type = "group" if (msg.get("type") or "") == "group" else "private"
        target_id = msg.get("group_openid") if target_type == "group" else msg.get("user_openid")
        bot_id = msg.get("bot_id") or ""
        if not target_id or runtime is None:
            return
        for step in steps or []:
            try:
                if step.get("type") == "text":
                    runtime.send_text(target_type, target_id, step.get("text") or "", bot_id=bot_id)
                else:
                    runtime.send_image(target_type, target_id, image_url=step.get("url", ""),
                                       blob=step.get("blob"),
                                       file_name=step.get("file_name") or "image.png",
                                       bot_id=bot_id)
            except Exception as exc:
                self.log.error("AstrBot 插件发送失败（%s）：%s", step.get("type"), exc)

    # ------------------------------------------------------------------ 列表
    def list_plugins(self) -> List[Dict[str, Any]]:
        out = []
        for info in self.plugins:
            item = {key: value for key, value in info.items() if key not in ("path",)}
            item["disabled"] = bool(self.disabled.get(info["name"], info.get("disabled")))
            item.setdefault("format", "AstrBot")
            out.append(item)
        return out

    def names(self) -> List[str]:
        return [item["name"] for item in self.plugins]
