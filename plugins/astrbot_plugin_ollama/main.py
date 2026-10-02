"""
Ollama 本地 AI 插件 - 默认用本地 Ollama 回答用户问题，不消耗 API 额度
====================================================================
用户直接提问（如 "你好"、"写首诗"），机器人就调用本机 Ollama 的本地模型回答。

依赖：
  1. 装好 Ollama 并启动
  2. 拉取至少一个模型，例如：ollama pull qwen2.5
  3. 确认 Ollama 能访问（默认 http://127.0.0.1:11434）

配置：插件目录下的 `_conf_schema.json` 定义，用户在网页「插件管理」/ WebUI 里填，
      实际保存在 data/config/astrbot_plugin_ollama_config.json。

`force_takeover` 默认为 **false**：只有在内置 AI 不可用时才接管普通消息
（true = 所有普通消息都由本地 Ollama 回答，内置 AI 不再生效）。
"""

import asyncio
import collections
import threading

import requests

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_ollama"

DEFAULT_CONFIG = {
    "host": "http://127.0.0.1:11434",
    "model": "qwen2.5",
    "think": False,
    "history": 6,
    "timeout": 300,
    "force_takeover": False,
    "max_sessions": 200,
    "temperature": 0.7,
    "system_prompt": "你是一个乐于助人的 AI 助手，请用简洁清晰的中文回答问题。",
}


def _normalize_host(h) -> str:
    h = str(h or "").strip().rstrip("/")
    if not h:
        return DEFAULT_CONFIG["host"]
    if h.isdigit():
        h = "127.0.0.1:" + h
    if "://" not in h:
        h = "http://" + h
    body = h.split("://", 1)[1]
    if "/" not in body and ":" not in body:
        h += ":11434"
    return h


def _as_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v if v is not None else "").strip().lower()
    if s in ("1", "true", "yes", "on", "enable", "enabled", "是"):
        return True
    if s in ("0", "false", "no", "off", "disable", "disabled", "否"):
        return False
    return default


