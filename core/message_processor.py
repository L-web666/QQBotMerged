# -*- coding: utf-8 -*-
"""消息回复链路：过滤 → 指令 → 关键词 → 插件 → AI → 分段发送

合并来源与顺序（与原 `API_qqbot` 一致，并补齐原项目里“声明了却没生效”的能力）：
1. 全局开关（群聊/私聊自动回复）
2. 本地禁言拦截（群成员被禁言时机器人完全不响应）
3. 敏感词输入拦截
4. 无意义消息过滤
5. 内置指令（/帮助、/清空上下文 等，不消耗 AI）
6. 关键词回复（精确 → 模糊）
7. 插件分发
8. 回复限速
9. AI 回复（图片交给多模态；可关闭内置 AI 交给插件接管）
10. 输出敏感词打码、Markdown 清理、超长分段发送

**每个机器人一条独立队列与工作线程**，避免一个机器人卡住另一个。
"""

import logging
import queue
import re
import threading
import time
from typing import Any, Dict, List, Optional

from core import message_text
from core.gateway import INTENT_GROUP_AND_C2C_EVENT

logger = logging.getLogger(__name__)

# 消息开头的 @提及（群里 @ 机器人时会出现，需要先剥掉再判断指令/关键词）；
# 覆盖旧格式 `<@openid>` / `<@all>` 与官方新格式 `<qqbot-at-user id="…" />`
# `<qqbot-at-everyone />`，避免 AI 看到原始标记
MENTION_PREFIX_RE = re.compile(
    r"^\s*(?:(?:%s|%s|%s|%s)\s*)+" % (
        message_text.MENTION_RE.pattern, message_text.AT_ALL_LEGACY_RE.pattern,
        message_text.AT_USER_NEW_RE.pattern, message_text.AT_EVERYONE_NEW_RE.pattern),
    re.IGNORECASE)

HELP_TEXT = """可用指令：
/帮助 · 显示这条帮助
/清空上下文 · 清空本会话的记忆
/群设置 · 查看当前群的生效配置（群聊）
/图片说明 <图片链接> · 让 AI 描述一张图片

其他说明：
· 关键词回复与插件会优先于 AI 处理；
· 管理员可在网页后台「群管理」里设置成员禁言。"""


