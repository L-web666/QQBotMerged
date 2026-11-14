# -*- coding: utf-8 -*-
"""配置安全体检：启动时找出"看起来能跑、其实很危险"的配置组合。

**为什么需要**：单个配置项单独看都正常，危险往往出在组合上。例如
「令牌够强」+「监听 0.0.0.0」看着没问题，但如果是明文 HTTP，令牌就在局域网里裸奔；
又比如「放在 Nginx 后面」+「没开 trust_proxy」，所有请求来源都会被认成同一个 IP，
一个人猜错几次就能把所有人锁在门外。

本模块只**给建议**，不替代 `run.py` 里那道拒绝启动的硬性检查
（「非本机 + 无令牌」直接退出，那是最后一道闸）。这里管的是更细的组合风险。

分级：
  · `critical` —— 等于没有防护，必须修
  · `warning`  —— 有实际风险，建议修
  · `info`     —— 提醒，视部署方式而定
"""

import os
from typing import Any, Dict, List

from core import version as version_module

# 多行日志的续行缩进。日志前缀是「时间 [级别] 」（`01:56:55 [INFO] `，共 16 列），
# 续行缩到与之对齐，读起来是整齐的两段式：
#   01:56:55 [INFO] 🔵 [提示] 云同步已开启
#   01:56:55 [INFO]    说明：……
_SUB = "    "


def _is_local_host(host: str) -> bool:
    """监听地址是否只对本机开放。"""
    return (host or "").strip() in ("127.0.0.1", "localhost", "::1", "::")


