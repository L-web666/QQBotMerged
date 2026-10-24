# -*- coding: utf-8 -*-
"""收消息、广播、发送出口与网页视图（RuntimeMessagingMixin）。

本模块是 `core/runtime_pkg/` 包的一部分（由 `core/runtime.py` 拆分而来）；
由 `core/runtime_pkg/core.py` 里的 `Runtime` 组合使用。
"""

import copy
import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from core import media_store as media_module
from core import message_text
from core import paths
from core.ai_client import AIClient, load_ai_config
from core.context_manager import ContextManager
from core.gateway import DEFAULT_INTENTS, QQGateway
from core.group_manager import GroupManager, member_role as _member_role
from core.message_filter import MessageFilter
from core.plugin_manager import PluginBot, PluginManager
from core.qq_api import QQApiClient, MARKDOWN_FALLBACK_NOTE
from core.storage import (MemoryStore, SQLiteStore, conv_key_for, is_image_attachment,
                          normalize_attachments, preview_text)



class RuntimeMessagingMixin:
    """收消息、广播、发送出口与网页视图（RuntimeMessagingMixin）（被 `Runtime` 继承；不要单独实例化）。"""


    # ================================================================== 订阅（网页实时推送）
    def subscribe(self, callback: Callable[[Dict[str, Any]], None]):
        with self._sub_lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Dict[str, Any]], None]):
        with self._sub_lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def broadcast(self, event: Dict[str, Any]):
        with self._sub_lock:
            targets = list(self._subscribers)
        for callback in targets:
            try:
                callback(event)
            except Exception as exc:
                self.log.debug("推送事件失败: %s", exc)

    # ================================================================== 消息入口
    def on_gateway_message(self, message: Dict[str, Any]):
        """网关回调入口：先落库与留存，再分发到回复链路。"""
        try:
            self._handle_incoming(message)
        except Exception as exc:
            self.log.error("处理入站消息异常: %s", exc, exc_info=True)

    def _handle_incoming(self, message: Dict[str, Any]):
        if message.get("type") == "system":
            self.broadcast({"type": "system_event", "bot_id": message.get("bot_id"),
                            "event": message.get("event")})
            return

        bot_id = message.get("bot_id") or ""
        msg_type = message.get("type") or "private"
        group_openid = message.get("group_openid") or ""
        openid = message.get("openid") or ""
        conv_key = conv_key_for(bot_id, msg_type, group_openid, openid)
        message["conv_key"] = conv_key

        # 群消息里通常带昵称 → 记下来，私聊里就能显示同一个人的名字
        # （同一机器人在群聊与私聊看到的是同一个 openid）
        username = (message.get("username") or "").strip()
        if username and openid and msg_type in ("group", "channel"):
            try:
                self.store.set_user_name(openid, username)
            except Exception as exc:
                self.log.debug("记录群成员昵称失败: %s", exc)

        # 群成员身份：官方群消息的 author.member_role（owner/admin/member）每条都带，
        # 顺手写进群成员表 —— 群管理页的「身份」列才有真实值，不用等平台成员接口
        # （那个要机器人是管理员 + 白名单）。同一个人在新消息里出现了身份就以新的为准。
        self._remember_member_role(message, bot_id, msg_type, group_openid, openid)

        # 去重：同一条消息（同一机器人 + 同一 msg_id）只处理一次。
        # 触发场景：断线重连/会话恢复时平台重推、或（异常情况下）有多个实例在跑。
        msg_id = message.get("msg_id") or ""
        if msg_id and self.store.has_message(bot_id, msg_id):
            self.log.warning("忽略重复消息（bot=%s msg_id=%s），避免重复回复", bot_id, msg_id)
            self.stats.record("duplicates")
            return

        # 附件：先把 file_info 换成可下载地址，再立即留存
        attachments = self._resolve_attachment_urls(message, bot_id, msg_type, group_openid, openid)
        message["attachments"] = attachments

        media_urls = []
        if self.media.enabled:
            for attachment in attachments:
                url = attachment.get("url")
                if not url:
                    continue
                record = self.media.download(
                    url, file_name=attachment.get("file_name") or "",
                    source="received", bot_id=bot_id, conv_key=conv_key,
                    msg_id=message.get("msg_id") or "")
                if record and record.get("local_url"):
                    attachment["local_url"] = record["local_url"]
                    media_urls.append(record["local_url"])
                    self.stats.record("media_saved")
                elif record is None:
                    self.stats.record("media_failed")
        image_urls = [a.get("local_url") or a.get("url") for a in attachments
                      if media_module.is_probably_image(a.get("url") or "", a.get("content_type") or "")]
        message["media_local"] = media_urls[0] if media_urls else ""
        message["image_url"] = ""

        # 落库
        try:
            saved = self.store.add({
                "bot_id": bot_id,
                "direction": "in",
                "type": msg_type,
                "group_openid": group_openid,
                "openid": openid,
                "username": message.get("username") or "",
                "content": message.get("content") or "",
                "attachments": attachments,
                "quote": message.get("quote") or {},
                "mentions": message.get("mentions") or [],
                "msg_id": message.get("msg_id") or "",
                "msg_idx": message.get("msg_idx") or "",
                "raw_event": message.get("raw_event") or "",
            })
        except Exception as exc:
            self.log.error("消息落库失败: %s", exc)
            saved = None

        self.stats.record("messages")
        self.stats.record("messages_group" if msg_type == "group" else "messages_c2c")
        if attachments:
            self.stats.record("messages_with_file")

        # 附上本地留存地址，供网页显示
        message["_saved"] = saved
        message["_image_urls"] = image_urls
        self.broadcast({"type": "message", "conversation": conv_key,
                        "message": self.public_message(message, bot_id)})

        # 交给回复链路（AI 回复 / 关键词 / 插件）
        self.processor.submit(message)

    def _remember_member_role(self, message: Dict[str, Any], bot_id: str, msg_type: str,
                              group_openid: str, openid: str) -> None:
        """把群消息里带的成员身份存进群成员表（owner / admin / member）。

        QQ 官方群消息的 `author.member_role` 每条都带真实身份，而且不需要管理员权限，
        比群成员接口（要机器人是管理员 + 平台白名单）可靠得多。这里只做"顺带记录"：
        出现过就更新，没出现（私聊 / 老事件）就什么都不动，绝不把已知身份改回未知。
        """
        if msg_type not in ("group", "channel") or not group_openid:
            return
        role = _member_role(message.get("member_role"))
        if not openid:
            return
        # 同一条消息里 mentions 也可能带别人的身份（含机器人自己），一并记下
        batch = []
        if role:
            batch.append({
                "member_openid": openid,
                "username": (message.get("username") or "").strip(),
                "role": role,
            })
        for mention in message.get("mentions") or []:
            mention_role = _member_role((mention or {}).get("member_role"))
            mention_openid = str((mention or {}).get("openid") or "").strip()
            if not mention_role or not mention_openid:
                continue
            batch.append({
                "member_openid": mention_openid,
                "username": str((mention or {}).get("username") or "").strip(),
                "role": mention_role,
            })
        if not batch:
            return
        try:
            self.store.upsert_members(bot_id, group_openid, batch)
        except Exception as exc:
            self.log.debug("记录群成员身份失败: %s", exc)

    def _resolve_attachment_urls(self, message: Dict[str, Any], bot_id: str, msg_type: str,
                                 group_openid: str, openid: str) -> List[Dict[str, Any]]:
        """把只有 file_info 的附件换成真实下载地址（QQ 富媒体规范）。"""
        attachments = normalize_attachments(message.get("attachments"))
        client = self.get_client(bot_id)
        scope_openid = group_openid if msg_type == "group" else openid
        for attachment in attachments:
            if attachment.get("url") or not attachment.get("file_info"):
                continue
            if client is None:
                continue
            url = client.file_download_url("group" if msg_type == "group" else "c2c",
                                           scope_openid, attachment["file_info"])
            if url:
                attachment["url"] = url
        return attachments

    # 引用（quote）与撤回（recall）是两回事：
    #   · 撤回：官方限制 **2 分钟** 内（RECALL_WINDOW_SECONDS）；
    #   · 引用：没有 2 分钟限制 —— `msg_id` 只是"被动回复"字段，过期后平台会拒，
    #           但 `message_reference`（引用字段）仍可用于较早的消息。
    # 所以引用时按"被引用消息有多旧"选择字段，绝不能因为超过 2 分钟就不给引用。
    PASSIVE_REPLY_WINDOW_SECONDS = 300

    def _quote_reply_style(self, target_type: str, openid: str, bot_id: str,
                           reply_msg_id: str) -> str:
        """决定这次引用用哪种字段：both / message_reference / msg_id。

        - 找不到本地记录（不知新旧）→ 只用 `message_reference`（最稳，不受被动回复时限影响）
        - 被引用的是 5 分钟内的入站消息 → `both`（被动回复 + 引用，最贴近平台语义）
        - 否则 → 只用 `message_reference`
        """
        if not reply_msg_id:
            return ""
        record = None
        try:
            record = self.store.find_by_msg_id(reply_msg_id, bot_id=bot_id)
        except Exception:
            record = None
        if not record:
            return "message_reference"
        age = time.time() - float(record.get("ts") or 0)
        if (record.get("direction") or "") == "in" and age <= self.PASSIVE_REPLY_WINDOW_SECONDS:
            return "both"
        return "message_reference"

    def send_text(self, target_type: str, openid: str, content: str, bot_id: str = "",
                  reply_msg_id: str = "", direction: str = "out", extra: Dict[str, Any] = None,
                  fmt: str = "") -> Dict[str, Any]:
        """发送文本并落库/广播。返回 API 返回的数据（失败抛异常）。

        `fmt`：`""` = 按该机器人的 `send.message_format` 配置；显式传 `text`/`markdown` 则以此为准。

        **格式自动升级**：正文里有 `@某人` 标签或指令标签（`<qqbot-cmd-enter/>` 等）时，
        即使要求的是纯文本也会改用 Markdown 发送 —— 这两类标签实测只有 markdown 消息会被
        客户端渲染（纯文本发出去就是一串尖括号），说明写进返回值的 `_note` 里。

        markdown 被平台拒绝时客户端会自动降级为纯文本，降级原因同样在 `_note` 里。
        """
        client = self.get_client(bot_id)
        bot_id = self.pick_bot_id(bot_id)
        if client is None:
            raise RuntimeError("没有可用的机器人（请在「设置 → 机器人账号」里配置并启用）")
        cfg = self.bot_config(bot_id)
        # 发送侧 @：把即将弃用的 <@openid> 换成官方新标签 `<qqbot-at-user id="openid" />`，
        # 这样网页 / 定时任务 / 插件里写旧写法也能正常工作（幂等：已是新格式的原样保留）。
        content = message_text.normalize_outgoing_at(content or "")
        limit = cfg.int_of("send", "message_max_length", default=4000)
        if len(content or "") > limit:
            raise ValueError(f"文本过长（最多 {limit} 字）")
        requested = str(fmt or "").strip().lower()
        if requested not in ("text", "markdown"):
            requested = str(cfg.str_of("send", "message_format", default="text") or "text").strip().lower()
        if requested not in ("text", "markdown"):
            requested = "text"
        # @ 某人 / 指令标签只在 markdown 消息里生效 → 纯文本请求自动升级为 markdown
        auto_note = ""
        if requested == "text":
            need = message_text.markdown_only_tags(content)
            if need:
                requested = "markdown"
                auto_note = message_text.AUTO_MARKDOWN_NOTE.format(what="、".join(need))
        info = client.send_text("group" if target_type == "group" else "private",
                                openid, content, reply_msg_id,
                                reply_style=self._quote_reply_style(target_type, openid, bot_id,
                                                                    reply_msg_id),
                                fmt=requested)
        info = info or {}
        note = str(info.get("_note") or "")
        # 降级发生后，实际发出去的是纯文本
        used = "text" if (requested == "markdown" and MARKDOWN_FALLBACK_NOTE in note) else requested
        record_extra = dict(extra) if isinstance(extra, dict) else {}
        record_extra["format"] = used
        self._record_outgoing(bot_id, target_type, openid, content=content,
                              msg_id=info.get("id") or info.get("msg_id") or "",
                              msg_idx=info.get("msg_idx") or info.get("msgIdx") or "",
                              reply_to=reply_msg_id, extra=record_extra)
        self.stats.record("sent_text")
        result = dict(info)
        result["format"] = used
        if auto_note:
            result["auto_format"] = used == "markdown"
            note = "；".join(item for item in (note, auto_note) if item)
            if note:
                result["_note"] = note
        return result

    def send_image(self, target_type: str, openid: str, image_url: str = "", blob: bytes = None,
                   file_name: str = "image.png", content: str = "", bot_id: str = "",
                   reply_msg_id: str = "") -> Dict[str, Any]:
        """发送图片（支持本地字节流或链接）。"""
        client = self.get_client(bot_id)
        bot_id = self.pick_bot_id(bot_id)
        if client is None:
            raise RuntimeError("没有可用的机器人（请在「设置 → 机器人账号」里配置并启用）")
        scope = "group" if target_type == "group" else "private"
        reply_style = self._quote_reply_style(target_type, openid, bot_id, reply_msg_id)
        display_url = image_url
        if blob:
            record = self.media.save_bytes(blob, file_name=file_name, source="sent",
                                           bot_id=bot_id,
                                           conv_key=conv_key_for(bot_id, scope, openid, openid))
            display_url = record.get("local_url") or ""
            info = client.send_image_by_data(scope, openid, blob, file_name or "image.png",
                                             content, reply_msg_id, reply_style=reply_style)
        else:
            info, display_url = client.send_image_by_url(
                scope, openid, image_url, content, reply_msg_id, reply_style=reply_style,
                local_loader=self._load_local_media, remote_loader=self._load_remote_media)
        self._record_outgoing(bot_id, target_type, openid, content=content, image_url=display_url,
                              msg_id=(info or {}).get("id") or "", reply_to=reply_msg_id)
        self.stats.record("sent_image")
        return {"info": info, "image_url": display_url, "note": (info or {}).get("_note", "")}

    def send_file(self, target_type: str, openid: str, blob: bytes, file_name: str,
                  content: str = "", bot_id: str = "", reply_msg_id: str = "",
                  fallback_link: bool = True) -> Dict[str, Any]:
        """发送文件。

        QQ 机器人平台对“文件消息”的支持有限：先尝试平台接口，失败时（默认）退化为
        “把文件存到本地 + 发一条下载链接”的可读方案，并把失败原因一并返回。
        """
        client = self.get_client(bot_id)
        bot_id = self.pick_bot_id(bot_id)
        if client is None:
            raise RuntimeError("没有可用的机器人（请在「设置 → 机器人账号」里配置并启用）")
        conv_key = conv_key_for(bot_id, "group" if target_type == "group" else "private",
                                openid if target_type == "group" else "", 
                                "" if target_type == "group" else openid)
        record = self.media.save_bytes(blob, file_name=file_name, source="sent",
                                       bot_id=bot_id, conv_key=conv_key)
        local_url = record.get("local_url") or ""
        error_text = ""
        try:
            info = client.send_file("group" if target_type == "group" else "private",
                                    openid, blob, file_name, content, reply_msg_id,
                                    reply_style=self._quote_reply_style(
                                        target_type, openid, bot_id, reply_msg_id))
            # 文件内容不在文本里，若不补一句说明，聊天界面就会出现一个"空消息"
            # （用户反馈过：发文件时显示空消息）。这里记录成"📎 文件名"，
            # 并把文件作为附件一起存下来，界面就能显示出这个文件。
            shown = content or ""
            label = f"📎 {file_name}"
            record_text = (shown + "\n" + label) if shown else label
            self._record_outgoing(
                bot_id, target_type, openid, content=record_text,
                msg_id=(info or {}).get("id") or "", reply_to=reply_msg_id,
                extra={"file_name": file_name},
                attachments=[{
                    "url": "", "local_url": local_url, "file_name": file_name,
                    "content_type": record.get("content_type") or "",
                }])
            self.stats.record("sent_file")
            return {"info": info, "image_url": "", "file_url": local_url, "mode": "file",
                    "note": (info or {}).get("_note", "")}
        except Exception as exc:
            error_text = str(exc)
            self.log.warning("文件消息发送失败，将改用链接方式：%s", exc)
            if not fallback_link:
                raise
        # 退化：发文本链接（QQ 内可点开下载）
        link, link_note = self._public_file_link(local_url)
        text = (content + "\n" if content else "") + f"📎 文件：{file_name}\n{link}"
        client.send_text("group" if target_type == "group" else "private", openid, text, reply_msg_id)
        self._record_outgoing(bot_id, target_type, openid, content=text, reply_to=reply_msg_id,
                              extra={"file_name": file_name, "fallback": True})
        self.stats.record("sent_file")
        return {"info": {}, "image_url": "", "file_url": local_url, "link": link, "mode": "link",
                "note": f"平台不支持文件消息，已改为发送下载链接（{link_note}；"
                        f"原因：{error_text[:160]}）"}

    def _record_outgoing(self, bot_id: str, target_type: str, openid: str, content: str = "",
                         image_url: str = "", msg_id: str = "", msg_idx: str = "",
                         reply_to: str = "", extra: Dict[str, Any] = None,
                         attachments: List[Dict[str, Any]] = None):
        is_group = target_type == "group"
        conversation = conv_key_for(bot_id, "group" if is_group else "private",
                                    openid if is_group else "", "" if is_group else openid)
        message = {
            "bot_id": bot_id,
            "direction": "out",
            "type": "group" if is_group else "private",
            # 关键：私聊也要写 openid、群聊写 group_openid，
            # 否则机器人发的消息会落到一个 openid 为空的"幽灵会话"里，
            # 会话名会显示成"我"，和真实用户的会话分家。
            "conv_key": conversation,
            "group_openid": openid if is_group else "",
            "openid": "" if is_group else openid,
            "username": "我",
            "content": content,
            "image_url": image_url,
            "attachments": attachments or [],
            "msg_id": msg_id,
            "msg_idx": msg_idx,
            "reply_to": reply_to,
        }
        try:
            saved = self.store.add(message)
        except Exception as exc:
            self.log.error("发送记录落库失败: %s", exc)
            saved = None
        self.broadcast({"type": "message", "conversation": saved["conv_key"] if saved else "",
                        "message": self.public_message(message, bot_id)})
        return saved

    def public_message(self, message: Dict[str, Any], bot_id: str = "") -> Dict[str, Any]:
        """给网页的消息结构（补上渲染需要的一切字段）。"""
        content = message.get("content") or ""
        display, face_images = message_text.extract_face_info(content)
        display = message_text.mention_markup_to_text(display, message.get("mentions"))
        attachments = normalize_attachments(message.get("attachments"))
        # 只有**图片附件**的地址才能当 image_url：否则发一个 .exe/.py 文件时，
        # 前端会把文件地址当图片渲染，然后显示"图片加载失败"（用户反馈过这个错误提示）
        local_images = [item.get("local_url") for item in attachments
                        if item.get("local_url") and is_image_attachment(item)]
        image_url = message.get("image_url") or ""
        if not image_url and local_images:
            image_url = local_images[0]
        quote = message.get("quote") or {}
        if isinstance(quote, dict) and quote.get("content"):
            quote = dict(quote)
            quote["content_display"] = message_text.face_markup_to_text(quote["content"])
        if message_text.is_pure_face_markup(content) and (face_images or image_url):
            display = ""
        direction = message.get("direction") or "in"
        username = message.get("username") or ""
        # 收到的消息里，机器人自己的昵称"我"绝不能当作用户名显示
        if direction != "out" and username == "我":
            username = ""
        sender_name = ""
        if direction != "out":
            sender_name = (self.lookup_name(message.get("openid") or "")
                           or username or self.short_label(message.get("openid") or ""))
        return {
            "id": message.get("id"),
            "bot_id": bot_id or message.get("bot_id") or "",
            "conv_key": message.get("conv_key") or "",
            "type": message.get("type") or "private",
            "direction": direction,
            "group_openid": message.get("group_openid") or "",
            "openid": message.get("openid") or "",
            "username": username,
            "sender_name": sender_name,
            "content": content,
            "content_display": display,
            "content_images": face_images,
            "image_url": image_url,
            "media_local": message.get("media_local") or "",
            "attachments": attachments,
            "quote": quote,
            "mentions": message.get("mentions") or [],
            "msg_id": message.get("msg_id") or "",
            "msg_idx": message.get("msg_idx") or "",
            "reply_to": message.get("reply_to") or "",
            "time": message.get("time") or time.strftime("%Y-%m-%d %H:%M:%S",
                                                         time.localtime(message.get("ts") or time.time())),
            "ts": message.get("ts") or time.time(),
            "preview": preview_text(message),
        }

    def public_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self.public_message(item, item.get("bot_id") or "") for item in messages]

    def public_conversations(self, bot_id: str = "") -> List[Dict[str, Any]]:
        out = []
        groups = self.group_manager.group_names()
        for conv in self.store.conversations(bot_id=bot_id):
            item = dict(conv)
            group_openid = conv.get("group_openid") or ""
            openid = conv.get("openid") or ""
            is_group = conv.get("type") == "group"
            # 跳过"幽灵会话"：既不是群、也没有用户 openid 的会话无法发送，
            # 通常是历史版本把机器人自己的消息记错了地方留下的
            if not is_group and not openid:
                continue
            if is_group and not group_openid:
                continue
            name = ""
            if is_group:
                name = groups.get(group_openid) or ""
                if not name:
                    # 群名以 QQ 为准；拿不到时先用会话表里保存的群名兜底
                    # （绝不用"最后发言者的昵称"，那会让群名乱变）
                    name = conv.get("username") or ""
                if not name:
                    name = self.short_group_label(group_openid)
                if not groups.get(group_openid):
                    self._refresh_group_name_async(group_openid, conv.get("bot_id") or "")
            else:
                # 私聊名一律以"用户自己发的消息/手动别名"为准，
                # 不能直接用会话表里的 username（可能是机器人自己的"我"）
                name = self.lookup_name(openid) or ""
                # QQ 私聊事件经常不返回昵称，这时给一个可区分的占位名，
                # 方便用户在会话列表里认出是谁（可在页面上手动命名）
                if not name:
                    name = self.short_label(openid)
            item["name"] = name
            item["named"] = bool(self.lookup_name(openid)) if not is_group else bool(
                groups.get(group_openid))
            item["display"] = name or conv.get("conv_key") or ""
            item["key"] = conv.get("conv_key") or ""
            out.append(item)
        return out