def _as_think(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("none", "null", "-", ""):
        return None
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


class OllamaPlugin(Star):
    def __init__(self, context: Context, config=None):
        """官方签名：`(self, context, config)`；config 即本插件的 AstrBotConfig。"""
        super().__init__(context)
        self._histories = collections.OrderedDict()
        self._hist_lock = threading.Lock()
        self._gen_lock = threading.Lock()
        self._think_ok = True
        self.config = config if config is not None else self._context_config()
        self._cfg = dict(DEFAULT_CONFIG)
        self._load_config()

    def _context_config(self):
        """拿不到构造参数时（个别宿主）退回到 context 上的插件配置。"""
        try:
            return self.context.get_config() or {}
        except Exception:
            return {}

    def _load_config(self):
        cfg = self.config if isinstance(self.config, dict) else {}
        c = dict(DEFAULT_CONFIG)
        for key in DEFAULT_CONFIG:
            if key in cfg and cfg[key] is not None:
                c[key] = cfg[key]
        self._cfg = {
            "host": _normalize_host(c["host"]),
            "model": str(c["model"] or "").strip() or DEFAULT_CONFIG["model"],
            "think": _as_think(c["think"], DEFAULT_CONFIG["think"]),
            "history": max(0, int(c.get("history", 6) or 0)),
            "timeout": max(5, int(c.get("timeout", 300) or 300)),
            "force_takeover": _as_bool(c.get("force_takeover", False), False),
            "max_sessions": max(0, int(c.get("max_sessions", 200) or 0)),
            "temperature": min(2.0, max(0.0, float(c.get("temperature", 0.7) or 0.7))),
            "system_prompt": str(c.get("system_prompt") or DEFAULT_CONFIG["system_prompt"]),
        }

    def _builtin_ai_usable(self) -> bool:
        """内置 AI 是否可用：`force_takeover=false` 时据此决定要不要让给核心。

        两条路都试：AstrBot 官方的 `context.get_config()`（全局配置，里面有 ai 段），
        以及宿主 provider 暴露的 `usable`（本程序 QQBotMerged 的实现）。
        """
        try:
            cfg = self.context.get_config() or {}
            ai = cfg.get("ai") if isinstance(cfg, dict) else None
            if isinstance(ai, dict) and ai.get("enabled", True) and all(
                    str(ai.get(key, "") or "").strip()
                    for key in ("api_key", "base_url", "model")):
                return True
        except Exception:
            pass
        try:
            provider = self.context.get_using_provider()
        except Exception:
            provider = None
        usable = getattr(provider, "usable", None)
        if usable is not None:
            return bool(usable)
        try:
            client = getattr(provider, "_client")()
        except Exception:
            client = None
        return bool(getattr(client, "usable", False))

    def _ask_ollama_locked(self, messages):
        """串行化本地模型调用（同一个 Ollama 同时跑多个请求会互相拖慢）。"""
        with self._gen_lock:
            return self._ask_ollama(messages)

    def _ask_ollama(self, messages):
        host = self._cfg["host"]
        model = self._cfg["model"]
        think = self._cfg["think"]
        timeout = self._cfg["timeout"]
        url = host.rstrip("/") + "/api/chat"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self._cfg["temperature"]},
        }
        if think is not None and self._think_ok:
            payload["think"] = bool(think)
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            if resp.status_code == 400 and "think" in payload and "think" in resp.text.lower():
                self._think_ok = False
                payload.pop("think", None)
                resp = requests.post(url, json=payload, timeout=timeout)
            if resp.status_code != 200:
                return None, f"Ollama 返回错误 {resp.status_code}: {resp.text[:200]}"
            data = resp.json()
            msg = data.get("message") or {}
            text = (msg.get("content") or "").strip()
            if not text:
                if (msg.get("thinking") or "").strip():
                    return None, "模型只输出了思考内容；建议把 think 设为 false"
                return None, "Ollama 没有返回内容"
            return text, None
        except requests.exceptions.ConnectionError:
            return None, f"连接不上 Ollama（当前地址 {host}）。请检查 host 配置。"
        except requests.exceptions.ReadTimeout:
            return None, f"Ollama 响应超时（{timeout}s，模型 {model}）"
        except Exception as e:
            return None, f"调用 Ollama 出错: {e}"

    def _get_history(self, uid):
        with self._hist_lock:
            msgs = self._histories.get(uid)
            if msgs is None:
                return []
            self._histories.move_to_end(uid)
            return list(msgs)

    def _save_history(self, uid, messages):
        with self._hist_lock:
            keep = self._cfg["history"]
            self._histories[uid] = messages[-keep * 2:] if keep > 0 else []
            self._histories.move_to_end(uid)
            limit = self._cfg["max_sessions"]
            if limit > 0:
                while len(self._histories) > limit:
                    self._histories.popitem(last=False)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """处理所有普通消息（非指令）"""
        content = event.message_str.strip()
        if not content:
            return
        if content.startswith("/"):
            return

        # 非强制模式：内置 AI 可用时让给内置 AI
        if not self._cfg["force_takeover"] and self._builtin_ai_usable():
            return

        uid = event.get_sender_id()
        history = self._get_history(uid)
        messages = [{"role": "system", "content": self._cfg["system_prompt"]}]
        messages.extend(history)
        messages.append({"role": "user", "content": content})

        logger.info(f"Ollama 回答({self._cfg['model']}): {content[:40]}")

        # 同步 requests 放到线程里跑，避免阻塞事件循环（整条消息链路都在这个循环上）
        text, err = await asyncio.to_thread(self._ask_ollama_locked, messages)

        if err:
            logger.error(f"Ollama 出错: {err}")
            yield event.plain_result(
                f"⚠️ {err}\n\n检查：1) Ollama 是否已启动  2) 模型名是否正确（ollama list 查看）"
            )
            return

        if not text:
            yield event.plain_result("⚠️ Ollama 没有返回内容，请稍后再试。")
            return

        if self._cfg["history"] > 0:
            history.append({"role": "user", "content": content})
            history.append({"role": "assistant", "content": text})
            self._save_history(uid, history)

        yield event.plain_result(text)

    async def terminate(self):
        """插件卸载/停用时清理会话记忆"""
        with self._hist_lock:
            self._histories.clear()