def check_config(config) -> List[Dict[str, Any]]:
    """对当前配置做一次安全体检，返回问题列表。

    每一项：{"level": "critical|warning|info", "title": str, "detail": str,
             "fix": str, "key": 配置项路径}
    """
    issues: List[Dict[str, Any]] = []

    def add(level: str, title: str, detail: str, fix: str, key: str):
        issues.append({"level": level, "title": title, "detail": detail,
                       "fix": fix, "key": key})

    try:
        host = config.str_of("web", "host", default="127.0.0.1")
        port = config.int_of("web", "port", default=8666)
        token = config.str_of("web", "token", default="")
        public_url = config.str_of("web", "public_base_url", default="")
        trust_proxy = config.bool_of("web", "trust_proxy", default=False)
        base_url = config.str_of("ai", "base_url", default="")
        ai_enabled = config.bool_of("ai", "enabled", default=False)
        sandbox = config.str_of("plugins", "sandbox", default="loose")
        plugins_enabled = config.bool_of("plugins", "enabled", default=True)
        auto_install = config.bool_of("update", "auto_install", default=False)
        cloud_enabled = config.bool_of("cloud_sync", "enabled", default=False)
    except Exception:
        # 配置读不出来时体检本身不该把事情搞砸
        return issues

    local_only = _is_local_host(host)

    # ---------------------------------------------------------------- 令牌
    if not token and not local_only:
        add("critical", "后台对局域网/公网开放且没有令牌",
            "监听 %s:%s，任何能访问该端口的人都能发消息、改配置、禁言、关程序。" % (host, port),
            "设置 web.token，或改成 --host 127.0.0.1", "web.token")

    if not token and local_only:
        # **本机监听 + 空令牌也不是安全的**：你打开的任意网页都能用"跨站简单请求"
        # 让本机浏览器替你发 POST（text/plain 不触发预检），从而关掉程序、驱动更新端点。
        # CSRF 校验已经挡住了这条路，但"无鉴权"本身仍是不该保留的默认状态。
        add("warning", "仅本机监听但没有设置访问令牌",
            "未设置令牌时后台接口不做鉴权。虽然只监听 %s，但你在浏览器里打开的任意网页"
            "都可能借本机发起请求（CSRF）。" % host,
            "建议仍设置一个访问令牌（首次运行会自动生成一个）", "web.token")

    if token:
        from core.security_rules import token_generator_hint, token_weakness
        weak = token_weakness(token)
        if weak:
            level = "critical" if not local_only else "warning"
            add(level, "访问令牌强度不足",
                "当前令牌%s；%s。" % (weak,
                                     "又对局域网开放，等于没有防护" if not local_only
                                     else "虽然只本机可访问，但不要用它当共享口令"),
                token_generator_hint() + " 生成一个", "web.token")

    # ---------------------------------------------------------------- 明文 HTTP
    if token and not local_only and not str(public_url or "").lower().startswith("https://"):
        add("warning", "令牌会在网络上明文传输",
            "监听 %s 且没有走 HTTPS（web.public_base_url=%r）。令牌走 URL 参数传输，"
            "同网段的人抓到请求就能直接拿走后台权限。" % (host, public_url or ""),
            "用反向代理套一层 HTTPS，或只在受信任的内网使用",
            "web.public_base_url")

    # ---------------------------------------------------------------- 反向代理
    if trust_proxy and not public_url:
        add("info", "打开了 trust_proxy 但没设置对外地址",
            "程序会按 X-Forwarded-For 识别来源。如果前面其实没有代理，"
            "客户端可以伪造这个头绕过令牌失败计数。",
            "确认前面确实有 Nginx/CDN；没有就关掉 web.trust_proxy", "web.trust_proxy")

    if (not trust_proxy) and public_url.lower().startswith("https://") and not local_only:
        add("info", "看起来在反向代理后面，但没开 trust_proxy",
            "所有请求的来源都会被识别成代理的 IP。一个人连续猜错 10 次令牌，"
            "就会把其他人一起锁在门外 5 分钟。",
            "如果确实有反向代理，打开 web.trust_proxy 并按需配置真实来源",
            "web.trust_proxy")

    # ---------------------------------------------------------------- AI 接口
    if ai_enabled and str(base_url or "").lower().startswith("http://"):
        add("warning", "AI 接口地址是明文 HTTP",
            "ai.base_url=%r：API Key 与聊天内容会以明文经过网络，中间人可以镜像你的密钥与对话。" % base_url,
            "换成 https:// 的服务地址", "ai.base_url")

    # ---------------------------------------------------------------- 插件
    if plugins_enabled and sandbox == "off":
        add("info", "插件沙箱已关闭",
            "插件可以任意读写文件、执行系统命令。只有在你完全信任所有插件源码时才这样用。",
            "把 plugins.sandbox 设为 loose（默认）或 strict", "plugins.sandbox")

    # ---------------------------------------------------------------- 自动更新
    if auto_install:
        add("info", "已开启启动时自动安装更新",
            "发现新版本会直接下载并覆盖代码。更新包有双源校验（commit + sha256），"
            "但两个来源都在 GitHub 上，仓库被攻破时挡不住。",
            "不确定就关掉 update.auto_install，改成手动点「检查更新」",
            "update.auto_install")

    # ---------------------------------------------------------------- 云同步
    if cloud_enabled:
        add("info", "云同步已开启",
            "data/ 下的用户数据会上传到 Cloudflare D1。"
            "含密钥的文件默认跳过（cloud_sync.skip_secret_files），请确认该开关没被关掉。",
            "确认 cloud_sync.skip_secret_files 保持开启", "cloud_sync.enabled")

    return issues


def format_report(issues: List[Dict[str, Any]]) -> List[str]:
    """把体检结果整理成若干行日志文本（带图标与修复建议）。

    **缩进说明**：日志行的前缀是 `时间 [级别] `（如 `01:56:55 [INFO] `，约 16 列），
    多行内容如果只写 `"     说明："` 就会和标题行参差不齐。
    这里统一用 `_SUB` 缩进，让"说明/建议"与标题文字大致对齐；
    标题行本身不加前导空格，直接跟在 `[INFO] ` 后面。
    """
    if not issues:
        return ["✅ 配置安全体检：没有发现明显问题"]
    icons = {"critical": "🔴", "warning": "🟠", "info": "🔵"}
    labels = {"critical": "严重", "warning": "风险", "info": "提示"}
    lines = ["配置安全体检：发现 %d 项需要注意" % len(issues)]
    for item in issues:
        level = item.get("level") or "info"
        lines.append("%s [%s] %s" % (icons.get(level, "•"),
                                     labels.get(level, level), item.get("title")))
        if item.get("detail"):
            lines.append("%s说明：%s" % (_SUB, item["detail"]))
        if item.get("fix"):
            lines.append("%s建议：%s" % (_SUB, item["fix"]))
    return lines
