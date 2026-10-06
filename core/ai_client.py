# -*- coding: utf-8 -*-
"""AI 客户端（OpenAI 兼容接口）

来源：原 `API_qqbot/core/ai_client.py`，并补上：
- 多模态图片识别（把收到的图片交给视觉模型描述）；
- 配置热更新（api_key / base_url / model / 温度 / 超时均可随时改）；
- 输出清理（去掉思考过程与 Markdown 符号）；
- 未配置时明确“不可用”，由上层决定兜底回复。
"""

import base64
import json
import logging
import re
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.IGNORECASE | re.DOTALL)
CODE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)


class AIClient:
    """一个 OpenAI 兼容的对话客户端（全局共用一份配置）。"""

    def __init__(self, config: Dict[str, Any] = None, logger_obj: logging.Logger = None):
        self.log = logger_obj or logger
        self.config: Dict[str, Any] = dict(config or {})
        self.api_key = self.config.get("api_key", "") or ""
        self.base_url = (self.config.get("base_url", "") or "").rstrip("/")
        self.model = self.config.get("model", "") or ""
        self.enabled = bool(self.config.get("enabled", True))
        self.available = bool(self.api_key and self.base_url and self.model)
        self.session = requests.Session()
        self._warned = False
        self.last_error = ""

    # ------------------------------------------------------------------ 状态
    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.api_key and self.base_url and self.model)

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "usable": self.usable,
            "model": self.model,
            "base_url": self.base_url,
            "has_key": bool(self.api_key),
            "last_error": self.last_error,
        }

    def update_config(self, config: Dict[str, Any]):
        """热更新配置。"""
        self.config = dict(config or {})
        self.api_key = self.config.get("api_key", "") or ""
        self.base_url = (self.config.get("base_url", "") or "").rstrip("/")
        self.model = self.config.get("model", "") or ""
        self.enabled = bool(self.config.get("enabled", True))
        self.available = bool(self.api_key and self.base_url and self.model)
        self._warned = False

    # ------------------------------------------------------------------ 请求
    def _endpoint(self) -> str:
        base = self.base_url or ""
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    @property
    def temperature(self) -> float:
        try:
            return float(self.config.get("temperature", 0.8))
        except (TypeError, ValueError):
            return 0.8

    @property
    def max_tokens(self) -> int:
        try:
            return int(self.config.get("max_tokens", 1024))
        except (TypeError, ValueError):
            return 1024

    @property
    def timeout(self) -> float:
        try:
            return float(self.config.get("timeout_seconds", 60))
        except (TypeError, ValueError):
            return 60.0

    def chat(self, messages: List[Dict[str, Any]], **overrides) -> Optional[str]:
        """调用对话接口，返回文本；失败返回 None（错误写入 last_error）。"""
        if not self.usable:
            self.last_error = "内置 AI 未启用或未配置完整"
            if not self._warned:
                self.log.debug("内置 AI 不可用：%s", self.last_error)
                self._warned = True
            return None
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": overrides.pop("temperature", self.temperature),
            "max_tokens": overrides.pop("max_tokens", self.max_tokens),
            "stream": False,
        }
        payload.update(overrides)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self.session.post(self._endpoint(), headers=headers, json=payload,
                                         timeout=self.timeout)
        except Exception as exc:
            self.last_error = f"请求异常: {exc}"
            self.log.error("AI 请求异常: %s", exc)
            return None
        if response.status_code != 200:
            self.last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            self.log.error("AI 请求失败: %s", self.last_error)
            return None
        try:
            result = response.json()
        except ValueError:
            self.last_error = f"响应不是合法 JSON: {response.text[:200]}"
            self.log.error("AI 响应解析失败: %s", self.last_error)
            return None
        self.last_error = ""
        return self._extract_text(result)

    @staticmethod
    def _extract_text(result: Dict[str, Any]) -> Optional[str]:
        try:
            choices = result.get("choices") or []
            if not choices:
                return None
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text") or "")
                    elif isinstance(item, str):
                        parts.append(item)
                content = "".join(parts)
            if not content:
                content = choices[0].get("text") or ""
            return str(content).strip() or None
        except Exception:
            return None

    # ------------------------------------------------------------------ 多模态
    def chat_with_images(self, messages: List[Dict[str, Any]], image_urls: List[str],
                         prompt: str = "") -> Optional[str]:
        """带图片的对话（图片可以是公网 URL 或 data URL）。"""
        if not image_urls:
            return self.chat(messages)
        if not self.config.get("vision_enabled", True):
            return self.chat(messages)
        content: List[Dict[str, Any]] = []
        text = prompt or "请用中文描述这张图片的内容。"
        content.append({"type": "text", "text": text})
        for url in image_urls[:4]:
            if not url:
                continue
            content.append({"type": "image_url", "image_url": {"url": url}})
        if len(content) == 1:
            return self.chat(messages)
        vision_messages = [msg for msg in messages if msg.get("role") == "system"]
        vision_messages.append({"role": "user", "content": content})
        text_result = self.chat(vision_messages)
        if text_result:
            return text_result
        # 视觉调用失败：退回纯文本对话（至少把用户的文字答上）
        self.log.warning("多模态调用失败（%s），退回纯文本", self.last_error)
        return self.chat(messages)

    def describe_image(self, url: str, prompt: str = "") -> Optional[str]:
        """单独描述一张图片（用于自动为收到的图片生成说明）。"""
        messages = [{"role": "user", "content": prompt or self.config.get("vision_prompt")
                     or "请用中文简要描述这张图片的内容。"}]
        return self.chat_with_images(messages, [url], prompt or self.config.get("vision_prompt")
                                     or "请用中文简要描述这张图片的内容。")

    def image_to_data_url(self, blob: bytes, mime: str = "image/png") -> str:
        return f"data:{mime};base64,{base64.b64encode(blob).decode()}"

    # ------------------------------------------------------------------ 输出清理
    @staticmethod
    def filter_thinking(text: str) -> str:
        """去掉模型输出的思考/推理块。"""
        if not text:
            return text
        return THINK_RE.sub("", text).strip()

    @staticmethod
    def extract_code_blocks(text: str) -> List[str]:
        return [block.strip() for block in CODE_BLOCK_RE.findall(text or "")]


def load_ai_config(config) -> Dict[str, Any]:
    """从统一配置里取出 AI 配置（供热更新与初始化复用）。"""
    return {
        "enabled": config.bool_of("ai", "enabled", default=True),
        "api_key": config.str_of("ai", "api_key", default=""),
        "base_url": config.str_of("ai", "base_url", default=""),
        "model": config.str_of("ai", "model", default=""),
        "system_prompt": config.str_of("ai", "system_prompt", default="你是一个智能助手。"),
        "temperature": config.float_of("ai", "temperature", default=0.8),
        "max_tokens": config.int_of("ai", "max_tokens", default=1024),
        "timeout_seconds": config.int_of("ai", "timeout_seconds", default=60),
        "vision_enabled": config.bool_of("ai", "vision_enabled", default=True),
        "vision_prompt": config.str_of("ai", "vision_prompt", default="请用中文描述这张图片的内容。"),
        "no_ai_reply": config.str_of("ai", "no_ai_reply", default=""),
    }
