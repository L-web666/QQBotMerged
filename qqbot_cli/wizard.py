# -*- coding: utf-8 -*-
"""初始向导：问几个必填项，生成 `config.json`。

**只问最少的几项**：主程序有 118 个配置项，全问不现实。这里只问
「装了没它就干不了活」的那几个，其余走默认值，装完在网页设置页改。

写出来的 `config.json` 只包含**用户填的**和必要的骨架；主程序启动时会
按 `core/config_schema.py` 自动补全缺的项（缺失项自动补全相当于自带迁移）。
"""

import json
import os
import secrets
from typing import Dict, List, Optional

from . import paths


# ---------------------------------------------------------------- 输入
def ask(prompt: str, default: str = "", allow_empty: bool = True) -> str:
    """问一句；直接回车用默认值。"""
    suffix = " [%s]" % default if default else ""
    while True:
        try:
            answer = input("%s%s: " % (prompt, suffix)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise
        if answer:
            return answer
        if default or allow_empty:
            return default
        print("  这一项不能留空，请再输一次。")


def ask_yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = input("%s [%s]: " % (prompt, hint)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        raise
    if not answer:
        return default
    return answer in ("y", "yes", "是", "1", "对")


def ask_port(prompt: str, default: int = 8666) -> int:
    while True:
        text = ask(prompt, str(default))
        try:
            port = int(text)
        except ValueError:
            print("  端口要是数字，再试一次。")
            continue
        if 1 <= port <= 65535:
            return port
        print("  端口范围是 1~65535，再试一次。")


# ---------------------------------------------------------------- 向导
def run_wizard(install_dir: str, tag: str = "",
               existing: Optional[Dict] = None) -> Dict:
    """跑一遍向导，返回写进 config.json 的那份数据。"""
    existing = existing or {}
    web = existing.get("web") or {}
    bots: List[Dict] = list(existing.get("bots") or [])
    first_bot = bots[0] if bots else {}

    print()
    print("=" * 60)
    print("  基本配置")
    print("  （都可以留空，装好之后在网页「设置」里改）")
    print("=" * 60)
    print()

    app_id = ask("AppID（QQ 开放平台的机器人 AppID）", first_bot.get("app_id") or "")
    app_secret = ask("AppSecret", first_bot.get("app_secret") or "")
    bot_name = ask("给这个机器人起个名字", first_bot.get("name") or "我的机器人")

    print()
    print("-- AI 回复（可选，留空就只用关键词和插件）--")
    ai = existing.get("ai") or {}
    api_key = ask("AI 的 API Key", ai.get("api_key") or "")
    base_url = ask("AI 接口地址", ai.get("base_url") or "https://api.deepseek.com")
    model = ask("AI 模型名", ai.get("model") or "deepseek-chat")

    print()
    print("-- 网页后台 --")
    port = ask_port("网页端口", int(web.get("port") or 8666))

    token = str(web.get("token") or "").strip()
    if not token:
        token = secrets.token_urlsafe(24)
        print("  已自动生成访问令牌（登录网页要用，稍后会一并打印）")

    bot_entry = {
        "id": first_bot.get("id") or "bot1",
        "name": bot_name or "我的机器人",
        "enabled": bool(app_id and app_secret),
        "app_id": app_id,
        "app_secret": app_secret,
    }
    if first_bot:
        others = bots[1:]
    else:
        others = []

    config = {
        "web": {
            "host": web.get("host") or "127.0.0.1",
            "port": port,
            "token": token,
        },
        "ai": {
            "api_key": api_key,
            "base_url": base_url or "https://api.deepseek.com",
            "model": model or "deepseek-chat",
        },
        "bots": [bot_entry] + others,
        "active_bot_id": bot_entry["id"],
    }
    if existing:
        # 保留用户原文件里其它分组，只覆盖我们问过的这几项
        merged = dict(existing)
        merged["web"] = dict(web, **config["web"])
        merged["ai"] = dict(ai, **config["ai"])
        merged["bots"] = config["bots"]
        merged["active_bot_id"] = config["active_bot_id"]
        config = merged

    if not app_id or not app_secret:
        print()
        print("  注意：没填 AppID/AppSecret，机器人会处于「未启用」状态。")
        print("        装好之后在网页「设置 → 机器人账号」里补上即可启用。")
    return config


def write_config(install_dir: str, config: Dict) -> str:
    """写 config.json；已有的先备份一份。返回文件路径。"""
    target = os.path.join(install_dir, "config.json")
    if os.path.exists(target):
        backup = target + ".bak"
        try:
            os.replace(target, backup)
            print("  原 config.json 已备份为 config.json.bak")
        except OSError:
            pass
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
    return target


def show_summary(install_dir: str, config: Dict, python_versions: Dict[str, str]) -> None:
    """装完把关键信息打印出来（令牌要能看到，否则用户进不去后台）。"""
    web = config.get("web") or {}
    host = web.get("host") or "127.0.0.1"
    port = web.get("port") or 8666
    token = web.get("token") or ""
    shown_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = "http://%s:%s/" % (shown_host, port)
    if token:
        url += "?token=%s" % token

    print()
    print("=" * 60)
    print("  安装完成")
    print("=" * 60)
    print("  安装目录 : %s" % install_dir)
    if python_versions:
        print("  依赖版本 : %s" % "、".join(
            "%s %s" % (name, ver) for name, ver in python_versions.items()))
    print()
    print("  后台地址 : %s" % url)
    if token:
        print("  访问令牌 : %s" % token)
        print("             （也写在 config.json 的 web.token 里）")
    print()
    print("  启动命令 : qqbot")
    print()
    print("  想看/改里面的文件，进这个目录即可：")
    print("    %s" % paths.install_help_command(install_dir))
    if paths.is_windows():
        print("    （或在资源管理器地址栏粘贴上面的路径）")
    else:
        print("    （或用文件管理器打开上面的路径）")
    print("      配置 config.json · 数据 data/ · 插件 plugins/ · 日志 data/logs/")
    print()
    print("  备份数据 : qqbot export")
    print("  卸载     : qqbot uninstall")
    print("  换目录装 : qqbot --dir <路径>")
    print("=" * 60)
