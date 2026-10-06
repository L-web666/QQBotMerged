# -*- coding: utf-8 -*-
"""往运行中的程序里注入一条模拟消息（离线演示 / 界面调试用）

做法：不连接 QQ，直接构造网关事件交给运行时，效果与真实收到消息一致
（落库 → 广播到网页 → 走关键词/插件/AI 回复链路）。

用法：
    python tests/simulate_message.py                             # 默认注入一条私聊
    python tests/simulate_message.py --type group                # 注入群聊 @ 消息
    python tests/simulate_message.py --text "你好" --with-image  # 带图片（使用本地生成的小图）
    python tests/simulate_message.py --bot bot1 --openid USER_X
"""

import argparse
import base64
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(BASE)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import paths                       # noqa: E402
from core.config_manager import load_config   # noqa: E402
from core.logger import Logger                # noqa: E402
from core.runtime import Runtime              # noqa: E402

TINY_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAP0lEQVR42u3PQREAAAgDINc/9Cz4"
    b"dQOS0FWSJEmSJEmSJEmSJEmSJEmSJEmSJEmSJEmSJEmSJEmSJEmSJH3tAyHmAAFzXFmvAAAAAElF"
    b"TkSuQmCC")


def main():
    parser = argparse.ArgumentParser(description="注入一条模拟消息")
    parser.add_argument("--bot", default="", help="机器人 id（默认用第一个）")
    parser.add_argument("--type", default="private", choices=["private", "group"], help="会话类型")
    parser.add_argument("--openid", default="SIM_USER_1", help="用户 openid")
    parser.add_argument("--group", default="SIM_GROUP_1", help="群 openid（--type group 时）")
    parser.add_argument("--text", default="你好呀（模拟消息）", help="消息文本")
    parser.add_argument("--with-image", action="store_true", help="附带一张图片（会走留存流程）")
    parser.add_argument("--serve", action="store_true",
                        help="保持进程存活以提供本地图片下载（--with-image 演示用）")
    args = parser.parse_args()

    paths.ensure_dirs()
    logger = Logger(console_color=True, level="INFO", log_dir=paths.LOG_DIR)
    log = logger.get_logger()

    config_manager = load_config()
    runtime = Runtime(config_manager, log)

    bot_id = args.bot or next(iter(runtime.bots), "")
    if not bot_id:
        print("⚠️ 配置里没有启用的机器人（设置 → 机器人账号），仍会注入消息但不发回复")

    attachments = []
    if args.with_image:
        # 把图片存进媒体目录，并用本地 HTTP 地址模拟 QQ 的媒体链接
        record = runtime.media.save_bytes(TINY_PNG, "image/png", file_name="sim_image.png",
                                          source="received", bot_id=bot_id)
        port = runtime.config.int_of("web", "port", default=8666)
        url = f"http://127.0.0.1:{port}{record['local_url']}"
        attachments.append({"url": url, "content_type": "image/png", "file_name": "sim_image.png"})
        print(f"已准备图片：{url}")

    message = {
        "bot_id": bot_id,
        "type": args.type,
        "group_openid": args.group if args.type == "group" else "",
        "openid": args.openid,
        "member_openid": args.openid,
        "username": "模拟用户",
        "content": args.text,
        "msg_id": f"SIM_{int(time.time())}",
        "is_at_bot": args.type == "group",
        "attachments": attachments,
        "quote": {},
        "mentions": [],
        "raw_event": "{}",
    }
    runtime.processor._running = True
    if bot_id:
        runtime.processor._ensure_worker(bot_id)
    runtime._handle_incoming(message)
    key = f"{bot_id}:{'group:' + args.group if args.type == 'group' else 'private:' + args.openid}"
    print(f"✅ 已注入消息：{key}")
    print("   打开网页「聊天窗口」即可看到（若开着实时推送会自动出现）")

    if args.serve:
        print("   进程保持运行中（Ctrl+C 退出）…")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    # 先停工作线程再关库，避免线程在关库后访问数据库
    runtime.processor.stop()
    time.sleep(0.3)
    runtime.store.close()


if __name__ == "__main__":
    main()
