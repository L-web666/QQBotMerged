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
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

FACE_TYPE_STICKERS = ("4", "6")
STICKER_TEXT = "[动画表情]"

FACE_TAG_RE = re.compile(r"<(faceType|face|emoji)\b([^>]*)>", re.IGNORECASE)
FACE_EXT_RE = re.compile(r'ext="([^"]*)"', re.IGNORECASE)
FACE_ID_RE = re.compile(r'faceId="?(\d+)"?', re.IGNORECASE)
FACE_TYPE_RE = re.compile(r'faceType="?(\d+)"?', re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
MENTION_RE = re.compile(r"<@!?([0-9A-Za-z_.\-]{4,80})>")
# 官方新格式的 @ 标记：<qqbot-at-user id="xxx" />、<qqbot-at-everyone />
# 收：用来把消息显示成可读的 @昵称；发：官方要求用它嵌入正文（旧协议 <@userid> 即将弃用）。
AT_USER_NEW_RE = re.compile(r"<qqbot-at-user\s+id=[\"']?([^\"'\s/>]+)[\"']?\s*/?>", re.IGNORECASE)
AT_EVERYONE_NEW_RE = re.compile(r"<qqbot-at-everyone\s*/?>", re.IGNORECASE)
# 群聊里 @全体成员 的旧标记
AT_ALL_LEGACY_RE = re.compile(r"<@!?all>", re.IGNORECASE)
# 官方 @ 标签模板（群聊/文字子频道，含文本的消息类型都支持）
AT_USER_TAG = '<qqbot-at-user id="{openid}" />'
# 私聊不支持 @ 标签：群里 @ 某人只在群聊生效（官方：群聊 & 文字子频道可用）
AT_EVERYONE_TAG = "<qqbot-at-everyone />"

# ---------------------------------------------------------------- 指令标签（仅 markdown）
# 官方「指令操作」：
#   <qqbot-cmd-enter text="xxx" />                          用户点击后**直接发送** text
#   <qqbot-cmd-input text="xxx" show="xxx" reference="false" /> 用户点击后**插入输入框**
# text / show 必填或选填，最长 100 字符，且**传值前必须 urlencode**；
# 客户端展示为可点击的「/回车指令」「/参数指令」标签。
# 注意：群聊与文字子频道**都不支持回车指令**（官方原文），仅 markdown 消息能用。
CMD_TEXT_MAX = 100
CMD_ENTER_TEMPLATE = '<qqbot-cmd-enter text="{text}" />'
CMD_INPUT_TEMPLATE = '<qqbot-cmd-input text="{text}"{show}{reference} />'
CMD_TAG_RE = re.compile(r"<qqbot-cmd-(enter|input)\b([^>]*?)/?>", re.IGNORECASE)
CMD_TEXT_ATTR_RE = re.compile(r'text=["\']([^"\']*)["\']', re.IGNORECASE)
CMD_SHOW_ATTR_RE = re.compile(r'show=["\']([^"\']*)["\']', re.IGNORECASE)
CMD_REFERENCE_ATTR_RE = re.compile(r'reference=["\']?([^"\'\s/>]+)', re.IGNORECASE)
# 自动改用 Markdown 时写给用户的说明（`@` / 指令标签都只在 markdown 里生效）
AUTO_MARKDOWN_NOTE = "检测到{what}（该标签只在 Markdown 消息里生效），已自动改用 Markdown 发送"

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
    """给 AI 看的纯文本：表情标记转文本，@ 转昵称，指令标签转可读文字。"""
    text, _ = extract_face_info(content or "")
    text = mention_markup_to_text(text, mentions)
    return cmd_markup_to_text(text).strip()


# ---------------------------------------------------------------------- 发送侧 @
def build_at_user(openid: str) -> str:
    """官方「@某人」标签（群聊 & 文字子频道可用，文本/markdown 消息都能嵌）。

    协议：嵌入正文 `<qqbot-at-user id="用户openid" />`，客户端会渲染成 @用户 标签。
    私聊不支持，调用方需要自己判断场景。
    """
    value = str(openid or "").strip()
    return AT_USER_TAG.format(openid=value) if value else ""


def at_user_ids(text: str) -> List[str]:
    """取出正文里所有「@某人」的 openid（去重、保持出现顺序）。"""
    out: List[str] = []
    for match in AT_USER_NEW_RE.finditer(text or ""):
        value = str(match.group(1) or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def normalize_outgoing_at(text: str) -> str:
    """把即将弃用的 @ 写法换成官方新格式（只处理**要发出去**的正文）。

    - `<@openid>` / `<@!openid>` → `<qqbot-at-user id="openid" />`；
    - `<@all>` / `<@!all>` / `@everyone` → 纯文本 `@全体成员`
      （官方：@全部成员**仅文字子频道可用**，群聊里发真标签不生效，这里退化成可读文字）；
    - 已经是新格式的原样保留；重复调用结果不变（幂等）。
    """
    text = text or ""
    if not text:
        return text
    text = AT_ALL_LEGACY_RE.sub("@全体成员", text)
    text = re.sub(r"(?<![\w@])@everyone\b", "@全体成员", text)
    return MENTION_RE.sub(lambda match: build_at_user(match.group(1)), text)


# ---------------------------------------------------------------- 发送侧：指令标签
def encode_cmd_value(value: str) -> str:
    """按官方要求给指令标签的 `text` / `show` 做 urlencode。

    用 `quote(..., safe="")` 全编码：`&`、`=`、空格都会变成 `%xx`，
    不会把标签属性结构弄乱（官方原文：「传值时需要 urlencode」）。
    """
    return quote(str(value or ""), safe="")


def decode_cmd_value(value: str) -> str:
    """反向解析标签里的值（容错：解不开或没编码过时原样返回）。"""
    text = str(value or "")
    if not text:
        return ""
    try:
        return unquote(text)
    except Exception:                                   # noqa: BLE001 极少见，不因此报错
        return text


def _cmd_value(value: str, limit: int = CMD_TEXT_MAX) -> str:
    """整理一个指令值：去首尾空白并按官方 100 字符上限截断。"""
    return str(value or "").strip()[:max(1, int(limit or CMD_TEXT_MAX))]


def build_cmd_enter(text: str) -> str:
    """生成「回车指令」标签：用户点击后**直接发送** `text`（官方：仅 markdown）。

    `text` 为空时返回空串（不生成半截标签，避免发出去是一堆乱码）。
    """
    value = _cmd_value(text)
    if not value:
        return ""
    return CMD_ENTER_TEMPLATE.format(text=encode_cmd_value(value))


def build_cmd_input(text: str, show: str = "", reference: bool = False) -> str:
    """生成「参数指令」标签：用户点击后把 `text` **插入输入框**自行编辑发送。

    - `show` 是用户在消息里看到的文字（选填，官方默认取 `text`；与 text 相同时不重复写）；
    - `reference=True` 时带上消息原文引用（官方默认 false，只写 true）。
    """
    value = _cmd_value(text)
    if not value:
        return ""
    shown = _cmd_value(show)
    show_attr = "" if (not shown or shown == value) else f' show="{encode_cmd_value(shown)}"'
    reference_attr = ' reference="true"' if reference else ""
    return CMD_INPUT_TEMPLATE.format(text=encode_cmd_value(value), show=show_attr,
                                     reference=reference_attr)


def read_cmd_tag(text: str) -> Dict[str, Any]:
    """解析**一个**指令标签，返回 `{kind, text, show, reference}`（值已解码）。

    不是指令标签时返回空字典；`kind` 为 `enter` / `input`。
    """
    match = CMD_TAG_RE.search(str(text or ""))
    if not match:
        return {}
    attrs = match.group(2) or ""
    text_match = CMD_TEXT_ATTR_RE.search(attrs)
    show_match = CMD_SHOW_ATTR_RE.search(attrs)
    ref_match = CMD_REFERENCE_ATTR_RE.search(attrs)
    value = decode_cmd_value(text_match.group(1)) if text_match else ""
    shown = decode_cmd_value(show_match.group(1)) if show_match else value
    reference = bool(ref_match) and str(ref_match.group(1)).strip().lower() in ("1", "true", "yes")
    return {"kind": match.group(1).lower(), "text": value, "show": shown,
            "reference": reference, "raw": match.group(0)}


def cmd_tags(text: str) -> List[str]:
    """取出正文里的指令标签类型（按出现顺序，含重复，例如 `["enter", "input"]`）。"""
    return [match.group(1).lower() for match in CMD_TAG_RE.finditer(str(text or ""))]


def cmd_markup_to_text(text: str) -> str:
    """把指令标签换成可读文字（展示 / 给 AI 看）：

    - `<qqbot-cmd-enter text="%2Fhelp" />` → `/回车指令 /help`
    - `<qqbot-cmd-input text="%2Fban" show="封禁" />` → `/参数指令 封禁`
    """
    if not text or "<qqbot-cmd" not in str(text).lower():
        return text or ""

    def repl(match: "re.Match") -> str:
        info = read_cmd_tag(match.group(0))
        label = "/回车指令" if info.get("kind") == "enter" else "/参数指令"
        value = info.get("show") or info.get("text") or ""
        return f"{label} {value}".strip()

    return CMD_TAG_RE.sub(repl, str(text))


def markdown_only_tags(text: str) -> List[str]:
    """列出正文里「只在 markdown 消息里生效」的标签名字（用于自动改用 markdown）。

    返回可读名字的列表（去重、固定顺序）：`@某人`、`指令标签`。
    官方文档里 `@某人` 虽然写了文本消息也能用，但实测**只有 markdown 消息会被渲染**，
    所以这里把带 `@`/指令标签的文本消息自动升级成 markdown（见 `Runtime.send_text`）。
    """
    value = str(text or "")
    parts: List[str] = []
    if at_user_ids(value) or MENTION_RE.search(value):
        parts.append("@某人")
    if cmd_tags(value):
        parts.append("指令标签")
    return parts


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
    text = cmd_markup_to_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    if images and not text:
        return "[图片]"
    if len(text) > limit:
        return text[:limit] + "…"
    return text or ("[图片]" if images else "")
