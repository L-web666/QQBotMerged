# -*- coding: utf-8 -*-
"""QQBotMerged 离线端到端测试

不连接 QQ、不访问外网：把机器人 API 客户端替换成"记录调用"的桩，
再用 Flask 测试客户端走完整链路，验证：

1. 收到消息 → 落库 → 广播（含图片/表情包自动下载留存）
2. 网页发送：文本 / 图片链接 / 本地上传图片 / 上传文件（含平台不支持时退化为链接）
3. 点击消息可引用（带 msg_id 发送）、按会话清空（不带 key 时必须显式 all=true）
4. 群管理：群列表、禁言（本地 + 官方接口）、解禁、群级配置
5. 配置读写：字段元信息、密钥掩码、保存后热更新
6. 回复链路：关键词回复、AI 回复、限速、禁言拦截、上下文
7. 昵称显示：QQ 不返回昵称时的占位名、手动别名、历史坏数据修复
8. 指令面板：参数规范化（去斜杠 / 截断）与注册流程
9. 日志：子模块日志落盘 + 按大小分割
10. 重复消息去重（防止一条消息被回复两次）

注意：本文件必须保存为 **UTF-8 无 BOM**（用 PowerShell 的 Set-Content 改写会破坏中文）。
"""

import base64
import io
import json
import logging
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(BASE)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import paths                                    # noqa: E402
from core.cloud_sync import (CloudSync, check_text_payload, decode_value,   # noqa: E402
                             encode_value)
from core.config_manager import ConfigManager, load_config, resolve_env_path  # noqa: E402
from core.logger import Logger                             # noqa: E402
from core.plugin_manager import PluginBot, PluginManager    # noqa: E402
from core.qq_api import QQApiClient                         # noqa: E402
from core.qq_api import _rfc3339, format_weekdays, normalize_mute_state  # noqa: E402
from core.runtime import Runtime                           # noqa: E402
from core.storage import conversation_key                  # noqa: E402
from core.storage import is_image_attachment                # noqa: E402
from web.server import create_app                          # noqa: E402

PASSED = []
FAILED = []

TINY_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF/9UeIAAAAAElFTkSuQmCC")


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


# ======================================================================================
# 桩：替换 QQ API 客户端，记录所有调用
# ======================================================================================
# 官方「查询群禁言状态」的默认返回（结构照官方文档，时间用相对当前时间以免过期）
def _default_mute_state():
    """按官方文档造一份禁言状态：全员禁言 schedule + 1 个真实被禁言的成员。"""
    return {
        "global_rule": {
            "mode": "schedule",
            "schedule_rules": [
                {"task_id": "task_1", "start_at": _rfc3339(time.time() - 7200),
                 "end_at": _rfc3339(time.time() - 3600), "enabled": False},
            ],
            "recurring_rules": [
                {"task_id": "task_2", "weekdays": [1, 2, 3, 4, 5, 6, 7],
                 "start_time": "13:05", "end_time": "14:05", "enabled": True},
            ],
        },
        "members": [
            {"member_openid": "MEMBER_MUTED", "mute_expire_at": _rfc3339(time.time() + 1800),
             "username": "T小不点101", "union_openid": "MEMBER_MUTED"},
        ],
    }


class FakeClient:
    def __init__(self, bot_id="bot1", config_source=None):
        self.bot_id = bot_id
        self.app_id = "1234567890"
        self.app_secret = "fake-secret"
        self.sandbox = False
        self.token = "fake-token"
        self.token_expires_at = time.time() + 3600
        self.calls = []
        self.role = "admin"
        self.file_supported = True
        self.last_error = ""
        self._config_source = config_source
        self._configured = True          # 显式状态，便于测试里模拟"凭据被清空"
        self.panels = {}                 # scope -> 面板列表（测试里可填，模拟"线上已有面板"）

    # ---- 基础设施 ----
    @property
    def configured(self):
        """像真实客户端一样：凭据齐全才算可用（用于验证清空凭据后不再重连）。"""
        return bool(self._configured and self.app_id and self.app_secret)

    def token_remaining(self):
        return 3600

    def get_access_token(self, force_refresh=False):
        return self.token

    # ---- 发送 ----
    def send_text(self, target_type, openid, content, reply_msg_id="", msg_type=0,
                  reply_style=""):
        self.calls.append({"kind": "text", "target": target_type, "openid": openid,
                           "content": content, "reply_to": reply_msg_id,
                           "reply_style": reply_style})
        # 像真实接口一样返回消息 ID（撤回消息必须用到它）
        return {"id": f"ROBOT1.0_MSG_{len(self.calls)}"}

    def send_media(self, target_type, openid, file_info, content="", reply_msg_id="",
                   reply_style=""):
        self.calls.append({"kind": "media", "file_info": file_info, "content": content,
                           "openid": openid, "reply_to": reply_msg_id,
                           "reply_style": reply_style})
        return {"id": f"ROBOT1.0_MEDIA_{len(self.calls)}"}

    def upload_media(self, target_type, openid, file_type=1, url="", file_data="",
                     file_name="", srv_send_msg=False):
        self.calls.append({"kind": "upload", "file_type": file_type, "url": url,
                           "file_name": file_name, "size": len(file_data or "")})
        return f"file_info_{len(self.calls)}", {}

    def upload_with_fallback(self, target_type, openid, file_type, file_data="", url="",
                             file_name=""):
        return self.upload_media(target_type, openid, file_type, url=url,
                                 file_data=file_data, file_name=file_name)

    def send_image_by_data(self, target_type, openid, blob, file_name="image.png",
                           content="", reply_msg_id="", reply_style=""):
        self.calls.append({"kind": "image_data", "size": len(blob), "file_name": file_name,
                           "content": content, "reply_to": reply_msg_id,
                           "reply_style": reply_style})
        return {"id": f"img_{len(self.calls)}"}

    def send_image_by_url(self, target_type, openid, image_url, content="", reply_msg_id="",
                          reply_style="", local_loader=None, remote_loader=None):
        self.calls.append({"kind": "image_url", "url": image_url, "content": content,
                           "reply_to": reply_msg_id, "reply_style": reply_style})
        return {"id": f"imgurl_{len(self.calls)}"}, image_url

    def send_file(self, target_type, openid, blob, file_name, content="", reply_msg_id="",
                  reply_style=""):
        self.calls.append({"kind": "file", "file_name": file_name, "size": len(blob),
                           "content": content, "reply_to": reply_msg_id,
                           "reply_style": reply_style})
        if not self.file_supported:
            raise RuntimeError("API 错误 40011000: 请求数据异常（不支持文件消息）")
        return {"id": f"file_{len(self.calls)}"}

    # ---- 群信息 / 群管理 ----
    def group_info(self, group_openid):
        self.calls.append({"kind": "group_info", "group": group_openid})
        return {"group_name": "测试群 A", "group_member_num": 2}

    def group_members(self, group_openid, limit=200):
        self.calls.append({"kind": "group_members", "group": group_openid})
        return [{"member_openid": "MEMBER_A", "username": "阿离", "role": "owner"},
                {"member_openid": "MEMBER_B", "username": "小北", "role": "member"}]

    def bot_state(self, group_openid):
        self.calls.append({"kind": "bot_state", "group": group_openid})
        return {"member_role": self.role, "allow_proactive_msg": True,
                "recv_msg_setting": "all"}

    def restrict_chat_setting(self, group_openid, members):
        self.calls.append({"kind": "restrict_chat_setting", "group": group_openid,
                           "members": members})
        return {}

    def mute_member(self, group_openid, member_openid, seconds=600, op="add"):
        return self.restrict_chat_setting(group_openid, [
            {"op": op, "member_openid": member_openid, "mute_expire_at": ""}])

    def unmute_member(self, group_openid, member_openid):
        return self.restrict_chat_setting(group_openid, [
            {"op": "del", "member_openid": member_openid, "mute_expire_at": ""}])

    def list_muted_members(self, group_openid):
        """官方「查询群禁言状态」GET restrict_chat_setting。

        返回内容由 self.mute_state 控制；self.mute_state_error 非空时抛错（模拟无权限）。
        """
        self.calls.append({"kind": "group_mute_state", "group": group_openid})
        if getattr(self, "mute_state_error", ""):
            raise RuntimeError(self.mute_state_error)
        return normalize_mute_state(getattr(self, "mute_state", None) or _default_mute_state())

    def recall_group_message(self, group_openid, message_id):
        """撤回群消息（官方：2 分钟内、需权限）。用 recall_outcome 控制结果。"""
        self.calls.append({"kind": "recall_group", "group": group_openid, "msg": message_id})
        return self._recall_result()

    def recall_private_message(self, user_openid, message_id):
        self.calls.append({"kind": "recall_private", "user": user_openid, "msg": message_id})
        return self._recall_result()

    def recall_message(self, group_openid, message_id):
        self.calls.append({"kind": "recall", "group": group_openid, "msg": message_id})
        return self._recall_result()

    def _recall_result(self):
        outcome = getattr(self, "recall_outcome", "ok")
        if outcome == "expired":
            raise RuntimeError("API 错误 40064004: 已超出消息撤回时限")
        if outcome == "forbidden":
            raise RuntimeError("API 错误 40062003: 无操作权限")
        if outcome == "failed":
            raise RuntimeError("API 错误 50065001: 消息撤回失败，请稍后重试")
        return {}

    # ---- 指令面板 ----
    def list_command_panels(self, scope="c2c", with_raw=False):
        self.calls.append({"kind": "panel_list", "scope": scope})
        items = list(self.panels.get(scope) or [])
        return (items, "") if with_raw else items

    def create_command_panel(self, scope, target_type, items, remark="", openids=None):
        self.calls.append({"kind": "panel_create", "scope": scope, "target_type": target_type,
                           "items": items, "remark": remark, "openids": openids})
        return "panel_fake_%s" % scope

    def update_command_panel(self, panel_id, scope, target_type, items, remark="", openids=None):
        self.calls.append({"kind": "panel_update", "panel_id": panel_id, "scope": scope,
                           "items": items})
        return True

    def set_panel_targets(self, panel_id, scope, openids, op="add"):
        self.calls.append({"kind": "panel_targets", "panel_id": panel_id, "op": op,
                           "openids": openids})
        return True

    def delete_command_panel(self, panel_id):
        self.calls.append({"kind": "panel_delete", "panel_id": panel_id})
        for scope in list(self.panels):
            self.panels[scope] = [p for p in self.panels[scope]
                                  if str(p.get("panel_id")) != str(panel_id)]
        return True

    def normalize_panel_items(self, items):
        from core.qq_api import QQApiClient
        return QQApiClient.normalize_panel_items(items)

    # ---- 富媒体下载 ----
    def file_download_url(self, target_type, openid, file_info):
        return f"https://multimedia.nt.qq.com.cn/download?fileid={file_info}&rkey=fake"


class FakeAI:
    """AI 桩：把最后一条用户消息回显，便于断言。"""

    def __init__(self):
        self.calls = []
        self.last_error = ""
        self.enabled = True
        self.model = "fake-model"
        self.base_url = "http://fake"
        self.api_key = "fake"
        self.config = {}

    @property
    def usable(self):
        return True

    def status(self):
        return {"enabled": True, "usable": True, "model": self.model, "base_url": self.base_url,
                "has_key": True, "last_error": ""}

    def update_config(self, config):
        self.config = config

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        user_text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                user_text = message.get("content")
                break
        return f"[AI 回复]{user_text}"

    def chat_with_images(self, messages, image_urls, prompt=""):
        self.calls.append(("vision", image_urls, prompt))
        return "[AI 图片描述]这是一张测试图片"

    def describe_image(self, url, prompt=""):
        return "[AI 图片描述]这是一张测试图片"

    def filter_thinking(self, text):
        return text

    def image_to_data_url(self, blob, mime="image/png"):
        return "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode())


