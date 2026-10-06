# -*- coding: utf-8 -*-
"""Web 后台（Flask 单端口）

把原来两个程序的后台合成一个站点：
- 左侧统一导航（聊天窗口 / 状态 / 统计 / 设置 / 插件管理 / 日志 / 上下文 / 指令面板 / 群管理 / 关于）；
- 一套鉴权（`web.token`，留空则不校验）；
- 接口按用途分组：`/api/chat/*`（会话与发送）、`/api/admin/*`（运维与配置）、`/api/groups/*`（群管理）。

发送能力（合并版重点）：
- 文本；
- 图片：**链接**（公网 / 本地留存 / 本机地址自动下载再上传）或**本地上传**（multipart 或 base64）；
- 文件：本地上传，平台不支持文件消息时自动退化为发下载链接，并把原因回给页面。
"""

import json
import logging
import mimetypes
import os
import queue
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from flask import (Flask, Blueprint, Response, jsonify, render_template, request,
                   send_file, send_from_directory, stream_with_context)

from core import config_schema as schema
from core import media_store as media_module
from core import paths
from core.cloud_sync import SYNC_PATHS
from core.storage import conversation_key, conv_key_for

logger = logging.getLogger(__name__)


def create_app(config_manager, logger_obj: logging.Logger = None) -> Flask:
    app = Flask(__name__, template_folder=paths.TEMPLATE_DIR, static_folder=paths.STATIC_DIR)
    # 上传上限：QQ 官方对图片/视频/语音/文件的硬限制都是 200MB，
    # 这里留一点余量给 multipart 包头（业务上限由 send.max_file_mb 再控一次）
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024
    app.config["JSON_AS_ASCII"] = False
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    state: Dict[str, Any] = {"runtime": None, "started_at": time.time()}
    secret = {"token": config_manager.config.str_of("web", "token", default="")}

    # ================================================================== 工具
    def runtime():
        return state.get("runtime")

    def check_token() -> Optional[Response]:
        """校验访问令牌：配置了 token 就必须带对（?token= 或 X-Auth-Token）。"""
        expected = secret.get("token") or config_manager.config.str_of("web", "token", default="")
        if not expected:
            return None
        provided = (request.args.get("token") or request.headers.get("X-Auth-Token") or "").strip()
        if provided == expected:
            return None
        return jsonify({"success": False, "message": "token 无效或缺失", "auth_required": True}), 401

    def guard():
        """返回 401 或 None，供每个接口调用。"""
        blocked = check_token()
        return blocked

    def current_bot_id() -> str:
        """当前选中的机器人：显式传 bot 优先，否则用后台选中的那个。

        这样"聊天窗口 / 设置 / 群管理"永远只针对一个机器人，
        不会出现"程序不知道用哪个机器人请求资源"的歧义。
        """
        explicit = (request.values.get("bot") or "").strip()
        if explicit:
            return explicit
        if request.is_json:
            body = request.get_json(silent=True) or {}
            if isinstance(body, dict) and str(body.get("bot_id") or "").strip():
                return str(body["bot_id"]).strip()
        return config_manager.active_bot_id()

    def known_bot(bot_id: str) -> bool:
        """该 id 是不是配置里登记过的机器人（用来识别"页面上的机器人已经过期"）。"""
        candidate = str(bot_id or "").strip()
        if not candidate:
            return False
        rt = runtime()
        if rt is not None and candidate in rt.bots:
            return True
        for item in (config_manager.config.get("bots") or []):
            if isinstance(item, dict) and str(item.get("id") or "").strip() == candidate:
                return True
        return False

    def parse_conv_key(key: str):
        """把 `bot:group:xxx` / `group:xxx` 拆成 (bot_id, type, openid)。"""
        parts = (key or "").split(":")
        if len(parts) >= 3 and parts[1] in ("group", "private", "channel"):
            return parts[0], parts[1], ":".join(parts[2:])
        if len(parts) >= 2 and parts[0] in ("group", "private", "channel"):
            return "", parts[0], ":".join(parts[1:])
        return "", "", ""

    @app.errorhandler(413)
    def too_large(_exc):
        return jsonify({"success": False, "message": "上传内容过大（服务端上限 64MB）"}), 413

    @app.errorhandler(404)
    def not_found(_exc):
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "message": "接口不存在"}), 404
        return render_template("index.html", boot=boot_payload()), 404

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    def boot_payload() -> Dict[str, Any]:
        """注入页面的启动数据（主题、动画、轮询间隔等）。"""
        cfg = config_manager.config
        return {
            "ui": {
                "theme": cfg.str_of("ui", "theme", default="auto"),
                "animation": cfg.str_of("ui", "animation", default="full"),
                "compact": cfg.bool_of("ui", "compact_mode", default=False),
                "bubbles": cfg.bool_of("ui", "message_bubbles", default=True),
                "avatars": cfg.bool_of("ui", "show_avatar", default=True),
                "lightbox": cfg.bool_of("ui", "image_lightbox", default=True),
                "poll_interval_ms": cfg.int_of("ui", "poll_interval_ms", default=2000),
            },
            "limits": {
                "max_image_mb": cfg.float_of("send", "max_image_mb", default=6),
                "max_file_mb": cfg.float_of("send", "max_file_mb", default=8),
                "message_max_length": cfg.int_of("send", "message_max_length", default=4000),
                "page_size": cfg.int_of("web", "page_size", default=200),
            },
            "auth_required": bool(secret.get("token")),
        }

    # ================================================================== 页面
    @app.route("/")
    def index():
        return render_template("index.html", boot=boot_payload())

    @app.route("/health")
    def health():
        """健康检查（供监控/自检使用，同样受令牌保护）。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        bots = []
        if rt is not None:
            bots = [{"id": bot.id, "name": bot.name, "online": bot.gateway.ready,
                     "state": bot.gateway.status_text()} for bot in rt.bots.values()]
        return jsonify({
            "status": "ok",
            "uptime_seconds": int(time.time() - state["started_at"]),
            "bots": bots,
            "online": sum(1 for bot in bots if bot["online"]),
            "messages": (rt.store.count() if rt is not None else 0),
            "media_saved": (
                (rt.store.media_stats().get("done") or {}).get("count", 0) if rt is not None else 0),
            "ai_usable": bool(rt is not None and rt.ai_client.usable),
        })

    # ---------------- 静态媒体 ----------------
    @app.route("/media/<path:filename>")
    def media_file(filename: str):
        """返回本地留存的媒体文件（QQ 链接会过期，本地副本不受影响）。"""
        rt = runtime()
        base = paths.MEDIA_DIR
        if rt is not None:
            base = rt.media.media_dir
        safe = media_module.safe_name(filename)
        full = os.path.join(base, safe)
        if not os.path.isfile(full):
            return jsonify({"success": False, "message": "文件不存在"}), 404
        mime, _ = mimetypes.guess_type(full)
        return send_file(full, mimetype=mime or "application/octet-stream",
                         download_name=safe, conditional=True)

    @app.route("/api/chat/image")
    def image_proxy():
        """图片代理：解决 QQ 图片的防盗链/过期问题（只允许媒体域名）。"""
        blocked = check_token()
        if blocked:
            return blocked
        url = (request.args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return jsonify({"success": False, "message": "url 必须是 http/https 地址"}), 400
        host = (urlparse(url).hostname or "").lower()
        if not any(host == suffix or host.endswith("." + suffix)
                   for suffix in media_module.ALLOWED_HOST_SUFFIX):
            return jsonify({"success": False,
                            "message": "仅支持腾讯系媒体域名（*.qq.com / *.qpic.cn 等）"}), 400
        rt = runtime()
        try:
            blob, content_type = (rt.media.fetch(url) if rt else (b"", ""))
        except Exception as exc:
            return jsonify({"success": False, "message": f"图片拉取失败：{exc}"}), 502
        if not blob:
            return jsonify({"success": False, "message": "图片拉取失败"}), 502
        mime = (content_type or "").split(";")[0].strip() or "image/png"
        response = Response(blob, mimetype=mime)
        response.headers["Cache-Control"] = "public, max-age=1800"
        return response

    # ================================================================== 实时推送
    @app.route("/api/chat/stream")
    def chat_stream():
        """SSE 实时推送（新消息、状态变化）。前端断线时回退到轮询。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        client_queue: "queue.Queue" = queue.Queue(maxsize=200)

        def on_event(event: Dict[str, Any]):
            try:
                client_queue.put_nowait(event)
            except queue.Full:
                pass

        @stream_with_context
        def generate():
            if rt is not None:
                rt.subscribe(on_event)
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        event = client_queue.get(timeout=20)
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    except queue.Empty:
                        yield ": ping\n\n"
            finally:
                if rt is not None:
                    rt.unsubscribe(on_event)

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ================================================================== 聊天接口
    @app.route("/api/chat/conversations")
    def chat_conversations():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        bot_id = current_bot_id()
        conversations = rt.public_conversations(bot_id)
        unread = rt.store.unread(bot_id)
        for item in conversations:
            item["unread"] = unread.get(item["key"], 0)
        return jsonify({
            "success": True,
            "conversations": conversations,
            "bots": [bot.status() for bot in rt.bots.values()],
            "unread_total": sum(unread.values()),
            "storage": "sqlite" if rt.store.__class__.__name__ == "SQLiteStore" else "memory",
        })

    @app.route("/api/chat/messages")
    def chat_messages():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        key = (request.args.get("key") or "").strip()
        if not key:
            return jsonify({"success": False, "message": "需要 key 参数"}), 400
        limit = request.args.get("limit", type=int) or config_manager.config.int_of(
            "web", "page_size", default=200)
        before_id = request.args.get("before", type=int)
        messages = rt.store.get_conversation(key, limit=limit, before_id=before_id)
        conv = rt.store.conversation(key)
        # 群聊里"能不能撤回别人发的消息"取决于机器人是不是管理员，
        # 这里把身份一并给前端（有缓存；未知时后台补查一次），按钮文案据此提示
        group_role = ""
        if (conv or {}).get("type") == "group":
            group_openid = (conv or {}).get("group_openid") or ""
            group_role = rt.warm_group_role((conv or {}).get("bot_id") or "", group_openid)
        return jsonify({
            "success": True,
            "key": key,
            "count": len(messages),
            "messages": rt.public_messages(messages),
            "has_more": bool(before_id and messages),
            "group_role": group_role,
            "can_recall_members": group_role in ("owner", "admin"),
            "conversation": {
                "key": key, "name": (conv or {}).get("username") or "",
                "message_count": (conv or {}).get("message_count") or 0,
                "type": (conv or {}).get("type") or "",
            },
        })

    @app.route("/api/chat/send", methods=["POST"])
    def chat_send():
        """统一发送接口：文本 / 图片链接 / 图片上传 / 文件上传。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503

        form = request.form if request.form else {}
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            payload = {}

        def field(name: str, default: str = "") -> str:
            value = form.get(name)
            if value is None:
                value = payload.get(name)
            return default if value is None else str(value)

        target_type = (field("targetType") or field("target_type") or "").strip()
        openid = field("openid").strip()
        content = field("content")
        reply_msg_id = (field("msg_id") or "").strip()
        image_url = (field("image_url") or "").strip()
        bot_id = (field("bot_id") or field("bot")).strip()

        if target_type in ("group", "群聊"):
            target_type = "group"
        elif target_type in ("private", "c2c", "私聊"):
            target_type = "private"
        else:
            return jsonify({"success": False, "message": "targetType 必须是 group 或 private"}), 400
        if not openid or len(openid) > 128 or any(ch.isspace() for ch in openid):
            return jsonify({"success": False, "message": "openid 无效"}), 400

        # ---------------- 取上传内容（multipart 优先，其次 base64/DataURL） ----------------
        upload = request.files.get("file") or request.files.get("image")
        blob: Optional[bytes] = None
        file_name = ""
        if upload is not None and upload.filename:
            file_name = media_module.safe_name(upload.filename)
            blob = upload.read()
        else:
            data_b64 = field("file_data") or field("image_data")
            if data_b64:
                try:
                    blob, _mime, ext = rt.media.decode_data_url(data_b64)
                except ValueError as exc:
                    return jsonify({"success": False, "message": str(exc)}), 400
                file_name = media_module.safe_name(field("file_name") or field("image_name")
                                                   or f"upload{ext}")
        if blob is not None and not file_name:
            file_name = f"upload_{uuid.uuid4().hex[:8]}"

        if not content and not image_url and blob is None:
            return jsonify({"success": False, "message": "消息内容不能为空"}), 400
        limit = config_manager.config.int_of("send", "message_max_length", default=4000)
        if len(content) > limit:
            return jsonify({"success": False, "message": f"文本过长（最多 {limit} 字）"}), 400

        is_image = bool(blob is not None and (
            file_name.lower().endswith(tuple(media_module.IMAGE_EXT))
            or (field("as_image") in ("1", "true", "yes"))))
        # 只按扩展名判断是不够的：把 .exe 改名成 .png 也会被当图片发出去，
        # 到了 QQ 那边多半以"文件类型不符"失败。这里再看一眼文件头（魔数）。
        image_note = ""
        forced_image = field("as_image") in ("1", "true", "yes")
        if blob is not None and is_image:
            mime, ext = media_module.sniff_image_type(blob)
            if not mime:
                if forced_image:
                    return jsonify({"success": False,
                                    "message": "这个文件的内容不是图片（缺少图片文件头），"
                                               "即使扩展名是图片也无法作为图片发送；"
                                               "请去掉「按图片发送」或改用文件发送"}), 400
                is_image = False
                image_note = "扩展名像图片但内容不是图片，已按普通文件发送"
                logger.warning("上传内容与扩展名不符（%s），按文件处理", file_name)
            else:
                file_name = os.path.splitext(file_name)[0] + (ext or "")
        # 本地留存的文件，发送时如果带图片后缀也按图片走
        try:
            if blob is not None and is_image:
                result = rt.send_image(target_type, openid, blob=blob, file_name=file_name,
                                       content=content, bot_id=bot_id, reply_msg_id=reply_msg_id)
                return jsonify({"success": True, "mode": "image", "data": result.get("info") or {},
                                "image_url": result.get("image_url") or "",
                                "note": "；".join(
                                    item for item in (image_note, result.get("note") or "")
                                    if item)})
            if blob is not None:
                result = rt.send_file(target_type, openid, blob, file_name, content=content,
                                      bot_id=bot_id, reply_msg_id=reply_msg_id)
                return jsonify({"success": True, "mode": result.get("mode") or "file",
                                "data": result.get("info") or {},
                                "file_url": result.get("file_url") or "",
                                "link": result.get("link") or "",
                                "note": "；".join(
                                    item for item in (image_note, result.get("note") or "")
                                    if item)})
            if image_url:
                result = rt.send_image(target_type, openid, image_url=image_url, content=content,
                                       bot_id=bot_id, reply_msg_id=reply_msg_id)
                return jsonify({"success": True, "mode": "image_link",
                                "data": result.get("info") or {},
                                "image_url": result.get("image_url") or "",
                                "note": result.get("note") or ""})
            info = rt.send_text(target_type, openid, content, bot_id=bot_id,
                                reply_msg_id=reply_msg_id)
            return jsonify({"success": True, "mode": "text", "data": info,
                            "note": info.get("_note") or ""})
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        except Exception as exc:
            logger.error("发送失败: target=%s openid=%s 错误=%s", target_type, openid, exc)
            message = f"发送失败：{exc}"
            if "上传URL错误" in str(exc) or "40093010" in str(exc):
                message += "（QQ 无法下载该图片链接，可改用本地上传发送）"
            return jsonify({"success": False, "message": message}), 502

    @app.route("/api/chat/clear", methods=["POST"])
    def chat_clear():
        """清空聊天记录：必须带 key（按会话清空）或 all=true（清空全部）。

        前端会做“二次点击确认”，服务端这里也再次校验参数，避免误调用清空全部。
        """
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        payload = request.get_json(silent=True) or {}
        key = str(payload.get("key") or request.args.get("key") or "").strip()
        clear_all = bool(payload.get("all")) and not key
        if not key and not clear_all:
            return jsonify({"success": False,
                            "message": "需要指定要清空的会话（key），或显式传 all=true 清空全部"}), 400
        # 清空只作用于"当前机器人"：不传 bot_id 时不再清所有机器人的记录/上下文
        bot_id = str(payload.get("bot_id") or "").strip() or current_bot_id()
        removed = rt.clear_messages(key if not clear_all else "", bot_id=bot_id)
        label = key if key else f"当前机器人（{bot_id or '未知'}）的全部会话"
        return jsonify({"success": True, "cleared": label, "removed": removed, "bot_id": bot_id,
                        "message": f"已清空 {label} 的聊天记录（{removed} 条）；"
                                   "其它机器人的记录未受影响"})

    @app.route("/api/chat/read", methods=["POST"])
    def chat_read():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        payload = request.get_json(silent=True) or {}
        rt.store.mark_read(str(payload.get("key") or "").strip())
        return jsonify({"success": True})

    @app.route("/api/chat/raw")
    def chat_raw():
        """查看某条消息的原始事件（排查表情包/引用结构用）。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        key = (request.args.get("key") or "").strip()
        msg_id = (request.args.get("msg_id") or "").strip()
        if rt is None or not key or not msg_id:
            return jsonify({"success": False, "message": "需要 key 与 msg_id"}), 400
        raw = rt.store.get_raw_event(key, msg_id)
        if not raw:
            return jsonify({"success": False, "message": "没有该消息的原始数据"}), 404
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        return jsonify({"success": True, "raw": raw, "parsed": parsed})

    @app.route("/api/chat/group_name", methods=["POST"])
    def chat_group_name():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        payload = request.get_json(silent=True) or {}
        openid = str(payload.get("openid") or "").strip()
        if not openid:
            return jsonify({"success": False, "message": "缺少 openid"}), 400
        result = rt.group_manager.refresh_group_name(
            openid, str(payload.get("bot_id") or "").strip() or current_bot_id())
        status = 200 if result.get("success") else 502
        return jsonify(result), status

    @app.route("/api/chat/media")
    def chat_media():
        """媒体留存列表（网页「媒体」面板与调试用）。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        items = rt.store.list_media(
            limit=request.args.get("limit", type=int) or 100,
            conv_key=(request.args.get("key") or "").strip(),
            state=(request.args.get("state") or "").strip())
        for item in items:
            item["local_url"] = rt.media.local_url(item.get("local_name") or "")
            try:
                full = os.path.join(rt.media.media_dir, item.get("local_name") or "")
                item["exists"] = os.path.isfile(full)
            except Exception:
                item["exists"] = False
        return jsonify({"success": True, "media": items, "stats": rt.store.media_stats()})

    @app.route("/api/chat/alias", methods=["GET", "POST"])
    def chat_alias():
        """手动给 openid 起名（QQ 私聊事件经常不返回昵称，这是唯一的可靠办法）。

        GET  ：返回全部别名 {openid: 名字}
        POST ：{openid, name} 设置名字；name 为空表示删除别名
        """
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        if request.method == "GET":
            return jsonify({"success": True, "aliases": rt.store.aliases()})
        payload = request.get_json(silent=True) or {}
        openid = str(payload.get("openid") or "").strip()
        if not openid:
            return jsonify({"success": False, "message": "缺少 openid"}), 400
        name = str(payload.get("name") or "").strip()
        rt.store.set_alias(openid, name)
        rt.broadcast({"type": "conversations_changed"})
        return jsonify({"success": True,
                        "message": (f"已命名为「{name}」" if name else "已清除自定义名称")})

    @app.route("/api/chat/participants")
    def chat_participants():
        """列出所有出现过的用户（用于在设置/聊天页批量命名）。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        aliases = rt.store.aliases()
        seen: Dict[str, Dict[str, Any]] = {}
        for conv in rt.store.conversations(limit=2000):
            if conv.get("type") == "group":
                continue
            openid = conv.get("openid") or ""
            if not openid:
                continue
            entry = seen.setdefault(openid, {
                "openid": openid,
                "alias": aliases.get(openid, ""),
                "detected_name": rt.lookup_name(openid),
                "message_count": 0,
                "last_time": "",
                "bots": [],
            })
            entry["message_count"] += int(conv.get("message_count") or 0)
            if (conv.get("last_time") or "") > entry["last_time"]:
                entry["last_time"] = conv.get("last_time") or ""
            bot_id = conv.get("bot_id") or ""
            if bot_id and bot_id not in entry["bots"]:
                entry["bots"].append(bot_id)
            entry["display"] = entry["alias"] or entry["detected_name"] or rt.short_label(openid)
        people = sorted(seen.values(), key=lambda item: -item["message_count"])
        return jsonify({"success": True, "participants": people,
                        "hint": "QQ 私聊事件通常不返回昵称，可在这里给用户起个名字，"
                                "起名后会显示在聊天窗口与会话列表里"})

    # ================================================================== 运维接口
    @app.route("/api/admin/status")
    def admin_status():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        status = rt.status()
        status["success"] = True
        status["queue_detail"] = rt.processor.queue_detail()
        status["logs"] = _log_tail(60)
        status["version"] = _version()
        status["web_url"] = rt.web_url
        status["bind"] = {"host": rt.bind_host, "port": rt.bind_port}
        return jsonify(status)

    def _log_tail(lines: int = 60) -> str:
        holder = state.get("log_manager")
        if holder is None:
            return ""
        try:
            data = holder.read_tail(limit=lines)
            return "\n".join(data.get("lines") or [])
        except Exception:
            return ""

    @app.route("/api/admin/stats")
    def admin_stats():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        summary = rt.stats.summary()
        summary["success"] = True
        summary["storage"] = rt.store.stats_summary()
        return jsonify(summary)

    @app.route("/api/admin/config", methods=["GET", "POST"])
    def admin_config():
        if request.method == "GET":
            blocked = check_token()
            if blocked:
                return blocked
            payload = config_manager.payload()
            payload["success"] = True
            rt = runtime()
            if rt is not None:
                payload["runtime_web"] = {
                    "url": rt.web_url,
                    "host": rt.bind_host,
                    "port": rt.bind_port,
                    "note": "这是程序**实际**在用的地址；改「监听端口/地址」需要重启程序才生效",
                }
            return jsonify(payload)

        blocked = check_token()
        if blocked:
            return blocked
        body = request.get_json(silent=True) or {}
        incoming = body.get("config") if isinstance(body.get("config"), dict) else body
        if not isinstance(incoming, dict):
            return jsonify({"success": False, "message": "请求体需要是配置对象或 {config: {...}}"}), 400
        # 设置页作用于"当前选中的机器人"：按机器人的分组写进它的覆盖。
        # 注意：`bot_id="   "` 是 truthy，所以必须先 strip 再判空——否则纯空白会被当成
        # 一个"有效 id"传下去，apply_incoming 里 `if bot_id` 判假，per-bot 段
        # （panels / ai / reply / filters / features / send / scheduler）就会被静默
        # 写进**全局**配置，等于一次性改了所有机器人。
        requested_bot = str(body.get("bot_id") or "").strip()
        # 没带 id（或只带了空白）→ 回退到后台当前选中的机器人；
        # active_bot_id() 自己会跳过已被删除的 id。
        target_bot = requested_bot or str(config_manager.active_bot_id() or "").strip()
        has_per_bot = any(schema.is_per_bot(path) for path in schema.flatten(incoming))
        if target_bot and not known_bot(target_bot):
            # 页面缓存的机器人已经不存在：宁可报错让用户刷新，也不静默把保存丢进全局
            return jsonify({"success": False,
                            "message": f"未知机器人 {target_bot}："
                                       "请刷新页面后在左上角重新选择机器人"}), 400
        if not requested_bot and has_per_bot and (config_manager.config.get("bots") or []):
            # 请求里没有可用的机器人 id（缺失，或只给了空白/纯空格的 bot_id），却带着
            # per-bot 段：此时**不能**按"当前选中"猜——页面过期时会静默改错机器人，
            # 而 apply_incoming(incoming, "") 更会把 per-bot 段写进全局、串到所有机器人。
            return jsonify({"success": False,
                            "message": "这个请求包含「每个机器人自己的设置」，但没带机器人 id："
                                       "请刷新页面后在左上角选择机器人再保存"}), 400
        # 配置里一个机器人都没有时 target_bot 必然为空，per-bot 段照旧写全局（兜底，不拦）
        try:
            applied, need_restart, changed = config_manager.apply_incoming(incoming, target_bot)
        except Exception as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        rt = runtime()
        hot = []
        if rt is not None and changed:
            try:
                hot = rt.on_config_changed(source="网页设置")
            except Exception as exc:
                logger.error("配置热更新失败: %s", exc)
        secret["token"] = config_manager.config.str_of("web", "token", default="")

        # 指令面板是注册在 QQ 服务器上的，改完必须重新注册，否则"保存了但不生效"
        panels_result = None
        panels_note = ""
        if rt is not None and any(str(item).split("（")[0].startswith("panels")
                                  for item in changed):
            try:
                panels_result = rt.register_command_panels()
                ok_count = sum(1 for item in panels_result.values()
                               if isinstance(item, dict) and not item.get("skipped")
                               and not item.get("error"))
                skipped = [item.get("skipped") for item in panels_result.values()
                           if isinstance(item, dict) and item.get("skipped")]
                if ok_count:
                    panels_note = f"指令面板已按新配置重新注册（{ok_count} 个机器人）。"
                elif skipped:
                    panels_note = "指令面板未注册：" + "；".join(str(text) for text in skipped) + "。"
            except Exception as exc:
                logger.error("重新注册指令面板失败: %s", exc)
                panels_result = {"error": str(exc)}
                panels_note = f"指令面板重新注册失败：{exc}"

        # 改了端口/地址：进程不会自动换端口，必须把"现在该访问哪个地址"讲清楚
        restart_notice = ""
        if rt is not None and any(
                item in ("web.port", "web.host") for item in changed):
            want = (f"{config_manager.config.str_of('web', 'host', default='127.0.0.1')}:"
                    f"{config_manager.config.int_of('web', 'port', default=8666)}")
            restart_notice = (
                f"⚠️ 你改了网页监听地址（{want}），但它需要重启程序才生效。"
                f"当前程序仍监听在 {rt.bind_host}:{rt.bind_port}，请继续用 {rt.web_url} 访问；"
                f"重启后请改用新地址。"
                if rt.bind_port else
                f"⚠️ 网页监听地址已改为 {want}，需要重启程序才生效。")

        payload = config_manager.payload()
        if rt is not None:
            payload["runtime_web"] = {
                "url": rt.web_url, "host": rt.bind_host, "port": rt.bind_port,
                "note": "这是程序实际在用的地址；改端口/地址需要重启才生效",
            }
        payload.update({
            "success": True,
            "changed": changed,
            "hot_applied": hot,
            "need_restart": need_restart,
            "restart_notice": restart_notice,
            "panels": panels_result,
            "message": ("已保存。" if changed else "没有变化。")
                       + (f"已热更新 {len(hot)} 项；" if hot else "")
                       + (panels_note if panels_note else "")
                       + (f"需重启生效：{'、'.join(need_restart)}。" if need_restart else ""),
        })
        if restart_notice:
            logger.warning("%s", restart_notice)
        return jsonify(payload)

    @app.route("/api/admin/plugins")
    def admin_plugins():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        manager = rt.plugin_manager
        # 自愈：万一插件目录不是绝对路径（老实例/手工构造），这里按程序根目录纠正
        if manager.plugin_dir and not os.path.isabs(manager.plugin_dir):
            manager.plugin_dir = os.path.join(paths.BASE_DIR, manager.plugin_dir)
        # 按机器人隔离：返回"当前选中的机器人"能用的插件（供页面打勾）
        bot_id = current_bot_id()
        plugins = manager.list_plugins()
        allowed = manager.allowed_names(bot_id)          # None = 不限制（全部可用）
        bot_allowed = {}
        for info in plugins:
            name = str(info.get("name") or "")
            bot_allowed[name] = True if allowed is None else (name in allowed)
        bot_entry = next((item for item in (config_manager.config.get("bots") or [])
                          if isinstance(item, dict) and str(item.get("id")) == bot_id), None)
        return jsonify({
            "success": True,
            "enabled": config_manager.config.bool_of("plugins", "enabled", default=True),
            "plugins": plugins,
            "dir": manager.plugin_dir,
            "data_dir": manager.data_dir,
            "diagnostics": manager.diagnostics(),
            "bot_id": bot_id,
            "bot_name": str((bot_entry or {}).get("name") or bot_id),
            "bots": [{"id": str(item.get("id") or ""), "name": str(item.get("name") or ""),
                      "enabled": bool(item.get("enabled"))}
                     for item in (config_manager.config.get("bots") or [])
                     if isinstance(item, dict)],
            "bot_allowed": bot_allowed,
            "bot_policy": manager.bot_policy(bot_id),
        })

    @app.route("/api/admin/plugins/bot", methods=["POST"])
    def admin_plugins_bot():
        """按机器人设置"启用哪些插件"（每个机器人可以跑不同的插件）。

        请求：`{"bot_id": "...", "allowed": ["插件名", ...]}`。
        服务端会归一化成"黑名单"存储（`plugins.disabled_names`）；勾满全部插件 =
        两个名单都清空（该机器人不限制）。插件名必须是当前已加载的插件。
        """
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        body = request.get_json(silent=True) or {}
        bot_id = str(body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"success": False,
                            "message": "没带机器人 id：请刷新页面后在左上角重新选择机器人"}), 400
        if not known_bot(bot_id):
            return jsonify({"success": False, "message": f"未知机器人 {bot_id}：请刷新页面后重试"}), 400
        raw = body.get("allowed")
        if not isinstance(raw, list):
            return jsonify({"success": False,
                            "message": "allowed 需要是插件名数组（例如 [\"astrbot_plugin_dice\"]）"}), 400
        manager = rt.plugin_manager
        known = [str(name) for name in manager.names()]
        allowed: List[str] = []
        for item in raw:
            name = str(item or "").strip()
            if name and name not in allowed:
                allowed.append(name)
        unknown = [name for name in allowed if name not in known]
        if unknown:
            return jsonify({"success": False,
                            "message": f"未知插件：{'、'.join(unknown)}（请先点「重载」重新扫描）"}), 400
        if set(allowed) >= set(known):
            enabled_names: List[str] = []
            disabled_names: List[str] = []
        else:
            enabled_names = []                  # 统一用黑名单表达，避免两份名单互相打架
            disabled_names = [name for name in known if name not in allowed]
        config_manager.set_for_bot(bot_id, "plugins.enabled_names", enabled_names)
        config_manager.set_for_bot(bot_id, "plugins.disabled_names", disabled_names)
        config_manager.save()
        try:
            rt.on_config_changed(source="插件按机器人设置")
        except Exception as exc:
            logger.warning("插件按机器人设置后热更新失败：%s", exc)
        allowed_now = manager.allowed_names(bot_id)
        summary = "全部插件" if allowed_now is None else f"{len(allowed_now)}/{len(known)} 个插件"
        logger.info("机器人 %s 的插件范围已更新：%s", bot_id, summary)
        return jsonify({"success": True, "bot_id": bot_id, "bot_allowed": {
            str(info.get("name") or ""): (True if allowed_now is None
                                          else str(info.get("name") or "") in allowed_now)
            for info in manager.list_plugins()},
            "bot_policy": manager.bot_policy(bot_id),
            "message": f"机器人 {bot_id} 现在使用{summary}"})

    @app.route("/api/admin/plugins/apply", methods=["POST"])
    def admin_plugins_apply():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        body = request.get_json(silent=True) or {}
        changes = body.get("changes") or {}
        if not isinstance(changes, dict):
            return jsonify({"success": False, "message": "changes 需要是 {插件名: 是否停用}"}), 400
        known = set(rt.plugin_manager.names())
        unknown = [name for name in changes if name not in known]
        if unknown:
            return jsonify({"success": False, "message": f"未知插件：{'、'.join(unknown)}"}), 400
        for name, disabled in changes.items():
            rt.plugin_manager.set_disabled(name, bool(disabled))
        count = rt.plugin_manager.apply_changes()
        return jsonify({"success": True, "count": count,
                        "plugins": rt.plugin_manager.list_plugins(),
                        "diagnostics": rt.plugin_manager.diagnostics(),
                        "message": f"已应用 {len(changes)} 项修改，当前 {count} 个插件"})

    @app.route("/api/admin/plugins/reload", methods=["POST"])
    def admin_plugins_reload():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        plugins = rt.plugin_manager.reload()
        return jsonify({"success": True, "count": len(plugins),
                        "plugins": rt.plugin_manager.list_plugins(),
                        "diagnostics": rt.plugin_manager.diagnostics(),
                        "message": f"已重新扫描插件目录，当前 {len(plugins)} 个插件"})

    @app.route("/api/admin/plugins/force_load", methods=["POST"])
    def admin_plugins_force_load():
        """即使配置里关掉了插件系统，也强制扫描加载一次（排障用）。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        plugins = rt.plugin_manager.load_plugins(force=True)
        return jsonify({"success": True, "count": len(plugins),
                        "plugins": rt.plugin_manager.list_plugins(),
                        "diagnostics": rt.plugin_manager.diagnostics(),
                        "message": f"已强制加载，共 {len(plugins)} 个插件"})

    @app.route("/api/admin/logs")
    def admin_logs():
        blocked = check_token()
        if blocked:
            return blocked
        holder = state.get("log_manager")
        if holder is None:
            return jsonify({"success": False, "message": "日志不可用"}), 503
        data = holder.read_tail(
            name=(request.args.get("file") or "").strip(),
            limit=request.args.get("lines", type=int) or 300,
            level=(request.args.get("level") or "").strip(),
            keyword=(request.args.get("keyword") or "").strip())
        data["success"] = True
        return jsonify(data)

    @app.route("/api/admin/logs/list")
    def admin_logs_list():
        blocked = check_token()
        if blocked:
            return blocked
        holder = state.get("log_manager")
        if holder is None:
            return jsonify({"success": False, "message": "日志不可用"}), 503
        return jsonify({"success": True, "files": holder.list_files(),
                        "current": os.path.basename(holder.current_file or "")})

    @app.route("/api/admin/logs/download")
    def admin_logs_download():
        blocked = check_token()
        if blocked:
            return blocked
        holder = state.get("log_manager")
        if holder is None:
            return jsonify({"success": False, "message": "日志不可用"}), 503
        name = media_module.safe_name(request.args.get("name") or "")
        content = holder.read_file(name)
        if content is None:
            return jsonify({"success": False, "message": "日志文件不存在"}), 404
        return Response(content, mimetype="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.route("/api/admin/context")
    def admin_context():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        # 上下文按机器人隔离：只列当前机器人的文件，避免看着别人的文件点删除。
        # 这里用**服务端记录的当前机器人**（左侧切换器会同步到服务端），
        # 而不是请求里带来的值——请求体/查询串可能是页面切换前的旧值。
        bot_id = config_manager.active_bot_id()
        bot = rt.bots.get(bot_id)
        summary = rt.context_manager.summary(bot_id=bot_id)
        summary["success"] = True
        summary["bot_id"] = bot_id
        summary["bot_name"] = (bot.name if bot else bot_id)
        summary["max_history"] = rt.context_manager.MAX_HISTORY
        summary["all_bots"] = rt.context_manager.summary()
        return jsonify(summary)

    @app.route("/api/admin/context/delete", methods=["POST"])
    def admin_context_delete():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        body = request.get_json(silent=True) or {}
        scope = str(body.get("scope") or "")
        # ⚠️ 授权只看**服务端记录的当前机器人**：
        # 请求体里的 bot_id 是页面缓存的值，切换机器人后页面还没刷新时它就是过期的，
        # 拿它做权限判断会放行"删掉不属于当前机器人的文件"（用户实际踩到过）。
        active_bot = config_manager.active_bot_id()
        if not active_bot:
            return jsonify({"success": False, "message": "没有选中的机器人"}), 400
        requested = str(body.get("bot_id") or "").strip()
        if requested and requested != active_bot:
            return jsonify({"success": False, "bot_id": active_bot,
                            "message": f"页面上的机器人（{requested}）已经不是当前选中的机器人"
                                       f"（当前是 {active_bot}），已拒绝操作；请先刷新页面"}), 409
        if body.get("all"):
            removed = rt.context_manager.clear(bot_id=active_bot)
            return jsonify({"success": True, "removed": removed, "bot_id": active_bot,
                            "message": f"已清空当前机器人（{active_bot}）的 {removed} 个上下文文件；"
                                       "其它机器人的上下文未受影响"})
        name = str(body.get("name") or "")
        # 只允许删除属于**当前机器人**的文件（文件名以 <bot_id>__ 开头）
        owner = name.split("__", 1)[0] if "__" in name else ""
        if owner != active_bot:
            return jsonify({"success": False, "bot_id": active_bot, "owner": owner,
                            "message": "这个上下文文件不属于当前机器人，已拒绝删除"
                                       "（请先切换到对应机器人再操作）"}), 403
        ok = rt.context_manager.delete_file(scope, name, bot_id=active_bot)
        return jsonify({"success": ok, "message": "已删除" if ok else "删除失败或文件不存在"})

    @app.route("/api/admin/media", methods=["GET", "DELETE"])
    def admin_media():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        if request.method == "GET":
            items = rt.store.list_media(limit=request.args.get("limit", type=int) or 200)
            for item in items:
                item["local_url"] = rt.media.local_url(item.get("local_name") or "")
            return jsonify({"success": True, "media": items, "stats": rt.store.media_stats(),
                            "dir": rt.media.media_dir,
                            "save_enabled": rt.media.enabled})
        days = request.args.get("days", type=int) or 0
        removed = rt.media.cleanup(days)
        return jsonify({"success": True, "removed": removed,
                        "message": f"已清理 {removed} 个过期媒体文件"})

    @app.route("/api/admin/panels")
    def admin_panels():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        # ⚠️ 必须用**服务端当前选中的机器人**取客户端：
        # 以前这里是 rt.get_client()（不传 bot_id），拿到的是"第一个在线/可用的机器人"，
        # 于是切到 bot2 后看到的仍是 bot1 的面板（用户报告的核心问题）；
        # 而且绝不允许在没有 bot_id 的情况下回退到 rt.get_client()。
        active_bot = config_manager.active_bot_id()
        requested = (request.args.get("bot") or request.args.get("bot_id") or "").strip()
        if requested and requested != active_bot and known_bot(requested):
            # 页面缓存的机器人已经不是服务端选中的那个 → 和 /api/admin/context/delete 一样拒掉
            return jsonify({"success": False, "bot_id": active_bot,
                            "message": f"页面显示的是机器人 {requested}，服务端当前是 "
                                       f"{active_bot}，请刷新页面后重试"}), 409
        bot_id = current_bot_id()
        if not bot_id:
            return jsonify({"success": False, "message": "没有选中的机器人"}), 400
        client = rt.get_client(bot_id)
        if client is None:
            return jsonify({"success": False,
                            "message": f"机器人 {bot_id} 没有可用的连接（未启用或未填凭据）"}), 400
        panel_ids = rt._load_panel_ids()
        registered = {scope: panel_ids.get(f"{bot_id}:{scope}", "")
                      for scope in ("c2c", "group")}
        panels, diag = [], {}
        for scope in ("c2c", "group"):
            try:
                items, raw = client.list_command_panels(scope, with_raw=True)
                for item in items or []:
                    if not isinstance(item, dict):
                        continue
                    item = dict(item)
                    item["bot_id"] = bot_id
                    item_scope = str(item.get("scope") or item.get("panel_scope") or "")
                    item_panel_id = str(item.get("panel_id") or "")
                    compare = ((item_scope,) if item_scope in ("c2c", "group")
                               else ("c2c", "group"))
                    item["registered"] = bool(item_panel_id) and any(
                        item_panel_id == registered.get(name) for name in compare)
                    panels.append(item)
                diag[scope] = raw[:500] if not items else ""
            except Exception as exc:
                diag[scope] = f"{exc}"
        bot = rt.bots.get(bot_id)
        return jsonify({"success": True, "bot_id": bot_id,
                        "bot_name": (bot.name if bot else bot_id),
                        "panels": panels, "registered": registered, "diag": diag})

    @app.route("/api/admin/panels/delete", methods=["POST"])
    def admin_panels_delete():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}         # 数组/字符串等非 dict 的 JSON 一律当空体，别让 .get 抛 500
        panel_id = str(body.get("panel_id") or "").strip()
        if not panel_id:
            return jsonify({"success": False, "message": "缺少 panel_id"}), 400
        # 机器人来源：body.bot_id → ?bot=/?bot_id= → 服务端当前选中的那个
        active_bot = config_manager.active_bot_id()
        requested = str(body.get("bot_id") or "").strip() or \
            (request.args.get("bot") or request.args.get("bot_id") or "").strip()
        if requested and requested != active_bot and known_bot(requested):
            return jsonify({"success": False, "bot_id": active_bot,
                            "message": f"页面显示的是机器人 {requested}，服务端当前是 "
                                       f"{active_bot}，请刷新页面后重试"}), 409
        bot_id = requested or active_bot
        if not bot_id:
            return jsonify({"success": False, "message": "没有选中的机器人"}), 400
        client = rt.get_client(bot_id)
        if client is None:
            return jsonify({"success": False,
                            "message": f"机器人 {bot_id} 没有可用的连接（未启用或未填凭据）"}), 400
        # 归属校验：**线上列表为准**——面板列表项本身没有 bot 字段，但"用本机器人的 client
        # 查出来的列表"就是权威的所有权证明；缓存只作兜底（刚注册/权限暂时查不到时用）。
        # 校验不通过时绝不调用平台的删除接口。
        owned_scope = ""
        for scope in ("c2c", "group"):
            try:
                items = client.list_command_panels(scope) or []
            except Exception as exc:
                rt.log.debug("[%s] 校验面板归属时查询 %s 列表失败：%s", bot_id, scope, exc)
                continue
            for item in items:
                if isinstance(item, dict) and str(item.get("panel_id") or "") == panel_id:
                    owned_scope = scope
                    break
            if owned_scope:
                break
        if not owned_scope:
            panel_ids = rt._load_panel_ids()
            for scope in ("c2c", "group"):
                if panel_ids.get(f"{bot_id}:{scope}") == panel_id:
                    owned_scope = scope
                    break
        if not owned_scope:
            return jsonify({"success": False, "bot_id": bot_id,
                            "message": f"这个面板不属于机器人 {bot_id}，或已被删除"
                                       "（页面可能没刷新），已拒绝删除"}), 403
        try:
            ok = client.delete_command_panel(panel_id)
        except Exception as exc:
            return jsonify({"success": False, "message": f"删除失败：{exc}"}), 502
        if ok:
            # 删掉本机器人对应 scope 的缓存键，避免下次注册又去更新一个已不存在的面板。
            # 与注册流程共用同一把锁，防止两边"整份读-改-写"互相覆盖。
            with rt._panel_ids_lock:
                panel_ids = rt._load_panel_ids()
                if panel_ids.get(f"{bot_id}:{owned_scope}") == panel_id:
                    panel_ids.pop(f"{bot_id}:{owned_scope}", None)
                    rt._save_panel_ids(panel_ids)
        return jsonify({"success": bool(ok), "message": "已删除面板" if ok else "删除失败",
                        "bot_id": bot_id,
                        "removed": {"scope": owned_scope, "panel_id": panel_id}})

    @app.route("/api/admin/panels/reload", methods=["POST"])
    def admin_panels_reload():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        try:
            result = rt.register_command_panels()
        except Exception as exc:
            return jsonify({"success": False, "message": f"注册失败：{exc}"}), 502
        bot_id = current_bot_id()
        bot = rt.bots.get(bot_id)
        bot_name = bot.name if bot else bot_id
        return jsonify({"success": True, "bot_id": bot_id, "bot_name": bot_name,
                        "message": f"已按当前配置重新注册指令面板（当前机器人：{bot_name}；"
                                   f"共 {len(rt.bots)} 个机器人）", "detail": result})

    @app.route("/api/admin/receiver/<bot_id>/<action>", methods=["POST"])
    def admin_receiver(bot_id: str, action: str):
        """手动重连 / 断开某个机器人的网关（排障用）。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        bot = (rt.bots.get(bot_id) if rt else None)
        if bot is None:
            return jsonify({"success": False, "message": f"机器人 {bot_id} 不存在"}), 400
        if action in ("restart", "start") and getattr(rt, "bots_disabled", False):
            return jsonify({"success": False,
                            "message": "当前是以「--no-bots」调试模式启动的，不会连接 QQ。"
                                       "请关闭程序后用 python run.py（不带 --no-bots）重新启动。"}), 409
        if action == "restart":
            bot.gateway.restart()
            return jsonify({"success": True, "message": f"{bot.name} 已触发重连"})
        if action == "stop":
            bot.gateway.stop()
            return jsonify({"success": True, "message": f"{bot.name} 已断开"})
        if action == "start":
            bot.gateway.start()
            return jsonify({"success": True, "message": f"{bot.name} 已启动连接"})
        return jsonify({"success": False, "message": "action 需要是 restart/stop/start"}), 400

    @app.route("/api/admin/maintenance", methods=["POST"])
    def admin_maintenance():
        """维护操作：清理媒体 / 裁剪数据库 / 清空全部消息。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        body = request.get_json(silent=True) or {}
        action = str(body.get("action") or "")
        if action == "cleanup_media":
            removed = rt.media.cleanup(int(body.get("days") or 0))
            return jsonify({"success": True, "message": f"已清理 {removed} 个媒体文件"})
        if action == "trim_db":
            rt.store.trim()
            return jsonify({"success": True, "message": "已按保留策略裁剪消息库"})
        if action == "expire_mutings":
            count = rt.store.expire_mutings()
            return jsonify({"success": True, "message": f"已归档 {count} 条到期禁言"})
        if action == "fix_group_names":
            # 手动修正：清理"群名被写成最后发言者昵称"的历史数据（默认不会自动做）
            result = rt.group_manager.repair_suspect_names()
            return jsonify(result), (200 if result.get("success") else 502)
        if action == "group_name_suspects":
            return jsonify({"success": True,
                            "count": rt.store.group_name_suspects()})
        return jsonify({"success": False, "message": "未知的维护操作"}), 400

    # ================================================================== 群管理
    @app.route("/api/groups")
    def groups_list():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        # 只列出**当前选中的机器人**看到过的群，避免两个机器人的群混在一起
        bot_id = current_bot_id()
        bot = rt.bots.get(bot_id)
        manager = rt.group_manager
        # 页面打开时在后台补刷一次群名（没名字/太旧的），前端轮询会自然看到新名字
        manager.trigger_name_refresh(bot_id)
        return jsonify({
            "success": True,
            "bot_id": bot_id,
            "bot_name": (bot.name if bot else bot_id),
            "groups": manager.groups(bot_id),
            "mute_mode": manager.mute_mode(),
            "name_ttl": 30 * 60,
        })
    @app.route("/api/groups/detail")
    def groups_detail():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        group_openid = (request.args.get("group_openid") or "").strip()
        bot_id = current_bot_id()
        if not group_openid:
            return jsonify({"success": False, "message": "缺少 group_openid"}), 400
        refresh = request.args.get("refresh") in ("1", "true", "yes")
        data = rt.group_manager.members(group_openid, bot_id, refresh=refresh)
        bot = rt.bots.get(bot_id)
        data.update({
            "success": True,
            "group_openid": group_openid,
            "bot_id": bot_id,
            "bot_name": (bot.name if bot else bot_id),
            "bot_ids": rt.group_manager.group_bot_ids(group_openid),
            "name": rt.group_manager.group_names().get(group_openid, ""),
            "settings": rt.group_manager.get_settings(group_openid),
            "mutings": rt.group_manager.mutings(group_openid, bot_id),
            "name_updated": rt.group_manager.name_updated_at(group_openid),
            "name_updated_text": (time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(rt.group_manager.name_updated_at(group_openid)))
                if rt.group_manager.name_updated_at(group_openid) else ""),
        })
        return jsonify(data)

    @app.route("/api/groups/refresh", methods=["POST"])
    def groups_refresh():
        """刷新群名 / 群成员（分开控制，避免一次打太多接口）。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        body = request.get_json(silent=True) or {}
        group_openid = str(body.get("group_openid") or "").strip()
        # 没显式传 bot_id 时用当前选中的机器人，避免禁言记录变成"不属于任何机器人"
        bot_id = str(body.get("bot_id") or "").strip() or current_bot_id()
        what = str(body.get("what") or "members")
        # 批量刷新群名（"刷新全部群名"按钮）：一次刷一批，剩下的下一批继续
        if what == "names":
            limit = body.get("limit")
            try:
                limit = max(1, min(20, int(limit))) if limit not in (None, "") else 6
            except (TypeError, ValueError):
                limit = 6
            result = rt.group_manager.refresh_names(
                "", force=bool(body.get("force")), limit=limit, pace=0.6)
            return jsonify(result), 200
        if not group_openid:
            return jsonify({"success": False, "message": "缺少 group_openid"}), 400
        if what == "name":
            result = rt.group_manager.refresh_group_name(group_openid, bot_id)
        else:
            result = rt.group_manager.refresh_members(group_openid, bot_id)
        return jsonify(result), (200 if result.get("success") else 502)

    @app.route("/api/groups/mute", methods=["POST"])
    def groups_mute():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        body = request.get_json(silent=True) or {}
        group_openid = str(body.get("group_openid") or "").strip()
        member_openid = str(body.get("member_openid") or "").strip()
        if not group_openid or not member_openid:
            return jsonify({"success": False, "message": "缺少 group_openid 或 member_openid"}), 400
        minutes = body.get("minutes")
        try:
            minutes = int(minutes) if minutes not in (None, "") else 10
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "minutes 需要是整数（分钟），30 天以内"}), 400
        # 禁言方式由「群管理」页在选择时传入：local / api / both
        mode = str(body.get("mode") or "").strip().lower()
        # 记录归属到当前机器人，两个机器人的禁言名单互不干扰
        bot_id = str(body.get("bot_id") or "").strip() or current_bot_id()
        result = rt.group_manager.mute(group_openid, member_openid, minutes,
                                       reason=str(body.get("reason") or ""),
                                       bot_id=bot_id,
                                       username=str(body.get("username") or ""),
                                       mode=mode)
        return jsonify(result), (200 if result.get("success") else 502)

    @app.route("/api/groups/unmute", methods=["POST"])
    def groups_unmute():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        body = request.get_json(silent=True) or {}
        group_openid = str(body.get("group_openid") or "").strip()
        member_openid = str(body.get("member_openid") or "").strip()
        if not group_openid or not member_openid:
            return jsonify({"success": False, "message": "缺少 group_openid 或 member_openid"}), 400
        result = rt.group_manager.unmute(group_openid, member_openid,
                                         str(body.get("bot_id") or "").strip() or current_bot_id(),
                                         mode=str(body.get("mode") or ""))
        return jsonify(result), (200 if result.get("success") else 502)

    @app.route("/api/groups/mutings")
    def groups_mutings():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        group_openid = (request.args.get("group_openid") or "").strip()
        # 本地禁言按机器人分开（两个机器人各禁各的）
        bot_id = current_bot_id()
        platform = (rt.group_manager.platform_mute_state(
            group_openid, bot_id, refresh=request.args.get("refresh") in ("1", "true", "yes"))
            if group_openid else {"ok": False, "error": "缺少 group_openid", "members": []})
        return jsonify({"success": True,
                        "bot_id": bot_id,
                        "mutings": rt.group_manager.mutings(group_openid, bot_id),
                        "platform_mutings": platform.get("members") or [],
                        "global_mute": {
                            "ok": bool(platform.get("ok")),
                            "mode": platform.get("mode") or "unknown",
                            "text": platform.get("text") or "",
                            "rules": (platform.get("schedule_rules") or [])
                                     + (platform.get("recurring_rules") or []),
                            "error": platform.get("error") or "",
                        }})

    @app.route("/api/groups/settings", methods=["POST"])
    def groups_settings():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        body = request.get_json(silent=True) or {}
        group_openid = str(body.get("group_openid") or "").strip()
        if not group_openid:
            return jsonify({"success": False, "message": "缺少 group_openid"}), 400
        if body.get("reset"):
            rt.group_manager.clear_settings(group_openid)
            return jsonify({"success": True, "message": "已恢复为全局设置",
                            "settings": rt.group_manager.get_settings(group_openid)})
        result = rt.group_manager.set_settings(group_openid, body.get("settings") or {})
        result["message"] = "群配置已保存"
        return jsonify(result)

    @app.route("/api/groups/recall", methods=["POST"])
    def groups_recall():
        """撤回消息（仅限机器人自己发出的）。群里走官方接口，私聊只标记本地记录。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        body = request.get_json(silent=True) or {}
        message_id = body.get("message_id")
        msg_id = str(body.get("msg_id") or "").strip()
        if not message_id and not msg_id:
            return jsonify({"success": False, "message": "缺少 message_id 或 msg_id"}), 400
        try:
            message_id = int(message_id) if message_id not in (None, "") else 0
        except (TypeError, ValueError):
            message_id = 0
        result = rt.recall_message(message_id=message_id, msg_id=msg_id,
                                   bot_id=str(body.get("bot_id") or ""),
                                   as_admin=bool(body.get("as_admin")))
        return jsonify(result), (200 if result.get("success") else 502)

    # ================================================================== 关闭程序
    @app.route("/api/admin/shutdown", methods=["GET", "POST"])
    def admin_shutdown():
        """关闭整个程序（网页按钮）。

        GET 返回是否已请求关闭（前端用它确认状态）；
        POST 触发关闭：先停机器人、停同步，再结束进程。
        """
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        if request.method == "GET":
            return jsonify({"success": True, "shutdown": rt.is_shutdown_requested()})
        body = request.get_json(silent=True) or {}
        reason = str(body.get("reason") or "网页按钮")
        rt.request_shutdown(reason)
        return jsonify({"success": True,
                        "message": "已请求关闭程序，正在停止机器人与网页服务…"})

    @app.route("/api/admin/bots")
    def admin_bots():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None:
            return jsonify({"success": False, "message": "服务尚未就绪"}), 503
        return jsonify({"success": True, "bots": [bot.status() for bot in rt.bots.values()]})

    # ================================================================== 机器人选择（全局）
    @app.route("/api/bots")
    def bots_list():
        """左侧导航栏顶部的机器人选择器用：列出所有机器人 + 当前选中的那个。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        active = config_manager.active_bot_id()
        configured = []
        for item in (config_manager.config.get("bots") or []):
            if not isinstance(item, dict):
                continue
            bot_id = str(item.get("id") or "")
            live = rt.bots.get(bot_id) if rt is not None else None
            configured.append({
                "id": bot_id,
                "name": str(item.get("name") or bot_id),
                "enabled": bool(item.get("enabled")),
                "has_credentials": bool(item.get("app_id") and item.get("app_secret")),
                "online": bool(live and live.gateway.ready),
                "state": live.gateway.status_text() if live else "未连接",
                "configured": bool(live and live.client.configured),
                "overrides": bool(item.get("overrides")),
            })
        return jsonify({"success": True, "active": active, "bots": configured})

    @app.route("/api/bots/active", methods=["POST"])
    def bots_set_active():
        """切换当前选中的机器人。之后所有设置与聊天操作都以它为准。"""
        blocked = check_token()
        if blocked:
            return blocked
        body = request.get_json(silent=True) or {}
        bot_id = str(body.get("bot_id") or request.values.get("bot") or "").strip()
        if not bot_id:
            return jsonify({"success": False, "message": "缺少 bot_id"}), 400
        chosen = config_manager.set_active_bot(bot_id)
        if not chosen:
            return jsonify({"success": False, "message": f"机器人 {bot_id} 不存在"}), 400
        rt = runtime()
        if rt is not None:
            rt.broadcast({"type": "active_bot_changed", "bot_id": chosen})
        return jsonify({"success": True, "active": chosen,
                        "message": f"已切换到机器人 {chosen}（设置页与聊天窗口都以它为准）"})

    @app.route("/api/admin/config/copy_to_all", methods=["POST"])
    def admin_config_copy_to_all():
        """把当前机器人的设置一键复制到所有其它机器人。"""
        blocked = check_token()
        if blocked:
            return blocked
        body = request.get_json(silent=True) or {}
        bot_id = str(body.get("bot_id") or config_manager.active_bot_id()).strip()
        if not bot_id:
            return jsonify({"success": False, "message": "没有可用的机器人"}), 400
        result = config_manager.copy_bot_settings_to_all(bot_id)
        rt = runtime()
        if rt is not None and result.get("copied"):
            try:
                rt.on_config_changed(source="一键应用到所有机器人")
            except Exception as exc:
                logger.warning("复制后热更新失败：%s", exc)
        return jsonify({"success": True, **result})

    @app.route("/api/admin/config/reset_bot", methods=["POST"])
    def admin_config_reset_bot():
        """清空当前机器人的自定义设置，让它回到全局默认。"""
        blocked = check_token()
        if blocked:
            return blocked
        body = request.get_json(silent=True) or {}
        bot_id = str(body.get("bot_id") or config_manager.active_bot_id()).strip()
        if not bot_id:
            return jsonify({"success": False, "message": "没有可用的机器人"}), 400
        changed = config_manager.clear_bot_overrides(bot_id)
        rt = runtime()
        if rt is not None:
            try:
                rt.on_config_changed(source="重置机器人设置")
            except Exception as exc:
                logger.debug("重置后热更新失败：%s", exc)
        return jsonify({"success": True,
                        "message": "已清空该机器人的自定义设置，已回到全局默认" if changed
                                   else "该机器人本来就没有自定义设置"})

    @app.route("/api/admin/ping")
    def admin_ping():
        blocked = check_token()
        if blocked:
            return blocked
        return jsonify({"success": True, "time": time.strftime("%Y-%m-%d %H:%M:%S")})

    # ================================================================== 云同步
    @app.route("/api/admin/cloud")
    def admin_cloud():
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None or rt.cloud_sync is None:
            return jsonify({"success": True, "enabled": False,
                            "message": "云同步模块未就绪"})
        sync = rt.cloud_sync
        info = sync.pause_info()
        info.update({
            "success": True,
            "enabled": sync.enabled,
            "configured": bool(sync.backend() and sync.backend().configured()),
            "totals": dict(sync.totals),
            "sync_paths": list(SYNC_PATHS),
        })
        return jsonify(info)

    @app.route("/api/admin/cloud/<action>", methods=["POST"])
    def admin_cloud_action(action: str):
        """云同步操作：test（测连接）/ sync（立即同步）/ resume（解除暂停）。"""
        blocked = check_token()
        if blocked:
            return blocked
        rt = runtime()
        if rt is None or rt.cloud_sync is None:
            return jsonify({"success": False, "message": "云同步模块未就绪"}), 400
        sync = rt.cloud_sync
        if action == "test":
            result = sync.test_connection()
            return jsonify(result), (200 if result.get("ok") else 400)
        if action == "sync":
            result = sync.sync_once(force=True)
            return jsonify(result), (200 if result.get("ok") else 502)
        if action == "resume":
            return jsonify(sync.resume("网页手动解除"))
        return jsonify({"success": False, "message": "action 需要是 test/sync/resume"}), 400

    def _version() -> str:
        try:
            from core import __version__
            return __version__
        except Exception:
            return "1.0.0"

    # 把运行时与日志管理器注入（由 run.py 调用）
    def attach(rt, log_manager=None):
        state["runtime"] = rt
        state["log_manager"] = log_manager
        state["logger"] = logger_obj

    app.attach_state = attach          # type: ignore[attr-defined]
    app.state = state                  # type: ignore[attr-defined]
    return app