class MessageProcessor:
    """按机器人分配工作线程的消息处理器。"""

    def __init__(self, runtime):
        self.runtime = runtime
        self.log = runtime.log
        self.config = runtime.config
        self._queues: Dict[str, "queue.Queue"] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._running = False
        self._lock = threading.RLock()
        # "机器人忙"的提示最多同时发 5 条：用信号量而不是"读-改-写计数器"，
        # 否则多个提交线程几乎同时通过 `>= 5` 检查会多发好几条（并发计数没锁）。
        self._busy_slots = threading.Semaphore(5)
        self.busy_threads = 0
        self.processed = 0
        self.failed = 0

    # ================================================================== 队列
    @property
    def max_queue_size(self) -> int:
        return max(1, self.config.int_of("reply", "max_queue_size", default=10))

    def queue_size(self) -> int:
        with self._lock:
            return sum(item.qsize() for item in self._queues.values())

    def queue_detail(self) -> Dict[str, int]:
        with self._lock:
            return {bot_id: item.qsize() for bot_id, item in self._queues.items()}

    def start(self):
        self._running = True
        with self._lock:
            for bot_id, bot in self.runtime.bots.items():
                if bot.enabled:
                    self._ensure_worker(bot_id)
        self.log.info("消息处理器已启动（每个机器人一条队列）")

    def stop(self):
        self._running = False
        for thread in list(self._threads.values()):
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
        self._threads.clear()
        self._queues.clear()

    def _ensure_worker(self, bot_id: str):
        with self._lock:
            if bot_id in self._threads and self._threads[bot_id].is_alive():
                return
            self._queues[bot_id] = queue.Queue(maxsize=self.max_queue_size)
            thread = threading.Thread(target=self._worker_loop, args=(bot_id,),
                                      name=f"processor-{bot_id}", daemon=True)
            self._threads[bot_id] = thread
            thread.start()

    def submit(self, message: Dict[str, Any]):
        """把消息放进对应机器人的队列；队列满时友好提示（并在后台线程回复，不阻塞网关）。"""
        bot_id = message.get("bot_id") or ""
        self._ensure_worker(bot_id)
        with self._lock:
            target = self._queues.get(bot_id)
        if target is None:
            return
        try:
            target.put_nowait(message)
        except queue.Full:
            self.log.warning("[%s] 消息队列已满，丢弃一条并提示用户", bot_id)
            self.runtime.stats.record("busy_replies")
            threading.Thread(target=self._send_busy_reply, args=(message,), daemon=True).start()

    def _send_busy_reply(self, message: Dict[str, Any]):
        # 非阻塞抢一个名额：抢不到说明已经有 5 条繁忙提示在发了，直接跳过
        if not self._busy_slots.acquire(blocking=False):
            return
        self.busy_threads += 1
        try:
            self._reply(message, "🤖 机器人正在处理其他消息，请稍后再试。")
        except Exception as exc:
            self.log.debug("繁忙提示发送失败: %s", exc)
        finally:
            self.busy_threads -= 1
            self._busy_slots.release()

    def _worker_loop(self, bot_id: str):
        while self._running:
            with self._lock:
                target = self._queues.get(bot_id)
            if target is None:
                return
            try:
                message = target.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.process(message)
                self.processed += 1
            except Exception as exc:
                self.failed += 1
                self.runtime.stats.record("process_errors")
                self.log.error("[%s] 处理消息异常: %s", bot_id, exc, exc_info=True)
                try:
                    self._reply(message, "⚠️ 系统暂时遇到了问题，请稍后再试。")
                except Exception:
                    pass

    # ================================================================== 主链路
    def process(self, message: Dict[str, Any]):
        runtime = self.runtime
        bot_id = message.get("bot_id") or ""
        # 一切设置都取"这个机器人自己的配置"（全局配置 + 该机器人的覆盖），
        # 这样在网页上选中哪个机器人，就精确用哪个机器人的 AI/过滤/限速等
        config = runtime.bot_config(bot_id)
        message_filter = runtime.bot_message_filter(bot_id)
        msg_type = message.get("type") or "private"
        content = (message.get("content") or "").strip()
        group_openid = message.get("group_openid") or ""
        user_openid = message.get("openid") or ""
        member_openid = message.get("member_openid") or user_openid
        attachments = message.get("attachments") or []
        mentions = message.get("mentions") or []

        # 频道私信与私聊同路径
        is_group = msg_type == "group"
        target_id = group_openid if is_group else user_openid
        if not target_id:
            return

        # ---------- 1. 全局开关 ----------
        if is_group and not runtime.group_manager.effective(
                group_openid, "auto_reply",
                config.bool_of("features", "auto_reply_in_group", default=True)):
            self.log.info("群 %s 已关闭自动回复，本条消息已跳过", group_openid)
            return
        if not is_group and not config.bool_of("features", "auto_reply_in_private", default=True):
            return

        # ---------- 2. 本地禁言拦截 ----------
        if is_group:
            mute = runtime.is_muted(bot_id, group_openid, member_openid)
            if mute:
                runtime.stats.record("muted_blocked")
                self.log.info("成员 %s 处于禁言状态（%s），已忽略其消息",
                              member_openid, mute.get("remaining_text") or "禁言中")
                return

        # ---------- 3. 敏感词输入拦截 ----------
        if message_filter.sensitive_block_input and message_filter.contains_sensitive(content):
            runtime.stats.record("sensitive_blocked")
            self.log.info("消息命中敏感词且配置为拦截，已忽略")
            return

        # ---------- 4. 群聊是否需要 @ ----------
        require_mention = runtime.group_manager.effective(
            group_openid, "require_mention",
            config.bool_of("reply", "require_mention", default=True)) if is_group else False
        if is_group and require_mention and not message.get("is_at_bot"):
            # 只有 @ 机器人的群消息才回复（开了「接收所有消息」时群里每条消息都会推过来）
            self.log.info("群消息未 @ 机器人，已跳过（事件=%s，@全体成员不算 @ 机器人）",
                          message.get("event") or "?")
            return

        # ---------- 5. 去掉开头的 @提及 ----------
        # 群里 @ 机器人时，消息文本形如 "<@机器人openid> /帮助"，
        # 前面的 @ 会让"/帮助"这类指令判断失效（以前群聊里指令完全不生效，就是这个原因）。
        raw_content = content
        content = MENTION_PREFIX_RE.sub("", content).strip()
        if content != raw_content:
            self.log.debug("去掉消息开头的 @提及: %r -> %r", raw_content[:40], content[:40])
        if not content and not attachments:
            # 只 @ 了机器人、没有正文 → 不回复（避免无意义应答）
            self.log.info("消息只包含 @提及、没有正文，已跳过")
            return
        message["content"] = content

        # ---------- 6. 附件 → 文本化 ----------
        has_attachment = bool(attachments)
        image_urls = [item for item in (message.get("_image_urls") or []) if item]
        if not content and has_attachment:
            if image_urls:
                content = config.str_of("ai", "vision_prompt", default="请用中文描述这张图片的内容。")
            else:
                names = [item.get("file_name") or "文件" for item in attachments[:3]]
                content = "（对方发来附件：" + "、".join(names) + "）"
            message["content"] = content
            self.log.debug("纯附件消息补全为: %s", content)

        # ---------- 7. 无意义过滤 ----------
        keyword_enabled = runtime.group_manager.effective(
            group_openid, "keywords_enabled",
            config.bool_of("filters", "keywords_enabled", default=True))
        if not has_attachment and message_filter.is_meaningless(content):
            runtime.stats.record("filtered")
            self.log.debug("无意义消息，已忽略: %s", content[:30])
            return

        context_type = "group" if is_group else "private"
        context_openid = user_openid

        # ---------- 8. 内置指令 ----------
        handled = self._handle_command(message, content, bot_id, target_id, is_group, context_type,
                                       context_openid, group_openid)
        if handled:
            return

        # ---------- 9. 关键词回复 ----------
        if keyword_enabled:
            reply = message_filter.match_keyword(content)
            if reply:
                runtime.stats.record("keyword_hits")
                self._reply(message, reply)
                return

        # ---------- 10. 插件（AstrBot 插件）----------
        # 群名要从 group_manager 取（store 上没有 group_names 这个方法；
        # 以前写错成 runtime.store.group_names()，群里任何"不是指令也不是关键词"的
        # 消息走到这一步都会抛 AttributeError，被兜底成"系统暂时遇到了问题"，
        # 私聊不取群名所以看不出来）。
        group_name = ""
        if is_group:
            try:
                group_name = runtime.group_manager.group_names().get(group_openid, "")
            except Exception as exc:
                self.log.debug("取群名失败（不影响回复）：%s", exc)
        plugin_result = runtime.plugin_manager.dispatch_message({
            "type": msg_type,
            "content": content,
            "user_openid": user_openid,
            "member_openid": member_openid,
            "user_name": message.get("username") or "",
            "group_openid": group_openid,
            "group_name": group_name,
            "msg_id": message.get("msg_id") or "",
            "bot_id": bot_id,
            "attachments": attachments,
            "mentions": mentions,
            "is_at_bot": bool(message.get("is_at_bot")),
            "is_admin": self._is_admin(message),
            "ts": message.get("ts") or time.time(),
        })
        if plugin_result and plugin_result.get("handled"):
            runtime.stats.record("plugin_replies")
            if not (plugin_result.get("sequence") or plugin_result.get("text")
                    or plugin_result.get("images")) and not plugin_result.get("via_sink"):
                # 插件说“我处理了”却没有任何内容 → 看起来就是机器人不回复，必须留痕
                self.log.warning("插件 %s 处理了这条消息但没有返回任何内容（消息被吞掉）",
                                 plugin_result.get("source") or "?")
            self._send_plugin_result(message, plugin_result)
            return

        # ---------- 10. 限速 ----------
        interval = float(runtime.group_manager.effective(
            group_openid, "rate_limit_seconds",
            config.float_of("filters", "rate_limit_seconds", default=3)) or 0)
        rate_key = f"{bot_id}:{target_id}:{user_openid}"
        if config.bool_of("filters", "rate_limit_enabled", default=True) and \
                runtime.rate_limited(rate_key, interval):
            runtime.stats.record("rate_limited")
            self._reply(message, "⏳ 请稍等一下，我上一条还没说完呢～")
            return

        # ---------- 11. AI 回复 ----------
        self._ai_reply(message, content, bot_id, target_id, is_group, context_type,
                       context_openid, image_urls)

    # ================================================================== 内置指令
    def _handle_command(self, message, content, bot_id, target_id, is_group, context_type,
                        context_openid, group_openid) -> bool:
        runtime = self.runtime
        if not content.startswith("/"):
            return False
        command = content.split()[0].lower() if content.split() else content.lower()
        admin_ids = runtime.config.list_of("security", "admin_openids", default=[])
        is_admin = bool(admin_ids) and (message.get("openid") in admin_ids)

        if command in ("/帮助", "/help", "/菜单", "/指令"):
            runtime.stats.record("commands")
            text = HELP_TEXT
            if is_admin:
                text += "\n\n你是管理员：网页后台可管理插件、群成员与全部配置。"
            self._reply(message, text)
            return True

        if command in ("/clear", "/清空上下文", "/重置对话"):
            runtime.stats.record("commands")
            runtime.context_manager.clear(bot_id, context_type, context_openid)
            self._reply(message, "🧹 已清空本会话的对话记忆。")
            return True

        if command == "/群设置" and is_group:
            runtime.stats.record("commands")
            settings = runtime.group_manager.get_settings(group_openid)["effective"]
            lines = ["本群当前生效配置："]
            lines.append(f"· 需要 @ 才回复：{'是' if settings.get('require_mention') else '否'}")
            lines.append(f"· 自动回复：{'开启' if settings.get('auto_reply') else '关闭'}")
            lines.append(f"· 关键词回复：{'开启' if settings.get('keywords_enabled') else '关闭'}")
            lines.append(f"· 回复限速：{settings.get('rate_limit_seconds')} 秒")
            lines.append(f"· 图片留存：{'开启' if settings.get('save_media') else '关闭'}")
            lines.append(f"· 上下文条数：{settings.get('max_history')}")
            self._reply(message, "\n".join(lines))
            return True

        if command == "/图片说明" and is_admin:
            parts = content.split(maxsplit=1)
            if len(parts) < 2:
                self._reply(message, "用法：/图片说明 <图片链接>")
                return True
            runtime.stats.record("commands")
            description = runtime.bot_ai_client(bot_id).describe_image(parts[1].strip())
            self._reply(message, description or "❌ 图片识别失败（请确认已配置支持视觉的模型）")
            return True

        return False

    # ================================================================== AI
    def _ai_reply(self, message, content, bot_id, target_id, is_group, context_type,
                  context_openid, image_urls):
        runtime = self.runtime
        config = runtime.bot_config(bot_id)          # 这个机器人自己的 AI 设置
        ai_client = runtime.bot_ai_client(bot_id)
        message_filter = runtime.bot_message_filter(bot_id)

        if not config.bool_of("ai", "enabled", default=True):
            # 内置 AI 关闭：交给插件/关键词；没被接管的按兜底回复
            fallback = config.str_of("ai", "no_ai_reply", default="")
            if fallback:
                self._reply(message, fallback)
            return

        if not ai_client.usable:
            fallback = config.str_of("ai", "no_ai_reply", default="")
            runtime.stats.record("ai_calls")
            runtime.stats.record("ai_errors")
            if fallback:
                self._reply(message, fallback)
            else:
                self.log.debug("内置 AI 不可用且未配置兜底回复，保持安静")
            return

        system_prompt = config.str_of("ai", "system_prompt", default="你是一个智能助手。")
        # AstrBot 插件的 on_llm_request 钩子：允许插件追加 system_prompt / 额外上下文
        try:
            host = getattr(runtime.plugin_manager, "host", None)
            if host is not None and host.llm_request_hooks(bot_id):
                request = host.build_llm_request(content, system_prompt, image_urls, bot_id=bot_id)
                system_prompt = request.system_prompt or system_prompt
                extra_text = request.extra_user_text()
                if extra_text:
                    content = f"{content}\n\n{extra_text}" if content else extra_text
        except Exception as exc:
            self.log.debug("插件 on_llm_request 钩子执行异常：%s", exc)
        # 群级配置里的"上下文条数"只影响这一次调用，不再去改全局的 MAX_HISTORY
        # （ContextManager 是所有机器人共用的，改实例属性会互相覆盖）
        max_history = runtime.group_manager.effective(
            group_openid=message.get("group_openid") or "", key="max_history",
            default=None)
        if max_history is not None:
            try:
                max_history = int(max_history)
            except (TypeError, ValueError):
                max_history = None
        messages = runtime.context_manager.format_for_prompt(
            bot_id, context_type, context_openid, system_prompt, max_history=max_history)
        # 最后一条用户消息就是当前内容（历史里还没写入）
        if image_urls:
            prompt = content or config.str_of("ai", "vision_prompt", default="请描述这张图片。")
            messages = [item for item in messages if item.get("role") == "system"]
            messages.append({"role": "user", "content": prompt})
            runtime.stats.record("ai_calls")
            answer = ai_client.chat_with_images(messages, image_urls[:4], prompt)
        else:
            messages.append({"role": "user", "content": content})
            runtime.stats.record("ai_calls")
            answer = ai_client.chat(messages)

        if not answer:
            runtime.stats.record("ai_errors")
            self.log.warning("[%s] AI 未返回内容（%s）", bot_id, ai_client.last_error)
            self._reply(message, "⚠️ AI 服务暂时不可用，请稍后再试。")
            return

        answer = ai_client.filter_thinking(answer)
        if config.bool_of("reply", "strip_markdown", default=True):
            answer = message_text.strip_markdown(answer)
        if message_filter.contains_sensitive(answer):
            answer = message_filter.mask_sensitive(answer)
            runtime.stats.record("sensitive_masked")
        if not answer:
            answer = "抱歉，我无法生成合适的回复。"

        # 存上下文（用户消息 + 助手回复）
        if config.bool_of("reply", "context_enabled", default=True):
            runtime.context_manager.append(bot_id, context_type, context_openid, "user", content,
                                           message.get("username") or "", max_history=max_history)
            runtime.context_manager.append(bot_id, context_type, context_openid, "assistant", answer,
                                           max_history=max_history)

        self._reply(message, answer)
        runtime.mark_replied(f"{bot_id}:{target_id}:{message.get('openid') or ''}")

    # ================================================================== 发送
    def _is_admin(self, message: Dict[str, Any]) -> bool:
        try:
            admin_ids = self.runtime.config.list_of("security", "admin_openids", default=[])
        except Exception:
            admin_ids = []
        return bool(admin_ids) and (message.get("openid") in admin_ids)

    def _send_plugin_result(self, message: Dict[str, Any], result: Dict[str, Any]):
        """发送插件结果：按 AstrBot 消息链的顺序发（文本分段、图片单独发）。"""
        runtime = self.runtime
        sequence = list(result.get("sequence") or [])
        if not sequence:
            if result.get("text"):
                sequence.append({"type": "text", "text": result["text"]})
            sequence.extend(result.get("images") or [])
        if not sequence:
            return
        # 只有一段文本时走原来的回复逻辑（保留分段与引用设置）
        if len(sequence) == 1 and sequence[0].get("type") == "text":
            self._reply(message, sequence[0].get("text") or "")
            return
        limit = self._segment_limit()
        steps: List[Dict[str, Any]] = []
        for step in sequence:
            if step.get("type") != "text":
                steps.append(step)
                continue
            for chunk in (message_text.split_message(step.get("text") or "", limit)
                          or [step.get("text") or ""]):
                if chunk:
                    steps.append({"type": "text", "text": chunk})
        runtime.plugin_manager.send_steps(runtime, message, steps)
        if any(step.get("type") == "text" for step in steps):
            runtime.stats.record("replies")

    def _segment_limit(self) -> int:
        return self.runtime.config.int_of("reply", "max_segment_length", default=2000)

    def _reply(self, message: Dict[str, Any], text: str):
        """把回复按长度分段发出（首段带引用，若开启）。"""
        if not text:
            return
        runtime = self.runtime
        target_type = "group" if (message.get("type") == "group") else "private"
        target_id = message.get("group_openid") if target_type == "group" else message.get("openid")
        if not target_id:
            return
        limit = runtime.config.int_of("reply", "max_segment_length", default=2000)
        chunks = message_text.split_message(text, limit) or [text]
        reply_id = ""
        if runtime.config.bool_of("reply", "quote_reply", default=False):
            reply_id = message.get("msg_id") or ""
        sent_any = False
        for index, chunk in enumerate(chunks):
            try:
                runtime.send_text(target_type, target_id, chunk, bot_id=message.get("bot_id") or "",
                                  reply_msg_id=reply_id if index == 0 else "")
                sent_any = True
            except Exception as exc:
                self.log.error("回复发送失败: %s", exc)
                break
            if index < len(chunks) - 1:
                time.sleep(0.5)
        if sent_any:
            runtime.stats.record("replies")
