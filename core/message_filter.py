# -*- coding: utf-8 -*-
"""关键词回复与消息过滤

来源：原 `API_qqbot/core/message_filter.py`（精确/模糊关键词、无意义消息过滤），
并按合并版配置结构（`filters.*`）重新组织；限速逻辑放在消息处理器里（需要按用户维度）。
"""

import logging
import re
from typing import Any, Dict, Optional

from core.message_text import looks_meaningless

logger = logging.getLogger(__name__)


class MessageFilter:
    """关键词匹配与无意义消息判断。"""

    def __init__(self, config: Dict[str, Any] = None, logger_obj: logging.Logger = None):
        self.log = logger_obj or logger
        self.update(config or {})

    def update(self, config: Dict[str, Any]):
        self.enabled = bool(config.get("keywords_enabled", True))
        self.exact_responses: Dict[str, str] = {
            str(key).strip(): str(value)
            for key, value in (config.get("exact_match_responses") or {}).items()
            if str(key).strip()
        }
        self.fuzzy_responses: Dict[str, str] = {
            str(key).strip(): str(value)
            for key, value in (config.get("fuzzy_match_responses") or {}).items()
            if str(key).strip()
        }
        self.filter_meaningless = bool(config.get("filter_meaningless", True))
        self.sensitive_enabled = bool(config.get("sensitive_enabled", False))
        self.sensitive_words = [str(word).strip() for word in (config.get("sensitive_list") or [])
                                if str(word).strip()]
        self.sensitive_replacement = str(config.get("sensitive_replacement") or "***")
        self.sensitive_block_input = bool(config.get("sensitive_block_input", False))

    # ------------------------------------------------------------------ 关键词
    def match_keyword(self, content: str) -> Optional[str]:
        """返回关键词命中的回复内容（精确优先，其次模糊），都不命中返回 None。"""
        if not self.enabled or not content:
            return None
        text = content.strip()
        if text in self.exact_responses:
            return self.exact_responses[text]
        for keyword, response in self.fuzzy_responses.items():
            if keyword in text:
                return response
        return None

    # ------------------------------------------------------------------ 敏感词
    def contains_sensitive(self, text: str) -> bool:
        if not self.sensitive_enabled or not text or not self.sensitive_words:
            return False
        return any(word in text for word in self.sensitive_words)

    def mask_sensitive(self, text: str) -> str:
        if not self.sensitive_enabled or not text or not self.sensitive_words:
            return text
        out = text
        for word in self.sensitive_words:
            if word:
                out = out.replace(word, self.sensitive_replacement)
        return out

    # ------------------------------------------------------------------ 无意义
    def is_meaningless(self, content: str) -> bool:
        if not self.filter_meaningless:
            return False
        return looks_meaningless(content)

    # ------------------------------------------------------------------ 限速
    @staticmethod
    def parse_duration(text: str) -> int:
        """解析形如 10m / 2h / 600 的时长（返回秒），解析失败返回 0。"""
        match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(text or ""))
        if not match:
            return 0
        value = int(match.group(1))
        unit = match.group(2)
        return value * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