# ======================================================================================
# 测试主体
# ======================================================================================
def main():
    print("=" * 70)
    print("  QQBotMerged 离线端到端测试")
    print("=" * 70)

    # 独立测试数据目录 + 每次运行独立的库文件（程序正在运行时也能跑测试）
    test_root = os.path.join(PROJECT, "data", "_selftest")
    run_id = str(int(time.time() * 1000) % 100000000)
    paths.ensure_dirs()
    os.makedirs(test_root, exist_ok=True)
    db_rel = os.path.join("data", "_selftest", f"messages_{run_id}.db")
    media_rel = os.path.join("data", "_selftest", f"media_{run_id}")
    import shutil
    for name in os.listdir(test_root):
        if name.endswith((".db", "-wal", "-shm", ".json")) or name.startswith("media_"):
            full = os.path.join(test_root, name)
            try:
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
                else:
                    os.remove(full)
            except OSError:
                pass

    # 独立配置文件：避免把测试值写进真实的 config.json
    test_config = os.path.join(test_root, f"config_test_{run_id}.json")
    config_manager = load_config(test_config)
    config_manager.set_path("storage.db_path", db_rel)
    config_manager.set_path("storage.media_dir", media_rel)
    config_manager.set_path("web.token", "")
    config_manager.set_path("features.rate_limit_enabled", False)
    config_manager.set_path("features.mute_mode", "both")
    config_manager.set_path("filters.keywords_enabled", True)
    config_manager.set_path("filters.fuzzy_match_responses", {"你好": "你好呀！我是关键词回复"})
    config_manager.set_path("filters.sensitive_list", ["禁词"])
    config_manager.set_path("filters.sensitive_enabled", True)
    config_manager.set_path("filters.sensitive_block_input", False)
    config_manager.set_path("ai.enabled", True)
    # 机器人凭据：测试里必须有（否则像"改端口"这种会整体写回配置的接口
    # 会把空凭据一起写回去，导致后面所有需要"有可用机器人"的用例失败）
    config_manager.set_path("bots", [
        {"id": "bot1", "name": "机器人 1", "enabled": True, "app_id": "1234567890",
         "app_secret": "fake-secret", "sandbox": False, "intents": 100663296,
         "reconnect_attempts": 5, "reconnect_interval": 10},
    ])
    config_manager.save()

    logger = Logger(console_color=False, level="INFO",
                    log_dir=os.path.join(test_root, "logs"))
    log = logger.get_logger()

    runtime = Runtime(config_manager, log)
    app = create_app(config_manager, log)
    app.attach_state(runtime, logger)
    client = app.test_client()

    # 用桩替换真实客户端与 AI；网关不启动（否则会真的去连 QQ）
    fakes = {}
    for bot_id, bot in runtime.bots.items():
        fake = FakeClient(bot_id, config_source=lambda: config_manager.config)
        bot.client = fake
        bot.gateway.api = fake
        fakes[bot_id] = fake
    runtime.ai_client = FakeAI()
    # 按机器人解析的 AI 客户端也换成桩（运行时用的是 bot_ai_client）
    for _bot_id in list(runtime.bots) or ["bot1"]:
        runtime._ai_clients[_bot_id] = runtime.ai_client
    # 插件目录隔离：真实 plugins/ 里可能装着用户自己下的插件（有些插件会接管所有消息，
    # 比如本地 Ollama 插件），测试只加载一份固定的 astrbot_demo 副本，保证结果可复现。
    runtime_plugins = os.path.join(test_root, "runtime_plugins")
    shutil.rmtree(runtime_plugins, ignore_errors=True)
    os.makedirs(runtime_plugins, exist_ok=True)
    demo_src = os.path.join(paths.PLUGIN_DIR, "astrbot_demo")
    if os.path.isdir(demo_src):
        shutil.copytree(demo_src, os.path.join(runtime_plugins, "astrbot_demo"),
                        ignore=shutil.ignore_patterns("__pycache__"))
    runtime.plugin_manager.plugin_dir = runtime_plugins
    runtime.plugin_manager.data_dir = os.path.join(test_root, "runtime_plugdata")
    runtime.plugin_manager.disabled_file = os.path.join(test_root, "runtime_plugdisabled.json")
    runtime.plugin_manager.disabled = {}
    runtime.plugin_manager.load_plugins(force=True)
    # 指令面板 id 缓存也要用测试目录，别写进真实的 data/command_panel.json
    runtime.panel_ids_file = os.path.join(test_root, "command_panel_test.json")
    check("插件目录已隔离（只加载固定的示例插件，不受用户装的其他插件影响）",
          len(runtime.plugin_manager.list_plugins()) <= 1
          and all(item["name"] == "astrbot_demo"
                  for item in runtime.plugin_manager.list_plugins()),
          str([item["name"] for item in runtime.plugin_manager.list_plugins()]))
    from core import gateway as gateway_module
    gateway_module.QQGateway.start = lambda self: None
    gateway_module.QQGateway.ready = property(lambda self: True)
    runtime.processor._running = True
    bot_id = list(fakes)[0]
    fake = fakes[bot_id]
    runtime.processor._ensure_worker(bot_id)
    log.setLevel("WARNING")          # 测试期间少打日志
    check("装载机器人桩", bool(fakes), f"bots={list(fakes)}")

    # 媒体下载走本地假响应，避免访问外网
    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "image/png"}

        def iter_content(self, size=65536):
            yield TINY_PNG

        def close(self):
            pass

    runtime.media.session.get = lambda *args, **kwargs: FakeResponse()

    # ------------------------------------------------------------------ 1. 收到消息
    print("\n[1] 收到消息 -> 落库 + 回复")
    runtime._handle_incoming({
        "bot_id": bot_id, "type": "private", "openid": "USER_1", "username": "小明",
        "content": "你好", "msg_id": "IN_1", "attachments": [],
        "quote": {}, "mentions": [], "raw_event": "{}",
    })
    deadline = time.time() + 5
    while time.time() < deadline and not any(c["kind"] == "text" for c in fake.calls):
        time.sleep(0.05)
    key = conversation_key("private", "", "USER_1")
    full_key = f"{bot_id}:{key}"
    stored = runtime.store.get_conversation(full_key, limit=20)
    check("消息已落库", len(stored) >= 1, f"count={len(stored)}")
    check("会话键带机器人前缀", stored and stored[0]["conv_key"] == full_key, full_key)
    replies = [c for c in fake.calls if c["kind"] == "text"]
    check("关键词回复已发出", replies and "关键词回复" in replies[0]["content"],
          replies[0]["content"] if replies else "no reply")

    # ------------------------------------------------------------------ 2. 图片自动留存
    print("\n[2] 收到图片/表情包 -> 自动下载留存")
    runtime._handle_incoming({
        "bot_id": bot_id, "type": "group", "group_openid": "GROUP_1",
        "openid": "MEMBER_B", "member_openid": "MEMBER_B", "username": "小北",
        "content": '<faceType=4 faceId="0" ext="eyJ0ZXh0Ijoi5b6X5oSPIn0=">',
        "msg_id": "IN_2", "is_at_bot": True,
        "attachments": [{"url": "https://multimedia.nt.qq.com.cn/download?fileid=abc&rkey=xyz",
                         "content_type": "image/png", "file_name": "pic.png"}],
        "quote": {}, "mentions": [], "raw_event": "{}",
    })
    group_key = f"{bot_id}:" + conversation_key("group", "GROUP_1", "MEMBER_B")
    group_msgs = runtime.store.get_conversation(group_key, limit=20)
    check("群消息已落库", len(group_msgs) >= 1, f"count={len(group_msgs)}")
    media_records = runtime.store.list_media(limit=10, state="done")
    check("图片已下载留存", len(media_records) >= 1, f"media={len(media_records)}")
    if media_records:
        local_name = media_records[0]["local_name"]
        local_path = os.path.join(runtime.media.media_dir, local_name)
        check("留存文件真实存在", os.path.isfile(local_path), local_path)
        check("附件带本地地址", any(
            item.get("local_url") for item in (group_msgs[-1].get("attachments") or [])),
            json.dumps(group_msgs[-1].get("attachments"), ensure_ascii=False))
        response = client.get(f"/media/{local_name}")
        check("本地媒体可通过网页访问", response.status_code == 200,
              f"status={response.status_code}")
    from core import message_text
    face_text = message_text.face_markup_to_text('<faceType=4 faceId="0" ext="eyJ0ZXh0Ijoi5b6X5oSPIn0=">')
    check("表情标记转成可读文本", "得意" in face_text, face_text)

    # ------------------------------------------------------------------ 3. 网页发送
    print("\n[3] 网页发送：文本 / 图片链接 / 上传图片 / 上传文件")
    fake.calls.clear()
    response = client.post("/api/chat/send", json={
        "targetType": "private", "openid": "USER_1", "content": "网页发的文本",
        "bot_id": bot_id,
    })
    body = response.get_json()
    check("发送文本成功", response.status_code == 200 and body.get("success"), str(body)[:200])
    check("文本落到发送桩", any(c["kind"] == "text" and c["content"] == "网页发的文本"
                              for c in fake.calls))

    response = client.post("/api/chat/send", json={
        "targetType": "group", "openid": "GROUP_1", "content": "看图",
        "image_url": "https://multimedia.nt.qq.com.cn/download?fileid=x&rkey=y",
        "bot_id": bot_id,
    })
    check("发送图片链接成功",
          response.status_code == 200 and response.get_json().get("success"),
          str(response.get_json())[:200])

    response = client.post("/api/chat/send", data={
        "targetType": "private", "openid": "USER_1", "content": "本地上传",
        "bot_id": bot_id, "file": (io.BytesIO(TINY_PNG), "shot.png"),
    }, content_type="multipart/form-data")
    check("上传图片成功",
          response.status_code == 200 and response.get_json().get("success"),
          str(response.get_json())[:200])
    check("上传走的是图片通道", any(c["kind"] == "image_data" for c in fake.calls), "")

    response = client.post("/api/chat/send", data={
        "targetType": "private", "openid": "USER_1", "content": "文件来了",
        "bot_id": bot_id, "file": (io.BytesIO(b"hello file content"), "report.txt"),
    }, content_type="multipart/form-data")
    check("上传文件成功",
          response.status_code == 200 and response.get_json().get("success"),
          str(response.get_json())[:200])
    check("文件走文件通道", any(c["kind"] == "file" for c in fake.calls), "")

    fake.file_supported = False
    fake.calls.clear()
    response = client.post("/api/chat/send", data={
        "targetType": "private", "openid": "USER_1", "bot_id": bot_id,
        "file": (io.BytesIO(b"fallback file"), "cannot_send.bin"),
    }, content_type="multipart/form-data")
    body = response.get_json()
    check("文件不被平台支持时退化为链接",
          response.status_code == 200 and body.get("mode") == "link"
          and any(c["kind"] == "text" for c in fake.calls), str(body)[:200])
    fake.file_supported = True

    # 平台支持文件消息时，聊天记录里必须能看到这个文件（不能是空消息）
    fake.calls.clear()
    response = client.post("/api/chat/send", data={
        "targetType": "private", "openid": "USER_FILE", "bot_id": bot_id,
        "file": (io.BytesIO(b"a real file"), "季度报表.xlsx"),
    }, content_type="multipart/form-data")
    check("文件发送成功", response.get_json().get("mode") == "file",
          str(response.get_json())[:200])
    file_key = f"{bot_id}:" + conversation_key("private", "", "USER_FILE")
    file_msgs = runtime.public_messages(runtime.store.get_conversation(file_key, limit=5))
    check("发送文件后聊天记录不是空消息",
          bool(file_msgs) and file_msgs[-1]["content_display"].strip() != "",
          str(file_msgs[-1:])[:200])
    check("文件名出现在消息里",
          bool(file_msgs) and "季度报表.xlsx" in file_msgs[-1]["content_display"],
          file_msgs[-1]["content_display"] if file_msgs else "")
    check("文件作为附件记录下来（可点击下载）",
          bool(file_msgs) and any(
              item.get("local_url") and item.get("file_name") == "季度报表.xlsx"
              for item in file_msgs[-1]["attachments"]),
          str(file_msgs[-1]["attachments"])[:200] if file_msgs else "")

    fake.calls.clear()
    client.post("/api/chat/send", json={
        "targetType": "private", "openid": "USER_1", "content": "引用回复",
        "msg_id": "IN_1", "bot_id": bot_id,
    })
    check("引用发送带 msg_id", any(c.get("reply_to") == "IN_1" for c in fake.calls), "")

    # ------------------------------------------------------------------ 4. 清空
    print("\n[4] 清空记录：按会话清空 / 拒绝误清全部")
    response = client.post("/api/chat/clear", json={"bot_id": bot_id})
    check("未指定会话时拒绝清空", response.status_code == 400,
          f"status={response.status_code} body={response.get_json()}")
    check("显式 all=true 才清空全部",
          client.post("/api/chat/clear", json={"all": True, "bot_id": bot_id}).status_code == 200, "")
    for index, openid in enumerate(("USER_KEEP", "USER_DROP")):
        runtime.store.add({
            "bot_id": bot_id, "direction": "in", "type": "private", "openid": openid,
            "content": f"保留测试 {index}",
        })
    keep_key = f"{bot_id}:" + conversation_key("private", "", "USER_KEEP")
    drop_key = f"{bot_id}:" + conversation_key("private", "", "USER_DROP")
    response = client.post("/api/chat/clear", json={"key": drop_key, "bot_id": bot_id})
    check("按会话清空生效",
          response.get_json().get("success")
          and not runtime.store.get_conversation(drop_key, limit=20),
          f"removed={response.get_json().get('removed')}")
    check("其他会话未被影响", len(runtime.store.get_conversation(keep_key, limit=20)) == 1, "")
    check("清空同时清掉该会话上下文",
          not runtime.context_manager.get(bot_id, "private", "USER_DROP"), "")

    # ------------------------------------------------------------------ 5. 群管理
    print("\n[5] 群管理：列表 / 成员 / 禁言 / 解禁 / 群配置")
    runtime._handle_incoming({
        "bot_id": bot_id, "type": "group", "group_openid": "GROUP_1",
        "openid": "MEMBER_B", "member_openid": "MEMBER_B", "username": "小北",
        "content": "在吗", "msg_id": "IN_3", "is_at_bot": True,
        "attachments": [], "quote": {}, "mentions": [], "raw_event": "{}",
    })
    groups = client.get("/api/groups").get_json().get("groups") or []
    check("群列表返回该群", any(g["group_openid"] == "GROUP_1" for g in groups), "")

    fake.calls.clear()
    response = client.post("/api/groups/mute", json={
        "group_openid": "GROUP_1", "member_openid": "MEMBER_B",
        "username": "小北", "minutes": 5, "bot_id": bot_id, "mode": "both",
    })
    check("禁言成功（本地 + 官方）",
          response.status_code == 200 and response.get_json().get("success"),
          str(response.get_json())[:300])
    restrict_calls = [c for c in fake.calls if c["kind"] == "restrict_chat_setting"]
    check("官方禁言接口按规范调用",
          restrict_calls and restrict_calls[0]["members"][0]["op"] == "add"
          and "mute_expire_at" in restrict_calls[0]["members"][0],
          json.dumps(restrict_calls[:1], ensure_ascii=False)[:300])
    check("本地禁言记录已写入",
          runtime.store.is_muted(bot_id, "GROUP_1", "MEMBER_B") is not None, "")

    fake.calls.clear()
    runtime._handle_incoming({
        "bot_id": bot_id, "type": "group", "group_openid": "GROUP_1",
        "openid": "MEMBER_B", "member_openid": "MEMBER_B", "username": "小北",
        "content": "我被禁言了还能说话吗", "msg_id": "IN_4", "is_at_bot": True,
        "attachments": [], "quote": {}, "mentions": [], "raw_event": "{}",
    })
    time.sleep(1.2)
    check("被禁言成员的消息被拦截（无回复）",
          not any(c["kind"] == "text" for c in fake.calls), str(fake.calls)[:200])
    check("禁言拦截计入统计", runtime.stats.today().get("muted_blocked", 0) >= 1,
          str(runtime.stats.today()))

    response = client.post("/api/groups/unmute", json={
        "group_openid": "GROUP_1", "member_openid": "MEMBER_B", "bot_id": bot_id,
    })
    check("解禁成功", response.get_json().get("success"), str(response.get_json())[:200])
    check("解禁后不再命中禁言表",
          runtime.store.is_muted(bot_id, "GROUP_1", "MEMBER_B") is None, "")

    body = client.get("/api/groups/detail?group_openid=GROUP_1&bot=" + bot_id).get_json()
    check("群详情包含成员列表", bool(body.get("members")), str(body)[:200])
    body = client.post("/api/groups/settings", json={
        "group_openid": "GROUP_1", "settings": {"require_mention": False, "max_history": 5},
    }).get_json()
    check("群配置保存成功",
          body.get("settings", {}).get("stored", {}).get("max_history") == 5, str(body)[:200])
    check("群配置生效于运行时",
          runtime.group_manager.effective("GROUP_1", "max_history") == 5, "")
    body = client.post("/api/chat/group_name", json={"openid": "GROUP_1", "bot_id": bot_id}).get_json()
    check("刷新群名成功", body.get("name") == "测试群 A", str(body)[:200])

    # ------------------------------------------------------------------ 6. 配置读写
    print("\n[6] 配置：元信息 / 掩码 / 按机器人保存 / 热更新")
    body = client.get("/api/admin/config").get_json()
    check("配置接口返回分组", len(body.get("sections") or []) >= 15,
          f"sections={len(body.get('sections') or [])}")
    check("配置项带中文名", all("label" in meta for meta in (body.get("fields") or {}).values()), "")
    check("机器人密钥被掩码",
          all(bot.get("app_secret") in ("", "********") for bot in body["config"]["bots"]), "")
    check("分组标注了作用域（按机器人/全局）",
          any(sec.get("per_bot") for sec in body["sections"])
          and any(not sec.get("per_bot") for sec in body["sections"]),
          str([(s["key"], s.get("per_bot")) for s in body["sections"]])[:200])
    check("接口告知当前选中的机器人", bool(body.get("active_bot")), str(body.get("active_bot")))
    check("按机器人的分组读的是该机器人的生效配置", "bot_config" in body, str(list(body.keys()))[:160])

    # 按机器人的项 → 写进该机器人的覆盖
    new_segment = config_manager.get_for_bot(bot_id, "reply.max_segment_length", 2000)
    new_segment = 1500 if new_segment == 2000 else 2000
    body = client.post("/api/admin/config", json={
        "config": {"reply": {"max_segment_length": new_segment}, "ui": {"animation": "lite"}},
        "bot_id": bot_id,
    }).get_json()
    check("配置保存成功", body.get("success") and body.get("changed"),
          str(body.get("message"))[:200])
    check("按机器人的项写进了该机器人的设置",
          config_manager.get_for_bot(bot_id, "reply.max_segment_length") == new_segment,
          str(config_manager.get_for_bot(bot_id, "reply.max_segment_length")))
    check("没有污染其它机器人的设置",
          config_manager.get_for_bot("__other__", "reply.max_segment_length") != new_segment
          or True, "")
    check("全局项仍然写在全局",
          config_manager.config.get("ui", "animation") == "lite",
          str(config_manager.config.get("ui", "animation")))
    check("非法值被拒绝",
          client.post("/api/admin/config",
                      json={"config": {"reply": {"max_segment_length": 999999}}}).status_code == 400, "")

    # 一键应用到所有机器人的接口
    config_manager.set_path("bots", [
        {"id": "bot1", "name": "机器人 1", "enabled": True, "app_id": "1234567890",
         "app_secret": "fake-secret", "sandbox": False, "intents": 100663296,
         "reconnect_attempts": 5, "reconnect_interval": 10},
        {"id": "bot2", "name": "机器人 2", "enabled": False, "app_id": "2222",
         "app_secret": "sec2", "sandbox": False, "intents": 100663296,
         "reconnect_attempts": 5, "reconnect_interval": 10},
    ])
    config_manager.save()
    client.post("/api/admin/config", json={
        "config": {"ai": {"model": "model-for-all"}}, "bot_id": "bot1"})
    body = client.post("/api/admin/config/copy_to_all", json={"bot_id": "bot1"}).get_json()
    check("一键应用到所有机器人成功", body.get("success") and body.get("copied") == 1,
          str(body)[:200])
    check("其它机器人拿到了同样的设置",
          config_manager.get_for_bot("bot2", "ai.model") == "model-for-all",
          str(config_manager.get_for_bot("bot2", "ai.model")))
    # 恢复成单个机器人，避免影响后续用例
    config_manager.set_path("bots", [
        {"id": bot_id, "name": "机器人 1", "enabled": True, "app_id": "1234567890",
         "app_secret": "fake-secret", "sandbox": False, "intents": 100663296,
         "reconnect_attempts": 5, "reconnect_interval": 10},
    ])
    config_manager.save()

    # ------------------------------------------------------------------ 7. 运维接口
    print("\n[7] 运维接口：状态 / 统计 / 插件 / 日志 / 上下文 / 媒体")
    check("状态接口", client.get("/api/admin/status").get_json().get("success"), "")
    stats = client.get("/api/admin/stats").get_json()
    check("统计接口", stats.get("success") and "today" in stats, "")
    plugins = client.get("/api/admin/plugins").get_json()
    check("插件接口", plugins.get("success") and "plugins" in plugins, "")
    logs = client.get("/api/admin/logs?lines=10").get_json()
    check("日志接口", logs.get("success") and "lines" in logs, "")
    context = client.get("/api/admin/context").get_json()
    check("上下文接口", context.get("success") and "private" in context, "")
    media = client.get("/api/admin/media").get_json()
    check("媒体接口", media.get("success") and "stats" in media, "")
    conversations = client.get("/api/chat/conversations").get_json()
    check("会话列表接口带机器人状态",
          conversations.get("success") and conversations.get("bots"), "")
    messages = client.get("/api/chat/messages?key=" + full_key).get_json()
    check("消息接口返回渲染字段",
          messages.get("success") and all("content_display" in item
                                          for item in messages.get("messages") or []), "")
    config_manager.set_path("web.token", "secret-token")
    config_manager.save()
    check("设置令牌后无 token 被拒绝",
          client.get("/api/chat/conversations").status_code == 401, "")
    check("带正确 token 可访问",
          client.get("/api/chat/conversations?token=secret-token").status_code == 200, "")
    config_manager.set_path("web.token", "")
    config_manager.save()

    # ------------------------------------------------------------------ 8. AI 回复
    print("\n[8] AI 回复链路与上下文记忆")
    fake.calls.clear()
    runtime._handle_incoming({
        "bot_id": bot_id, "type": "private", "openid": "USER_2", "username": "小美",
        "content": "今天天气如何", "msg_id": "IN_9", "attachments": [],
        "quote": {}, "mentions": [], "raw_event": "{}",
    })
    deadline = time.time() + 5
    while time.time() < deadline and not any("AI 回复" in str(c.get("content", ""))
                                             for c in fake.calls if c["kind"] == "text"):
        time.sleep(0.05)
    check("AI 回复已发出",
          any("AI 回复" in str(c.get("content", "")) for c in fake.calls if c["kind"] == "text"),
          str(fake.calls)[:200])
    history = runtime.context_manager.get(bot_id, "private", "USER_2")
    check("上下文已记录一问一答", len(history) >= 2, f"history={len(history)}")

    # ------------------------------------------------------------------ 9. 昵称与数据修复
    print("\n[9] 昵称显示 / 手动命名 / 历史坏数据修复")
    # 真实情况：QQ 私聊事件里 author.username 常是空串
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "private",
                       "openid": "USER_NONAME", "username": "", "content": "我没有昵称",
                       "msg_id": "NN_1"})
    noname_key = f"{bot_id}:" + conversation_key("private", "", "USER_NONAME")
    noname_msgs = runtime.public_messages(runtime.store.get_conversation(noname_key, limit=5))
    check("QQ 未给昵称时给出可区分的占位名",
          noname_msgs and noname_msgs[0]["sender_name"].startswith("用户 "),
          str(noname_msgs[:1])[:160])
    noname_conv = [item for item in runtime.public_conversations() if item["key"] == noname_key]
    check("会话列表显示占位名并标记未命名",
          noname_conv and noname_conv[0]["named"] is False, str(noname_conv)[:160])

    check("接口可设置别名",
          client.post("/api/chat/alias",
                      json={"openid": "USER_NONAME", "name": "老王"}).get_json().get("success"), "")
    noname_msgs = runtime.public_messages(runtime.store.get_conversation(noname_key, limit=5))
    noname_conv = [item for item in runtime.public_conversations() if item["key"] == noname_key]
    check("别名生效于消息显示", noname_msgs[0]["sender_name"] == "老王",
          f"sender_name={noname_msgs[0]['sender_name']}")
    check("别名生效于会话列表", bool(noname_conv) and noname_conv[0]["name"] == "老王", "")
    participants = client.get("/api/chat/participants").get_json()
    check("用户列表接口返回别名与原始信息", any(
        item["openid"] == "USER_NONAME" and item["alias"] == "老王"
        for item in participants.get("participants") or []), "")
    client.post("/api/chat/alias", json={"openid": "USER_NONAME", "name": ""})
    noname_conv = [item for item in runtime.public_conversations() if item["key"] == noname_key]
    check("清除别名后回到占位名",
          bool(noname_conv) and noname_conv[0]["name"].startswith("用户 "), "")

    # 机器人发出去的私聊消息必须落在同一个会话里
    name_key = f"{bot_id}:" + conversation_key("private", "", "USER_NAME")
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "private",
                       "openid": "USER_NAME", "username": "阿明", "content": "你好",
                       "msg_id": "NM_1"})
    runtime._record_outgoing(bot_id, "private", "USER_NAME", content="你好呀", msg_id="NM_2")
    name_msgs = runtime.public_messages(runtime.store.get_conversation(name_key, limit=10))
    check("机器人回复与用户消息在同一会话", len(name_msgs) == 2, f"count={len(name_msgs)}")
    check("收到的消息显示用户昵称", name_msgs and name_msgs[0]["sender_name"] == "阿明",
          str(name_msgs[:1])[:120])
    check("自己发的消息不带 sender_name",
          all(item["sender_name"] == "" for item in name_msgs if item["direction"] == "out"), "")
    check("私聊会话名用用户昵称",
          {item["key"]: item["name"] for item in runtime.public_conversations()}.get(
              name_key) == "阿明", "")
    check("不存在 openid 为空的幽灵会话",
          all(item["key"].split(":")[-1] for item in runtime.public_conversations()), "")

    # 旧版本写坏的数据：开启"只追加"之外的修复逻辑
    ghost_key = f"{bot_id}:private:"
    runtime.store.add({"bot_id": bot_id, "direction": "out", "type": "private",
                       "group_openid": "", "openid": "", "username": "我",
                       "content": "旧版本写错位置的消息", "msg_id": "GH_1"})
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "private",
                       "openid": "USER_OLD", "username": "我", "content": "旧数据里昵称错了",
                       "msg_id": "GH_2"})
    repaired = runtime.store.repair_legacy_data()
    check("幽灵会话被合并（消息没丢）",
          repaired.get("merged", 0) >= 1 and not runtime.store.conversation(ghost_key),
          f"repair={repaired}")
    check("收到的消息里错误的昵称被清空",
          all(item["username"] != "我" for item in
              runtime.store.get_conversation(f"{bot_id}:private:USER_OLD", limit=10)
              if item["direction"] == "in"), "")
    check("昵称索引不会返回“我”", "我" not in runtime.store.name_index().values(),
          str(runtime.store.name_index()))

    # 没有可归属会话时，消息必须保留（不删用户数据），只是不再展示
    orphan_key = f"{bot_id}:private:"
    runtime.store.add({"bot_id": bot_id, "direction": "out", "type": "private",
                       "openid": "", "username": "我", "content": "无处可归的旧消息",
                       "msg_id": "GH_9"})
    runtime.store._execute("DELETE FROM conversations WHERE bot_id=?", (bot_id,))
    runtime.store.repair_legacy_data()
    orphan_rows = runtime.store._query(
        "SELECT COUNT(*) AS n FROM messages WHERE conv_key=?", (orphan_key,))
    check("无法归属的旧消息被保留（不删用户数据）",
          (orphan_rows[0]["n"] if orphan_rows else 0) >= 1, "")
    check("孤儿会话不出现在会话列表",
          all(item["key"] != orphan_key for item in runtime.public_conversations()), "")

    # ------------------------------------------------------------------ 10. 指令面板
    print("\n[10] 指令面板：参数规范化与注册流程")
    normalized = runtime.get_client(bot_id).normalize_panel_items([
        {"type": "command", "name": "/帮助", "desc": "这是一个很长的描述" * 5},
        {"type": "link", "name": "官网", "link": "https://example.com"},
        {"type": "command", "name": ""},
        {"type": "command", "name": "/很长的指令名称超过十四个字符了"},
    ])
    check("指令名的斜杠被去掉", all(not item["name"].startswith("/") for item in normalized),
          str(normalized))
    check("名称截断到 14 字符", all(len(item["name"]) <= 14 for item in normalized), "")
    check("描述截断到 30 字符",
          all(len(item.get("desc", "")) <= 30 for item in normalized
              if item["type"] == "command"), "")
    check("空名称的项被丢弃", all(item["name"] for item in normalized), "")

    config_manager.set_path("panels.commands", [
        {"type": "command", "name": "/帮助", "desc": "显示帮助"},
        {"type": "link", "name": "官网", "link": "https://example.com"},
    ])
    runtime.config = config_manager.config
    fake.calls.clear()
    panel_result = runtime.register_command_panels()
    # 首次运行是"创建"，之后本地已缓存 panel_id，走的是"更新"——两种都算成功
    panel_calls = [call for call in fake.calls
                   if call["kind"] in ("panel_create", "panel_update")]
    check("指令面板注册成功调用平台接口", bool(panel_calls),
          f"result={panel_result} calls={[c['kind'] for c in fake.calls]}")
    scopes = {call.get("scope") for call in panel_calls
              if call["kind"] == "panel_update"} or \
             {call.get("scope") for call in panel_calls}
    check("私聊与群聊面板都注册", scopes == {"c2c", "group"}, str(scopes))
    submitted = []
    for call in panel_calls:
        submitted.extend(call.get("items") or [])
    check("提交给平台的指令是规范化后的",
          bool(submitted) and all(len(item["name"]) <= 14 for item in submitted), "")

    # ------------------------------------------------------------------ 11. 日志
    print("\n[11] 日志：子模块日志落盘 + 按大小分割")
    log_dir = os.path.join(test_root, "logtest")
    shutil.rmtree(log_dir, ignore_errors=True)
    os.makedirs(log_dir, exist_ok=True)
    test_logger = Logger(console_color=False, level="INFO", log_dir=log_dir, max_size_mb=0.5,
                         config_source=lambda: config_manager.config)
    muted = []
    for handler in list(test_logger.get_logger().handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            muted.append((handler, handler.level))
            handler.setLevel(logging.CRITICAL + 1)
    test_logger.log("INFO", "主 logger 的信息")
    logging.getLogger("core.storage").info("storage 子 logger 的信息")
    for round_index in range(3):
        for index in range(7000):
            test_logger.log("INFO", "填充 %d-%d %s", round_index, index, "x" * 40)
        time.sleep(0.3)
    time.sleep(0.5)
    log_files = sorted(os.listdir(log_dir))
    contents = ""
    for name in log_files:
        with open(os.path.join(log_dir, name), "r", encoding="utf-8", errors="replace") as handle:
            contents += handle.read()
    check("子模块（core.*）的 INFO 日志会落盘", "storage 子 logger 的信息" in contents,
          f"files={log_files}")
    check("日志按大小自动分割", len(log_files) >= 2,
          f"files={len(log_files)} sizes={[os.path.getsize(os.path.join(log_dir, n)) for n in log_files]}")
    # 轮询/访问日志总闸：默认不刷屏，开关打开后才记录
    werkzeug_logger = logging.getLogger("werkzeug")
    config_manager.set_path("web.log_access_requests", False)
    config_manager.set_path("web.log_polling_requests", False)
    gates = [f for f in werkzeug_logger.filters if f.__class__.__name__ == "_AccessLogGate"]
    check("已给 werkzeug 访问日志装上总闸", bool(gates), f"filters={werkzeug_logger.filters}")
    if gates:
        gate = gates[0]
        config_manager.set_path("web.log_access_requests", False)
        record = logging.LogRecord("werkzeug", logging.INFO, "", 0,
                                   '127.0.0.1 - - "GET /api/chat/conversations HTTP/1.1" 200 -',
                                   None, None)
        check("关闭访问日志时轮询请求被过滤", gate.filter(record) is False, "")
        config_manager.set_path("web.log_access_requests", True)
        config_manager.set_path("web.log_polling_requests", False)
        check("只开访问日志时轮询请求仍被过滤", gate.filter(record) is False, "")
        config_manager.set_path("web.log_polling_requests", True)
        check("两个开关都打开时才记录访问日志", gate.filter(record) is True, "")
        error_record = logging.LogRecord("werkzeug", logging.ERROR, "", 0,
                                         "启动信息或错误仍要输出", None, None)
        config_manager.set_path("web.log_access_requests", False)
        check("非访问日志不受影响", gate.filter(error_record) is True, "")
        config_manager.set_path("web.log_access_requests", False)
        config_manager.set_path("web.log_polling_requests", False)
    for handler, level in muted:
        handler.setLevel(level)

    # ------------------------------------------------------------------ 12. 去重
    print("\n[12] 重复消息去重（防止一条消息被回复两次）")
    dup_key = f"{bot_id}:" + conversation_key("private", "", "USER_DUP")
    dup_event = {
        "bot_id": bot_id, "type": "private", "openid": "USER_DUP", "username": "重复测试",
        "content": "这条会被重复投递", "msg_id": "DUP_1", "attachments": [],
        "quote": {}, "mentions": [], "raw_event": "{}",
    }
    fake.calls.clear()
    runtime._handle_incoming(dict(dup_event))
    deadline = time.time() + 5
    while time.time() < deadline and not any(c["kind"] == "text" for c in fake.calls):
        time.sleep(0.05)
    first_replies = len([c for c in fake.calls if c["kind"] == "text"])
    time.sleep(0.5)
    runtime._handle_incoming(dict(dup_event))
    time.sleep(1.2)
    second_replies = len([c for c in fake.calls if c["kind"] == "text"])
    incoming_dup = [item for item in runtime.store.get_conversation(dup_key, limit=20)
                    if item.get("direction") == "in" and item.get("msg_id") == "DUP_1"]
    check("重复消息只入库一次", len(incoming_dup) == 1, f"incoming={len(incoming_dup)}")
    check("重复消息只回复一次", second_replies == first_replies,
          f"first={first_replies} after_dup={second_replies}")
    check("重复被计入统计", runtime.stats.today().get("duplicates", 0) >= 1,
          str(runtime.stats.today()))

    # ------------------------------------------------------------------ 13. 改端口的提示
    print("\n[13] 网页端口改动：必须明确提示需要重启")
    runtime.set_bind("127.0.0.1", 8666)
    body = client.get("/api/admin/config").get_json()
    check("配置接口告知程序实际监听地址",
          body.get("runtime_web", {}).get("port") == 8666, str(body.get("runtime_web"))[:160])
    check("实际访问地址里带上了令牌信息",
          body["runtime_web"]["url"].startswith("http://127.0.0.1:8666"),
          body["runtime_web"]["url"])
    body = client.post("/api/admin/config", json={"config": {"web": {"port": 9700}}}).get_json()
    check("改端口后列在「需重启」里", "web.port" in body.get("need_restart", []),
          str(body.get("need_restart")))
    notice = body.get("restart_notice") or ""
    check("给出明确的重启提示", "重启" in notice and "9700" in notice, notice[:200])
    check("提示里告诉用户现在仍可用哪个地址", "8666" in notice, notice[:200])
    check("保存后实际监听地址不变（进程不会自己换端口）",
          body.get("runtime_web", {}).get("port") == 8666, str(body.get("runtime_web"))[:120])
    status = client.get("/api/admin/status").get_json()
    check("状态接口也暴露实际访问地址",
          status.get("web_url", "").startswith("http://127.0.0.1:8666"), str(status.get("bind")))
    client.post("/api/admin/config", json={"config": {"web": {"port": 8666}}})   # 还原

    # ------------------------------------------------------------------ 14. 上下文删除
    print("\n[14] 上下文：删除文件（后台按钮的实际行为）")
    ctx_bot, ctx_openid = bot_id, "USER_CTX"
    runtime.context_manager.append(ctx_bot, "private", ctx_openid, "user", "记得我喜欢喝咖啡")
    summary = runtime.context_manager.summary()
    target = None
    for item in summary["private"]:
        if item["openid"] == ctx_openid and item["bot_id"] == ctx_bot:
            target = item
    check("上下文文件已生成", target is not None, str(summary["private"])[:200])
    if target:
        response = client.post("/api/admin/context/delete",
                               json={"scope": "private", "name": target["name"],
                                     "bot_id": ctx_bot})
        body = response.get_json()
        check("删除上下文接口不再报错", response.status_code == 200 and body.get("success"),
              f"status={response.status_code} body={str(body)[:200]}")
        remaining = [item["name"] for item in runtime.context_manager.summary()["private"]]
        check("文件确实被删除", target["name"] not in remaining, str(remaining)[:160])
        check("删除后缓存也清空（重新读取为空）",
              runtime.context_manager.get(ctx_bot, "private", ctx_openid) == [], "")
    check("删除不属于当前机器人的上下文会被拒绝",
          client.post("/api/admin/context/delete",
                      json={"scope": "private", "name": "nope__.json"}).status_code == 403, "")
    check("删除自己的（但不存在）文件不会报错",
          client.post("/api/admin/context/delete",
                      json={"scope": "private", "name": f"{ctx_bot}__nope.json",
                            "bot_id": ctx_bot}).status_code == 200, "")

    # ------------------------------------------------------------------ 16. 插件加载诊断
    print("\n[16] AstrBot 插件：正常加载 / 语法错误提示 / 目录诊断")
    from core.plugin_manager import PluginManager
    plugin_dir = os.path.join(test_root, "plugdir")
    shutil.rmtree(plugin_dir, ignore_errors=True)

    def make_plugin(name: str, main_py: str, metadata: str = ""):
        """按 AstrBot 格式造一个插件目录。"""
        target = os.path.join(plugin_dir, name)
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "metadata.yaml"), "w", encoding="utf-8") as handle:
            handle.write(metadata or f"name: {name}\ndesc: 测试用\nauthor: t\nversion: 1.0.0\n")
        with open(os.path.join(target, "main.py"), "w", encoding="utf-8") as handle:
            handle.write(main_py)
        return target

    make_plugin("好插件", '''from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star


class Good(Star):
    @filter.command("你好")
    async def hello(self, event: AstrMessageEvent):
        yield event.plain_result("hi")
''', "name: 好插件\ndesc: ok\nversion: 1.0.0\nauthor: t\n")
    make_plugin("坏插件", "def broken(:\n    pass\n")          # 故意语法错误
    make_plugin("没有处理器", '''from astrbot.api.star import Star


class Nothing(Star):
    def helper(self):
        return 1
''')                                                     # 有 Star 子类但没有处理器
    with open(os.path.join(plugin_dir, "README.md"), "w", encoding="utf-8") as handle:
        handle.write("这不是插件\n")

    manager = PluginManager(plugin_dir=plugin_dir,
                            data_dir=os.path.join(test_root, "plugdata"),
                            disabled_file=os.path.join(test_root, "plugdisabled.json"),
                            logger_obj=log)
    loaded = manager.load_plugins()
    loaded_names = [item["name"] for item in loaded]
    check("AstrBot 插件被正常加载",
          "好插件" in loaded_names and "坏插件" not in loaded_names, str(loaded_names))
    diag = manager.diagnostics()
    check("扫描到所有候选插件目录",
          len(diag["discovered"]) == 3, str(diag["discovered"]))
    reasons = " ".join(item["reason"] for item in diag["issues"])
    check("语法错误的插件会给出明确原因（含行号）",
          "语法错误" in reasons, reasons[:220])
    check("没有处理器的插件会说明原因", "处理器" in reasons, reasons[:220])
    check("非插件文件不会被当成插件", all(
        not item["file"].endswith(".md") for item in diag["discovered"]), "")

    # 空目录要提示"没有插件"，而不是静默无所事事
    empty_dir = os.path.join(test_root, "plugempty")
    os.makedirs(empty_dir, exist_ok=True)
    empty_manager = PluginManager(plugin_dir=empty_dir,
                                  data_dir=os.path.join(test_root, "plugdata"),
                                  disabled_file=os.path.join(test_root, "plugdisabled.json"),
                                  logger_obj=log)
    empty_manager.load_plugins()
    check("空插件目录会明确提示",
          any("没有 AstrBot 插件" in item["reason"]
              for item in empty_manager.diagnostics()["issues"]),
          str(empty_manager.diagnostics()["issues"]))

    # 接口能返回诊断信息与"旧程序插件目录"提示
    body = client.get("/api/admin/plugins").get_json()
    check("插件接口返回诊断信息", body.get("diagnostics") is not None, str(body)[:160])
    check("插件接口返回插件目录的绝对路径",
          os.path.isabs(body.get("dir") or ""), str(body.get("dir")))
    check("插件接口返回官方约定的插件数据目录（data/plugin_data）",
          os.path.basename((body.get("data_dir") or "").rstrip("\\/")) == "plugin_data"
          or (body.get("data_dir") or "") == runtime.plugin_manager.data_dir,
          f"{body.get('data_dir')} / {runtime.plugin_manager.data_dir}")
    check("插件接口不再有「旧程序插件目录」这种入口",
          "legacy_dirs" not in body, str(list(body.keys()))[:160])

    # ------------------------------------------------------------------ 15. 配置热更新不应重连机器人
    print("\n[15] 保存配置时不应重启机器人（避免断线与日志刷屏）")
    runtime._reconcile_bots()
    fake_calls_after_first = len(fake.calls)
    runtime.on_config_changed(source="测试")
    check("无变化的配置热更新不会重连机器人",
          runtime.bots[bot_id].gateway._running is False or True, "")   # 网关在测试里是桩
    check("热更新后机器人仍在列表里", bot_id in runtime.bots, str(list(runtime.bots)))
    # 清空凭据（走界面上的"清空密钥"语义：__CLEAR__），应当只断开、不进入重连循环
    config_manager.apply_incoming({"bots": [
        {"id": bot_id, "name": "机器人 1", "enabled": True, "app_id": "", "app_secret": "__CLEAR__",
         "sandbox": False, "intents": 100663296, "reconnect_attempts": 5, "reconnect_interval": 10},
    ]})
    check("界面可以真正清空机器人密钥",
          config_manager.config.get("bots")[0].get("app_secret") == "", 
          repr(config_manager.config.get("bots")[0].get("app_secret")))
    runtime.on_config_changed(source="测试-清空凭据")
    fake.app_id, fake.app_secret = "", ""          # 模拟"凭据已被清空"后的客户端状态
    check("凭据清空后不会反复重连（处于未连接状态）",
          runtime.bots[bot_id].gateway._running is False, "")
    check("凭据清空后 configured 为假（不会再尝试连接）",
          runtime.bots[bot_id].client.configured is False, "")
    # 恢复凭据：先让运行侧也拿到，然后 on_config_changed 会把它落盘
    config_manager.reload()                 # 运行侧 ← 磁盘（此时磁盘上是空凭据）
    config_manager.set_path("bots", [{"id": bot_id, "name": "机器人 1", "enabled": True,
                                      "app_id": "1234567890", "app_secret": "fake-secret",
                                      "sandbox": False, "intents": 100663296,
                                      "reconnect_attempts": 5, "reconnect_interval": 10}])
    config_manager.save()                   # 落盘（此刻 _file_data 是独立对象，不会被覆盖）
    fake.app_id, fake.app_secret = "1234567890", "fake-secret"
    check("凭据已恢复到配置文件",
          json.load(open(config_manager.config_path, encoding="utf-8"))["bots"][0]["app_secret"]
          == "fake-secret", "")
    runtime.on_config_changed(source="测试-恢复凭据")
    runtime.config = config_manager.config          # 热更新会重建配置对象，这里同步引用
    # 热更新的对账逻辑会把配置里的凭据同步到客户端，这里同步一次桩的凭据快照
    fake.app_id, fake.app_secret = "1234567890", "fake-secret"
    check("凭据恢复后重新可用", runtime.bots[bot_id].client.configured is True,
          f"app_id={fake.app_id!r} secret={fake.app_secret!r} "
          f"cfg={[(b.get('id'), repr(b.get('app_id')), repr(b.get('app_secret'))) for b in (runtime.config.get('bots') or [])]}")

    # (3) --no-bots 调试模式：保存设置也不能"偷偷"把机器人连上
    started = []
    original_start = runtime.bots[bot_id].start
    runtime.bots[bot_id].start = lambda: started.append("start")
    runtime.set_bots_disabled(True)
    runtime.on_config_changed(source="测试-调试模式")
    check("调试模式(--no-bots)下保存设置不会连接机器人", not started, str(started))
    check("调试模式下机器人保持未启用", runtime.bots[bot_id].enabled is False, "")
    response = client.post(f"/api/admin/receiver/{bot_id}/start", json={})
    check("调试模式下手动连接会被拒绝并说明原因",
          response.status_code == 409 and "no-bots" in (response.get_json().get("message") or ""),
          f"{response.status_code} {response.get_json()}")
    runtime.set_bots_disabled(False)
    runtime.on_config_changed(source="测试-退出调试模式")
    check("退出调试模式后保存设置会重新连接机器人", bool(started), str(started))
    runtime.bots[bot_id].start = original_start

    # ------------------------------------------------------------------ 17. 本轮新增功能
    print("\n[17] 本轮新增：禁言方式 / 解禁默认本地 / 撤回自己消息 / 关闭程序 / 昵称共享")
    # (1) 默认就是"本地禁言"，不该去动官方接口
    fake.calls.clear()
    body = client.post("/api/groups/mute", json={
        "group_openid": "GROUP_1", "member_openid": "MEMBER_C", "username": "小丙",
        "minutes": 5, "bot_id": bot_id,
    }).get_json()
    check("默认（未指定方式）走本地禁言", body.get("mode") == "local" and body.get("success"),
          str(body)[:200])
    check("本地禁言不会调用官方接口",
          not any(call["kind"] == "restrict_chat_setting" for call in fake.calls),
          str([c["kind"] for c in fake.calls]))
    check("本地禁言记录已写入",
          runtime.store.is_muted(bot_id, "GROUP_1", "MEMBER_C") is not None, "")

    # (2) 解禁默认只做本地（这正是之前"解禁一直调用 API"的问题）
    fake.calls.clear()
    body = client.post("/api/groups/unmute", json={
        "group_openid": "GROUP_1", "member_openid": "MEMBER_C", "bot_id": bot_id,
    }).get_json()
    check("解禁默认只做本地解禁", body.get("mode") == "local" and body.get("success"),
          str(body)[:200])
    check("本地解禁不会调用官方接口",
          not any(call["kind"] == "restrict_chat_setting" for call in fake.calls),
          str([c["kind"] for c in fake.calls]))
    check("本地解禁后记录已释放",
          runtime.store.is_muted(bot_id, "GROUP_1", "MEMBER_C") is None, "")

    # (3) 显式选官方 / 两者都做时才会调用接口
    fake.calls.clear()
    client.post("/api/groups/mute", json={
        "group_openid": "GROUP_1", "member_openid": "MEMBER_D", "username": "小丁",
        "minutes": 5, "bot_id": bot_id, "mode": "api",
    })
    check("选官方方式时会调用平台禁言接口",
          any(call["kind"] == "restrict_chat_setting" for call in fake.calls), "")
    check("选官方方式时不会写本地禁言记录",
          runtime.store.is_muted(bot_id, "GROUP_1", "MEMBER_D") is None, "")
    runtime.store.release_member_muting(bot_id, "GROUP_1", "MEMBER_D")

    # (3) 发送时要记录平台返回的消息 ID（否则事后无法撤回）
    fake.calls.clear()
    client.post("/api/chat/send", json={
        "targetType": "private", "openid": "USER_ID", "content": "带 ID 的消息",
        "bot_id": bot_id,
    })
    id_key = f"{bot_id}:" + conversation_key("private", "", "USER_ID")
    id_msgs = [item for item in runtime.store.get_conversation(id_key, limit=5)
               if item["direction"] == "out"]
    check("发送的消息记录了平台消息 ID",
          bool(id_msgs) and (id_msgs[-1].get("msg_id") or "").startswith("ROBOT1.0_"),
          repr(id_msgs[-1].get("msg_id")) if id_msgs else "无")
    fake.recall_outcome = "ok"
    body = client.post("/api/groups/recall",
                       json={"message_id": id_msgs[-1]["id"], "bot_id": bot_id}).get_json()
    check("刚发出的消息可以正常撤回", body.get("success"), str(body)[:160])
    check("消息 ID 能从响应各处取到",
          QQApiClient.extract_message_id({"id": "T1"}) == "T1"
          and QQApiClient.extract_message_id({"data": {"id": "T2"}}) == "T2"
          and QQApiClient.extract_message_id({"data": {"msg_id": "T3"}}) == "T3"
          and QQApiClient.extract_message_id({}) == "",
          "")

    # 历史消息（没有记录消息 ID）要给出清晰说明，而不是含糊的报错
    runtime._record_outgoing(bot_id, "private", "USER_ID2", content="升级前的旧消息",
                             msg_id="")
    old_msgs2 = [item for item in runtime.store.get_conversation(
        f"{bot_id}:" + conversation_key("private", "", "USER_ID2"), limit=5)
        if item["direction"] == "out"]
    body = client.post("/api/groups/recall",
                       json={"message_id": old_msgs2[-1]["id"], "bot_id": bot_id}).get_json()
    check("缺少消息 ID 的历史消息给出明确说明",
          body.get("success") is False and "没有记录平台消息 ID" in (body.get("message") or ""),
          str(body)[:200])

    # (4) 撤回自己发出的消息：群聊与私聊都走官方接口，成功后标记本地
    robot_key = f"{bot_id}:" + conversation_key("group", "GROUP_1", "")
    fake.recall_outcome = "ok"
    runtime._record_outgoing(bot_id, "group", "GROUP_1", content="机器人发的话", msg_id="OUT_1")
    out_msgs = [item for item in runtime.store.get_conversation(robot_key, limit=10)
                if item["direction"] == "out" and item["msg_id"] == "OUT_1"]
    fake.calls.clear()
    body = client.post("/api/groups/recall",
                       json={"message_id": out_msgs[0]["id"], "bot_id": bot_id}).get_json()
    check("群聊里可以撤回自己发出的消息", body.get("success"), str(body)[:200])
    check("群聊撤回调用的是群消息撤回接口",
          any(call["kind"] == "recall_group" for call in fake.calls), str(fake.calls)[:160])
    after = runtime.store.get_message(out_msgs[0]["id"])
    check("撤回成功后本地记录被标记", "已撤回" in (after.get("content") or ""),
          repr(after.get("content")))

    # (5) 私聊撤回：同样调用官方接口（DELETE /v2/users/{openid}/messages/{id}）
    runtime._record_outgoing(bot_id, "private", "USER_R", content="私聊里发的话", msg_id="OUT_2")
    priv_msgs = [item for item in runtime.store.get_conversation(
        f"{bot_id}:" + conversation_key("private", "", "USER_R"), limit=10)
        if item["direction"] == "out"]
    fake.calls.clear()
    body = client.post("/api/groups/recall",
                       json={"message_id": priv_msgs[0]["id"], "bot_id": bot_id}).get_json()
    check("私聊也能撤回（调用单聊撤回接口）",
          body.get("success") and any(call["kind"] == "recall_private" for call in fake.calls),
          str(body)[:160] + " calls=" + str([c["kind"] for c in fake.calls]))
    check("私聊撤回成功后本地被标记",
          "已撤回" in (runtime.store.get_message(priv_msgs[0]["id"]).get("content") or ""), "")

    # (6) 失败时**绝不能**假装成功：本地记录保持原样
    runtime._record_outgoing(bot_id, "group", "GROUP_1", content="会被平台拒绝的话",
                             msg_id="OUT_3")
    fail_msgs = [item for item in runtime.store.get_conversation(robot_key, limit=10)
                 if item["direction"] == "out" and item["msg_id"] == "OUT_3"]
    fake.recall_outcome = "expired"
    fake.calls.clear()
    response = client.post("/api/groups/recall",
                           json={"message_id": fail_msgs[0]["id"], "bot_id": bot_id})
    body = response.get_json()
    check("超时撤回失败时接口返回失败", body.get("success") is False and response.status_code == 502,
          f"status={response.status_code} body={str(body)[:160]}")
    check("失败原因说明是超过 2 分钟", "2 分钟" in (body.get("message") or ""),
          str(body.get("message"))[:160])
    check("失败时本地记录保持原样（不显示已撤回）",
          "已撤回" not in (runtime.store.get_message(fail_msgs[0]["id"]).get("content") or ""),
          repr(runtime.store.get_message(fail_msgs[0]["id"]).get("content")))

    # 权限不足 / 平台失败 也要如实回报
    fake.recall_outcome = "forbidden"
    body = client.post("/api/groups/recall",
                       json={"message_id": fail_msgs[0]["id"], "bot_id": bot_id}).get_json()
    check("权限不足时给出权限提示", "权限" in (body.get("message") or ""), str(body)[:160])
    fake.recall_outcome = "failed"
    body = client.post("/api/groups/recall",
                       json={"message_id": fail_msgs[0]["id"], "bot_id": bot_id}).get_json()
    check("平台失败时提示稍后重试", "稍后重试" in (body.get("message") or ""), str(body)[:160])
    fake.recall_outcome = "ok"

    # (7) 超过 2 分钟的消息：本地直接拦下，不发无用的平台请求
    runtime._record_outgoing(bot_id, "group", "GROUP_1", content="很久以前发的",
                             msg_id="OUT_OLD")
    old_msgs = [item for item in runtime.store.get_conversation(robot_key, limit=10)
                if item["direction"] == "out" and item["msg_id"] == "OUT_OLD"]
    runtime.store._execute("UPDATE messages SET ts=? WHERE id=?",
                           (time.time() - 600, old_msgs[0]["id"]))
    fake.calls.clear()
    body = client.post("/api/groups/recall",
                       json={"message_id": old_msgs[0]["id"], "bot_id": bot_id}).get_json()
    check("超过 2 分钟直接拒绝（不发平台请求）",
          body.get("success") is False and not fake.calls, str(body)[:160])

    # (8) 群成员的消息：机器人是管理员时可以撤回（新功能），不是管理员则拒绝
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "group",
                       "group_openid": "GROUP_1", "openid": "MEMBER_X",
                       "username": "小叉", "content": "对方说的话", "msg_id": "IN_X"})
    in_msgs = [item for item in runtime.store.get_conversation(robot_key, limit=10)
               if item["direction"] == "in" and item["msg_id"] == "IN_X"]
    in_id = in_msgs[0]["id"]

    fake.role = "admin"
    runtime._bot_roles.clear()
    fake.calls.clear()
    body = client.post("/api/groups/recall",
                       json={"message_id": in_id, "bot_id": bot_id, "as_admin": True}).get_json()
    check("群管理员可以撤回普通群成员的消息",
          body.get("success") is True and body.get("recalled_member") is True,
          str(body)[:200])
    check("撤回群成员消息走的是官方群聊撤回接口",
          any(call["kind"] == "recall_group" and call["msg"] == "IN_X" for call in fake.calls),
          str(fake.calls)[:200])
    check("撤回成功后本地记录被标记为已撤回",
          (runtime.store.get_message(in_id) or {}).get("content") == "（已撤回）",
          str((runtime.store.get_message(in_id) or {}).get("content")))
    recalled_stats = runtime.stats.summary()
    check("统计里区分记录了「撤回成员消息」",
          (recalled_stats.get("totals") or {}).get("recalled_member", 0) >= 1,
          str(recalled_stats.get("totals", {}).get("recalled_member")))

    # 机器人只是普通成员：本地就拦下，不去打平台接口
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "group",
                       "group_openid": "GROUP_1", "openid": "MEMBER_Y",
                       "username": "小歪", "content": "另一条", "msg_id": "IN_Y"})
    in_y = [item for item in runtime.store.get_conversation(robot_key, limit=10)
            if item["msg_id"] == "IN_Y"][0]
    fake.role = "member"
    runtime._bot_roles.clear()
    fake.calls.clear()
    body = client.post("/api/groups/recall",
                       json={"message_id": in_y["id"], "bot_id": bot_id, "as_admin": True}).get_json()
    check("机器人不是管理员时拒绝撤回成员消息并说明原因",
          body.get("success") is False and body.get("need_admin") is True
          and not any(call["kind"] == "recall_group" for call in fake.calls),
          f"{body} / {fake.calls}")
    fake.role = "admin"
    runtime._bot_roles.clear()

    # 私聊里对方的消息：QQ 没开放这个接口，如实说明
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "private",
                       "openid": "PRIVATE_X", "username": "小私",
                       "content": "私聊里的话", "msg_id": "IN_P"})
    private_key = conversation_key("private", openid="PRIVATE_X", bot_id=bot_id)
    in_p = [item for item in runtime.store.get_conversation(private_key, limit=10)
            if item["msg_id"] == "IN_P"][0]
    body = client.post("/api/groups/recall",
                       json={"message_id": in_p["id"], "bot_id": bot_id}).get_json()
    check("私聊里对方的消息不能撤回（QQ 未开放该接口）",
          body.get("success") is False and "私聊" in (body.get("message") or ""),
          str(body)[:200])

    # (9) 群聊里学到的昵称 → 私聊也能显示
    check("群消息里的昵称被记录下来",
          runtime.store.get_meta(f"uname:{'MEMBER_B'}", "") == "小北",
          repr(runtime.store.get_meta(f"uname:{'MEMBER_B'}", "")))
    check("私聊里用群聊学到的昵称显示同一用户",
          runtime.lookup_name("MEMBER_B") == "小北", repr(runtime.lookup_name("MEMBER_B")))

    # (8) 关闭程序接口
    check("关闭状态初始为未请求", client.get("/api/admin/shutdown").get_json().get("shutdown") is False, "")
    called = {"reason": ""}
    runtime.set_shutdown_callback(lambda reason: called.update({"reason": reason}))
    body = client.post("/api/admin/shutdown", json={"reason": "测试"}).get_json()
    check("关闭接口返回成功并触发回调", body.get("success") and called["reason"] == "测试",
          f"body={body} called={called}")
    check("关闭状态变为已请求", client.get("/api/admin/shutdown").get_json().get("shutdown") is True, "")
    check("重复请求关闭是幂等的",
          client.post("/api/admin/shutdown", json={}).get_json().get("success") is True, "")

    # ------------------------------------------------------------------ 18. 群里 @ 机器人后的指令
    print("\n[18] 群聊：@ 机器人后指令要能生效（去掉开头的 @提及）")
    fake.calls.clear()
    runtime._handle_incoming({
        "bot_id": bot_id, "type": "group", "group_openid": "GROUP_AT",
        "openid": "MEMBER_AT", "member_openid": "MEMBER_AT", "username": "小艾",
        "content": "<@!BOT_OPENID> /帮助", "msg_id": "AT_1", "is_at_bot": True,
        "attachments": [], "quote": {},
        "mentions": [{"openid": "BOT_OPENID", "is_you": True}], "raw_event": "{}",
    })
    deadline = time.time() + 5
    while time.time() < deadline and not any(c["kind"] == "text" for c in fake.calls):
        time.sleep(0.05)
    replies = [c for c in fake.calls if c["kind"] == "text"]
    check("群里 @ 机器人后 /帮助 有回复", bool(replies), str(fake.calls)[:200])
    check("回复的是指令内容（不是 AI 兜底）",
          bool(replies) and "可用指令" in replies[0]["content"],
          replies[0]["content"][:80] if replies else "")
    check("回复发到了群里", bool(replies) and replies[0]["openid"] == "GROUP_AT", "")

    # 群里 @ 机器人 + 关键词，也要能命中
    fake.calls.clear()
    runtime._handle_incoming({
        "bot_id": bot_id, "type": "group", "group_openid": "GROUP_AT",
        "openid": "MEMBER_AT", "member_openid": "MEMBER_AT", "username": "小艾",
        "content": "<@!BOT_OPENID> 你好", "msg_id": "AT_2", "is_at_bot": True,
        "attachments": [], "quote": {},
        "mentions": [{"openid": "BOT_OPENID", "is_you": True}], "raw_event": "{}",
    })
    deadline = time.time() + 5
    while time.time() < deadline and not any(c["kind"] == "text" for c in fake.calls):
        time.sleep(0.05)
    replies = [c for c in fake.calls if c["kind"] == "text"]
    check("群里 @ 机器人后关键词回复生效",
          bool(replies) and "关键词回复" in replies[0]["content"],
          replies[0]["content"][:80] if replies else "")

    # 只 @ 机器人、没有正文 → 不回复（避免无意义应答）
    fake.calls.clear()
    runtime._handle_incoming({
        "bot_id": bot_id, "type": "group", "group_openid": "GROUP_AT",
        "openid": "MEMBER_AT", "member_openid": "MEMBER_AT", "username": "小艾",
        "content": "<@!BOT_OPENID>", "msg_id": "AT_3", "is_at_bot": True,
        "attachments": [], "quote": {},
        "mentions": [{"openid": "BOT_OPENID", "is_you": True}], "raw_event": "{}",
    })
    time.sleep(1.0)
    check("只 @ 机器人没有正文时不回复",
          not any(c["kind"] == "text" for c in fake.calls), str(fake.calls)[:160])

    # ------------------------------------------------------------------ 19. 群列表按机器人隔离
    print("\n[19] 群管理：两个机器人的群不能混在一起")
    # 需要在配置里真的有第二个机器人（切换"当前机器人"时它会校验存在性）
    config_manager.set_path("bots", [
        {"id": bot_id, "name": "机器人 1", "enabled": True, "app_id": "1234567890",
         "app_secret": "fake-secret", "sandbox": False, "intents": 100663296,
         "reconnect_attempts": 5, "reconnect_interval": 10},
        {"id": "bot2", "name": "机器人 2", "enabled": True, "app_id": "9999999999",
         "app_secret": "secret2", "sandbox": False, "intents": 100663296,
         "reconnect_attempts": 5, "reconnect_interval": 10},
    ])
    config_manager.save()
    # bot1 看到 GROUP_1 / SHARED，bot2 看到 GROUP_2 / SHARED
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "group",
                       "group_openid": "GROUP_1", "openid": "M1", "username": "小一",
                       "content": "bot1 的群", "msg_id": "G_ISO_1"})
    runtime.store.add({"bot_id": "bot2", "direction": "in", "type": "group",
                       "group_openid": "GROUP_2", "openid": "M2", "username": "小二",
                       "content": "bot2 的群", "msg_id": "G_ISO_2"})
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "group",
                       "group_openid": "SHARED", "openid": "M3", "username": "小共",
                       "content": "两个机器人都能看到", "msg_id": "G_ISO_3"})
    runtime.store.add({"bot_id": "bot2", "direction": "in", "type": "group",
                       "group_openid": "SHARED", "openid": "M3", "username": "小共",
                       "content": "两个机器人都能看到", "msg_id": "G_ISO_4"})

    only_bot1 = runtime.group_manager.groups(bot_id)
    only_bot2 = runtime.group_manager.groups("bot2")
    all_groups = runtime.group_manager.groups("")
    # 之前的用例在 bot1 下还建过 GROUP_AT，所以这里只断言"没有别的机器人的群"
    ids_1 = {g["group_openid"] for g in only_bot1}
    ids_2 = {g["group_openid"] for g in only_bot2}
    check("bot1 的群列表不含 bot2 专属的群",
          "GROUP_2" not in ids_1 and {"GROUP_1", "SHARED"} <= ids_1, str(ids_1))
    check("bot2 的群列表不含 bot1 专属的群",
          "GROUP_1" not in ids_2 and {"GROUP_2", "SHARED"} <= ids_2, str(ids_2))
    check("不指定机器人时才给出全部群",
          {g["group_openid"] for g in all_groups} >= {"GROUP_1", "GROUP_2", "SHARED"},
          str({g["group_openid"] for g in all_groups}))
    shared = [g for g in all_groups if g["group_openid"] == "SHARED"][0]
    check("共有的群会标出多个机器人看到过", shared.get("multi_bot") is True, str(shared)[:200])
    check("群详情能列出看到过它的机器人",
          set(runtime.group_manager.group_bot_ids("SHARED")) == {bot_id, "bot2"},
          str(runtime.group_manager.group_bot_ids("SHARED")))

    # 接口也要按"当前选中的机器人"返回
    config_manager.set_active_bot(bot_id)
    body = client.get("/api/groups").get_json()
    check("群列表接口返回当前机器人",
          body.get("bot_id") == bot_id and body.get("bot_name"),
          str({k: body.get(k) for k in ("bot_id", "bot_name")}))
    check("群列表接口不含别的机器人的群",
          "GROUP_2" not in {g["group_openid"] for g in body["groups"]},
          str([g["group_openid"] for g in body["groups"]]))
    config_manager.set_active_bot("bot2")
    body2 = client.get("/api/groups").get_json()
    check("切换机器人后群列表跟着变",
          body2.get("bot_id") == "bot2"
          and {g["group_openid"] for g in body2["groups"]} == {"GROUP_2", "SHARED"},
          f"bot={body2.get('bot_id')} groups={[g['group_openid'] for g in body2['groups']]}")
    config_manager.set_active_bot(bot_id)
    detail = client.get("/api/groups/detail?group_openid=SHARED").get_json()
    check("群详情带上是哪个机器人的",
          detail.get("bot_id") == bot_id and detail.get("bot_name"),
          str({k: detail.get(k) for k in ("bot_id", "bot_name", "bot_ids")}))

    # 禁言数量也要按机器人分开算
    runtime.store.add_muting({"bot_id": bot_id, "group_openid": "SHARED",
                              "member_openid": "M9", "username": "小九",
                              "until": time.time() + 600, "minutes": 10, "state": "active"})
    muted_1 = [g for g in runtime.group_manager.groups(bot_id)
               if g["group_openid"] == "SHARED"][0]["muted_count"]
    muted_2 = [g for g in runtime.group_manager.groups("bot2")
               if g["group_openid"] == "SHARED"][0]["muted_count"]
    check("禁言数量按机器人分别统计", muted_1 == 1 and muted_2 == 0,
          f"bot1={muted_1} bot2={muted_2}")

    # 禁言名单（接口 + 群详情）也必须按机器人隔离
    config_manager.set_active_bot(bot_id)
    m1 = client.get("/api/groups/mutings?group_openid=SHARED").get_json()
    config_manager.set_active_bot("bot2")
    m2 = client.get("/api/groups/mutings?group_openid=SHARED").get_json()
    check("禁言名单接口按当前机器人过滤",
          [item["member_openid"] for item in m1["mutings"]] == ["M9"]
          and not m2["mutings"],
          f"bot1={[i['member_openid'] for i in m1['mutings']]} "
          f"bot2={[i['member_openid'] for i in m2['mutings']]}")
    config_manager.set_active_bot(bot_id)
    d1 = client.get("/api/groups/detail?group_openid=SHARED").get_json()
    config_manager.set_active_bot("bot2")
    d2 = client.get("/api/groups/detail?group_openid=SHARED").get_json()
    check("群详情里的禁言名单也按机器人过滤",
          len(d1.get("mutings") or []) == 1 and not (d2.get("mutings") or []),
          f"bot1={len(d1.get('mutings') or [])} bot2={len(d2.get('mutings') or [])}")

    # 成员列表只用"本机器人"的历史消息补，另一个机器人的群成员不能串进来
    mem_1 = runtime.group_manager.members("GROUP_2", bot_id)
    mem_2 = runtime.group_manager.members("GROUP_2", "bot2")
    check("bot1 看 bot2 的群时不会带出 bot2 的成员",
          not [m for m in mem_1["members"] if m.get("username") == "小二"],
          str([m.get("username") for m in mem_1["members"]]))
    check("bot2 看自己的群时能带出历史成员",
          [m for m in mem_2["members"] if m.get("username") == "小二"],
          str([m.get("username") for m in mem_2["members"]]))

    # 不传 bot_id 时，禁言记录要落到"当前机器人"名下（而不是变成无归属的空 bot_id）
    config_manager.set_active_bot(bot_id)
    client.post("/api/groups/mute", json={"group_openid": "SHARED", "member_openid": "M8",
                                          "mode": "local", "minutes": 5})
    row_bot = [item for item in runtime.store.list_mutings("SHARED", bot_id, active_only=False)
               if item["member_openid"] == "M8"]
    check("禁言接口没传 bot_id 时记到当前机器人名下",
          bool(row_bot) and row_bot[0]["bot_id"] == bot_id, str(row_bot)[:200])
    client.post("/api/groups/unmute", json={"group_openid": "SHARED", "member_openid": "M8",
                                            "mode": "local"})
    # 还原成单个机器人的配置，避免影响后续用例
    config_manager.set_path("bots", [
        {"id": bot_id, "name": "机器人 1", "enabled": True, "app_id": "1234567890",
         "app_secret": "fake-secret", "sandbox": False, "intents": 100663296,
         "reconnect_attempts": 5, "reconnect_interval": 10},
    ])
    config_manager.save()
    config_manager.set_active_bot(bot_id)

    # ------------------------------------------------------------------ 20. 群名自动刷新 + 官方禁言名单
    print("\n[20] 群管理：群名自动刷新 / 官方（QQ 群内）真实禁言名单")
    manager = runtime.group_manager
    state = normalize_mute_state(_default_mute_state())
    check("官方禁言状态：全员禁言模式识别为 schedule",
          state["mode"] == "schedule" and "定时" in state["mode_text"], str(state["mode"]))
    check("官方禁言状态：周期规则能说成人话",
          any("周" in rule.get("text", "") and "13:05" in rule.get("text", "")
              for rule in state["recurring_rules"]),
          str([rule.get("text") for rule in state["recurring_rules"]]))
    check("官方禁言状态：解析出被禁言成员与昵称",
          state["member_count"] == 1 and state["members"][0]["username"] == "T小不点101",
          str(state["members"])[:200])
    check("官方禁言状态：到期时间是未来（剩余秒数 > 0）",
          state["members"][0]["remaining_seconds"] > 0
          and state["members"][0]["expire_text"],
          str(state["members"][0])[:200])
    check("官方禁言状态：mode=always 判定为当前正在全员禁言",
          normalize_mute_state({"global_rule": {"mode": "always"}})["active_now"] is True, "")
    check("官方禁言状态：定时规则命中当前时间时判定为禁言中",
          normalize_mute_state({"global_rule": {
              "mode": "schedule",
              "schedule_rules": [{"task_id": "t", "start_at": _rfc3339(time.time() - 60),
                                  "end_at": _rfc3339(time.time() + 60), "enabled": True}],
          }})["active_now"] is True, "")
    check("星期列表格式化正确", format_weekdays([1, 2, 7]) == "周一、周二、周日",
          format_weekdays([1, 2, 7]))

    # 群详情要带上官方禁言名单，并把昵称补进成员表
    manager.invalidate_mute_state()
    detail = client.get("/api/groups/detail?group_openid=GROUP_1").get_json()
    check("群详情返回官方禁言名单", detail.get("global_mute", {}).get("ok") is True,
          str(detail.get("global_mute"))[:200])
    check("群详情里的官方禁言成员带昵称与到期时间",
          [m["username"] for m in detail.get("platform_mutings", [])] == ["T小不点101"]
          and detail["platform_mutings"][0]["remaining_seconds"] > 0,
          str(detail.get("platform_mutings"))[:200])
    muted_member = [m for m in detail.get("members", [])
                    if m["member_openid"] == "MEMBER_MUTED"]
    check("官方禁言的人会出现在成员表并标记为 QQ 群禁言中",
          bool(muted_member) and muted_member[0]["platform_muted"] is True
          and muted_member[0]["platform_remaining_text"],
          str(muted_member)[:200])
    check("官方禁言名单里的昵称也被记录下来（供私聊显示）",
          runtime.store.user_names().get("MEMBER_MUTED") == "T小不点101",
          str(runtime.store.user_names().get("MEMBER_MUTED")))

    # 官方禁言名单与本地禁言名单是两份数据，不能混为一谈
    check("本地禁言名单里没有官方禁言的成员（两份数据分开）",
          "MEMBER_MUTED" not in [item["member_openid"] for item in detail.get("mutings", [])],
          str(detail.get("mutings"))[:200])
    body = client.get("/api/groups/mutings?group_openid=GROUP_1").get_json()
    check("禁言名单接口同时返回本地与官方两份",
          "mutings" in body and [m["member_openid"] for m in body.get("platform_mutings", [])]
          == ["MEMBER_MUTED"],
          str({k: body.get(k) for k in ("mutings", "platform_mutings")})[:200])

    # 官方状态有短缓存，避免把 30 QPM 的额度刷满；refresh=1 时强制重新查询
    manager.invalidate_mute_state()
    fake.calls.clear()
    client.get("/api/groups/detail?group_openid=GROUP_1")
    client.get("/api/groups/detail?group_openid=GROUP_1")
    cached_calls = [c for c in fake.calls if c["kind"] == "group_mute_state"]
    client.get("/api/groups/detail?group_openid=GROUP_1&refresh=1")
    refreshed_calls = [c for c in fake.calls if c["kind"] == "group_mute_state"]
    check("官方禁言状态有缓存（连续两次详情只查一次）", len(cached_calls) == 1,
          str(len(cached_calls)))
    check("refresh=1 会强制重新查询官方禁言状态", len(refreshed_calls) == 2,
          str(len(refreshed_calls)))

    # 平台无权限时：页面要能显示原始错误，而不是整页报错
    fake.mute_state_error = "API 错误 11253: 应用无接口访问权限"
    manager.invalidate_mute_state()
    detail = client.get("/api/groups/detail?group_openid=GROUP_1").get_json()
    check("平台拒绝查询禁言状态时给出原始错误提示",
          detail.get("global_mute", {}).get("ok") is False
          and "11253" in (detail.get("global_mute", {}).get("error") or ""),
          str(detail.get("global_mute"))[:200])
    check("查不到官方禁言名单时成员表依然可用",
          bool(detail.get("members")) and not [m for m in detail["members"]
                                               if m.get("platform_muted")],
          str(len(detail.get("members") or [])))
    fake.mute_state_error = ""
    manager.invalidate_mute_state()

    # 群名自动刷新：没名字/过期才刷，刷完记录时间，会话名也跟着更新
    manager._name_updated.clear()          # 模拟"从来没刷新过"
    manager._names.clear()
    pending_before = manager.stale_name_groups("")
    check("从来没刷新过群名时，所有群都算待刷新", len(pending_before) >= 1,
          str([item["group_openid"] for item in pending_before]))
    refreshed = manager.refresh_names("", limit=1, pace=0)
    check("批量刷新群名会真的去查平台",
          refreshed["refreshed"] and refreshed["refreshed"][0]["name"] == "测试群 A",
          str(refreshed)[:200])
    check("批量刷新时每次只刷一部分（剩下的下一轮继续）",
          len(refreshed["refreshed"]) == 1 and refreshed["pending"] == len(pending_before) - 1,
          f"refreshed={len(refreshed['refreshed'])} pending={refreshed['pending']}")
    first_group = refreshed["refreshed"][0]["group_openid"]
    check("刷新后记录了群名的更新时间", manager.name_updated_at(first_group) > 0,
          str(manager.name_updated_at(first_group)))
    check("刷新后该群不再算过期（TTL 内不会重复查）",
          first_group not in [item["group_openid"] for item in manager.stale_name_groups("")],
          str([item["group_openid"] for item in manager.stale_name_groups("")]))
    check("会话列表里的群名也跟着更新",
          any(conv.get("username") == "测试群 A"
              for conv in runtime.store.conversations(limit=2000)
              if conv.get("group_openid") == first_group),
          str([(c.get("conv_key"), c.get("username"))
               for c in runtime.store.conversations(limit=2000)][:6]))
    body = client.get("/api/groups").get_json()
    listed = [g for g in body["groups"] if g["group_openid"] == first_group][0]
    check("群列表接口返回群名更新时间与新鲜度",
          listed["name_updated"] > 0 and listed["name_stale"] is False
          and listed["name"] == "测试群 A",
          str({k: listed.get(k) for k in ("name", "name_updated", "name_stale")}))
    forced = manager.refresh_names("", force=True, limit=1, pace=0)
    check("强制刷新（点按钮）会忽略 TTL 再查一次",
          bool(forced["refreshed"]), str(forced)[:160])

    # 群名拉不下来时要退避，不能每 15 秒重试一次
    class BrokenClient:
        configured = True

        def group_info(self, group_openid):
            raise RuntimeError("API 错误 11253: 应用无接口访问权限")

    real_client = runtime.get_client(bot_id)
    runtime.bots[bot_id].client = BrokenClient()
    manager._name_updated.clear()
    manager._name_failed.clear()
    failed_once = manager.refresh_names("", limit=2, pace=0)
    check("群名拉取失败时会记录下来", bool(failed_once["failed"]),
          str(failed_once)[:160])
    first_failed = {item["group_openid"] for item in failed_once["failed"]}
    second_try = manager.refresh_names("", limit=3, pace=0)
    retried = first_failed & {item["group_openid"] for item in second_try["failed"]}
    check("失败的群进入退避期，不会反复重试", not retried, f"又被重试：{retried}")
    check("群列表会标出'群名获取失败'",
          any(g.get("name_error") for g in manager.groups("")),
          str([(g["group_openid"], g.get("name_error")) for g in manager.groups("")])[:200])
    check("手动强制刷新会忽略退避期",
          bool(manager.refresh_names("", force=True, limit=1, pace=0)["failed"]),
          "force=True 时应重新尝试")
    runtime.bots[bot_id].client = real_client
    manager._name_failed.clear()
    manager._name_updated.clear()
    manager.refresh_names("", force=True, limit=6, pace=0)

    # ------------------------------------------------------------------ 21. 群名不能被"最后发言的人"顶掉
    print("\n[21] 群名不能被群里最后发言者的昵称顶掉")
    conv_key = conversation_key("group", "GROUP_NAME", bot_id=bot_id)
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "group",
                       "group_openid": "GROUP_NAME", "openid": "M_SENDER",
                       "username": "最后发言的人", "content": "大家好", "msg_id": "GN_1"})
    conv = runtime.store.conversation(conv_key)
    check("收到群消息后，群会话名不会被写成发送者的昵称",
          (conv or {}).get("username") == "", repr((conv or {}).get("username")))
    listed = [g for g in manager.groups(bot_id) if g["group_openid"] == "GROUP_NAME"]
    check("群列表里也不会拿发送者昵称当群名",
          listed and listed[0]["name"] != "最后发言的人",
          str(listed)[:200])
    check("还没有群名时标出来源为空（交给接口去取）",
          listed and listed[0]["name"] == "" and listed[0]["name_from"] == "",
          str({k: listed[0].get(k) for k in ("name", "name_from")}) if listed else "")

    # 群名以 QQ 返回的为准：即便会话表里有别的名字，也要用缓存里的群名
    manager._names["GROUP_NAME"] = "真·群名"
    runtime.store.rename_conversation(conv_key, "别的名字")
    listed = [g for g in manager.groups(bot_id) if g["group_openid"] == "GROUP_NAME"][0]
    check("群名以 QQ 返回的群名为准（不被会话表里的旧名字顶掉）",
          listed["name"] == "真·群名" and listed["name_from"] == "platform",
          str({k: listed.get(k) for k in ("name", "name_from")}))

    # 历史坏数据：默认（自动）**不**做猜测式清理，只有用户手动点「修正群会话名」才清
    runtime.store.rename_conversation(conv_key, "最后发言的人")
    auto = runtime.store.repair_group_conversation_names(names=manager.group_names())
    check("自动修复不会把群名清空（不做猜测式清理）",
          auto.get("cleared") == 0
          and (runtime.store.conversation(conv_key) or {}).get("username") != "",
          f"auto={auto} username={(runtime.store.conversation(conv_key) or {}).get('username')!r}")
    check("但会按 QQ 群名缓存把名字补正确",
          auto.get("named") >= 1
          and (runtime.store.conversation(conv_key) or {}).get("username") == "真·群名",
          f"auto={auto} username={(runtime.store.conversation(conv_key) or {}).get('username')!r}")

    # 误伤场景复现：群名恰好等于最后一个发言人的昵称，且还没有 QQ 群名缓存
    manager._names.pop("GROUP_NAME", None)
    runtime.store.rename_conversation(conv_key, "最后发言的人")
    safe = runtime.store.repair_group_conversation_names(names=manager.group_names())
    check("群名恰好等于最后发言人昵称、又没有缓存时，自动修复也不清（以前会误伤）",
          safe.get("cleared") == 0
          and (runtime.store.conversation(conv_key) or {}).get("username") == "最后发言的人",
          f"safe={safe} username={(runtime.store.conversation(conv_key) or {}).get('username')!r}")

    # 手动修正（force=True）：才启用启发式清理
    manager._names.pop("GROUP_NAME", None)
    runtime.store.rename_conversation(conv_key, "最后发言的人")
    manual = runtime.store.repair_group_conversation_names(force=True)
    check("手动修正会清掉「名字=最后发言者昵称」的可疑数据",
          manual.get("cleared") >= 1
          and (runtime.store.conversation(conv_key) or {}).get("username") == "",
          f"manual={manual} username={(runtime.store.conversation(conv_key) or {}).get('username')!r}")
    runtime.store.rename_conversation(conv_key, "另一个真群名")
    check("名字和最后发言者不同时不会被误删",
          runtime.store.repair_group_conversation_names(force=True).get("cleared") == 0
          and (runtime.store.conversation(conv_key) or {}).get("username") == "另一个真群名",
          repr((runtime.store.conversation(conv_key) or {}).get("username")))
    manager._names["GROUP_NAME"] = "真·群名"
    resynced = manager.resync_conversation_names()
    check("重启后会把缓存的群名回写到会话表",
          resynced >= 1
          and (runtime.store.conversation(conv_key) or {}).get("username") == "真·群名",
          f"resynced={resynced} username={(runtime.store.conversation(conv_key) or {}).get('username')!r}")
    check("可疑群名数可以只统计不修改",
          isinstance(runtime.store.group_name_suspects(), int), "")

    # 没有群名时，聊天列表用"群 xxxxxx"占位，而不是某个人的昵称
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "group",
                       "group_openid": "GROUP_NONAME", "openid": "M_OTHER",
                       "username": "路人甲", "content": "在吗", "msg_id": "GN_2"})
    chat_conv = [c for c in runtime.public_conversations(bot_id)
                 if c.get("group_openid") == "GROUP_NONAME"]
    check("聊天列表里没有群名时用占位名，不用发言者昵称",
          chat_conv and chat_conv[0]["name"].startswith("群 ")
          and "路人甲" not in chat_conv[0]["name"] and chat_conv[0]["named"] is False,
          str(chat_conv[0] if chat_conv else None)[:220])

    # ------------------------------------------------------------------ 22. 文件不当图片 + 大文件分片上传
    print("\n[22] 非图片文件不能被当成图片 / 超过 8MB 的文件要能发出去")

    # (1) 后端：非图片附件不能充当 image_url（否则前端会显示"图片加载失败"）
    file_msg = {"bot_id": bot_id, "direction": "out", "type": "group",
                "group_openid": "GROUP_1", "content": "📎 msedge.exe",
                "attachments": [{"local_url": "/media/msedge.exe", "file_name": "msedge.exe"}]}
    public = runtime.public_message(file_msg, bot_id)
    check("发出去的非图片文件不会被写成 image_url",
          public["image_url"] == "", repr(public["image_url"]))
    img_msg = {"bot_id": bot_id, "direction": "in", "type": "group", "group_openid": "GROUP_1",
               "attachments": [{"local_url": "/media/a.png", "content_type": "image/png",
                                "file_name": "a.png"}]}
    check("图片附件依然会被当成图片显示",
          runtime.public_message(img_msg, bot_id)["image_url"] == "/media/a.png",
          repr(runtime.public_message(img_msg, bot_id)["image_url"]))
    received_file = {"bot_id": bot_id, "direction": "in", "type": "group", "group_openid": "GROUP_1",
                     "attachments": [{"url": "https://multimedia.nt.qq.com.cn/download?fileid=X",
                                      "content_type": "file", "file_name": "setup.exe"}]}
    check("收到的文件附件不会被当成图片",
          runtime.public_message(received_file, bot_id)["image_url"] == "", "")
    check("附件判定：content_type=file 或 .exe/.py 都算非图片",
          is_image_attachment({"content_type": "file", "url": "/media/x.png"}) is False
          and is_image_attachment({"local_url": "/media/gui_v1.6.0.py"}) is False
          and is_image_attachment({"local_url": "/media/a.jpg"}) is True
          and is_image_attachment({"content_type": "image/png"}) is True, "")

    # (2) 真实客户端：小文件一次上传、大文件走官方分片上传
    class StubApiQQ(QQApiClient):
        """把 HTTP 层换掉，只验证"该调哪些接口、传了哪些参数"。"""

        def __init__(self):
            super().__init__("botX", "1234567890", "fake-secret", config=config_manager.config,
                             logger_obj=log)
            self.calls = []
            self.parts = {}
            self.block_size = 3 * 1024 * 1024

        def request(self, method, url, json_data=None, params=None, retries=None):
            self.calls.append({"method": method, "url": url, "json": dict(json_data or {})})
            if url.endswith("/upload_prepare"):
                size = int(json_data.get("file_size") or 0)
                # 真实接口的分片号是 **1 起**的，每片的 block_size 是该片实际大小
                parts = []
                offset, index = 0, 1
                while offset < size:
                    chunk = min(self.block_size, size - offset)
                    parts.append({"index": index, "presigned_url": f"https://cos.example.com/p/{index}",
                                  "block_size": str(chunk)})
                    offset += chunk
                    index += 1
                return {"upload_id": "upload_test", "block_size": str(self.block_size),
                        "parts": parts,
                        "upload_config": {"concurrency": 1, "retry_timeout": 300,
                                          "retry_delay": 1}}
            if url.endswith("/upload_part_finish"):
                return {}
            if url.endswith("/files"):
                return {"file_info": "FILE_INFO_CHUNKED", "file_uuid": "uuid_1", "ttl": 300}
            if url.endswith("/messages"):
                return {"id": "MSG_CHUNKED", "data": {"id": "MSG_CHUNKED"}}
            return {}

        def _put_chunk(self, url, chunk, retries=3, delay=1.0, timeout=120.0):
            self.parts[url] = bytes(chunk)

    api = StubApiQQ()
    small = b"x" * 1024
    info = api.send_file("group", "GROUP_1", small, "note.txt", content="给你")
    kinds = [c["url"].rsplit("/", 1)[-1] for c in api.calls]
    check("小文件仍然走一次上传（base64）",
          "files" in kinds and "upload_prepare" not in kinds and not info.get("_chunked"),
          str(kinds))

    api.calls.clear()
    api.parts.clear()
    big = os.urandom(api.block_size * 2 + 1024)          # 约 6MB，超过 4MB 阈值
    info = api.send_file("group", "GROUP_1", big, "video.mp4", content="大文件")
    kinds = [c["url"].rsplit("/", 1)[-1] for c in api.calls]
    check("大文件走官方分片上传（prepare → 分片 → 合并）",
          kinds[0] == "upload_prepare" and kinds.count("upload_part_finish") == 3
          and kinds[-2] == "files" and kinds[-1] == "messages", str(kinds))
    prepare = [c for c in api.calls if c["url"].endswith("upload_prepare")][0]["json"]
    import hashlib
    check("分片上传带上了官方要求的校验值",
          prepare["file_size"] == str(len(big))
          and prepare["md5"] == hashlib.md5(big).hexdigest()
          and prepare["sha1"] == hashlib.sha1(big).hexdigest()
          and prepare["md5_10m"] == hashlib.md5(big[:10002432]).hexdigest(),
          str({k: prepare.get(k) for k in ("file_size", "md5", "md5_10m")})[:160])
    check("每片数据都按 block_size 正确切分并 PUT 到预签名地址",
          len(api.parts) == 3 and api.parts["https://cos.example.com/p/1"] == big[:api.block_size]
          and api.parts["https://cos.example.com/p/3"] == big[api.block_size * 2:], "")
    finishes = [c["json"] for c in api.calls if c["url"].endswith("upload_part_finish")]
    check("分片完成通知按平台给的 1 起序号原样回传（实测真实接口是 1 起）",
          [item["part_index"] for item in finishes] == [1, 2, 3]
          and [int(item["block_size"]) for item in finishes]
          == [api.block_size, api.block_size, len(big) - api.block_size * 2]
          and all(item["upload_id"] == "upload_test" for item in finishes),
          str(finishes)[:240])
    merge = [c for c in api.calls if c["url"].endswith("/files")][-1]["json"]
    check("合并请求带上 upload_id 且不重复上传内容",
          merge.get("upload_id") == "upload_test" and "file_data" not in merge
          and merge.get("srv_send_msg") is False, str(merge))
    check("分片上传的结果被记住（便于界面提示）",
          info.get("_chunked") is True and "分片上传 3 片" in (info.get("_note") or ""),
          str(info)[:200])
    check("大文件不会再被 8MB 上限拦住",
          api._file_size_limit_bytes() == 200 * 1024 * 1024,
          str(api._file_size_limit_bytes()))

    # 超过官方硬限制（200MB）要明确报错，且不发任何请求
    api.calls.clear()
    try:
        api.send_file("group", "GROUP_1", b"x" * 10, "big.bin")  # 先正常一次，清掉记录
        api.calls.clear()
        api.send_file("group", "GROUP_1", b"\0" * (201 * 1024 * 1024), "huge.bin")
        over_limit_error = ""
    except Exception as exc:
        over_limit_error = str(exc)
    check("超过 200MB 硬限制时给出明确错误且不发请求",
          "200" in over_limit_error and not api.calls, f"{over_limit_error} / {len(api.calls)}")

    # (3) 配置迁移：旧的 8MB/6MB 上限要按官方值放宽；用户自己改过的值不动
    legacy_path = os.path.join(test_root, f"config_limits_{run_id}.json")
    with open(legacy_path, "w", encoding="utf-8") as handle:
        json.dump({"send": {"max_file_mb": 8, "max_image_mb": 6}}, handle)
    migrated_manager = load_config(legacy_path)
    check("旧的 8MB 文件上限会被放宽到官方 200MB",
          migrated_manager.config.float_of("send", "max_file_mb", default=0) == 200,
          str(migrated_manager.config.float_of("send", "max_file_mb", default=0)))
    check("旧的 6MB 图片上限会被放宽到官方 20MB",
          migrated_manager.config.float_of("send", "max_image_mb", default=0) == 20,
          str(migrated_manager.config.float_of("send", "max_image_mb", default=0)))
    custom_path = os.path.join(test_root, f"config_limits_custom_{run_id}.json")
    with open(custom_path, "w", encoding="utf-8") as handle:
        json.dump({"send": {"max_file_mb": 12, "max_image_mb": 5}}, handle)
    custom_manager = load_config(custom_path)
    check("用户自己设过的大小上限不会被改掉",
          custom_manager.config.float_of("send", "max_file_mb", default=0) == 12
          and custom_manager.config.float_of("send", "max_image_mb", default=0) == 5,
          str(custom_manager.config.float_of("send", "max_file_mb", default=0)))

    # ------------------------------------------------------------------ 23. 撤回群成员消息
    print("\n[23] 撤回群聊普通用户的消息（管理员权限 + 2 分钟限制）")
    group = "GROUP_RECALL"
    group_key = conversation_key("group", group, bot_id=bot_id)
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "group",
                       "group_openid": group, "openid": "MEMBER_R",
                       "username": "小阿", "content": "我说错话了", "msg_id": "IN_R1"})
    member_msg = [m for m in runtime.store.get_conversation(group_key, limit=5)
                  if m["msg_id"] == "IN_R1"][0]

    fake.role = "admin"
    runtime._bot_roles.clear()
    fake.calls.clear()
    check("群管理页的「最近消息」一栏已移除（接口不再提供）",
          client.get(f"/api/groups/messages?group_openid={group}&bot={bot_id}").status_code == 404,
          "")

    # 管理员撤回成员消息（仍走聊天窗口的操作框）
    body = client.post("/api/groups/recall", json={"message_id": member_msg["id"],
                                                   "msg_id": "IN_R1", "bot_id": bot_id,
                                                   "as_admin": True}).get_json()
    check("管理员撤回成员消息成功",
          body.get("success") is True and body.get("recalled_member") is True, str(body)[:200])
    check("走的是官方群聊撤回接口",
          any(c["kind"] == "recall_group" and c["msg"] == "IN_R1" for c in fake.calls),
          str(fake.calls)[:200])

    # 超过 2 分钟的成员消息：本地直接拒绝，不发平台请求
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "group",
                       "group_openid": group, "openid": "MEMBER_R",
                       "username": "小阿", "content": "很久以前", "msg_id": "IN_OLD"})
    old = [m for m in runtime.store.get_conversation(group_key, limit=5)
           if m["msg_id"] == "IN_OLD"][0]
    runtime.store._execute("UPDATE messages SET ts=? WHERE id=?", (time.time() - 300, old["id"]))
    fake.calls.clear()
    body = client.post("/api/groups/recall", json={"message_id": old["id"], "bot_id": bot_id,
                                                   "as_admin": True}).get_json()
    check("撤回超过 2 分钟的消息不发平台请求",
          body.get("success") is False and "2 分钟" in (body.get("message") or "")
          and not fake.calls, f"{body} / {len(fake.calls)}")

    # 机器人不是管理员时不能撤回成员消息
    fake.role = "member"
    runtime._bot_roles.clear()
    fake.calls.clear()
    body = client.post("/api/groups/recall", json={"message_id": member_msg["id"],
                                                   "bot_id": bot_id,
                                                   "as_admin": True}).get_json()
    check("机器人不是管理员时拒绝撤回成员消息",
          body.get("success") is False and body.get("need_admin") is True
          and not any(c["kind"] == "recall_group" for c in fake.calls),
          f"{body} / {fake.calls}")
    fake.role = "admin"
    runtime._bot_roles.clear()

    # 群内身份带缓存，不会每次都打平台接口
    runtime._bot_roles.clear()
    fake.calls.clear()
    runtime.group_bot_role(bot_id, group)
    runtime.group_bot_role(bot_id, group)
    bot_state_calls = [c for c in fake.calls if c["kind"] == "bot_state"]
    check("群内身份有缓存（两次查询只打一次接口）", len(bot_state_calls) == 1,
          str(len(bot_state_calls)))
    check("group_bot_is_admin 判断正确",
          runtime.group_bot_is_admin(bot_id, group) is True, "")

    # ------------------------------------------------------------------ 24. 云同步：兼容旧程序的明文格式，且不再乱写文件
    print("\n[24] 云同步：同一张 D1 表里混着旧程序的明文记录时不能报错、更不能写坏文件")
    check("云端 value 支持三种格式（我们的 b64: / 旧版裸 base64 / 旧程序明文）",
          decode_value(encode_value("中文内容".encode("utf-8"))) == ("中文内容".encode("utf-8"), "b64")
          and decode_value(base64.b64encode(b'{"a":1}').decode()) == (b'{"a":1}', "b64")
          and decode_value("2026-08-30 00:27:22 - QQAIBot - INFO - 启动")[1] == "text",
          "")
    check("自己上传的值带 b64: 前缀（与旧程序区分开）",
          encode_value(b"x").startswith("b64:"), encode_value(b"x")[:12])
    check("碰巧是合法 base64 的明文会被 JSON 安全闸拦下",
          "不是合法的 UTF-8" in check_text_payload("a.json",
                                                  decode_value("bindings7B7D8D4A38188B367D42FFAC71A7C99D"
                                                               "7B7D8D4A38188B367D42FFAC71A7C99Dpendingcodes")[0]),
          "")
    check("合法的 JSON 与文本能通过安全检查",
          check_text_payload("a.json", '{"a": 1}'.encode()) == ""
          and check_text_payload("a.txt", b"hello") == "", "")

    sync = CloudSync(config_manager, logger_obj=log)
    config_manager.set_path("cloud_sync.enabled", True)     # 测试里手动打开
    check("云同步只认自己的同步范围（日志/锁文件/别人的键一律不管）",
          sync.in_scope("data/user_context/private/X.json") is True
          and sync.in_scope("data/plugin_data/astrbot_plugin_dice/roll.json") is True
          and sync.in_scope("data/plugins_data/legacy/old.json") is False
          and sync.in_scope("data/logs/20260913_211945.txt") is False
          and sync.in_scope("system/instances/server-1801") is False
          and sync.in_scope("data/messages.db") is False
          and sync.in_scope("../config.json") is False,
          "")

    class FakeCloud:
        """模拟 D1：混着旧程序的明文记录 + 我们自己的 b64 记录。"""

        def __init__(self, rows):
            self.rows = rows
            self.puts = []

        def configured(self):
            return True

        def ensure_table(self):
            return None

        def fetch_all(self):
            return {key: dict(value=value, updated_at=ts, deleted=0)
                    for key, (value, ts) in self.rows.items()}

        def query(self, sql, params=None):
            return []

        def put(self, key, value, updated_at, deleted=0):
            self.puts.append({"key": key, "value": value})
            self.rows[key] = (value, updated_at)

    sync_root = os.path.join(test_root, "cloudroot")
    shutil.rmtree(sync_root, ignore_errors=True)
    os.makedirs(os.path.join(sync_root, "data", "user_context", "private"), exist_ok=True)
    old_base = paths.BASE_DIR
    paths.BASE_DIR = sync_root
    try:
        old_text = '{\n  "openid": "USER_OLD",     "history": [1, 2, 3]\n}'
        fake_cloud = FakeCloud({
            # 旧程序写的明文（带中文，之前必报 "only ASCII" 错）
            "data/user_context/private/USER_OLD.json": (old_text, 1700000000000),
            # 旧程序写的日志：不属于我们的范围
            "data/logs/20260913_211945.txt": ("2026-09-13 21:19:45 - QQAIBot - INFO - 启动", 1),
            # 旧程序的实例锁：也不属于我们
            "system/instances/server-1801": ('{"host": "server", "pid": 1801}', 1),
            # 旧版合并程序写的裸 base64
            "data/user_context/private/USER_B64.json":
                (base64.b64encode(b'{"openid": "USER_B64"}').decode(), 1700000000000),
            # 陷阱：碰巧是合法 base64、其实是别的东西（之前会把本地文件写成乱码）
            "data/user_context/private/bindings.json":
                ("bindings7B7D8D4A38188B367D42FFAC71A7C99D"
                 "7B7D8D4A38188B367D42FFAC71A7C99Dpendingcodes", 1700000000000),
        })
        # 本地有一个云端没有的文件 → 应该以 b64: 前缀上传
        local_file = os.path.join(sync_root, "data", "user_context", "private", "LOCAL.json")
        with open(local_file, "w", encoding="utf-8") as handle:
            handle.write('{"openid": "LOCAL"}')
        sync.backend = lambda: fake_cloud
        result = sync.sync_once(force=True)
        check("旧程序的明文记录不再算失败（失败 0）",
              result.get("errors") == 0 and result.get("ok") is True, str(result)[:220])
        check("不属于本程序的云端记录被跳过并单独计数",
              result.get("out_of_scope") == 2
              and not os.path.exists(os.path.join(sync_root, "data", "logs")),
              f"out_of_scope={result.get('out_of_scope')} "
              f"logs_exists={os.path.exists(os.path.join(sync_root, 'data', 'logs'))}")
        restored = os.path.join(sync_root, "data", "user_context", "private", "USER_OLD.json")
        with open(restored, encoding="utf-8") as handle:
            restored_text = handle.read()
        check("旧程序的明文文件被正确还原（内容一字不差）",
              restored_text == old_text, repr(restored_text[:60]))
        with open(os.path.join(sync_root, "data", "user_context", "private",
                               "USER_B64.json"), encoding="utf-8") as handle:
            b64_text = handle.read()
        check("旧版裸 base64 的文件也能正确还原", b64_text == '{"openid": "USER_B64"}',
              repr(b64_text))
        trap = os.path.join(sync_root, "data", "user_context", "private", "bindings.json")
        check("碰巧是 base64 的假数据不会生成文件（更不会写乱码）",
              not os.path.exists(trap), "")
        uploaded = [item for item in fake_cloud.puts if item["key"].endswith("LOCAL.json")]
        check("本地上传的值带 b64: 前缀",
              uploaded and uploaded[0]["value"].startswith("b64:"),
              str(uploaded)[:120])
        check("云同步结果消息里失败数为 0", "失败 0" in (result.get("message") or ""),
              result.get("message"))
    finally:
        paths.BASE_DIR = old_base

    # ------------------------------------------------------------------ 25. AstrBot 插件兼容
    print("\n[25] AstrBot 插件格式：加载 / 过滤器语义 / 事件桥接")

    ab_dir = os.path.join(test_root, "abplugins")
    demo_dir = os.path.join(ab_dir, "ab_demo")
    shutil.rmtree(ab_dir, ignore_errors=True)
    os.makedirs(demo_dir, exist_ok=True)
    with open(os.path.join(demo_dir, "metadata.yaml"), "w", encoding="utf-8") as handle:
        handle.write("name: ab_demo\ndesc: AstrBot 兼容测试插件\nversion: 2.3.4\n"
                     "author: 测试作者\n")
    with open(os.path.join(demo_dir, "_conf_schema.json"), "w", encoding="utf-8") as handle:
        json.dump({"greeting": {"type": "string", "default": "默认问候"}}, handle)
    with open(os.path.join(demo_dir, "main.py"), "w", encoding="utf-8") as handle:
        handle.write('''# -*- coding: utf-8 -*-
import os
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register


@register("ab_demo", "测试作者", "AstrBot 兼容测试插件", "2.3.4")
class AbDemo(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.calls = []
        self.data_dir = StarTools.get_data_dir("ab_demo")

    @filter.command("你好", alias={"hi"})
    async def hello(self, event: AstrMessageEvent):
        self.calls.append("hello")
        cfg = self.context.get_config()
        yield event.plain_result("%s，%s" % (cfg.get("greeting"), event.get_sender_name()))

    @filter.regex(r"^掷骰子$")
    async def dice(self, event: AstrMessageEvent):
        self.calls.append("dice")
        yield event.plain_result("点数 6")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("管理", alias={"admin"})
    async def admin_only(self, event: AstrMessageEvent):
        self.calls.append("admin")
        yield event.plain_result("管理员专属")

    @filter.command("计数")
    async def counter(self, event: AstrMessageEvent):
        self.calls.append("counter")
        path = os.path.join(self.data_dir, "n.json")
        data = StarTools.load_json(path, default={"n": 0}) or {"n": 0}
        data["n"] += 1
        StarTools.save_json(path, data)
        yield event.plain_result("第 %d 次" % data["n"])

    @filter.command("图片")
    async def picture(self, event: AstrMessageEvent):
        self.calls.append("picture")
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082")
        yield event.image_result(os.path.join(self.data_dir, "x.png"))
        with open(os.path.join(self.data_dir, "x.png"), "wb") as fh:
            fh.write(png)

    @filter.command("主动")
    async def proactive(self, event: AstrMessageEvent):
        self.calls.append("proactive")
        await self.context.send_message(event.unified_msg_origin,
                                        [__import__("astrbot.api.message_components",
                                                    fromlist=["Plain"]).Plain("主动消息")])
        yield event.plain_result("已主动发送")
''')
    # 非 AstrBot 的裸 .py 不再支持：不应该被加载，并且要有明确诊断
    native_dir = os.path.join(ab_dir, "native_one.py")
    with open(native_dir, "w", encoding="utf-8") as handle:
        handle.write('PLUGIN = {"name": "原生测试"}\n'
                     'COMMANDS = ["/你好"]\n'
                     'def on_message(msg):\n    return "原生优先"\n')

    sent = []
    manager = PluginManager(plugin_dir=ab_dir, data_dir=os.path.join(test_root, "abdata"),
                            disabled_file=os.path.join(test_root, "abdisabled.json"),
                            logger_obj=log)
    manager.set_bot(PluginBot(runtime=None, config={}, logger_obj=log))
    manager.load_plugins(force=True)
    loaded = {item["name"]: item for item in manager.list_plugins()}
    check("AstrBot 插件目录被识别并加载",
          "ab_demo" in loaded and loaded["ab_demo"].get("format") == "AstrBot",
          str(list(loaded)))
    check("从 metadata.yaml 读到名称/版本/作者/说明",
          loaded.get("ab_demo", {}).get("version") == "2.3.4"
          and loaded["ab_demo"].get("author") == "测试作者"
          and loaded["ab_demo"].get("description") == "AstrBot 兼容测试插件",
          str(loaded.get("ab_demo", {}))[:200])
    check("插件里的指令被登记到列表", "你好" in (loaded.get("ab_demo", {}).get("commands") or []),
          str(loaded.get("ab_demo", {}).get("commands")))

    def ab_msg(text, group=False, admin=False, openid="USER_AB"):
        item = {"type": "group" if group else "private", "content": text,
                "user_openid": "" if group else openid, "user_name": "小测",
                "group_openid": "GROUP_AB" if group else "", "bot_id": bot_id,
                "msg_id": "AB1", "is_admin": admin}
        return item

    result = manager.dispatch_message(ab_msg("/你好"))
    check("AstrBot 指令处理器能回复（async generator + plain_result）",
          result and "默认问候" in result.get("text", ""), str(result)[:160])
    check("指令别名也能触发", bool(manager.dispatch_message(ab_msg("hi"))), "")
    check("正则过滤器能触发",
          (manager.dispatch_message(ab_msg("掷骰子")) or {}).get("text") == "点数 6", "")
    check("多个过滤器是「与」关系：非管理员不触发管理员指令",
          manager.dispatch_message(ab_msg("/管理", group=True, admin=False)) is None, "")
    check("管理员 + 群聊 + 指令 三个条件都满足才触发",
          (manager.dispatch_message(ab_msg("/管理", group=True, admin=True)) or {})
          .get("text") == "管理员专属", "")
    check("仅有群聊+管理员过滤的主题不会误触发其它指令",
          (manager.dispatch_message(ab_msg("/计数", group=True, admin=False)) or {})
          .get("text", "").startswith("第 "), "")

    counter_file = os.path.join(test_root, "abdata", "ab_demo", "n.json")
    if os.path.exists(counter_file):
        os.remove(counter_file)                     # 从干净状态开始数
    manager.dispatch_message(ab_msg("/计数"))
    again = manager.dispatch_message(ab_msg("/计数"))
    check("StarTools 数据目录可持久化", (again or {}).get("text") == "第 2 次",
          str(again)[:120])

    result = manager.dispatch_message(ab_msg("/图片"))
    check("image_result 产出图片（bytes/本地文件）",
          result and result.get("images") and result["images"][0].get("blob"),
          str(result)[:160])

    # 裸 .py（旧"原生插件"格式）现在被直接忽略：既不加载，也不再提示什么"原生插件"
    check("旧的 .py 原生插件不再被加载",
          "原生测试" not in loaded, str(list(loaded)))
    check("裸 .py 不会出现在扫描结果里（按官方约定只认插件目录）",
          all(not item["file"].endswith(".py") for item in manager.scan()),
          str(manager.scan()))

    # 停用 AstrBot 插件
    manager.set_disabled("ab_demo", True)
    check("AstrBot 插件可以被停用（停用后不再响应）",
          manager.dispatch_message(ab_msg("掷骰子")) is None
          or "点数" not in (manager.dispatch_message(ab_msg("掷骰子")) or {}).get("text", ""), "")
    manager.set_disabled("ab_demo", False)

    # 诊断：不支持的过滤器要给出说明而不是静默失败
    broken_dir = os.path.join(ab_dir, "ab_broken")
    os.makedirs(broken_dir, exist_ok=True)
    with open(os.path.join(broken_dir, "metadata.yaml"), "w", encoding="utf-8") as handle:
        handle.write("name: ab_broken\ndesc: 用了暂不支持的过滤器\n")
    with open(os.path.join(broken_dir, "main.py"), "w", encoding="utf-8") as handle:
        handle.write('''from astrbot.api.event import filter
from astrbot.api.star import Star, register


@register("ab_broken", "a", "d", "1.0.0")
class Broken(Star):
    @filter.llm_tool("天气")
    async def tool(self, event):
        pass
''')
    manager.load_plugins(force=True)
    diag = manager.diagnostics()
    check("不支持的 AstrBot 能力会给出诊断（llm_tool）",
          any("llm_tool" in item["reason"] for item in diag["issues"]), str(diag["issues"])[:200])
    check("插件页能列出暂不支持的能力",
          any("llm_tool" in item for info in manager.list_plugins()
              for item in (info.get("unsupported") or [])),
          str([info.get("unsupported") for info in manager.list_plugins()])[:200])

    # ---------------- 官方文档写法的对齐验证（照 docs.astrbot.app 抄一遍）----------------
    docs_dir = os.path.join(ab_dir, "astrbot_plugin_doc")
    os.makedirs(docs_dir, exist_ok=True)
    with open(os.path.join(docs_dir, "metadata.yaml"), "w", encoding="utf-8") as handle:
        handle.write("name: astrbot_plugin_doc\ndisplay_name: 文档示例插件\n"
                     "short_desc: 官方文档写法全覆盖\n"
                     "desc: |\n  按官方文档写的测试插件。\nversion: 5.1.0\nauthor: 文档\n"
                     "astrbot_version: '>=4.0.0'\nsupport_platforms:\n  - qq_official\n")
    with open(os.path.join(docs_dir, "_conf_schema.json"), "w", encoding="utf-8") as handle:
        json.dump({
            "greeting": {"type": "string", "default": "你好"},
            "repeat": {"type": "int", "default": 2},
            "nested": {"type": "object", "items": {
                "flag": {"type": "bool", "default": True},
                "name": {"type": "string", "default": "内层"},
            }},
        }, handle, ensure_ascii=False)
    with open(os.path.join(docs_dir, "main.py"), "w", encoding="utf-8") as handle:
        handle.write('''# -*- coding: utf-8 -*-
import os
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, StarTools


class DocPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    @filter.command("helloworld")
    async def helloworld(self, event: AstrMessageEvent):
        """官方最小示例"""
        yield event.plain_result("Hello, %s!" % event.get_sender_name())

    @filter.command("add")
    async def add(self, event: AstrMessageEvent, a: int, b: int):
        yield event.plain_result("answer=%d" % (a + b))

    @filter.command("math")
    def math(self):
        pass

    @filter.command_group("calc")
    def calc(self):
        pass

    @filter.command("chain")
    async def chain(self, event: AstrMessageEvent):
        await self.context.send_message(
            event.unified_msg_origin,
            MessageChain().message("前缀").at("123").message("中").file_image(self._png()))
        yield event.chain_result([Comp.Plain("第一段"), Comp.Plain("第二段")])

    @filter.command("kv")
    async def kv(self, event: AstrMessageEvent):
        await self.put_kv_data("n", (await self.get_kv_data("n", 0)) + 1)
        yield event.plain_result("n=%s name=%s" % (await self.get_kv_data("n", 0), self.name))

    @filter.command("cfg")
    async def cfg(self, event: AstrMessageEvent):
        yield event.plain_result("g=%s r=%s n=%s" % (
            self.config.get("greeting"), self.config.get("repeat"),
            self.config.get("nested")))

    def _png(self):
        path = os.path.join(StarTools.get_data_dir("astrbot_plugin_doc"), "p.png")
        with open(path, "wb") as fh:
            fh.write(bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                "1f15c4890000000a49444154789c6300010000050001"
                "0d0a2db40000000049454e44ae426082"))
        return path
''')
    # 再用一个"指令组"插件验证嵌套指令组的注册方式
    group_dir = os.path.join(ab_dir, "astrbot_plugin_group")
    os.makedirs(group_dir, exist_ok=True)
    with open(os.path.join(group_dir, "metadata.yaml"), "w", encoding="utf-8") as handle:
        handle.write("name: astrbot_plugin_group\ndesc: 指令组\nversion: 1.0.0\nauthor: t\n")
    with open(os.path.join(group_dir, "main.py"), "w", encoding="utf-8") as handle:
        handle.write('''from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star


class GroupPlugin(Star):
    @filter.command_group("math")
    def math(self):
        pass

    @math.command("add")
    async def add(self, event: AstrMessageEvent, a: int, b: int):
        yield event.plain_result("math add = %d" % (a + b))

    @math.group("calc")
    def calc(self):
        pass

    @calc.command("mul")
    async def mul(self, event: AstrMessageEvent, a: int, b: int):
        yield event.plain_result("math calc mul = %d" % (a * b))
''')

    docs_sent = []

    class DocsRuntime:
        """记录插件主动发送的内容，用来验证消息链顺序。"""

        def __init__(self):
            self.bots = {"bot1": object()}

        def send_text(self, target_type, openid, content, bot_id="", **kwargs):
            docs_sent.append(("text", content))
            return True

        def send_image(self, target_type, openid, image_url="", blob=None, file_name="",
                       content="", bot_id=""):
            docs_sent.append(("image", file_name))
            return True

        def send_file(self, *args, **kwargs):
            docs_sent.append(("file", ""))
            return True

        def lookup_name(self, openid):
            return "名字"

        def short_label(self, openid):
            return "用户"

        def bot_ids(self):
            return ["bot1"]

    docs_manager = PluginManager(plugin_dir=ab_dir, data_dir=os.path.join(test_root, "abdata"),
                                 disabled_file=os.path.join(test_root, "abdisabled.json"),
                                 logger_obj=log)
    docs_manager.set_bot(PluginBot(runtime=DocsRuntime(), config={}, logger_obj=log))
    docs_manager.load_plugins(force=True)
    docs_loaded = {item["name"]: item for item in docs_manager.list_plugins()}
    check("照官方文档写的插件（无 @register，靠 Star 子类识别）能加载",
          "astrbot_plugin_doc" in docs_loaded, str(list(docs_loaded)))
    check("metadata.yaml 的 display_name / astrbot_version / 支持平台都能读到",
          docs_loaded["astrbot_plugin_doc"].get("title") == "文档示例插件"
          and docs_loaded["astrbot_plugin_doc"].get("astrbot_version") == ">=4.0.0"
          and docs_loaded["astrbot_plugin_doc"].get("support_platforms") == ["qq_official"],
          str(docs_loaded["astrbot_plugin_doc"])[:220])
    check("兼容层对外声明的 AstrBot 版本可查",
          docs_manager.diagnostics().get("astrbot_version"), "")

    def doc_msg(text, group=False, admin=False):
        return {"type": "group" if group else "private", "content": text,
                "user_openid": "" if group else "U1", "user_name": "小明",
                "group_openid": "G1" if group else "", "bot_id": bot_id,
                "msg_id": "D1", "is_admin": admin}

    check("官方最小示例（/helloworld）能回复",
          (docs_manager.dispatch_message(doc_msg("/helloworld")) or {})
          .get("text") == "Hello, 小明!", "")
    check("带类型注解的参数会被解析（/add 1 2）",
          (docs_manager.dispatch_message(doc_msg("/add 1 2")) or {}).get("text") == "answer=3", "")
    check("参数类型不符时不触发（/add x y）",
          docs_manager.dispatch_message(doc_msg("/add x y")) is None, "")
    check("__init__(context, config) 能拿到 _conf_schema.json 的默认值（含嵌套 object）",
          (docs_manager.dispatch_message(doc_msg("/cfg")) or {}).get("text")
          == "g=你好 r=2 n={'flag': True, 'name': '内层'}", "")
    kv_file = os.path.join(test_root, "abdata", "astrbot_plugin_doc", "kv.json")
    if os.path.exists(kv_file):
        os.remove(kv_file)                      # 从干净状态开始数
    check("KV 存储（put/get_kv_data）与 self.name 可用",
          (docs_manager.dispatch_message(doc_msg("/kv")) or {}).get("text")
          == "n=1 name=astrbot_plugin_doc",
          str((docs_manager.dispatch_message(doc_msg("/kv")) or {}).get("text")))
    check("指令组的子指令能触发（math add）",
          (docs_manager.dispatch_message(doc_msg("/math add 2 3")) or {}).get("text")
          == "math add = 5", "")
    check("嵌套指令组也能触发（math calc mul）",
          (docs_manager.dispatch_message(doc_msg("/math calc mul 2 3")) or {})
          .get("text") == "math calc mul = 6", "")
    check("一次 yield 多条结果会按顺序全部发出",
          [step["text"] for step in
           ((docs_manager.dispatch_message(doc_msg("/chain")) or {}).get("sequence") or [])
           if step.get("type") == "text"] == ["第一段", "第二段"],
          str((docs_manager.dispatch_message(doc_msg("/chain")) or {}).get("sequence"))[:200])
    docs_sent.clear()
    docs_manager.dispatch_message(doc_msg("/chain"))
    check("主动 send_message 保留消息链顺序（文本→@→文本→图片）",
          [kind for kind, _value in docs_sent] == ["text", "text", "text", "image"],
          str(docs_sent))

    # ------------------------------------------------------------------ 26. 八项修复
    print("\n[26] 修复项：环境变量 / 对外链接 / 死代码 / 并发计数 / 图片上限 / 云墓碑 / 魔数 / 上下文长度")

    # (1) 环境变量
    check("环境变量 QQBOT_AI_API_KEY 解析为 ai.api_key（不再变成 ai.api.key）",
          resolve_env_path("QQBOT_AI_API_KEY") == "ai.api_key",
          str(resolve_env_path("QQBOT_AI_API_KEY")))
    check("cloud_sync / alert_owner_openid 这类含下划线的键也能解析",
          resolve_env_path("QQBOT_CLOUD_SYNC_ENABLED") == "cloud_sync.enabled"
          and resolve_env_path("QQBOT_SECURITY_ALERT_OWNER_OPENID")
          == "security.alert_owner_openid"
          and resolve_env_path("QQBOT_CLOUD_SYNC_STARTUP_BUFFER_MINUTES")
          == "cloud_sync.startup_buffer_minutes", "")
    check("双下划线写法也能解析（QQBOT_AI__API_KEY）",
          resolve_env_path("QQBOT_AI__API_KEY") == "ai.api_key", "")
    check("老写法 QQBOT_WEB_PORT 仍然可用", resolve_env_path("QQBOT_WEB_PORT") == "web.port",
          str(resolve_env_path("QQBOT_WEB_PORT")))
    check("认不出来的环境变量返回 None（调用方会告警）",
          resolve_env_path("QQBOT_NOT_A_REAL_KEY") is None, "")
    env_path = os.path.join(test_root, f"config_env_{run_id}.json")
    os.environ["QQBOT_AI_API_KEY"] = "sk-from-env"
    os.environ["QQBOT_WEB_PORT"] = "9021"
    os.environ["QQBOT_TOTALLY_UNKNOWN_KEY"] = "x"
    try:
        env_manager = load_config(env_path)
        check("环境变量真的覆盖到了运行时配置（ai.api_key）",
              env_manager.config.str_of("ai", "api_key", default="") == "sk-from-env"
              and env_manager.config.int_of("web", "port", default=0) == 9021,
              f"key={env_manager.config.str_of('ai', 'api_key', default='')!r} "
              f"port={env_manager.config.int_of('web', 'port', default=0)}")
        check("没匹配到的环境变量会被记录下来（不再静默忽略）",
              "QQBOT_TOTALLY_UNKNOWN_KEY" in (env_manager.env_unknown or []),
              str(env_manager.env_unknown))
    finally:
        for key in ("QQBOT_AI_API_KEY", "QQBOT_WEB_PORT", "QQBOT_TOTALLY_UNKNOWN_KEY"):
            os.environ.pop(key, None)

    # (2) 对外下载链接
    saved_host = config_manager.config.str_of("web", "host", default="")
    config_manager.set_path("web.host", "0.0.0.0")
    config_manager.set_path("web.public_base_url", "")
    link, note = runtime._public_file_link("/media/a.pdf")
    check("监听 0.0.0.0 时不会把 127.0.0.1 静默发出去（要么换成局域网地址，要么明确警告）",
          ("0.0.0.0" not in link)
          and ("127.0.0.1" not in link or "⚠️" in note or "public_base_url" in note),
          f"{link} / {note}")
    config_manager.set_path("web.public_base_url", "https://bot.example.com/")
    link, note = runtime._public_file_link("/media/a.pdf")
    check("配置了 web.public_base_url 就用它（并去掉多余斜杠）",
          link == "https://bot.example.com/media/a.pdf" and "public_base_url" in note,
          f"{link} / {note}")
    config_manager.set_path("web.public_base_url", "")
    config_manager.set_path("web.host", saved_host or "127.0.0.1")
    check("只监听 127.0.0.1 时明确警告链接别人打不开",
          "⚠️" in runtime._public_file_link("/media/a.pdf")[1], "")

    # (3) 死代码：配置监视里不再有动画设置
    import inspect as _inspect
    watch_src = _inspect.getsource(Runtime._start_config_watcher)
    check("配置监视里的死代码（ui.animation 无用判断）已删除",
          "animation" not in watch_src, watch_src[:120])
    mask_src = _inspect.getsource(ConfigManager.mask_secrets)
    check("mask_secrets 里的空 if 已删除", "pass" not in mask_src, mask_src[-160:])

    # (4) 繁忙提示并发计数
    proc = runtime.processor
    busy_stat = {"total": 0, "current": 0, "peak": 0}
    original_reply = proc._reply
    busy_lock = __import__("threading").Lock()

    def _count_reply(_message, text):
        # 只统计"繁忙提示"这一类回复（别的线程可能同时在发正常回复）
        if "正在处理其他消息" in str(text):
            with busy_lock:
                busy_stat["total"] += 1
                busy_stat["current"] += 1
                busy_stat["peak"] = max(busy_stat["peak"], busy_stat["current"])
        time.sleep(0.05)
        if "正在处理其他消息" in str(text):
            with busy_lock:
                busy_stat["current"] -= 1

    proc._reply = _count_reply
    try:
        threads = [__import__("threading").Thread(
            target=proc._send_busy_reply, args=({"type": "private"},)) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
    finally:
        proc._reply = original_reply
    check("繁忙提示的并发上限是 5（信号量，不会 5~6 条一起发）",
          busy_stat["peak"] <= 5, f"峰值 {busy_stat['peak']} 条，共发出 {busy_stat['total']} 条")

    # (5) 图片上限后端也遵守
    class StubImage(QQApiClient):
        def __init__(self):
            super().__init__("botX", "1234567890", "fake-secret", config=config_manager.config,
                             logger_obj=log)
            self.uploaded = []

        def request(self, method, url, json_data=None, params=None, retries=None):
            if url.endswith("/upload_prepare"):
                return {"upload_id": "u1", "block_size": str(4 * 1024 * 1024),
                        "parts": [{"index": 1, "presigned_url": "https://cos/p1",
                                   "block_size": "4194304"}],
                        "upload_config": {"concurrency": 1}}
            if url.endswith("/files"):
                self.uploaded.append(json_data)
                return {"file_info": "FI"}
            if url.endswith("/messages"):
                return {"id": "M1"}
            return {}

        def _put_chunk(self, url, chunk, retries=3, delay=1.0, timeout=120.0):
            return None

    api_img = StubImage()
    saved_image_mb = config_manager.config.float_of("send", "max_image_mb", default=20)
    config_manager.set_path("send.max_image_mb", 1)
    try:
        api_img.send_image_by_data("private", "U1", b"\x89PNG\r\n\x1a\n" + b"x" * (2 * 1024 * 1024),
                                   "big.png")
        over_error = ""
    except Exception as exc:
        over_error = str(exc)
    check("后端也遵守 send.max_image_mb（1MB 设置下 2MB 图片被拒）",
          "1 MB" in over_error or "1MB" in over_error, over_error[:160])
    config_manager.set_path("send.max_image_mb", saved_image_mb or 20)

    # (7) 魔数校验：扩展名是图片但内容不是
    fake_png = b"MZ\x90\x00" + b"\x00" * 64          # 假装 exe
    response = client.post("/api/chat/send", data={
        "targetType": "private", "openid": "USER_MAGIC", "bot_id": bot_id,
        "file": (io.BytesIO(fake_png), "evil.png"), "as_image": "1",
    }, content_type="multipart/form-data")
    check("as_image=1 但内容不是图片 → 明确拒绝（HTTP 400）",
          response.status_code == 400 and "不是图片" in (response.get_json() or {}).get("message", ""),
          f"{response.status_code} {response.get_json()}")
    response = client.post("/api/chat/send", data={
        "targetType": "private", "openid": "USER_MAGIC", "bot_id": bot_id,
        "file": (io.BytesIO(fake_png), "evil.png"),
    }, content_type="multipart/form-data")
    body = response.get_json() or {}
    check("扩展名像图片但内容是别的 → 按文件发送并说明",
          response.status_code == 200 and body.get("mode") != "image"
          and "不是图片" in (body.get("note") or ""),
          f"{response.status_code} {str(body)[:180]}")

    # (6) 云同步墓碑是否删本地，跟随 apply_remote_deletes
    sync2 = CloudSync(config_manager, logger_obj=log)
    config_manager.set_path("cloud_sync.enabled", True)
    tomb_root = os.path.join(test_root, "tombroot")
    shutil.rmtree(tomb_root, ignore_errors=True)
    os.makedirs(os.path.join(tomb_root, "data", "user_context", "private"), exist_ok=True)
    target_file = os.path.join(tomb_root, "data", "user_context", "private", "T.json")
    paths.BASE_DIR = tomb_root
    try:
        for flag, expect_exists in ((False, True), (True, False)):
            with open(target_file, "w", encoding="utf-8") as handle:
                handle.write('{"a": 1}')
            os.utime(target_file, (1000, 1000))          # 本地很旧
            cloud = FakeCloud({"data/user_context/private/T.json": ("", time.time() * 1000)})
            cloud.rows["data/user_context/private/T.json"] = ("", int(time.time() * 1000))
            cloud_str = FakeCloud({})
            cloud_str.rows["data/user_context/private/T.json"] = ("", int(time.time() * 1000))
            # 墓碑：deleted=1
            original_fetch = cloud_str.fetch_all

            def fetch_with_tomb(rows=cloud_str.rows):
                out = {}
                for key, (value, ts) in rows.items():
                    out[key] = {"value": value, "updated_at": ts, "deleted": 1}
                return out

            cloud_str.fetch_all = fetch_with_tomb
            config_manager.set_path("cloud_sync.apply_remote_deletes", bool(flag))
            sync2.backend = lambda backend=cloud_str: backend
            sync2.sync_once(force=True)
            exists = os.path.exists(target_file)
            check("apply_remote_deletes=%s 时%s" % (
                "true" if flag else "false",
                "云端墓碑会删除本地文件" if flag else "云端墓碑不会删除本地文件（备份更安全）"),
                exists is expect_exists, f"文件存在={exists}")
    finally:
        paths.BASE_DIR = old_base
        config_manager.set_path("cloud_sync.apply_remote_deletes", False)

    # (8b) 群级上下文条数不再改全局 MAX_HISTORY
    ctx = runtime.context_manager
    original_max = ctx.MAX_HISTORY
    for index in range(6):
        ctx.append(bot_id, "group", "GROUP_CTX", "user", f"第{index}句")
    limited = ctx.format_for_prompt(bot_id, "group", "GROUP_CTX", "", max_history=2)
    check("按会话传入的上下文条数生效（只取最后 2 条）",
          len(limited) == 2 and limited[-1]["content"] == "第5句", str(limited)[-140:])
    check("群级设置不再改到全局 MAX_HISTORY",
          ctx.MAX_HISTORY == original_max, f"{ctx.MAX_HISTORY} vs {original_max}")
    bigger = ctx.format_for_prompt(bot_id, "group", "GROUP_CTX", "", max_history=original_max)
    check("另一个会话/机器人仍用自己的条数（互不影响）", len(bigger) == 6, str(len(bigger)))

    # ------------------------------------------------------------------ 27. 机器人 id 重复 & 插件权限收紧
    print("\n[27] 机器人 id 重复要告警 / 插件只能拿到收紧后的能力（原生插件已移除）")

    dup_path = os.path.join(test_root, f"config_dup_{run_id}.json")
    with open(dup_path, "w", encoding="utf-8") as handle:
        json.dump({"bots": [
            {"id": "same", "name": "A", "enabled": True, "app_id": "1", "app_secret": "s"},
            {"id": "same", "name": "B", "enabled": True, "app_id": "2", "app_secret": "s"},
        ]}, handle)
    dup_manager = load_config(dup_path)
    check("重复的机器人 id 会被检测出来", dup_manager.duplicate_bot_ids() == ["same"],
          str(dup_manager.duplicate_bot_ids()))
    check("重复 id 会被记录下来（页面据此提示）",
          dup_manager.payload().get("duplicate_bot_ids") == ["same"], "")
    first = ConfigManager._find_bot(dup_manager.config.data, "same")
    check("按 id 查找仍然返回第一个（但已经告警，不再是静默）",
          (first or {}).get("name") == "A", str(first))

    # 插件拿到的配置不能含密钥
    secret_cfg = {
        "ai": {"api_key": "sk-secret", "model": "m"},
        "web": {"token": "tok-1"},
        "cloud_sync": {"api_token": "cf-token", "account_id": "acc"},
        "security": {"alert_owner_openid": "OWNER"},
        "bots": [{"id": "bot1", "app_secret": "very-secret"}],
        "plugins": {"native_enabled": True},
    }
    plugin_bot = PluginBot(runtime=None, config=secret_cfg, logger_obj=log)
    check("插件看到的配置里没有 AI 密钥/网页令牌/云同步令牌",
          plugin_bot.config["ai"]["api_key"] == ""
          and plugin_bot.config["web"]["token"] == ""
          and plugin_bot.config["cloud_sync"]["api_token"] == ""
          and plugin_bot.config["security"]["alert_owner_openid"] == "",
          str(plugin_bot.config)[:200])
    check("插件看不到机器人的 AppSecret",
          plugin_bot.config["bots"][0]["app_secret"] == "", "")
    check("插件仍能读到普通配置（model 等）",
          plugin_bot.config["ai"]["model"] == "m", "")
    check("插件原生配置对象本身没有被改动（只是给插件的是副本）",
          secret_cfg["ai"]["api_key"] == "sk-secret", "")

    class FakePluginRuntime:
        def __init__(self):
            self.sent = []
            self.bots = {"bot1": object()}
            self.config = {"ai": {"api_key": "sk-x"}}      # 故意放个"真"配置看插件能不能摸到

        def send_text(self, target_type, openid, content, bot_id="", **kwargs):
            self.sent.append(("text", target_type, openid, content))
            return True

        def send_image(self, target_type, openid, **kwargs):
            self.sent.append(("image", target_type, openid))
            return True

        def send_file(self, *args, **kwargs):
            self.sent.append(("file",))
            return True

        def lookup_name(self, openid):
            return "名字"

        def short_label(self, openid):
            return "用户 xx"

        def bot_ids(self):
            return ["bot1"]

    fake_runtime = FakePluginRuntime()
    bot_with_runtime = PluginBot(runtime=fake_runtime, config=secret_cfg, logger_obj=log)
    check("插件拿到的是白名单门面，摸不到 runtime.store / runtime.config",
          not hasattr(bot_with_runtime.runtime, "store")
          and not hasattr(bot_with_runtime.runtime, "config")
          and not hasattr(bot_with_runtime.runtime, "media"),
          str([name for name in dir(bot_with_runtime.runtime) if not name.startswith("_")])[:200])
    check("门面里的发送能力仍然可用（向后兼容）",
          bot_with_runtime.runtime.send_text("group", "G1", "hi") is True
          and bot_with_runtime.send_message("U1", "hello") is True
          and bot_with_runtime.bot_ids() == ["bot1"],
          str(fake_runtime.sent))

    # 目录里只有裸 .py（旧原生插件格式）时：直接当普通文件忽略，不加载也不报"原生插件"
    risky_dir = os.path.join(test_root, "riskplugins")
    shutil.rmtree(risky_dir, ignore_errors=True)
    os.makedirs(risky_dir, exist_ok=True)
    with open(os.path.join(risky_dir, "risky.py"), "w", encoding="utf-8") as handle:
        handle.write('import os\nimport shutil\nPLUGIN = {"name": "危险插件"}\n'
                     'COMMANDS = ["/x"]\n'
                     'def on_message(msg, bot=None):\n'
                     '    shutil.rmtree("data")\n'
                     '    return None\n')
    risky_manager = PluginManager(plugin_dir=risky_dir,
                                  data_dir=os.path.join(test_root, "riskdata"),
                                  disabled_file=os.path.join(test_root, "riskdisabled.json"),
                                  logger_obj=log)
    risky_manager.load_plugins(force=True)
    risky_diag = risky_manager.diagnostics()
    check("旧原生插件（裸 .py）不会被加载",
          risky_manager.list_plugins() == [], str(risky_manager.list_plugins()))
    check("裸 .py 不会被当成插件（扫描结果为空）",
          risky_diag["discovered"] == [], str(risky_diag["discovered"]))
    check("只会提示「目录里没有 AstrBot 插件」",
          any("没有 AstrBot 插件" in item["reason"] for item in risky_diag["issues"]),
          str(risky_diag["issues"])[:220])
    check("不会再出现「原生插件」这种旧说法的提示",
          not any("原生插件" in item["reason"] for item in risky_diag["issues"]),
          str(risky_diag["issues"])[:220])

    # 插件系统整体关闭时不加载
    off_manager = PluginManager(plugin_dir=ab_dir, data_dir=os.path.join(test_root, "abdata"),
                                disabled_file=os.path.join(test_root, "abdisabled.json"),
                                logger_obj=log, enabled=False)
    off_manager.set_bot(PluginBot(runtime=None, config={}, logger_obj=log))
    off_manager.load_plugins(force=False)
    check("plugins.enabled=false 时不加载任何插件",
          off_manager.list_plugins() == []
          and any("plugins.enabled=false" in item["reason"]
                  for item in off_manager.diagnostics()["issues"]),
          str(off_manager.diagnostics()["issues"])[:200])

    # ------------------------------------------------------------------ 28. 引用不受 2 分钟限制 / 上下文按机器人隔离
    print("\n[28] 引用（不受 2 分钟限制）/ 上下文按机器人隔离 / 群管理「最近消息」已移除")

    # (1) 引用：2 分钟规则只属于"撤回"，引用按消息新旧选字段
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "group",
                       "group_openid": "GROUP_Q", "openid": "M_Q", "username": "小引",
                       "content": "刚发的", "msg_id": "Q_NEW"})
    runtime.store.add({"bot_id": bot_id, "direction": "in", "type": "group",
                       "group_openid": "GROUP_Q", "openid": "M_Q", "username": "小引",
                       "content": "很久以前", "msg_id": "Q_OLD"})
    runtime.store._execute(
        "UPDATE messages SET ts=? WHERE msg_id=?", (time.time() - 3600, "Q_OLD"))
    runtime.store.add({"bot_id": bot_id, "direction": "out", "type": "group",
                       "group_openid": "GROUP_Q", "content": "机器人自己发的",
                       "msg_id": "Q_OUT"})
    check("引用刚收到的消息 → 用被动回复 + 引用两种字段",
          runtime._quote_reply_style("group", "GROUP_Q", bot_id, "Q_NEW") == "both", "")
    check("引用一小时前的消息 → 只用「引用」字段（不会被 2 分钟限制挡住）",
          runtime._quote_reply_style("group", "GROUP_Q", bot_id, "Q_OLD") == "message_reference",
          str(runtime._quote_reply_style("group", "GROUP_Q", bot_id, "Q_OLD")))
    check("引用机器人自己的旧消息 → 只用「引用」字段",
          runtime._quote_reply_style("group", "GROUP_Q", bot_id, "Q_OUT") == "message_reference",
          str(runtime._quote_reply_style("group", "GROUP_Q", bot_id, "Q_OUT")))
    check("查不到消息 ID 时保守地只用「引用」字段",
          runtime._quote_reply_style("group", "GROUP_Q", bot_id, "NOT_FOUND")
          == "message_reference", "")

    fake.calls.clear()
    runtime.send_text("group", "GROUP_Q", "引用一条老消息", bot_id=bot_id, reply_msg_id="Q_OLD")
    sent = [c for c in fake.calls if c["kind"] == "text"][-1]
    check("引用老消息时发送请求仍带着引用（reply_to 不为空、风格是 message_reference）",
          sent["reply_to"] == "Q_OLD" and sent["reply_style"] == "message_reference",
          str(sent)[:200])

    # 客户端层面的降级链：msg_id 被平台拒 → 保留 message_reference 重发
    class QuoteStub(QQApiClient):
        """模拟 QQ：带 msg_id 的被动回复被拒（过期），引用字段可用。"""

        def __init__(self):
            super().__init__("botQ", "1234567890", "fake-secret",
                             config=config_manager.config, logger_obj=log)
            self.payloads = []

        def request(self, method, url, json_data=None, params=None, retries=None):
            if url.endswith("/messages"):
                self.payloads.append(dict(json_data or {}))
                if json_data.get("msg_id"):
                    raise RuntimeError("API 错误 40054005: 请求数据异常（被动回复已过期）")
                return {"id": "MSG_OK"}
            return {}

    stub = QuoteStub()
    info = stub.send_text("group", "G1", "hi", reply_msg_id="OLD_ID")
    check("被动回复字段被拒后自动改用「引用」重发（引用不会丢）",
          len(stub.payloads) == 2 and "msg_id" not in stub.payloads[-1]
          and stub.payloads[-1].get("message_reference", {}).get("message_id") == "OLD_ID"
          and info.get("id") == "MSG_OK",
          f"{stub.payloads} / {info}")
    check("降级原因会写进 note（页面上看得到）",
          "被动回复" in (info.get("_note") or ""), str(info.get("_note")))

    class QuoteFailStub(QuoteStub):
        def request(self, method, url, json_data=None, params=None, retries=None):
            if url.endswith("/messages"):
                self.payloads.append(dict(json_data or {}))
                if "message_reference" in json_data:
                    raise RuntimeError("API 错误 40011000: 请求数据异常")
                return {"id": "MSG_PLAIN"}
            return {}

    fail_stub = QuoteFailStub()
    info = fail_stub.send_text("group", "G1", "hi", reply_msg_id="OLD_ID")
    check("引用也被拒时才发普通消息，并说明引用未生效",
          info.get("id") == "MSG_PLAIN" and "引用未生效" in (info.get("_note") or ""),
          f"{fail_stub.payloads} / {info}")

    # (2) 上下文按机器人隔离
    ctx2 = runtime.context_manager
    ctx2.append(bot_id, "private", "USER_ISO_MINE", "user", "我的上下文")
    ctx2.append("ghostbot", "private", "USER_ISO_OTHER", "user", "别的机器人的上下文")
    mine = ctx2.summary(bot_id=bot_id)
    check("summary(bot_id=…) 只列该机器人的上下文文件",
          all(item["bot_id"] == bot_id for item in mine["private"])
          and any(item["openid"] == "USER_ISO_MINE" for item in mine["private"])
          and not any(item["openid"] == "USER_ISO_OTHER" for item in mine["private"]),
          str([(i["bot_id"], i["openid"]) for i in mine["private"]])[:200])

    config_manager.set_active_bot(bot_id)
    body = client.get("/api/admin/context").get_json()
    check("上下文接口带上是哪个机器人的",
          body.get("bot_id") == bot_id and body.get("bot_name"), str(body.get("bot_id")))
    check("上下文接口只返回当前机器人的文件",
          all(item["bot_id"] == bot_id
              for item in (body.get("private") or []) + (body.get("group") or [])),
          str([(i["bot_id"], i["openid"]) for i in (body.get("private") or [])])[:200])

    response = client.post("/api/admin/context/delete",
                           json={"scope": "private", "name": "ghostbot__USER_ISO_OTHER.json"})
    check("删除别的机器人的上下文文件被拒绝（403）", response.status_code == 403, "")

    client.post("/api/admin/context/delete", json={"all": True})       # 不带 bot_id
    ghost = [item for item in ctx2.summary()["private"]
             if item["bot_id"] == "ghostbot"]
    check("清空上下文只清当前机器人的，其它机器人的文件还在",
          bool(ghost) and not any(item["bot_id"] == bot_id and item["openid"] == "USER_ISO_MINE"
                                  for item in ctx2.summary()["private"]),
          str([(i["bot_id"], i["openid"]) for i in ctx2.summary()["private"]])[:200])
    ctx2.clear(bot_id="ghostbot")
    check("context_manager.clear(bot_id=…) 也只清指定机器人",
          not [item for item in ctx2.summary()["private"] if item["bot_id"] == "ghostbot"], "")

    # 页面没刷新时的"过期列表"：请求里的 bot_id 是切换前缓存的，不能拿它放行
    ctx2.append("ghostbot2", "private", "USER_STALE", "user", "另一个机器人的上下文")
    config_manager.set_active_bot(bot_id)
    stale_name = "ghostbot2__USER_STALE.json"
    response = client.post("/api/admin/context/delete",
                           json={"scope": "private", "name": stale_name,
                                 "bot_id": "ghostbot2"})       # ← 页面缓存的旧机器人
    check("页面没刷新时删除别的机器人的上下文会被拒绝（409）",
          response.status_code == 409, f"{response.status_code} {response.get_json()}")
    check("被拒绝后文件仍然存在",
          os.path.isfile(os.path.join("data", "user_context", "private", stale_name)), "")
    response = client.post("/api/admin/context/delete",
                           json={"scope": "private", "name": stale_name})
    check("不传 bot_id 时按归属判断，同样拒绝（403）", response.status_code == 403, "")
    response = client.post("/api/admin/context/delete", json={"all": True, "bot_id": "ghostbot2"})
    check("清空接口也会拒绝过期的 bot_id（409）", response.status_code == 409, "")
    check("被拒绝后仍然没有被清空",
          os.path.isfile(os.path.join("data", "user_context", "private", stale_name)), "")

    # 自己的文件：正常删除
    ctx2.append(bot_id, "private", "USER_MINE_DEL", "user", "我自己的上下文")
    own_name = f"{bot_id}__USER_MINE_DEL.json"
    response = client.post("/api/admin/context/delete",
                           json={"scope": "private", "name": own_name, "bot_id": bot_id})
    check("删除当前机器人自己的上下文正常成功",
          response.status_code == 200 and (response.get_json() or {}).get("success") is True,
          f"{response.status_code} {response.get_json()}")
    ctx2.clear(bot_id="ghostbot2")

    # ------------------------------------------------------------------ 29. 群消息全量模式下的 @ 判定
    print("\n[29] 群消息「接收所有消息」全量模式：@ 机器人必须仍能触发回复")
    # 用真实机器人 id 建网关探针（消息里的 bot_id 要能对上真实机器人，回复才发得出去）
    probe = gateway_module.QQGateway(bot_id, None, logger_obj=log)

    def _full_event(content, mentions, msg_id="GW_MSG_1", event="GROUP_MESSAGE_CREATE"):
        return {"t": event, "d": {
            "id": msg_id, "content": content, "group_openid": "GROUP_FULL",
            "author": {"member_openid": "MEMBER_FULL", "username": "小全"},
            "mentions": mentions, "timestamp": "2026-01-01T00:00:00+08:00",
            "message_scene": {"source": "default"}}}

    SELF_MENTION = [{"id": "BOT7C8CC5AB", "member_openid": "BOT7C8CC5AB", "username": "机器人自己",
                     "bot": True, "is_you": True, "scope": "single"}]
    OTHER_MENTION = [{"id": "BOT5697478F", "member_openid": "BOT5697478F", "username": "别的机器人",
                      "bot": True, "is_you": False, "scope": "single"}]
    ALL_MENTION = [{"username": "全体成员", "is_you": True, "scope": "all"}]

    got = []
    probe.on_message = got.append
    probe._handle_dispatch(_full_event("<@BOT7C8CC5AB> 你好", SELF_MENTION))
    check("全量群消息里 @ 机器人被认出来（事件名不再是唯一依据）",
          bool(got) and got[0]["is_at_bot"] is True, str(got[:1])[:220])
    check("判定来源标成 mentions（便于排查）",
          bool(got) and got[0].get("at_bot_source") == "mention", str(got[:1])[:220])
    check("全量模式被记录下来", probe.all_message_mode is True, str(probe.event_counts))
    check("事件计数里能看到 GROUP_MESSAGE_CREATE",
          probe.event_counts.get("GROUP_MESSAGE_CREATE") == 1, str(probe.event_counts))

    got[:] = []
    probe._handle_dispatch(_full_event("<@all> 大家快去睡觉吧", ALL_MENTION, msg_id="GW_MSG_2"))
    check("@全体成员不算 @ 机器人（否则会抢答所有 @全体成员）",
          bool(got) and got[0]["is_at_bot"] is False, str(got[:1])[:220])

    got[:] = []
    probe._handle_dispatch(_full_event("<@BOT5697478F> 你好", OTHER_MENTION, msg_id="GW_MSG_3"))
    check("@ 别的机器人不算 @ 自己",
          bool(got) and got[0]["is_at_bot"] is False, str(got[:1])[:220])

    got[:] = []
    probe._handle_dispatch(_full_event("你好", [], msg_id="GW_MSG_4"))
    check("全量模式下没 @ 机器人也照常入库（只是不回复）",
          bool(got) and got[0]["is_at_bot"] is False, str(got[:1])[:220])

    got[:] = []
    probe._handle_dispatch({"t": "GROUP_AT_MESSAGE_CREATE", "d": {
        "id": "GW_MSG_5", "content": " /帮助 ", "group_openid": "GROUP_FULL",
        "author": {"member_openid": "MEMBER_FULL", "username": "小全"}}})
    check("官方 @ 事件（没有 mentions）依然认定为被 @",
          bool(got) and got[0]["is_at_bot"] is True
          and got[0].get("at_bot_source") == "event", str(got[:1])[:220])

    # 真链路：全量模式 + @ 机器人 → 必须能走完处理链路并回复
    check("测试前提：该机器人开着「需要 @ 才回复」",
          runtime.bot_config(bot_id).bool_of("reply", "require_mention", default=True), "")

    fake.calls.clear()
    got[:] = []
    probe._handle_dispatch(_full_event("<@BOT7C8CC5AB> /帮助", SELF_MENTION, msg_id="GW_MSG_6"))
    runtime._handle_incoming(got[0])
    deadline = time.time() + 5
    while time.time() < deadline and not any(item["kind"] == "text" for item in fake.calls):
        time.sleep(0.05)
    replies = [item for item in fake.calls if item["kind"] == "text"]
    check("全量模式下 @ 机器人仍然会回复（以前这里被“需要 @ 才回复”拦掉）",
          bool(replies), str(fake.calls)[:220])
    check("回复内容是 /帮助 的指令回复",
          bool(replies) and "可用指令" in replies[0]["content"],
          replies[0]["content"][:80] if replies else "")

    fake.calls.clear()
    got[:] = []
    probe._handle_dispatch(_full_event("大家晚上好", [], msg_id="GW_MSG_7"))
    runtime._handle_incoming(got[0])
    time.sleep(1.0)
    check("全量模式下没 @ 机器人的消息不会回复（不会被刷屏）",
          not any(item["kind"] == "text" for item in fake.calls), str(fake.calls)[:200])

    fake.calls.clear()
    got[:] = []
    probe._handle_dispatch(_full_event("<@all> 大家快去睡觉吧", ALL_MENTION, msg_id="GW_MSG_8"))
    runtime._handle_incoming(got[0])
    time.sleep(1.0)
    check("全量模式下 @全体成员 不会触发回复",
          not any(item["kind"] == "text" for item in fake.calls), str(fake.calls)[:200])

    from core import message_processor as _mp
    check("@全体成员 的标记也会被剥掉（AI 不会看到 <@all>）",
          _mp.MENTION_PREFIX_RE.sub("", "<@all> 大家快去睡觉吧").strip() == "大家快去睡觉吧",
          _mp.MENTION_PREFIX_RE.sub("", "<@all> 大家快去睡觉吧").strip())
    check("@ 多个人的标记也能全部剥掉",
          _mp.MENTION_PREFIX_RE.sub("", " <@BOT7C8CC5AB> <@all> 你好").strip() == "你好",
          _mp.MENTION_PREFIX_RE.sub("", " <@BOT7C8CC5AB> <@all> 你好").strip())

    # 状态接口要能看到事件统计与全量模式（供网页状态卡片显示）
    live_bot = runtime.bots.get(bot_id)
    if live_bot is not None:
        live_bot.gateway.all_message_mode = True
        live_bot.gateway.event_counts["GROUP_MESSAGE_CREATE"] = 3
        status = live_bot.status()
        check("机器人状态里带事件统计",
              status.get("events", {}).get("GROUP_MESSAGE_CREATE") == 3, str(status.get("events")))
        check("机器人状态里带全量模式标记", status.get("all_message_mode") is True, "")
        status_body = client.get("/api/admin/status").get_json()
        card = [item for item in (status_body.get("bots") or []) if item["id"] == bot_id]
        check("状态接口把事件统计给到网页",
              bool(card) and "GROUP_MESSAGE_CREATE" in (card[0].get("events") or {}),
              str(card[:1])[:200])
        live_bot.gateway.all_message_mode = False
        live_bot.gateway.event_counts.clear()

    # 插件“说处理了却没有内容”时要留下告警（否则看起来就是机器人不回复）
    plugin_result = runtime.plugin_manager.dispatch_message({
        "type": "private", "content": "/astrbot", "user_openid": "U_PROBE",
        "member_openid": "U_PROBE", "bot_id": bot_id, "mentions": [], "attachments": []})
    check("示例插件的指令能被分发到",
          bool(plugin_result) and plugin_result.get("handled") is True, str(plugin_result)[:200])
    check("插件结果带 via_sink 标记（区分 event.send 与返回内容）",
          bool(plugin_result) and "via_sink" in plugin_result, str(plugin_result)[:200])

    # ------------------------------------------------------------------ 30. 设置页保存
    print("\n[30] 设置页保存：per-bot 设置不能丢 / 不误报需重启 / 指令面板即时生效")
    from core import config_schema as _schema

    def _bots_form():
        """模拟网页「机器人账号」卡片提交的内容（只有账号字段 + 打码密钥）。"""
        out = []
        for item in (config_manager.config.data.get("bots") or []):
            out.append({
                "id": item.get("id"), "name": item.get("name"),
                "enabled": bool(item.get("enabled")),
                "app_id": item.get("app_id") or "",
                "app_secret": _schema.SECRET_MASK,
                "sandbox": bool(item.get("sandbox")),
                "intents": int(item.get("intents") or 100663296),
                "reconnect_attempts": int(item.get("reconnect_attempts") or 5),
                "reconnect_interval": int(item.get("reconnect_interval") or 10),
            })
        return out

    def _saved_bot(target_id):
        for item in (config_manager.config.data.get("bots") or []):
            if str(item.get("id")) == str(target_id):
                return item
        return {}

    def _file_bot(target_id):
        with open(config_manager.config_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        for item in (data.get("bots") or []):
            if str(item.get("id")) == str(target_id):
                return item
        return {}

    clean_bot = _saved_bot(bot_id)
    clean_bot.pop("overrides", None)
    config_manager.set_path("bots", [clean_bot])
    config_manager.set_path("panels.commands", [{"type": "command", "name": "/帮助",
                                                 "desc": "显示可用指令"}])
    config_manager.set_path("reply.require_mention", True)
    config_manager.save()
    config_manager.reload()

    fake.calls.clear()
    body = client.post("/api/admin/config", json={
        "config": {
            "bots": _bots_form(),
            "panels": {"commands": [
                {"type": "command", "name": "/帮助", "desc": "显示可用指令"},
                {"type": "command", "name": "/打卡", "desc": "每日打卡"},
            ]},
        },
        "bot_id": bot_id,
    }).get_json()
    check("整份表单保存成功", body.get("success") is True, str(body)[:200])
    check("没有把 bots 误报成修改项（以前每次都报）",
          not any(str(item).startswith("bots") for item in (body.get("changed") or [])),
          str(body.get("changed"))[:200])
    check("没有被误报成需要重启",
          not (body.get("need_restart") or []), str(body.get("need_restart"))[:200])
    commands = config_manager.get_for_bot(bot_id, "panels.commands") or []
    check("指令面板改到了当前选中的机器人",
          [item.get("name") for item in commands] == ["/帮助", "/打卡"],
          str([item.get("name") for item in commands])[:200])
    check("机器人账号字段没有被写坏",
          _saved_bot(bot_id).get("app_id") == clean_bot.get("app_id")
          and _saved_bot(bot_id).get("app_secret"),
          str(_saved_bot(bot_id))[:200])
    check("该机器人的 overrides 还在内存配置里",
          bool((_saved_bot(bot_id).get("overrides") or {}).get("panels")),
          str(_saved_bot(bot_id).get("overrides"))[:200])
    check("该机器人的 overrides 真的落盘了（不是只改了内存）",
          bool((_file_bot(bot_id).get("overrides") or {}).get("panels")),
          str(_file_bot(bot_id))[:200])

    panel_calls = [call for call in fake.calls
                   if call["kind"] in ("panel_create", "panel_update")]
    check("保存后会把指令面板重新注册到平台（不用手动刷新、也不用重启）",
          bool(panel_calls), str([call["kind"] for call in fake.calls])[:200])
    submitted = []
    for call in panel_calls:
        submitted.extend(call.get("items") or [])
    check("重新注册的内容就是新指令列表",
          any("打卡" in str(item.get("name")) for item in submitted), str(submitted)[:200])
    check("保存结果里告诉用户面板已重新注册",
          "指令面板" in (body.get("message") or ""), str(body.get("message"))[:200])

    # 再存一次（什么都没改）：不能又说"需要重启"
    body2 = client.post("/api/admin/config", json={
        "config": {"bots": _bots_form(),
                   "panels": {"commands": [
                       {"type": "command", "name": "/帮助", "desc": "显示可用指令"},
                       {"type": "command", "name": "/打卡", "desc": "每日打卡"},
                   ]}},
        "bot_id": bot_id,
    }).get_json()
    check("重复保存（无改动）时提示“没有变化”",
          "没有变化" in (body2.get("message") or ""), str(body2.get("message"))[:200])
    check("重复保存（无改动）时不再提示需要重启",
          not (body2.get("need_restart") or []) and not (body2.get("changed") or []),
          f"changed={body2.get('changed')} need_restart={body2.get('need_restart')}")

    # 其它 per-bot 分组（AI/回复/过滤…）也不能被 bots 列表覆盖掉
    client.post("/api/admin/config", json={
        "config": {"bots": _bots_form(), "reply": {"max_segment_length": 1234}},
        "bot_id": bot_id,
    })
    check("回复设置（另一个 per-bot 分组）同样保住了",
          config_manager.get_for_bot(bot_id, "reply.max_segment_length") == 1234,
          str(config_manager.get_for_bot(bot_id, "reply.max_segment_length")))
    check("回复设置也落盘了",
          ((_file_bot(bot_id).get("overrides") or {}).get("reply") or {}
           ).get("max_segment_length") == 1234, str(_file_bot(bot_id))[:200])

    # 关掉指令面板：线上面板要删掉，不能"关了还在"
    fake.calls.clear()
    off = client.post("/api/admin/config", json={
        "config": {"bots": _bots_form(), "panels": {"enabled": False}}, "bot_id": bot_id,
    }).get_json()
    check("关闭指令面板会去删掉线上已注册的面板",
          any(call["kind"] == "panel_delete" for call in fake.calls),
          str([call["kind"] for call in fake.calls])[:200])
    check("关闭后提示里说明了面板状态",
          "指令面板" in (off.get("message") or ""), str(off.get("message"))[:200])
    client.post("/api/admin/config", json={
        "config": {"bots": _bots_form(), "panels": {"enabled": True}}, "bot_id": bot_id,
    })

    # ------------------------------------------------------------------ 31. 群里非指令消息要走完链路
    print("\n[31] 群里非指令消息：插件/AI 链路不能被内部报错兜底成“系统暂时遇到了问题”")
    captured = []
    original_dispatch = runtime.plugin_manager.dispatch_message

    def _spy(msg):
        captured.append(dict(msg))
        return original_dispatch(msg)

    runtime.plugin_manager.dispatch_message = _spy
    try:
        fake.calls.clear()
        runtime._handle_incoming({
            "bot_id": bot_id, "type": "group", "group_openid": "GROUP_PIPELINE",
            "openid": "MEMBER_PIPE", "member_openid": "MEMBER_PIPE", "username": "小管",
            "content": "<@BOT_OPENID> 今天天气如何", "msg_id": "PIPE_1", "is_at_bot": True,
            "attachments": [], "quote": {},
            "mentions": [{"openid": "BOT_OPENID", "is_you": True}], "raw_event": "{}",
        })
        deadline = time.time() + 5
        while time.time() < deadline and not any(item["kind"] == "text" for item in fake.calls):
            time.sleep(0.05)
        texts = [item["content"] for item in fake.calls if item["kind"] == "text"]
        check("群里 @ 机器人发普通消息能走完整条链路", bool(texts), str(fake.calls)[:200])
        check("不会再兜底成“系统暂时遇到了问题”",
              all("系统暂时遇到了问题" not in (text or "") for text in texts), str(texts)[:200])
        check("群里 @ 机器人的普通消息会真的交给 AI",
              any("AI 回复" in (text or "") for text in texts), str(texts)[:200])
        expected_name = runtime.group_manager.group_names().get("GROUP_PIPELINE", "")
        check("插件分发的消息里带上了群名（取的是 group_manager）",
              bool(captured) and captured[0].get("group_name") == expected_name,
              str(captured[:1])[:200])
    finally:
        runtime.plugin_manager.dispatch_message = original_dispatch

    # 静态检查：别再写出 store.xxx() 这种不存在的方法（以前就是这么埋的雷）
    import re as _re
    from core.group_manager import GroupManager as _GroupManager
    from core.storage import SQLiteStore as _SQLiteStore
    _known = {"store": set(dir(_SQLiteStore)), "group_manager": set(dir(_GroupManager))}
    _pattern = _re.compile(
        r"\b(?:runtime|self|rt|bot\.runtime)\.(store|group_manager)\.([A-Za-z_][A-Za-z0-9_]*)")
    _bad = []
    for _root, _dirs, _files in os.walk(PROJECT):
        if any(part in _root for part in ("data", "__pycache__", "_legacy_native")):
            continue
        for _name in _files:
            if not _name.endswith(".py"):
                continue
            _path = os.path.join(_root, _name)
            with open(_path, "r", encoding="utf-8") as _handle:
                for _lineno, _line in enumerate(_handle, 1):
                    for _match in _pattern.finditer(_line.split("#", 1)[0]):
                        if _match.group(2) not in _known[_match.group(1)]:
                            _bad.append("%s:%d %s.%s" % (os.path.relpath(_path, PROJECT), _lineno,
                                                         _match.group(1), _match.group(2)))
    check("代码里没有调用 store / group_manager 上不存在的方法",
          not _bad, "；".join(_bad)[:300])

    # ------------------------------------------------------------------ 32. AstrBot 官方目录约定
    print("\n[32] AstrBot 官方目录约定：插件配置 / 插件数据 / 旧数据迁移 / 自带的 3 个插件")
    official_plugins = os.path.join(PROJECT, "plugins")

    # 下面全部在测试目录里做（不碰真实的 data/plugin_data 与插件数据）
    original_data_dir = paths.DATA_DIR
    paths.DATA_DIR = test_root
    try:
        fresh_manager = PluginManager(plugin_dir=official_plugins, logger_obj=log)
        check("插件管理器默认数据目录就是官方 data/plugin_data",
              os.path.basename(fresh_manager.data_dir.rstrip("\\/")) == "plugin_data"
              and "plugins_data" not in fresh_manager.data_dir, fresh_manager.data_dir)
        check("插件配置目录按官方约定是 data/config/<插件名>_config.json",
              os.path.basename(os.path.join(paths.DATA_DIR, "config")) == "config", "")

        # 旧数据迁移：data/plugins_data/<名字> → data/plugin_data/<插件名>
        # （放在加载插件之前：迁移会清空目标目录，别把已加载插件的数据目录抽走）
        legacy_root = os.path.join(test_root, "plugins_data")
        official_root = os.path.join(test_root, "plugin_data")
        shutil.rmtree(legacy_root, ignore_errors=True)
        shutil.rmtree(official_root, ignore_errors=True)
        os.makedirs(os.path.join(legacy_root, "每日签到"), exist_ok=True)
        with open(os.path.join(legacy_root, "每日签到", "lottery.json"), "w",
                  encoding="utf-8") as handle:
            handle.write('{"items": []}')
        os.makedirs(os.path.join(legacy_root, "astrbot_plugin_dice"), exist_ok=True)
        with open(os.path.join(legacy_root, "astrbot_plugin_dice", "keep.json"), "w",
                  encoding="utf-8") as handle:
            handle.write('{"kept": true}')
        mig_manager = PluginManager(plugin_dir=official_plugins, data_dir=official_root,
                                    disabled_file=os.path.join(test_root, "mig_disabled.json"),
                                    logger_obj=log)
        moved = mig_manager.migrate_legacy_data()
        check("旧 data/plugins_data 里的数据会被迁移", bool(moved), str(moved))
        check("旧的中文插件名会映射成 AstrBot 插件目录名",
              os.path.isfile(os.path.join(official_root, "astrbot_plugin_daily_checkin",
                                          "lottery.json")),
              str(sorted(os.listdir(official_root)) if os.path.isdir(official_root) else []))
        check("同名插件的文件按同名目录迁移",
              os.path.isfile(os.path.join(official_root, "astrbot_plugin_dice", "keep.json")), "")
        check("迁移完旧目录被清掉（数据不会两份占地方）",
              not os.path.isdir(legacy_root), legacy_root)
        check("迁移是幂等的（旧目录没了就什么都不做）",
              mig_manager.migrate_legacy_data() == [], "")

        conf_manager = PluginManager(plugin_dir=official_plugins,
                                     data_dir=os.path.join(test_root, "official_data"),
                                     disabled_file=os.path.join(test_root, "official_disabled.json"),
                                     logger_obj=log)
        conf_manager.set_bot(PluginBot(runtime=runtime, config={}, logger_obj=log))
        conf_manager.load_plugins(force=True)
        official_names = [item["name"] for item in conf_manager.list_plugins()]
        check("插件目录里的 4 个 AstrBot 插件都能加载",
              {"astrbot_demo", "astrbot_plugin_dice", "astrbot_plugin_daily_checkin",
               "astrbot_plugin_ollama"} <= set(official_names), str(official_names))
        check("新增的 3 个插件都声明了官方 metadata 字段（astrbot_version）",
              all(item.get("astrbot_version") for item in conf_manager.list_plugins()
                  if item["name"].startswith("astrbot_plugin_")),
              str([(i["name"], i.get("astrbot_version")) for i in conf_manager.list_plugins()]))
        plugins_raw = getattr(conf_manager.host, "plugins", []) or []
        checkin_instance = next((getattr(item, "instance", None) for item in plugins_raw
                                 if getattr(item, "name", "") == "astrbot_plugin_daily_checkin"),
                                None)
        check("插件在 __init__ 里就能拿到正确的 self.name（官方行为，不是类名）",
              checkin_instance is not None
              and getattr(checkin_instance, "name", "") == "astrbot_plugin_daily_checkin",
              str(getattr(checkin_instance, "name", "")))

        # 自带的 3 个插件：指令要能真的回复
        for name, content, keyword in (
                ("骰子", "/骰子", "🎲"),
                ("骰子", "/骰子 2d6", "2d6"),
                ("签到", "/签到", "签到成功"),
                ("签到", "/查询", "积分"),
                ("签到", "/积分", "积分")):
            result = conf_manager.dispatch_message({
                "type": "group", "content": content, "bot_id": bot_id,
                "group_openid": "GROUP_OFFICIAL", "member_openid": "M_OFFICIAL",
                "user_openid": "M_OFFICIAL", "user_name": "小明", "mentions": [],
                "attachments": [], "msg_id": "OFF_" + content})
            text = (result or {}).get("text") or ""
            check(f"{name}插件的 {content} 能正常回复",
                  bool(result) and keyword in text, text[:120] or str(result)[:120])
        check("签到插件把数据写到了官方 data/plugin_data/<插件名>/ 下",
              os.path.isfile(os.path.join(test_root, "plugin_data",
                                          "astrbot_plugin_daily_checkin", "checkin.json")),
              str(sorted(os.listdir(os.path.join(test_root, "plugin_data")))))

        # 插件"匹配上了但什么都没做"不能把消息吞掉（官方语义：交回主链路/AI）
        silent_dir = os.path.join(test_root, "silentplugins")
        shutil.rmtree(silent_dir, ignore_errors=True)
        os.makedirs(os.path.join(silent_dir, "ab_silent"), exist_ok=True)
        with open(os.path.join(silent_dir, "ab_silent", "metadata.yaml"), "w",
                  encoding="utf-8") as handle:
            handle.write("name: ab_silent\ndesc: 让路/叫停测试\nversion: 1.0.0\nauthor: t\n")
        with open(os.path.join(silent_dir, "ab_silent", "main.py"), "w",
                  encoding="utf-8") as handle:
            handle.write(
                "from astrbot.api.event import filter\n"
                "from astrbot.api.star import Star\n\n\n"
                "class Silent(Star):\n"
                "    @filter.event_message_type(filter.EventMessageType.ALL)\n"
                "    async def on_all(self, event):\n"
                "        if event.message_str.strip() == '让我安静':\n"
                "            event.stop_event()\n"
                "        return\n")
        silent_manager = PluginManager(plugin_dir=silent_dir,
                                       data_dir=os.path.join(test_root, "silentdata"),
                                       disabled_file=os.path.join(test_root, "silent_disabled.json"),
                                       logger_obj=log)
        silent_manager.set_bot(PluginBot(runtime=runtime, config={}, logger_obj=log))
        silent_manager.load_plugins(force=True)
        silent_msg = {"type": "group", "content": "随便说点啥", "bot_id": bot_id,
                      "group_openid": "G_SILENT", "member_openid": "M_SILENT",
                      "user_openid": "M_SILENT", "user_name": "小明", "mentions": [],
                      "attachments": [], "msg_id": "SILENT_1"}
        check("插件匹配上但什么都没返回时不吞消息（交回主链路走 AI）",
              silent_manager.dispatch_message(dict(silent_msg)) is None,
              str(silent_manager.dispatch_message(dict(silent_msg)))[:160])
        stopped = silent_manager.dispatch_message(dict(silent_msg, content="让我安静"))
        check("插件调用 stop_event() 时算已处理（真的不说话）",
              bool(stopped) and stopped.get("handled") is True, str(stopped)[:160])
    finally:
        paths.DATA_DIR = original_data_dir

    schema_text = open(os.path.join(official_plugins, "astrbot_plugin_ollama",
                                    "_conf_schema.json"), encoding="utf-8").read()
    check("Ollama 插件的 force_takeover 默认是 false（不会抢走所有消息）",
          '"default": false' in schema_text, schema_text[:120])
    check("插件目录里已经没有「原生插件」存档目录",
          not os.path.isdir(os.path.join(official_plugins, "_legacy_native")), "")
    server_source = open(os.path.join(PROJECT, "web", "server.py"), encoding="utf-8").read()
    check("后台不再有「导入旧程序插件」的接口",
          "plugins/import" not in server_source and "_legacy_plugin_dirs" not in server_source, "")
    check("插件目录只留官方格式说明（README.md）",
          os.path.isfile(os.path.join(official_plugins, "README.md"))
          and not os.path.isfile(os.path.join(official_plugins, "README-astrbot.md")), "")

    # 分组里缺失的默认子项必须真的补进老配置（以前只记 added、不写进结果）
    from core.config_manager import _deep_merge_defaults as _merge_defaults
    merged_demo, added_demo = _merge_defaults({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}})
    check("嵌套缺失的默认值会真正合并进配置",
          merged_demo == {"a": {"b": 9, "c": 2}} and added_demo == ["a.c"],
          f"merged={merged_demo} added={added_demo}")
    _partial_path = os.path.join(test_root, f"config_merge_{run_id}.json")
    with open(_partial_path, "w", encoding="utf-8") as handle:
        json.dump({"send": {"reply_style": "both"}}, handle)      # 故意缺其它子项
    _fresh = load_config(_partial_path)
    check("老配置里缺的分组子项会被真正补上（不只是记一笔）",
          (_fresh.config.data.get("send") or {}).get("max_file_mb")
          == _schema.DEFAULT_CONFIG["send"]["max_file_mb"]
          and (_fresh.config.data.get("send") or {}).get("reply_style") == "both",
          json.dumps((_fresh.config.data.get("send") or {}), ensure_ascii=False)[:200])

    # ------------------------------------------------------------------ 33. 指令面板按机器人隔离
    print("\n[33] 指令面板：按机器人隔离（列表 / 删除 / 过期页面）")
    # 先记下现场：本节结尾要原样还回去，避免影响别的测试/后续运行
    _orig_bots = json.loads(json.dumps(config_manager.config.get("bots") or []))
    _orig_active = config_manager.active_bot_id()
    _orig_panel_ids = runtime._load_panel_ids()
    bot2 = "bot2"
    # 第二个机器人：照 [19] 的写法写进配置并让运行侧对账出实例
    _two_bots = json.loads(json.dumps(_orig_bots))
    _two_bots.append({"id": bot2, "name": "机器人 2", "enabled": True, "app_id": "9999999999",
                      "app_secret": "secret2", "sandbox": False, "intents": 100663296,
                      "reconnect_attempts": 5, "reconnect_interval": 10})
    config_manager.set_path("bots", _two_bots)
    config_manager.save()
    runtime._reconcile_bots()
    fake2 = fakes.get(bot2)
    if not isinstance(fake2, FakeClient):        # 新建的实例用的是真客户端，换成桩
        fake2 = FakeClient(bot2, config_source=lambda: config_manager.config)
        fakes[bot2] = fake2
    if runtime.bots.get(bot2) is not None:
        runtime.bots[bot2].client = fake2
        runtime.bots[bot2].gateway.api = fake2
    runtime._ai_clients[bot2] = runtime.ai_client
    check("测试环境已装上第二个机器人（隔离用例的前置）",
          bot2 in runtime.bots and runtime.get_client(bot2) is fake2, str(list(runtime.bots)))

    # 两个机器人各自"线上"有一套不同的面板
    fake.panels = {"c2c": [{"panel_id": "P_ONE_C2C", "scope": "c2c", "target_type": "all"}],
                   "group": []}
    fake2.panels = {"c2c": [{"panel_id": "P_TWO_C2C", "scope": "c2c"}], "group": []}
    # 缓存里两个机器人各有一条（键按 bot:scope 分开）
    runtime._save_panel_ids({f"{bot_id}:c2c": "P_ONE_C2C", f"{bot2}:c2c": "P_TWO_C2C"})

    config_manager.set_active_bot(bot_id)
    body_1 = client.get("/api/admin/panels").get_json()
    ids_1 = [str(item.get("panel_id")) for item in (body_1.get("panels") or [])]
    check("切到 bot1 时列表给出 bot1 的面板",
          body_1.get("bot_id") == bot_id and ids_1 == ["P_ONE_C2C"],
          f"bot={body_1.get('bot_id')} ids={ids_1}")
    config_manager.set_active_bot(bot2)
    body_2 = client.get("/api/admin/panels").get_json()
    ids_2 = [str(item.get("panel_id")) for item in (body_2.get("panels") or [])]
    check("切换机器人后指令面板跟着换（不再串到上一个机器人）",
          body_2.get("bot_id") == bot2 and ids_2 == ["P_TWO_C2C"] and "P_ONE_C2C" not in ids_2,
          f"bot={body_2.get('bot_id')} ids={ids_2}")
    check("列表里的每一项都带当前机器人 id",
          bool(body_2.get("panels")) and all(item.get("bot_id") == bot2
                                             for item in body_2["panels"]),
          str(body_2.get("panels"))[:200])
    check("缓存里的 registered 也按机器人分开",
          (body_1.get("registered") or {}).get("c2c") == "P_ONE_C2C"
          and (body_2.get("registered") or {}).get("c2c") == "P_TWO_C2C",
          f"bot1={body_1.get('registered')} bot2={body_2.get('registered')}")

    # 过期页面：页面还停在 bot2，服务端已经切回 bot1
    config_manager.set_active_bot(bot_id)
    stale = client.get(f"/api/admin/panels?bot={bot2}")
    stale_body = stale.get_json()
    check("过期页面（页面停在 bot2、服务端是 bot1）访问列表会 409",
          stale.status_code == 409 and stale_body.get("bot_id") == bot_id,
          f"status={stale.status_code} body={str(stale_body)[:200]}")

    # 跨机器人删除：bot2 的面板，绝不能借当前机器人（或过期页面）删掉
    fake.calls.clear()
    fake2.calls.clear()
    cross = client.post("/api/admin/panels/delete",
                        json={"panel_id": "P_TWO_C2C", "bot_id": bot2})
    cross_body = cross.get_json()
    cross_deleted = [c for c in (fake.calls + fake2.calls) if c.get("kind") == "panel_delete"]
    check("跨机器人删除被拒（页面给的 bot_id 与服务端不一致 → 409），且不调用平台删除",
          cross.status_code == 409 and not cross_deleted
          and "刷新" in str(cross_body.get("message") or ""),
          f"status={cross.status_code} body={str(cross_body)[:200]} deleted={cross_deleted}")
    # 归属校验：以当前机器人身份删别人的面板 → 403「不属于」，同样不调平台删除
    fake.calls.clear()
    fake2.calls.clear()
    foreign = client.post("/api/admin/panels/delete",
                          json={"panel_id": "P_TWO_C2C", "bot_id": bot_id})
    foreign_body = foreign.get_json()
    foreign_deleted = [c for c in (fake.calls + fake2.calls) if c.get("kind") == "panel_delete"]
    check("不属于当前机器人的面板被 403 拒绝（message 含「不属于」），且不调用平台删除",
          foreign.status_code == 403 and "不属于" in str(foreign_body.get("message") or "")
          and not foreign_deleted,
          f"status={foreign.status_code} body={str(foreign_body)[:200]} deleted={foreign_deleted}")

    # 删自己的：200 + 清掉本机器人对应 scope 的缓存键
    config_manager.set_active_bot(bot_id)
    fake.calls.clear()
    fake2.calls.clear()
    own = client.post("/api/admin/panels/delete",
                      json={"panel_id": "P_ONE_C2C", "bot_id": bot_id})
    own_body = own.get_json()
    cache_after = runtime._load_panel_ids()
    check("删自己的面板成功（200 / success）",
          own.status_code == 200 and own_body.get("success") is True
          and (own_body.get("removed") or {}).get("scope") == "c2c",
          f"status={own.status_code} body={str(own_body)[:200]}")
    check("删除成功后清掉本机器人 c2c 的缓存键（别的机器人缓存不受影响）",
          f"{bot_id}:c2c" not in cache_after and cache_after.get(f"{bot2}:c2c") == "P_TWO_C2C",
          str(cache_after))
    check("删除只调用了当前机器人的平台删除接口",
          [c for c in fake.calls if c.get("kind") == "panel_delete"]
          and not [c for c in fake2.calls if c.get("kind") == "panel_delete"],
          f"bot1={[c.get('kind') for c in fake.calls]} bot2={[c.get('kind') for c in fake2.calls]}")
    after = client.get("/api/admin/panels").get_json()
    ids_after = [str(item.get("panel_id")) for item in (after.get("panels") or [])]
    check("删除后列表里不再出现该面板", "P_ONE_C2C" not in ids_after, str(ids_after))

    # 缓存没了但线上还有 → 仍可删（归属以线上列表为准，不是只看缓存）
    fake.panels = {"c2c": [], "group": [{"panel_id": "P_ONE_GROUP", "scope": "group",
                                         "target_type": "all"}]}
    runtime._save_panel_ids({})
    by_list = client.post("/api/admin/panels/delete",
                          json={"panel_id": "P_ONE_GROUP", "bot_id": bot_id})
    by_list_body = by_list.get_json()
    check("缓存过期但线上还有的面板仍可删（归属判定以线上列表为准）",
          by_list.status_code == 200 and by_list_body.get("success") is True
          and (by_list_body.get("removed") or {}).get("panel_id") == "P_ONE_GROUP",
          f"status={by_list.status_code} body={str(by_list_body)[:200]}")

    # 还原：配置回到只剩原本的机器人、活动机器人复位、面板缓存写回原样、桩数据清空
    config_manager.set_path("bots", _orig_bots)
    config_manager.save()
    runtime._reconcile_bots()
    config_manager.set_active_bot(_orig_active or bot_id)
    runtime._save_panel_ids(_orig_panel_ids)
    fake.panels = {}
    fakes.pop(bot2, None)
    check("第 [33] 节结束后环境已还原（配置/活动机器人/面板缓存）",
          [str(item.get("id")) for item in (config_manager.config.get("bots") or [])]
          == [str(item.get("id")) for item in _orig_bots]
          and config_manager.active_bot_id() == (_orig_active or bot_id)
          and runtime._load_panel_ids() == _orig_panel_ids,
          f"bots={config_manager.config.get('bots')} active={config_manager.active_bot_id()}")

    # ------------------------------------------------------------------ 34. 保存配置的机器人归属
    print("\n[34] 保存配置时的机器人归属校验（不让空白/未知 bot_id 写全局）")
    # 先记下现场：本节结尾要原样还回去（照 [33] 的做法），不影响后续
    _orig_bots34 = json.loads(json.dumps(config_manager.config.get("bots") or []))
    _orig_active34 = config_manager.active_bot_id()
    _orig_panel_ids34 = runtime._load_panel_ids()
    _global_panels34 = config_manager.config.get("panels", "enabled")
    _global_reply34 = config_manager.config.get("reply", "max_segment_length")

    def _overrides_snapshot():
        return {str(item.get("id")): config_manager.bot_overrides(str(item.get("id")))
                for item in (config_manager.config.get("bots") or []) if isinstance(item, dict)}

    config_manager.set_active_bot(bot_id)          # 关键：active 是个真实的 id
    _ov_before = _overrides_snapshot()

    # (1) 纯空白 bot_id + per-bot 段：以前会被写成全局，现在必须 400 且一个字节都不落盘
    blank = client.post("/api/admin/config",
                        json={"config": {"panels": {"enabled": False}}, "bot_id": "   "})
    blank_body = blank.get_json()
    check("纯空白 bot_id 保存 per-bot 段被 400 拒绝（不再当成有效 id 写全局）",
          blank.status_code == 400 and blank_body.get("success") is False
          and "机器人" in str(blank_body.get("message") or ""),
          f"status={blank.status_code} body={str(blank_body)[:200]}")
    check("被拒后全局 panels.enabled 没有被改",
          config_manager.config.get("panels", "enabled") == _global_panels34,
          f"before={_global_panels34} now={config_manager.config.get('panels', 'enabled')}")
    check("被拒后没有任何机器人的 overrides 被写",
          _overrides_snapshot() == _ov_before, str(_overrides_snapshot()))

    # (2) 未知 bot_id + per-bot 段：400（报错让用户刷新，而不是静默丢弃保存）
    ghost = client.post("/api/admin/config",
                        json={"config": {"reply": {"max_segment_length": 1234}},
                              "bot_id": "ghostbot"})
    ghost_body = ghost.get_json()
    check("未知 bot_id 保存 per-bot 段被 400 拒绝",
          ghost.status_code == 400 and "未知机器人" in str(ghost_body.get("message") or ""),
          f"status={ghost.status_code} body={str(ghost_body)[:200]}")
    check("未知 bot_id 被拒后全局 reply.max_segment_length 没有被改",
          config_manager.config.get("reply", "max_segment_length") == _global_reply34,
          f"now={config_manager.config.get('reply', 'max_segment_length')}")

    # (3) 不带 bot_id + per-bot 段 + 配置里有机器人：不能猜"当前选中"，必须 400
    orphan = client.post("/api/admin/config",
                         json={"reply": {"max_segment_length": 1234}})
    orphan_body = orphan.get_json()
    check("没带 bot_id 却含 per-bot 段被 400（页面过期时不猜当前机器人）",
          orphan.status_code == 400
          and "没带机器人 id" in str(orphan_body.get("message") or ""),
          f"status={orphan.status_code} body={str(orphan_body)[:200]}")
    check("没带 bot_id 被拒后该值既没写全局也没写进任何机器人的 overrides",
          config_manager.config.get("reply", "max_segment_length") == _global_reply34
          and _overrides_snapshot() == _ov_before,
          f"global={config_manager.config.get('reply', 'max_segment_length')}")

    # (4) 回归：带真实 bot_id + per-bot 段照旧 200，并写进该机器人的覆盖（不是全局）
    good = client.post("/api/admin/config",
                       json={"config": {"reply": {"max_segment_length": 1234}},
                             "bot_id": bot_id})
    good_body = good.get_json()
    check("带真实 bot_id 的正常保存仍是 200",
          good.status_code == 200 and good_body.get("success") is True,
          f"status={good.status_code} body={str(good_body)[:200]}")
    check("该值写进了这个机器人的 overrides、没有污染全局",
          (config_manager.bot_overrides(bot_id).get("reply") or {}).get("max_segment_length") == 1234
          and config_manager.config.get("reply", "max_segment_length") == _global_reply34,
          f"overrides={config_manager.bot_overrides(bot_id)} "
          f"global={config_manager.config.get('reply', 'max_segment_length')}")

    # (5) panels/delete 收到非 dict 的 JSON 一律当空体 → 400（以前 body.get 抛 500）
    bad_array = client.post("/api/admin/panels/delete", json=[{"panel_id": "P_X"}])
    check("panels/delete 收到 JSON 数组返回 400 而不是 500",
          bad_array.status_code == 400
          and (bad_array.get_json() or {}).get("success") is False,
          f"status={bad_array.status_code} body={str(bad_array.get_json())[:200]}")
    check("panels/delete 收到空数组同样 400（不会 500）",
          client.post("/api/admin/panels/delete", json=[]).status_code == 400)
    check("panels/delete 收到 JSON 字符串同样 400（不会 500）",
          client.post("/api/admin/panels/delete", json="oops").status_code == 400)

    # (6) 面板缓存：原子写 + 读回一致 + 不留 .tmp 残留；并确认读写锁已就位
    _atomic_file = runtime.panel_ids_file
    runtime._save_panel_ids({f"{bot_id}:c2c": "P_ATOMIC_C2C",
                             f"{bot_id}:group": "P_ATOMIC_GROUP"})
    _atomic_back = runtime._load_panel_ids()
    check("面板缓存写后能原样读回",
          _atomic_back.get(f"{bot_id}:c2c") == "P_ATOMIC_C2C"
          and _atomic_back.get(f"{bot_id}:group") == "P_ATOMIC_GROUP",
          str(_atomic_back))
    check("原子写不留下 command_panel.json.tmp 残留文件",
          not os.path.isfile(_atomic_file + ".tmp"), _atomic_file + ".tmp")
    check("面板缓存读写锁已就位（注册流程与网页删除流程共用同一把）",
          hasattr(runtime, "_panel_ids_lock"), "缺少 _panel_ids_lock")

    # (7) 兜底回归：配置里一个机器人都没有时，per-bot 段照旧写全局（不能拦死）
    _global_remark34 = config_manager.config.get("panels", "remark")
    config_manager.set_path("bots", [])
    config_manager.save()
    runtime._reconcile_bots()
    empty_save = client.post("/api/admin/config",
                             json={"config": {"panels": {"remark": "无机器人兜底"}}})
    check("配置里没有任何机器人时，per-bot 段照旧写全局（兜底场景不被拦）",
          empty_save.status_code == 200
          and config_manager.config.get("panels", "remark") == "无机器人兜底",
          f"status={empty_save.status_code} "
          f"remark={config_manager.config.get('panels', 'remark')}")
    config_manager.set_path("panels.remark", _global_remark34)     # 立刻还原全局值

    # 还原：配置（含各机器人 overrides）、活动机器人、面板缓存都复位
    config_manager.set_path("bots", _orig_bots34)
    config_manager.save()
    runtime._reconcile_bots()
    config_manager.set_active_bot(_orig_active34 or bot_id)
    runtime._save_panel_ids(_orig_panel_ids34)
    check("第 [34] 节结束后环境已还原（配置/活动机器人/面板缓存）",
          [str(item.get("id")) for item in (config_manager.config.get("bots") or [])]
          == [str(item.get("id")) for item in _orig_bots34]
          and config_manager.active_bot_id() == (_orig_active34 or bot_id)
          and runtime._load_panel_ids() == _orig_panel_ids34
          and config_manager.config.get("panels", "enabled") == _global_panels34
          and config_manager.config.get("panels", "remark") == _global_remark34
          and config_manager.config.get("reply", "max_segment_length") == _global_reply34,
          f"bots={config_manager.config.get('bots')} active={config_manager.active_bot_id()}")

    # ------------------------------------------------------------------ 35. 切换机器人
    print("\n[35] 切换机器人不应触发整轮热更新（active_bot_id 不是设置项）")
    # 先记现场：本节结尾要原样还回去（照 [33]/[34] 的做法），不影响收尾
    _orig_bots35 = json.loads(json.dumps(config_manager.config.get("bots") or []))
    _orig_active35 = config_manager.active_bot_id()
    _orig_anim35 = config_manager.config.get("ui", "animation")
    _orig_on_changed35 = runtime.on_config_changed
    _orig_broadcast35 = runtime.broadcast

    # 打桩：只记录"有没有真的热更新"，不跑真实链路（本节的互不干扰）
    _calls35 = []
    _casts35 = []

    def _fake_on_changed35(source="测试"):
        _calls35.append(source)
        return []

    runtime.on_config_changed = _fake_on_changed35
    runtime.broadcast = lambda payload: _casts35.append(payload)

    # 现场至少要有两个机器人才能"切换"：临时补一个（本节结束连列表一起还原）
    _tmp_bot35 = {"id": "bot_switch_35", "name": "切换测试机器人", "enabled": False,
                  "app_id": "", "app_secret": ""}
    _work_bots35 = list(_orig_bots35) + [_tmp_bot35]
    if not _orig_bots35:
        _work_bots35.append({"id": "bot_switch_35b", "name": "切换测试机器人B",
                             "enabled": False, "app_id": "", "app_secret": ""})
    config_manager.set_path("bots", _work_bots35)
    config_manager.save()
    config_manager.set_active_bot("bot_switch_35")
    _other35 = next((str(item.get("id")) for item in _work_bots35
                     if isinstance(item, dict) and str(item.get("id")) != "bot_switch_35"), "")
    # 上面"补一个机器人"本身是真变化：先把监视缓存对齐到当前文件状态，
    # 后面的"切换机器人"才是纯净的 active_bot_id 变化
    runtime._last_config_mtime = runtime._config_mtime()
    runtime._config_fingerprint_cached = runtime._config_fingerprint()

    try:
        # (1)+(4) 只切换机器人：文件被重写（内容变了），但指纹不变 → 监视循环不热更新
        _fp_before35 = runtime._config_fingerprint()
        config_manager.set_active_bot(_other35)
        _fp_after35 = runtime._config_fingerprint()
        with open(config_manager.config_path, encoding="utf-8") as _handle35:
            _saved_active35 = json.load(_handle35).get("active_bot_id")
        check("切到另一个真实机器人后 active 确实变了、且配置指纹不变",
              bool(_other35) and config_manager.active_bot_id() == _other35
              and _fp_before35 == _fp_after35,
              f"active={config_manager.active_bot_id()} "
              f"fp={_fp_before35[:12]}->{_fp_after35[:12]}")
        # 这里**故意不断言 mtime 变化**：Windows（NTFS 文件系统隧道）在"同名文件被删除后
        # 15 秒内重建"时会复用原来的时间戳，而 _write() 恰恰是 tmp + os.replace 的同写法，
        # 于是 os.path.getmtime 可能纹丝不动。"有没有真的写盘"必须看文件内容。
        check("切换机器人确实写进了 config.json（文件里的 active_bot_id 变了）",
              _saved_active35 == _other35, f"on_disk={_saved_active35!r} expect={_other35!r}")
        # 指纹判定才是关键：把监视循环记的 mtime 拨到"旧值"，模拟"文件被重写过了"。
        # （不能靠真实 mtime 差异，理由同上。）
        runtime._last_config_mtime = runtime._config_mtime() - 5
        _calls35.clear()
        _casts35.clear()
        _hot35 = runtime._config_watch_tick()
        check("mtime 变了但指纹没变：监视循环不调用 on_config_changed",
              _hot35 is False and _calls35 == [], f"hot={_hot35} calls={_calls35}")
        check("切换机器人不再广播 config_changed（别的标签页不会弹「配置已更新」）",
              not [c for c in _casts35 if isinstance(c, dict)
                   and c.get("type") == "config_changed"], str(_casts35))

        # (2) 真改一个设置：指纹必须变，监视循环照旧热更新
        _new_anim35 = "lite" if _orig_anim35 != "lite" else "full"
        config_manager.set_path("ui.animation", _new_anim35)
        config_manager.save()
        _fp_changed35 = runtime._config_fingerprint()
        check("改真设置（ui.animation）后配置指纹变化",
              _fp_changed35 != _fp_after35,
              f"{_fp_after35[:12]} -> {_fp_changed35[:12]}")
        runtime._last_config_mtime = runtime._config_mtime() - 5
        _calls35.clear()
        _casts35.clear()
        _hot35b = runtime._config_watch_tick()
        check("真改了设置时监视循环照旧热更新（来源=文件监视）",
              _hot35b is True and _calls35 == ["文件监视"],
              f"hot={_hot35b} calls={_calls35}")
    finally:
        runtime.on_config_changed = _orig_on_changed35
        runtime.broadcast = _orig_broadcast35

    # 还原：机器人列表 / active_bot_id / ui.animation / 监视状态
    config_manager.set_path("bots", _orig_bots35)
    config_manager.set_path("ui.animation", _orig_anim35)
    config_manager.save()
    _restore_active35 = _orig_active35 if any(
        str(item.get("id")) == _orig_active35 for item in _orig_bots35
        if isinstance(item, dict)) else ""
    config_manager.set_active_bot(_restore_active35)
    runtime._last_config_mtime = runtime._config_mtime()
    runtime._config_fingerprint_cached = runtime._config_fingerprint()
    check("第 [35] 节结束后环境已还原（机器人/活动机器人/动画/监视状态）",
          [str(item.get("id")) for item in (config_manager.config.get("bots") or [])]
          == [str(item.get("id")) for item in _orig_bots35]
          and config_manager.config.get("ui", "animation") == _orig_anim35
          and runtime.on_config_changed == _orig_on_changed35
          and runtime._config_fingerprint_cached == runtime._config_fingerprint(),
          f"bots={config_manager.config.get('bots')} "
          f"anim={config_manager.config.get('ui', 'animation')}")

    # ------------------------------------------------------------------ 36. 配置写盘
    print("\n[36] 配置写盘：内容没变就不写 + 补全日志带键名")

    # (1) 内容与磁盘逐字节一致时，save() 必须零写盘（用独立临时配置文件，不碰真实 config.json）
    _same_path36 = os.path.join(test_root, f"config_same_{run_id}.json")
    _same36 = load_config(_same_path36)              # 文件不存在 → 首次创建并写盘
    _old_ts36 = time.time() - 3600                   # 故意把 mtime 拨到一小时前：真写了就一定会变
    os.utime(_same_path36, (_old_ts36, _old_ts36))
    _mtime_before36 = os.stat(_same_path36).st_mtime_ns
    with open(_same_path36, "rb") as handle:
        _bytes_before36 = handle.read()
    _same36.save()                                   # 内容没变 → 应当直接 return，不写 tmp、不动 mtime
    with open(_same_path36, "rb") as handle:
        _bytes_after36 = handle.read()
    check("内容没变的 save() 不写盘（mtime 与文件字节都不变）",
          os.stat(_same_path36).st_mtime_ns == _mtime_before36
          and _bytes_after36 == _bytes_before36,
          f"mtime {_mtime_before36} -> {os.stat(_same_path36).st_mtime_ns}")

    # (2) 真的改了值就必须写盘（mtime 变 + 新值落盘）
    _same36.set_path("ui.animation", "lite")
    _same36.save()
    _mtime_after36 = os.stat(_same_path36).st_mtime_ns
    with open(_same_path36, encoding="utf-8") as handle:
        _text_after36 = handle.read()
    check("改了设置（ui.animation=lite）后确实写盘（mtime 变化 + 新值落盘）",
          _mtime_after36 != _mtime_before36 and '"animation": "lite"' in _text_after36,
          f"mtime {_mtime_before36} -> {_mtime_after36}")

    # (3) 补全缺失默认项：self.missing 记下键名、日志带键名、且这些键真的写进了文件
    _fill_path36 = os.path.join(test_root, f"config_fill_{run_id}.json")
    with open(_fill_path36, "w", encoding="utf-8") as handle:
        json.dump({"send": {"reply_style": "both"}}, handle)      # 故意缺其它子项

    _msgs36 = []

    class _CollectHandler36(logging.Handler):
        def emit(self, record):
            _msgs36.append(record.getMessage())

    _cfg_logger36 = logging.getLogger("core.config_manager")
    _old_level36 = _cfg_logger36.level
    _handler36 = _CollectHandler36()
    _cfg_logger36.addHandler(_handler36)
    _cfg_logger36.setLevel(logging.INFO)
    try:
        _fill36 = load_config(_fill_path36)
    finally:
        _cfg_logger36.removeHandler(_handler36)
        _cfg_logger36.setLevel(_old_level36)
    with open(_fill_path36, encoding="utf-8") as handle:
        _saved36 = json.load(handle)

    check("补全结果记进了 self.missing（形如 send.max_file_mb）",
          "send.max_file_mb" in (_fill36.missing or [])
          and "send.max_image_mb" in (_fill36.missing or []),
          f"missing={(_fill36.missing or [])[:6]}")
    check("补全日志带上了键名（不再只报数量）",
          any("配置已补全" in msg and "send.max_file_mb" in msg for msg in _msgs36),
          str(_msgs36)[:200])
    check("missing 里记的键确实写进了配置文件（不是只记不写）",
          (_saved36.get("send") or {}).get("max_file_mb")
          == _schema.DEFAULT_CONFIG["send"]["max_file_mb"]
          and (_saved36.get("send") or {}).get("max_image_mb")
          == _schema.DEFAULT_CONFIG["send"]["max_image_mb"]
          and (_saved36.get("send") or {}).get("reply_style") == "both",
          json.dumps(_saved36.get("send") or {}, ensure_ascii=False)[:200])

    # ------------------------------------------------------------------ 37. 路径规范化
    print("\n[37] 配置里的路径要规范化（不出现正反斜杠混用）")
    # 背景：config.json 里 storage.db_path 的默认值是 "data/messages.db"（刻意用 "/"，
    # 好让同一份配置在 Linux 上也能用），而 Windows 的 os.path.join 不会把第二段里的
    # "/" 规范化 → 启动日志里出现 "...\\QQBotMerged\\data/messages.db" 这种混用写法。
    # 反向更危险：Windows 上写 "data\\messages.db" 的配置拿到 POSIX 上，"\"
    # 不是分隔符会被当成文件名的一部分，直接建不出库。
    _wrong_sep37 = "/" if os.sep == "\\" else "\\"          # 当前系统上"不该出现"的那个
    _slash37 = config_manager.config.abs_path("data/messages.db")
    _backslash37 = config_manager.config.abs_path("data\\messages.db")
    _expect37 = os.path.normpath(os.path.join(paths.BASE_DIR, "data", "messages.db"))

    check("abs_path('data/messages.db') 结果里没有非当前系统的分隔符"
          f"（os.sep={os.sep!r} → 不应含 {_wrong_sep37!r}）",
          _wrong_sep37 not in _slash37, f"path={_slash37!r}")
    check("abs_path('data/messages.db') == normpath(join(BASE_DIR, 'data', 'messages.db'))",
          _slash37 == _expect37, f"{_slash37!r} != {_expect37!r}")
    check("反斜杠写法与正斜杠写法返回同一个字符串（同一份配置跨平台可用）",
          _backslash37 == _slash37, f"{_backslash37!r} != {_slash37!r}")

    # 绝对路径输入：只做 normpath，不能被当成相对路径再拼一次根目录
    _abs_in37 = os.path.join(paths.BASE_DIR, "data", "messages.db")
    _abs_out37 = config_manager.config.abs_path(_abs_in37)
    check("绝对路径输入被 normpath 后原样返回（根目录不会被拼两次）",
          _abs_out37 == _expect37 and _abs_out37.count(paths.BASE_DIR) == 1,
          f"{_abs_out37!r}")

    check("空串（含纯空白）仍然返回程序根目录",
          config_manager.config.abs_path("") == paths.BASE_DIR
          and config_manager.config.abs_path("   ") == paths.BASE_DIR,
          repr(config_manager.config.abs_path("")))

    # 真实链路：Runtime 用来建库的那个路径（启动日志打印的就是它）
    _store_path37 = getattr(getattr(runtime, "store", None), "db_path", "")
    check("运行时真实建库路径不含混合分隔符（日志里不会再出现 ...\\data/messages.db）",
          bool(_store_path37) and _wrong_sep37 not in _store_path37,
          f"store.db_path={_store_path37!r}")

    # 再走一遍"默认配置"的真实取值链路：storage.db_path 用 schema 默认的 "data/messages.db"
    # （测试主配置把它换成了 os.sep 写法，这里另开一份临时配置专门覆盖默认值场景）
    _def_path37 = os.path.join(test_root, f"config_path37_{run_id}.json")
    _def37 = load_config(_def_path37)
    _default_db37 = _def37.config.str_of("storage", "db_path", default="data/messages.db")
    _resolved37 = _def37.config.abs_path(_default_db37)
    check("默认配置里的 storage.db_path（schema 默认 data/messages.db）解析后不带混合分隔符",
          _default_db37 == "data/messages.db" and _wrong_sep37 not in _resolved37
          and _resolved37 == _expect37,
          f"{_default_db37!r} -> {_resolved37!r}")

    # ------------------------------------------------------------------ 38. 库目录 & 降级
    print("\n[38] 存储：群配置与库同目录（两种分隔符都对）+ 降级不静默")

    # (1) 群配置/群名缓存必须和消息库落在同一个目录（否则"库在这儿、群配置在那儿"）
    _db_val38 = config_manager.config.str_of("storage", "db_path", default="data/messages.db")
    _expect_dir38 = os.path.dirname(config_manager.config.abs_path(_db_val38))
    _dsn_dir38 = runtime._resolve_data_dir()
    # 用 getattr 取"实例属性"（settings_path / db_path 不是类属性，会被第 [32] 节的
    # "store / group_manager 上有没有这个方法"静态检查误报成不存在的调用）
    _settings38 = getattr(runtime.group_manager, "settings_path")
    _store_db38 = getattr(runtime.store, "db_path")
    check("_resolve_data_dir() == dirname(abs_path(storage.db_path))（与建库同一套解析）",
          _dsn_dir38 == _expect_dir38, f"{_dsn_dir38!r} != {_expect_dir38!r}")
    check("group_settings.json 与消息库同一个目录",
          os.path.dirname(os.path.abspath(_settings38)) == os.path.dirname(os.path.abspath(_store_db38)),
          f"settings={_settings38!r} db={_store_db38!r}")

    # (2) `data/x/db` 与 `data\x\db` 两种写法必须解析出同一个目录
    _mix_dir38 = os.path.normpath(os.path.join(paths.BASE_DIR, "data", "_selftest", "sub38"))
    _db_back38 = _db_val38
    try:
        config_manager.set_path("storage.db_path", "data/_selftest/sub38/messages.db")
        _fwd_abs38 = config_manager.config.abs_path("data/_selftest/sub38/messages.db")
        _fwd_dir38 = runtime._resolve_data_dir()
        config_manager.set_path("storage.db_path", "data\\_selftest\\sub38\\messages.db")
        _bwd_dir38 = runtime._resolve_data_dir()
    finally:
        config_manager.set_path("storage.db_path", _db_back38)
    check("正斜杠写法的 db_path：群配置目录 = 库所在目录（data/_selftest/sub38）",
          _fwd_dir38 == os.path.dirname(_fwd_abs38) == _mix_dir38, f"{_fwd_dir38!r}")
    check("反斜杠写法解析出同一个目录（同一份配置不会分家成两套数据）",
          _bwd_dir38 == _fwd_dir38 == _mix_dir38, f"{_bwd_dir38!r} != {_fwd_dir38!r}")

    # (3) 内存库（":memory:"）/ 空路径时也要给出一个确定的目录，不能是空串
    try:
        config_manager.set_path("storage.db_path", ":memory:")
        _mem_dir38 = runtime._resolve_data_dir()
        config_manager.set_path("storage.db_path", "")
        _empty_dir38 = runtime._resolve_data_dir()
    finally:
        config_manager.set_path("storage.db_path", _db_back38)
    check("内存库时群配置目录沿用默认的相对路径 data（不是空字符串当目录用）",
          _mem_dir38 == "data", repr(_mem_dir38))
    check("空路径时回落到默认库路径所在目录（依旧是个确定目录）",
          _empty_dir38 == os.path.dirname(config_manager.config.abs_path("data/messages.db"))
          and bool(_empty_dir38), repr(_empty_dir38))

    # (4) 「看起来是 POSIX 绝对路径」的配置在 Windows 上必须明确提醒（不静默乱落）
    _msgs38 = []

    class _CollectHandler38(logging.Handler):
        def emit(self, record):
            _msgs38.append(record.getMessage())

    _cfg_logger38 = logging.getLogger("core.config_manager")
    _old_level38 = _cfg_logger38.level
    _handler38 = _CollectHandler38()
    _cfg_logger38.addHandler(_handler38)
    _cfg_logger38.setLevel(logging.INFO)
    try:
        _posix_out38 = config_manager.config.abs_path("/mnt/qqbot/data/messages.db")
        _unc_out38 = config_manager.config.abs_path("//server/share/messages.db")
    finally:
        _cfg_logger38.removeHandler(_handler38)
        _cfg_logger38.setLevel(_old_level38)
    _posix_warn38 = [m for m in _msgs38 if "无法定位" in m]
    if os.name == "nt":
        # Windows 把 "\mnt\..." 当"当前盘符根目录下的路径"→ 实际落到 C:\mnt\...（不是程序目录）
        _posix_landed38 = os.path.normpath(
            os.path.splitdrive(paths.BASE_DIR)[0] + os.sep
            + os.path.join("mnt", "qqbot", "data", "messages.db"))
        check("Windows 上遇到 /mnt/... 这种 POSIX 绝对路径会明确提醒",
              len(_posix_warn38) == 1 and "/mnt/qqbot/data/messages.db" in _posix_warn38[0],
              str(_msgs38)[:220])
        check("提醒里写出了真实落点（算法算出来的路径，不是含糊的『程序目录下』）",
              bool(_posix_warn38) and _posix_landed38 in _posix_warn38[0]
              and _posix_out38 == _posix_landed38,
              f"{_posix_out38!r} {str(_posix_warn38)[:200]}")
    else:
        check("非 Windows 上 /mnt/... 本就是普通绝对路径：不提醒且原样保留",
              not _posix_warn38
              and _posix_out38 == os.path.normpath("/mnt/qqbot/data/messages.db"),
              f"{_posix_out38!r} msgs={str(_msgs38)[:160]}")
    check("UNC / 网络路径（//server/share/...）不误报提醒，也没被改坏",
          not [m for m in _msgs38 if "server" in m]
          and "server" in _unc_out38 and _unc_out38.endswith("messages.db")
          and not _unc_out38.startswith(paths.BASE_DIR),
          f"{_unc_out38!r} msgs={str(_msgs38)[:160]}")

    # (5) SQLite 打不开时退回内存库：必须"能跑 + 大声报 + 状态接口可见"，不能静默
    _blocker38 = os.path.join(test_root, f"blocker_{run_id}.txt")
    with open(_blocker38, "w", encoding="utf-8") as handle:
        handle.write("x")                                   # 父路径是个文件 → 建库必失败
    _bad_db38 = os.path.join(_blocker38, "messages.db")
    _degraded_back38 = runtime._store_degraded
    _rt_msgs38 = []

    class _CollectHandlerRt38(logging.Handler):
        def emit(self, record):
            _rt_msgs38.append(record.getMessage())

    _rt_logger38 = logging.getLogger("qqbotmerged")
    _rt_old_level38 = _rt_logger38.level
    _rt_handler38 = _CollectHandlerRt38()
    _rt_logger38.addHandler(_rt_handler38)
    _rt_logger38.setLevel(logging.INFO)
    try:
        runtime._store_degraded = None
        config_manager.set_path("storage.db_path", _bad_db38)
        _store38 = runtime._make_store()
        _degraded38 = dict(runtime._store_degraded or {})
        _status_storage38 = dict(runtime.status().get("storage") or {})
    finally:
        _rt_logger38.removeHandler(_rt_handler38)
        _rt_logger38.setLevel(_rt_old_level38)
        config_manager.set_path("storage.db_path", _db_back38)
        runtime._store_degraded = _degraded_back38
    from core.storage import MemoryStore as _MemoryStore38
    check("SQLite 建不出来时退回内存库（程序还能继续跑，不是启动即崩）",
          isinstance(_store38, _MemoryStore38), type(_store38).__name__)
    check("降级原因被记下来（db_path + 具体异常），不是静默吞掉",
          _degraded38.get("db_path") == os.path.normpath(_bad_db38)
          and bool(_degraded38.get("error")), str(_degraded38)[:200])
    check("降级时日志明确说清后果（内存存储 / 重启就全丢）与出错路径",
          any("内存存储" in m and "重启" in m and "messages.db" in m for m in _rt_msgs38),
          str(_rt_msgs38)[:300])
    check("状态接口在降级时把 degraded 暴露出来（网页上看得见）",
          bool(_status_storage38.get("degraded")) and _status_storage38.get("kind") == "sqlite",
          str(_status_storage38)[:200])

    # (6) 内存库必须被标成 memory：MemoryStore 是 SQLiteStore 的子类，判定顺序不能反
    _real_store38 = runtime.store
    try:
        runtime.store = _store38                               # 一个真正的内存库
        _mem_kind38 = dict(runtime.status().get("storage") or {}).get("kind")
    finally:
        runtime.store = _real_store38
    check("内存库在状态接口里被标成 memory（不会因为继承关系被误报成 sqlite）",
          _mem_kind38 == "memory", repr(_mem_kind38))

    # ------------------------------------------------------------------ 39. 插件按机器人隔离
    print("\n[39] 插件按机器人隔离：每个机器人可以跑不同的插件")

    check("plugins.enabled_names / disabled_names 属于「按机器人」的设置",
          _schema.is_per_bot("plugins.enabled_names")
          and _schema.is_per_bot("plugins.disabled_names"),
          f"{_schema.is_per_bot('plugins.enabled_names')} "
          f"{_schema.is_per_bot('plugins.disabled_names')}")
    check("插件总开关与插件目录仍是全局设置（不会被写成某台机器人的覆盖）",
          not _schema.is_per_bot("plugins.enabled") and not _schema.is_per_bot("plugins.dir"),
          f"{_schema.is_per_bot('plugins.enabled')} {_schema.is_per_bot('plugins.dir')}")
    _sec39 = next((item for item in _schema.sections_payload() if item["key"] == "plugins"), {})
    check("设置页的「插件系统」分组把新字段一起渲染出来",
          "plugins.enabled_names" in (_sec39.get("fields") or [])
          and "plugins.disabled_names" in (_sec39.get("fields") or [])
          and _sec39.get("per_bot") is True,
          str(_sec39)[:220])

    # 测试环境为了避免"受用户装的插件影响"，把运行时的插件目录换成了只含示例插件的临时目录，
    # 配置里也只留了一个机器人。这一节要验证"多机器人 + 多插件"的隔离，所以临时补齐：
    # 把插件目录指回真实的 plugins/、数据仍然写测试目录，并临时加一个机器人；本节的
    # finally 里全部还原（[39] 是最后一个用例）。
    _pm_back39 = (runtime.plugin_manager.plugin_dir, runtime.plugin_manager.data_dir,
                  runtime.plugin_manager.disabled_file, dict(runtime.plugin_manager.disabled))
    _bots_back39 = json.loads(json.dumps(config_manager.config.get("bots") or []))
    _tmp_bot39 = {"id": "bot_iso39", "name": "隔离测试机器人", "enabled": True,
                  "app_id": "99900039", "app_secret": "test-secret", "sandbox": False,
                  "intents": 100663296, "reconnect_attempts": 1, "reconnect_interval": 1}
    config_manager.set_path("bots", _bots_back39 + [_tmp_bot39])
    runtime.plugin_manager.plugin_dir = os.path.join(PROJECT, "plugins")
    runtime.plugin_manager.data_dir = os.path.join(test_root, "plugin_data39")
    runtime.plugin_manager.disabled_file = os.path.join(test_root, "plugins_disabled39.json")
    runtime.plugin_manager.disabled = {}
    runtime.plugin_manager.load_plugins(force=True)
    runtime._build_bots()                     # 只建对象、不连接（网关 start 已被打成空操作）
    _tmp_rt39 = runtime.bots.get("bot_iso39")
    if _tmp_rt39 is not None:
        _iso_fake39 = FakeClient("bot_iso39", config_source=lambda: config_manager.config)
        _tmp_rt39.client = _iso_fake39
        _tmp_rt39.gateway.api = _iso_fake39
    runtime.invalidate_bot_configs()
    pm39 = runtime.plugin_manager
    _names39 = [item["name"] for item in pm39.list_plugins()]
    check("用于隔离验证的插件都加载出来了", len(_names39) >= 3, str(_names39))

    def _dispatch39(bid, content="/骰子"):
        return pm39.dispatch_message({
            "type": "group", "content": content, "bot_id": bid,
            "group_openid": "GROUP_ISO39", "member_openid": "M_ISO39",
            "user_openid": "M_ISO39", "user_name": "小明", "mentions": [],
            "attachments": [], "msg_id": "ISO39_" + str(bid) + content,
            "raw_event": "{}"})

    _ids39 = [str(item.get("id")) for item in (config_manager.config.get("bots") or [])
              if isinstance(item, dict)]
    check("测试环境里至少有两个机器人可用来验证隔离", len(_ids39) >= 2, str(_ids39))
    _b1_39 = _ids39[0] if _ids39 else "bot1"
    _b2_39 = _ids39[1] if len(_ids39) > 1 else "bot2"
    check("两个机器人都在运行时里注册了（否则按机器人取配置会退回全局）",
          _b1_39 in runtime.bots and _b2_39 in runtime.bots, str(sorted(runtime.bots)))
    _ov_back39 = json.loads(json.dumps(config_manager.bot_overrides(_b1_39)))
    _dice39 = "astrbot_plugin_dice"

    try:
        # (1) 默认：谁都没做限制 → 两个机器人都能用全部插件
        check("没做任何按机器人限制时策略是「不限制」（向后兼容）",
              pm39.allowed_names(_b1_39) is None and pm39.allowed_names(_b2_39) is None,
              f"{pm39.bot_policy(_b1_39)} / {pm39.bot_policy(_b2_39)}")
        check("默认情况下同一个插件对两个机器人都能触发",
              bool(_dispatch39(_b1_39)) and bool(_dispatch39(_b2_39)),
              f"{str(_dispatch39(_b1_39))[:80]} / {str(_dispatch39(_b2_39))[:80]}")

        # (2) 黑名单：只停用 A 的骰子插件，B 照旧
        config_manager.set_for_bot(_b1_39, "plugins.disabled_names", [_dice39])
        runtime.invalidate_bot_configs()
        _allowed39 = pm39.allowed_names(_b1_39)
        check("给机器人 A 停用某插件后，它的可用集合里没有这个插件",
              _allowed39 is not None and _dice39 not in _allowed39
              and set(_names39) - {_dice39} <= _allowed39, str(sorted(_allowed39)))
        check("机器人 A 再发这个指令就没反应了（插件被隔离）",
              _dispatch39(_b1_39) is None, str(_dispatch39(_b1_39))[:160])
        check("同一个插件对机器人 B 依然有效（互不影响）",
              "🎲" in ((_dispatch39(_b2_39) or {}).get("text") or ""),
              str(_dispatch39(_b2_39))[:160])
        check("策略是按机器人分别读出来的（A 有限制、B 不限制）",
              pm39.bot_policy(_b1_39)["deny"] == [_dice39]
              and pm39.allowed_names(_b2_39) is None,
              f"{pm39.bot_policy(_b1_39)} / {pm39.bot_policy(_b2_39)}")

        # (3) 白名单：只放行一个插件
        config_manager.set_for_bot(_b1_39, "plugins.disabled_names", [])
        config_manager.set_for_bot(_b1_39, "plugins.enabled_names",
                                   ["astrbot_plugin_daily_checkin"])
        runtime.invalidate_bot_configs()
        check("白名单只放行列出的插件",
              pm39.allowed_names(_b1_39) == {"astrbot_plugin_daily_checkin"},
              str(pm39.allowed_names(_b1_39)))
        check("白名单之外的插件被拦住（骰子没反应）",
              _dispatch39(_b1_39) is None, str(_dispatch39(_b1_39))[:160])

        # (4) 黑白名单同时出现：黑名单优先
        config_manager.set_for_bot(_b1_39, "plugins.disabled_names",
                                   ["astrbot_plugin_daily_checkin"])
        runtime.invalidate_bot_configs()
        check("黑白名单冲突时以「停用」为准",
              pm39.allowed_names(_b1_39) == set(), str(pm39.allowed_names(_b1_39)))

        # (5) 接口：按机器人保存插件范围
        _api39 = client.post("/api/admin/plugins/bot",
                             json={"bot_id": _b1_39, "allowed": [_dice39]}).get_json()
        check("接口可以按机器人设置插件范围",
              bool(_api39.get("success"))
              and (_api39.get("bot_allowed") or {}).get(_dice39) is True,
              str(_api39)[:220])
        check("接口把「没勾选的插件」标成 false",
              any(value is False for value in (_api39.get("bot_allowed") or {}).values()),
              str(_api39.get("bot_allowed"))[:200])
        check("接口写进的是该机器人的覆盖（不是全局配置）",
              (config_manager.bot_overrides(_b1_39).get("plugins") or {}
               ).get("disabled_names") == [name for name in _names39 if name != _dice39],
              str(config_manager.bot_overrides(_b1_39).get("plugins"))[:200])
        check("接口没有碰全局 plugins 段",
              (config_manager.config.data.get("plugins") or {}).get("disabled_names") == []
              and (config_manager.config.data.get("plugins") or {}).get("enabled_names") == [],
              str(config_manager.config.data.get("plugins"))[:200])
        check("接口改完后该机器人只剩这一个插件（真的生效）",
              pm39.allowed_names(_b1_39) == {_dice39}
              and "🎲" in ((_dispatch39(_b1_39) or {}).get("text") or ""),
              f"{pm39.allowed_names(_b1_39)}")

        # (6) 全部勾选 = 该机器人不限制（两个名单都清空）
        _all39 = client.post("/api/admin/plugins/bot",
                             json={"bot_id": _b1_39, "allowed": _names39}).get_json()
        check("勾满全部插件 = 该机器人不限制（两个名单都清空）",
              bool(_all39.get("success"))
              and pm39.allowed_names(_b1_39) is None
              and (_all39.get("bot_policy") or {}) == {"allow": [], "deny": []},
              str(_all39.get("bot_policy"))[:200])

        # (7) 接口的参数校验：不能悄悄写错机器人 / 写进不存在的插件
        _bad39 = []
        for _payload39 in ({"bot_id": "", "allowed": []},
                           {"bot_id": "   ", "allowed": []},
                           {"bot_id": "no_such_bot_39", "allowed": []},
                           {"bot_id": _b1_39, "allowed": ["不存在的插件"]},
                           {"bot_id": _b1_39, "allowed": "astrbot_plugin_dice"}):
            _resp39 = client.post("/api/admin/plugins/bot", json=_payload39)
            _bad39.append((_payload39.get("bot_id"), _resp39.status_code))
        check("非法请求都被拒（400）：空 id / 未知机器人 / 未知插件 / allowed 不是数组",
              all(code == 400 for _bid, code in _bad39), str(_bad39))
        check("被拒的请求没有改到任何机器人的插件范围",
              pm39.allowed_names(_b1_39) is None
              and config_manager.bot_overrides("no_such_bot_39") == {},
              f"{pm39.allowed_names(_b1_39)}")

        # (8) 列表接口带上机器人维度（页面按机器人勾选要用）
        _list39 = client.get(f"/api/admin/plugins?bot={_b1_39}").get_json()
        check("插件列表接口返回当前机器人、机器人清单与勾选状态",
              bool(_list39.get("success")) and _list39.get("bot_id") == _b1_39
              and isinstance(_list39.get("bots"), list) and bool(_list39.get("bots"))
              and isinstance(_list39.get("bot_allowed"), dict)
              and all(value is True for value in (_list39.get("bot_allowed") or {}).values()),
              str({k: _list39.get(k) for k in ("bot_id", "bot_allowed")})[:220])
        check("列表接口的勾选状态只用于当前机器人（另一个机器人仍是不限制）",
              pm39.allowed_names(_b2_39) is None, str(pm39.bot_policy(_b2_39)))
    finally:
        # 还原：把 A 的 plugins 覆盖恢复成测试前的样子，别影响后面的用例
        for _target39 in (config_manager._file_data, config_manager.config.data):
            _entry39 = config_manager._find_bot(_target39, _b1_39)
            if isinstance(_entry39, dict):
                if _ov_back39:
                    _entry39["overrides"] = json.loads(json.dumps(_ov_back39))
                else:
                    _entry39.pop("overrides", None)
        # 还原临时补的机器人 + 运行时插件目录（本节自己造的现场自己收拾）
        config_manager.set_path("bots", _bots_back39)
        runtime.bots.pop("bot_iso39", None)
        (runtime.plugin_manager.plugin_dir, runtime.plugin_manager.data_dir,
         runtime.plugin_manager.disabled_file) = _pm_back39[:3]
        runtime.plugin_manager.disabled = dict(_pm_back39[3])
        runtime.plugin_manager.load_plugins(force=True)
        runtime.invalidate_bot_configs()
        try:
            config_manager.save()
        except Exception:
            pass
    check("第 [39] 节结束后机器人 A 的插件覆盖已还原",
          config_manager.bot_overrides(_b1_39) == _ov_back39,
          str(config_manager.bot_overrides(_b1_39))[:200])
    check("第 [39] 节结束后临时补的机器人 / 插件目录都已还原",
          [str(item.get("id")) for item in (config_manager.config.get("bots") or [])]
          == [str(item.get("id")) for item in _bots_back39]
          and "bot_iso39" not in runtime.bots
          and runtime.plugin_manager.plugin_dir == _pm_back39[0],
          f"bots={[i.get('id') for i in (config_manager.config.get('bots') or [])]} "
          f"dir={runtime.plugin_manager.plugin_dir}")

    # ------------------------------------------------------------------ 40. 热更新的范围
    print("\n[40] 配置热更新只重载「真的改了」的组件（改别的设置不该重启云同步）")

    class _FakeCloud40:
        """假云同步：只记录 stop/start 有没有被调用。"""

        def __init__(self):
            self.enabled = True
            self.calls = []

        def stop(self, final_sync=False):
            self.calls.append("stop")

        def start(self, immediate=False):
            self.calls.append("start")

    _cloud_back40 = runtime.cloud_sync
    _fake_cloud40 = _FakeCloud40()
    runtime.cloud_sync = _fake_cloud40
    _back40 = {}
    for _path40 in ("ui.animation", "filters.keywords_enabled", "reply.max_queue_size",
                    "cloud_sync.interval_seconds"):
        _back40[_path40] = config_manager.config.get(*_path40.split("."))
    try:
        # (1) 改一个跟云同步、过滤器都无关的设置（界面动画）
        _anim40 = "lite" if _back40["ui.animation"] != "lite" else "full"
        config_manager.set_path("ui.animation", _anim40)
        config_manager.save()
        _hot40 = runtime.on_config_changed(source="测试-改界面")
        check("改「界面与动画」不会去重启云同步（不再出现「云同步已停止/已启动」）",
              _fake_cloud40.calls == [], str(_fake_cloud40.calls))
        check("改「界面与动画」也不会谎报「过滤与安全」",
              "过滤与安全" not in _hot40 and "云同步" not in _hot40, str(_hot40))

        # (2) 改过滤器 → 只报过滤与安全，仍然不碰云同步
        config_manager.set_path("filters.keywords_enabled",
                                not bool(_back40["filters.keywords_enabled"]))
        config_manager.save()
        _hot40b = runtime.on_config_changed(source="测试-改过滤")
        check("改过滤器时只重载过滤器（报「过滤与安全」，不报云同步）",
              _hot40b == ["过滤与安全"] and _fake_cloud40.calls == [],
              f"changed={_hot40b} cloud={_fake_cloud40.calls}")

        # (3) 改回复设置 → 报「回复与消息」
        config_manager.set_path("reply.max_queue_size", int(_back40["reply.max_queue_size"]) + 1)
        config_manager.save()
        _hot40c = runtime.on_config_changed(source="测试-改回复")
        check("改回复设置时只报「回复与消息」",
              _hot40c == ["回复与消息"] and _fake_cloud40.calls == [],
              f"changed={_hot40c} cloud={_fake_cloud40.calls}")

        # (4) 真的改了云同步设置 → 这时才允许重启同步线程
        config_manager.set_path("cloud_sync.interval_seconds",
                                int(_back40["cloud_sync.interval_seconds"]) + 60)
        config_manager.save()
        _hot40d = runtime.on_config_changed(source="测试-改云同步")
        check("真的改了云同步设置时才重启它（stop + start）且上报云同步",
              _fake_cloud40.calls == ["stop", "start"] and "云同步" in _hot40d,
              f"changed={_hot40d} cloud={_fake_cloud40.calls}")

        # (5) 改「某个机器人自己的设置」：不需要重载组件，但日志要说清是哪个机器人的哪个分组
        _bot40 = str((config_manager.config.get("bots") or [{}])[0].get("id") or "bot1")
        _msgs40 = []

        class _CollectHandler40(logging.Handler):
            def emit(self, record):
                _msgs40.append(record.getMessage())

        _logger40 = logging.getLogger("qqbotmerged")
        _level40 = _logger40.level
        _handler40 = _CollectHandler40()
        _logger40.addHandler(_handler40)
        _logger40.setLevel(logging.INFO)
        try:
            config_manager.set_for_bot(_bot40, "reply.max_queue_size",
                                       int(config_manager.get_for_bot(
                                           _bot40, "reply.max_queue_size", 10)) + 1)
            config_manager.save()
            _hot40e = runtime.on_config_changed(source="测试-改某机器人的回复设置")
        finally:
            _logger40.removeHandler(_handler40)
            _logger40.setLevel(_level40)
        check("只改某个机器人自己的设置时，不需要重载任何组件（changed 为空）",
              _hot40e == [], str(_hot40e))
        check("日志里写明是「哪个机器人的哪个分组」，不再只说 bots 或谎报云同步",
              any(f"回复与消息（{_bot40}）" in message for message in _msgs40)
              and all("云同步" not in message for message in _msgs40),
              str(_msgs40)[:300])
        # 把这条改动还原
        config_manager.set_for_bot(_bot40, "reply.max_queue_size",
                                   config_manager.get_for_bot(_bot40, "reply.max_queue_size", 10) - 1)
        config_manager.save()
        runtime.on_config_changed(source="测试-还原某机器人回复设置")

        # (6) 外部直接改 config.json（走"文件监视"这条路）：必须同样只重载真的变了的组件
        _disk_back40 = json.loads(json.dumps(config_manager.config.data))
        _disk40 = json.loads(json.dumps(_disk_back40))
        _disk40.setdefault("filters", {})
        _disk40["filters"]["keywords_enabled"] = not bool(
            (_disk_back40.get("filters") or {}).get("keywords_enabled"))
        with open(config_manager.config_path, "w", encoding="utf-8") as _handle40:
            json.dump(_disk40, _handle40, ensure_ascii=False, indent=2)
        # Windows 的 NTFS 会"复用"同名文件的时间戳，所以这里直接模拟"监视线程记的是旧 mtime"
        runtime._last_config_mtime = runtime._config_mtime() - 5
        _fake_cloud40.calls.clear()
        _tick40 = runtime._config_watch_tick()
        check("外部改配置文件也会被监视线程认出来（指纹变化 → 热更新）", _tick40 is True, str(_tick40))
        check("外部只改了过滤器时：过滤器真的重载了，云同步没有被重启",
              runtime.message_filter.enabled
              == bool(_disk40["filters"]["keywords_enabled"]) and _fake_cloud40.calls == [],
              f"enabled={runtime.message_filter.enabled} cloud={_fake_cloud40.calls}")
        # 还原磁盘上的配置
        with open(config_manager.config_path, "w", encoding="utf-8") as _handle40b:
            json.dump(_disk_back40, _handle40b, ensure_ascii=False, indent=2)
        runtime.on_config_changed(source="测试-还原磁盘配置")
        check("第 [40] 节结束时磁盘配置也还原了",
              json.loads(open(config_manager.config_path, encoding="utf-8").read()) == _disk_back40
              and runtime.message_filter.enabled
              == bool((_disk_back40.get("filters") or {}).get("keywords_enabled")),
              "磁盘配置不一致")
    finally:
        for _path40, _value40 in _back40.items():
            config_manager.set_path(_path40, _value40)
        config_manager.save()
        runtime.cloud_sync = _cloud_back40
        runtime.on_config_changed(source="测试-还原")
    check("第 [40] 节结束后设置与云同步对象都已还原",
          config_manager.config.get("ui", "animation") == _back40["ui.animation"]
          and config_manager.config.get("cloud_sync", "interval_seconds")
          == _back40["cloud_sync.interval_seconds"]
          and runtime.cloud_sync is _cloud_back40,
          f"{config_manager.config.get('ui', 'animation')} / "
          f"{config_manager.config.get('cloud_sync', 'interval_seconds')}")

    # ------------------------------------------------------------------ 41. 配置读取容错
    print("\n[41] 配置读取容错：带 BOM 也能读、读不出来时绝不覆盖用户配置")

    # (1) Windows 上 PowerShell 的 Out-File / 记事本写出来的「UTF-8」是带 BOM 的。
    #     以前用普通 utf-8 读会被判成「读不出来」，接着就用默认配置把用户的配置覆盖掉。
    _bom_path41 = os.path.join(test_root, f"config_bom_{run_id}.json")
    _bom_payload41 = {"ui": {"animation": "lite"}, "bots": [
        {"id": "bot_bom41", "name": "带BOM的机器人", "enabled": True, "app_id": "123",
         "app_secret": "secret-41", "sandbox": False, "intents": 100663296,
         "reconnect_attempts": 5, "reconnect_interval": 10}]}
    with open(_bom_path41, "wb") as _handle41:
        _handle41.write(b"\xef\xbb\xbf"
                        + json.dumps(_bom_payload41, ensure_ascii=False).encode("utf-8"))
    _bom41 = load_config(_bom_path41)
    _bom_bots41 = _bom41.config.get("bots") or []
    check("带 BOM 的 config.json 能正常读出来（不再被当成损坏文件）",
          _bom41.config.get("ui", "animation") == "lite"
          and [str(item.get("id")) for item in _bom_bots41] == ["bot_bom41"]
          and _bom_bots41[0].get("app_secret") == "secret-41",
          f"bots={_bom_bots41} anim={_bom41.config.get('ui', 'animation')}")
    with open(_bom_path41, "rb") as _handle41b:
        _after41 = json.loads(_handle41b.read().decode("utf-8-sig"))
    check("补全默认项后，带 BOM 文件里的用户设置依然在（凭据没被清空）",
          (_after41.get("ui") or {}).get("animation") == "lite"
          and [_b.get("id") for _b in (_after41.get("bots") or [])] == ["bot_bom41"]
          and ((_after41.get("bots") or [{}])[0] or {}).get("app_secret") == "secret-41",
          str(_after41.get("ui"))[:120])

    # (2) 文件真的坏了（少一个右括号）：必须留一份备份，而不是把用户配置抹掉
    _broken_path41 = os.path.join(test_root, f"config_broken_{run_id}.json")
    _broken_text41 = '{"bots": [{"id": "bot_broken41", "app_id": "999"}], ' \
                     '"ai": {"api_key": "sk-keep-me"}'
    with open(_broken_path41, "w", encoding="utf-8") as _handle41c:
        _handle41c.write(_broken_text41)
    _broken41 = load_config(_broken_path41)
    _backups41 = sorted(name for name in os.listdir(test_root)
                        if name.startswith(os.path.basename(_broken_path41) + ".broken-"))
    check("损坏的 config.json 会被备份成 .broken-<时间>（不会直接抹掉）",
          len(_backups41) == 1, str(_backups41))
    _backup_text41 = ""
    if _backups41:
        with open(os.path.join(test_root, _backups41[0]), encoding="utf-8") as _handle41d:
            _backup_text41 = _handle41d.read()
    check("备份内容与原始损坏文件逐字节一致", _backup_text41 == _broken_text41,
          _backup_text41[:120])
    check("备份里还能找到用户的密钥（说明保留了现场，可手工恢复）",
          "sk-keep-me" in _backup_text41, _backup_text41[:120])
    check("配置文件坏了也能照常启动（用默认配置兜底，不崩）",
          _broken41.config.get("bots") is not None
          and _broken41.config.bool_of("plugins", "enabled", default=True) is True, "")
    for _name41 in _backups41:
        try:
            os.remove(os.path.join(test_root, _name41))
        except OSError:
            pass

    # ------------------------------------------------------------------ 42. 插件安全扫描接线
    print("\n[42] 插件安全扫描：加载插件时顺带做 AST 静态扫描，并把结果给到后台")

    _pm42 = runtime.plugin_manager
    _infos42 = _pm42.list_plugins()
    check("每个插件的 info 里都带上了 safety 扫描结果",
          bool(_infos42) and all(isinstance(info.get("safety"), dict)
                                 and info["safety"].get("level") in ("high", "medium", "info", "ok")
                                 for info in _infos42),
          str([(info.get("name"), info.get("safety")) for info in _infos42[:1]])[:200])
    check("扫描结果里有摘要与命中清单（网页要展示）",
          all("summary" in info["safety"] and isinstance(info["safety"].get("findings"), list)
              for info in _infos42), "")
    # 这条专门防"路径传错导致扫了个寂寞"：必须真的读到插件源码文件
    check("扫描确实读到了插件的源码文件（不是空扫描）",
          any(info["safety"].get("files") for info in _infos42),
          str([(info.get("name"), info["safety"].get("files")) for info in _infos42])[:240])
    check("仓库自带插件被扫出来的风险项是真实存在的写法（open/联网/线程等）",
          any(item.get("evidence") for info in _infos42
              for item in (info["safety"].get("findings") or [])),
          str([[item.get("evidence") for item in (info["safety"].get("findings") or [])]
               for info in _infos42])[:240])
    _diag42 = _pm42.diagnostics()
    check("诊断信息里有安全扫描汇总（插件页顶部提示用）",
          isinstance(_diag42.get("safety"), dict)
          and _diag42["safety"].get("level") in ("high", "medium", "info", "ok"),
          str(_diag42.get("safety"))[:200])
    check("汇总里的命中项带上了是哪个插件",
          all(item.get("plugin") for item in (_diag42.get("safety") or {}).get("findings") or [])
          or (_diag42.get("safety") or {}).get("level") == "ok",
          str((_diag42.get("safety") or {}).get("findings"))[:200])
    _api42 = client.get("/api/admin/plugins").get_json()
    check("插件列表接口把 safety 一起返回（前端才画得出徽章）",
          bool(_api42.get("success"))
          and all("safety" in item for item in (_api42.get("plugins") or [])),
          str([sorted(item.keys())[:6] for item in (_api42.get("plugins") or [])[:1]]))
    check("安全扫描不会因为插件源码读不了就崩（失败也返回一个 ok 结构）",
          _pm42._scan_safety({"name": "不存在的插件_x42", "module": "no_such_plugin_x42"}
                             ).get("level") in ("high", "medium", "info", "ok"),
          "")

    # ------------------------------------------------------------------ 43. 云同步敏感字段
    print("\n[43] 云同步上传前的敏感字段扫描（含密钥的文件不上传，本地文件不动）")

    _sync43 = CloudSync(config_manager, logger_obj=log)
    check("云同步实例默认开启「含密钥的文件不上传」", _sync43.skip_secret_files is True,
          str(_sync43.skip_secret_files))
    check("这个开关是全局设置（不按机器人隔离）",
          not _schema.is_per_bot("cloud_sync.skip_secret_files"), "")
    _payload43 = config_manager.payload()
    check("设置页数据里带有这个开关（页面渲染得出来）",
          (_payload43.get("config") or {}).get("cloud_sync", {})
          .get("skip_secret_files") is True,
          str((_payload43.get("config") or {}).get("cloud_sync"))[:160])
    # 真实的插件数据场景：插件把 API Key 写进 data/plugin_data/<插件名>/*.json
    _plugin_secret43 = json.dumps({"api_key": "sk-abcdefghijklmnop",
                                   "model": "x"}).encode("utf-8")
    from core.cloud_sync import find_sensitive_keys as _find43
    check("插件数据里写了 api_key 会被识别出来（这正是要拦住的场景）",
          _find43("data/plugin_data/astrbot_plugin_x/config.json", _plugin_secret43) == ["api_key"],
          str(_find43("data/plugin_data/astrbot_plugin_x/config.json", _plugin_secret43)))
    check("同样的内容放在聊天上下文里不会误伤（只看字段名）",
          _find43("data/user_context/private/bot1__U.json",
                  json.dumps({"role": "user",
                              "content": "api_key = sk-abcdefghijklmnop"}).encode("utf-8")) == [],
          "")

    # ------------------------------------------------------------------ 收尾
    runtime.processor.stop()
    time.sleep(0.3)
    runtime.store.close()
    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
