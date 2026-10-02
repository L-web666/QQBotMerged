# -*- coding: utf-8 -*-
"""消息文本处理：QQ 表情标记、@提及、引用预览。

来源：`app/message_store.py` 的表情/提及处理（这部分逻辑经过实践检验，逻辑保留、接口收敛）。
QQ 的表情包在消息里是形如 `<faceType=4 faceId="0" ext="base64...">` 的标记，
其中 `ext` 是 base64 编码的 JSON，里面通常带表情名称与图片地址。
"""

import base64
import json
import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

FACE_TYPE_STICKERS = ("4", "6")
STICKER_TEXT = "[动画表情]"

FACE_TAG_RE = re.compile(r"<(faceType|face|emoji)\b([^>]*)>", re.IGNORECASE)
FACE_EXT_RE = re.compile(r'ext="([^"]*)"', re.IGNORECASE)
FACE_ID_RE = re.compile(r'faceId="?(\d+)"?', re.IGNORECASE)
FACE_TYPE_RE = re.compile(r'faceType="?(\d+)"?', re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
MENTION_RE = re.compile(r"<@!?([0-9A-Za-z_.\-]{4,80})>")
# 官方新格式的 @ 标记（部分平台/客户端会下发）：<qqbot-at-user id="xxx" />、<qqbot-at-everyone />
# 只用于**把收到的消息显示成可读的 @昵称**，不参与发送。
AT_USER_NEW_RE = re.compile(r"<qqbot-at-user\s+id=[\"']?([^\"'\s/>]+)[\"']?\s*/?>", re.IGNORECASE)
AT_EVERYONE_NEW_RE = re.compile(r"<qqbot-at-everyone\s*/?>", re.IGNORECASE)
# 群聊里 @全体成员 的旧标记
AT_ALL_LEGACY_RE = re.compile(r"<@!?all>", re.IGNORECASE)
IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp)(?:[?#]|$)", re.IGNORECASE)

_name_resolver = None


def set_name_resolver(fn):
    """注入 openid → 昵称 的解析器（由运行时提供）。"""
    global _name_resolver
    _name_resolver = fn


def resolve_display_name(openid: str) -> str:
    if not openid or not _name_resolver:
        return ""
    try:
        return _name_resolver(openid) or ""
    except Exception:
        return ""


def _decode_face_ext_info(raw_b64: str) -> Dict[str, str]:
    """解码表情标记里的 ext 字段（base64 JSON），取出显示文本与图片地址。"""
    if not raw_b64:
        return {}
    text = raw_b64.strip().replace("\n", "")
    padding = "=" * (-len(text) % 4)
    try:
        raw = base64.b64decode(text + padding, validate=False)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        decoded = raw.decode("utf-8", "ignore")
    except Exception:
        return {}

    result: Dict[str, str] = {}
    try:
        payload = json.loads(decoded)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("text", "summary", "desc", "name", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                result["text"] = value.strip()
                break
        for key in ("url", "image", "image_url", "src", "icon"):
            value = payload.get(key)
            if isinstance(value, str) and value.lower().startswith("http"):
                result["url"] = value
                break
        if "text" not in result:
            for value in payload.values():
                if isinstance(value, str) and value.strip() and not value.lower().startswith("http"):
                    result["text"] = value.strip()
                    break
    if "url" not in result:
        found = URL_RE.search(decoded)
        if found:
            result["url"] = found.group(0)
    if "text" not in result and not result.get("url"):
        plain = decoded.strip()
        if plain and not plain.startswith("{"):
            result["text"] = plain[:60]
    return result


def extract_face_info(content: str) -> Tuple[str, List[str]]:
    """把表情标记换成可读文本，并收集表情图片地址。"""
    if not content or "<" not in content:
        return content or "", []
    images: List[str] = []

    def repl(match: "re.Match") -> str:
        attrs = match.group(2) or ""
        ext_match = FACE_EXT_RE.search(attrs)
        info = _decode_face_ext_info(ext_match.group(1)) if ext_match else {}
        if info.get("url"):
            images.append(info["url"])
        if info.get("text"):
            return info["text"]
        type_match = FACE_TYPE_RE.search(attrs)
        if type_match and type_match.group(1) in FACE_TYPE_STICKERS:
            return STICKER_TEXT
        id_match = FACE_ID_RE.search(attrs)
        if id_match and id_match.group(1).isdigit() and int(id_match.group(1)) > 0:
            return f"[表情{id_match.group(1)}]"
        return "[表情]"

    return FACE_TAG_RE.sub(repl, content), images


def is_pure_face_markup(content: str) -> bool:
    """内容是否只有表情标记（没有实际文字）。"""
    if not content:
        return False
    return not FACE_TAG_RE.sub("", content).strip()


def face_markup_to_text(content: str) -> str:
    return extract_face_info(content)[0]


def mention_markup_to_text(content: str, mentions: List[Dict[str, Any]] = None) -> str:
    """把 @ 标记换成 @昵称。

    兼容三种写法：
    - 官方新格式：`<qqbot-at-user id="..." />`、`<qqbot-at-everyone />`；
    - 群聊旧格式：`<@openid>`、`<@!openid>`、`<@all>`；
    - 频道旧格式：`@everyone`。
    """
    if not content:
        return content or ""
    index = {}
    for item in mentions or []:
        if isinstance(item, dict) and item.get("openid"):
            index[str(item["openid"])] = item

    def label(openid: str) -> str:
        item = index.get(str(openid))
        if item and (item.get("is_you") or item.get("is_bot")):
            return item.get("username") or resolve_display_name(openid) or "机器人"
        if item and item.get("username"):
            return str(item["username"])
        return resolve_display_name(openid) or "某人"

    text = content
    # 官方新格式
    text = AT_EVERYONE_NEW_RE.sub("@全体成员", text)
    text = AT_USER_NEW_RE.sub(lambda match: "@" + label(match.group(1)), text)
    # 旧写法
    text = AT_ALL_LEGACY_RE.sub("@全体成员", text)
    text = re.sub(r"@everyone\b", "@全体成员", text)
    if "<@" not in text:
        return text
    return MENTION_RE.sub(lambda match: "@" + label(match.group(1)), text)


def format_for_ai(content: str, mentions: List[Dict[str, Any]] = None) -> str:
    """给 AI 看的纯文本：表情标记转文本，@ 转昵称。"""
    text, _ = extract_face_info(content or "")
    return mention_markup_to_text(text, mentions).strip()


def strip_markdown(text: str) -> str:
    """去掉 AI 输出里的 Markdown 符号，让 QQ 里显示更干净。"""
    if not text:
        return text
    out = text
    out = re.sub(r"```[a-zA-Z0-9_+-]*\n?", "", out)
    out = out.replace("```", "")
    out = re.sub(r"`([^`]*)`", r"\1", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"\1", out)
    out = re.sub(r"__([^_]+)__", r"\1", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", out)
    out = re.sub(r"^#{1,6}\s*", "", out, flags=re.MULTILINE)
    out = re.sub(r"^\s*[-*+]\s+", "· ", out, flags=re.MULTILINE)
    out = re.sub(r"^\s*>\s?", "", out, flags=re.MULTILINE)
    out = re.sub(r"^\s*\|.*\|\s*$", lambda m: " | ".join(
        cell.strip() for cell in m.group(0).strip().strip("|").split("|") if cell.strip()), out, flags=re.MULTILINE)
    out = re.sub(r"^\s*[-:| ]{4,}\s*$", "", out, flags=re.MULTILINE)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1（\2）", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def split_message(text: str, max_length: int = 2000) -> List[str]:
    """把过长文本按段落/句子切成多条。"""
    text = text or ""
    limit = max(100, int(max_length or 2000))
    if len(text) <= limit:
        return [text] if text else []
    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for separator in ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", " "):
            position = window.rfind(separator)
            if position > limit * 0.4:
                cut = position + len(separator)
                break
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk]


def looks_meaningless(text: str) -> bool:
    """判断是不是无意义消息（纯数字/标点/单字/纯表情）。"""
    stripped = (text or "").strip()
    if not stripped:
        return True
    plain = FACE_TAG_RE.sub("", stripped)
    plain = MENTION_RE.sub("", plain)
    plain = re.sub(r"\s+", "", plain)
    if not plain:
        return True
    if re.fullmatch(r"[\d\W_]+", plain):
        return True
    if len(plain) <= 1:
        return True
    # 单字符重复刷屏（哈哈哈 除外）
    if len(plain) >= 4 and len(set(plain)) == 1:
        return True
    return False


def preview(content: str, limit: int = 60) -> str:
    text, images = extract_face_info(content or "")
    text = re.sub(r"\s+", " ", text).strip()
    if images and not text:
        return "[图片]"
    if len(text) > limit:
        return text[:limit] + "…"
    return text or ("[图片]" if images else "")
