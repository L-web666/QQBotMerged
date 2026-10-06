# -*- coding: utf-8 -*-
"""统一配置体系（合并版）

合并了原 `API_qqbot`（AI 机器人 / 插件 / 云同步 / 指令面板 / 定时任务）与
原 `app`（网页控制台 / 消息存储 / 发送策略 / 接收器）的全部可配置项，
并按功能类别归入 13 个分组，供后台「设置」页分类渲染。

关键约定：
- 所有配置项都必须是 `DEFAULT_CONFIG` 里已存在的路径（后台只认识这些键）；
- `FIELDS` 提供每项的中文名称、说明与类型，`SECTION_LABELS` 提供分组中文名；
- `LIST_FIELDS` / `TEXT_FIELDS` 决定网页用哪种控件渲染；
- 说明键（以 `_` 开头）不会被当成可编辑项。
"""

from typing import Any, Dict, List

# ======================================================================================
# 分组：键 → (中文名, 图标, 一句话说明)
# ======================================================================================
SECTION_LABELS: Dict[str, Dict[str, str]] = {
    "web": {"label": "网页后台", "icon": "🌐", "desc": "后台监听地址、端口与访问令牌"},
    "bots": {"label": "机器人账号", "icon": "🤖", "desc": "支持多个 QQ 机器人（各自独立连接）"},
    "ai": {"label": "AI 服务", "icon": "🧠", "desc": "OpenAI 兼容接口、模型与人设"},
    "reply": {"label": "回复与消息", "icon": "💬", "desc": "回复策略、分段、队列、无意义过滤"},
    "filters": {"label": "过滤与安全", "icon": "🛡️", "desc": "关键词回复、敏感词、回复限速"},
    "features": {"label": "功能开关", "icon": "🎛️", "desc": "群聊 @ 要求、图片留存、群成员管理"},
    "send": {"label": "发送策略", "icon": "📤", "desc": "引用方式、图片上限、重试与超时"},
    "storage": {"label": "存储与留存", "icon": "🗄️", "desc": "消息库、媒体留存与自动清理"},
    "receiver": {"label": "连接与重连", "icon": "🔌", "desc": "WebSocket 重连与心跳超时"},
    "scheduler": {"label": "定时任务", "icon": "⏰", "desc": "按北京时间定时推送"},
    "panels": {"label": "指令面板", "icon": "🎯", "desc": "QQ 聊天界面里的指令列表"},
    "plugins": {"label": "插件系统", "icon": "🧩", "desc": "plugins/ 目录扩展功能"},
    "cloud": {"label": "云同步", "icon": "☁️", "desc": "数据同步到 Cloudflare D1"},
    "logging": {"label": "日志", "icon": "📜", "desc": "日志级别、分割大小与控制台着色"},
    "ui": {"label": "界面与动画", "icon": "✨", "desc": "主题、动画强度与刷新频率"},
    "security": {"label": "权限与告警", "icon": "🔐", "desc": "管理员名单、出错告警主人"},
    "context": {"label": "对话上下文", "icon": "🧵", "desc": "记忆条数与开关"},
}

# ======================================================================================
# 默认配置
# ======================================================================================
DEFAULT_CONFIG: Dict[str, Any] = {
    # 当前选中的机器人（左侧导航栏顶部的选择器；设置页与聊天窗口都以它为准）
    "active_bot_id": "bot1",

    # ---------------- 网页后台 ----------------
    "web": {
        "host": "127.0.0.1",
        "port": 8666,
        "token": "",
        # 对外可访问的站点地址：发文件下载链接（平台不支持文件消息时的退化方案）要用它。
        # 留空时自动取监听地址；监听 0.0.0.0 时自动换成局域网地址。
        "public_base_url": "",
        "debug": False,
        "poll_interval_ms": 2000,
        "log_access_requests": False,
        "log_polling_requests": False,
        "page_size": 200,
        "_note": "host 默认仅本机可访问；改成 0.0.0.0 时务必设置 token（留空=不校验）。",
    },

    # ---------------- 机器人账号（可多个，各自一条独立 WebSocket 连接） ----------------
    # 默认只给一个空条目：需要更多机器人时在「设置 → 机器人账号」点"添加一个机器人"
    "bots": [
        {
            "id": "bot1",
            "name": "机器人 1",
            "enabled": True,
            "app_id": "",
            "app_secret": "",
            "sandbox": False,
            "intents": 100663296,
            "reconnect_attempts": 5,
            "reconnect_interval": 10,
            "__intents_note": "群@+单聊 = 1<<25 | 1<<26 = 100663296（默认）。要接收群内所有消息，"
                              "需在 QQ 后台申请权限并让群管理员开启“接收所有消息”（事件挂在 1<<25 上）；"
                              "群成员变动事件可再加 1<<24（16777216）。订阅未获权限的位置会导致网关拒绝连接，"
                              "程序会自动降级重试。",
        },
    ],

    # ---------------- AI 服务 ----------------
    "ai": {
        "enabled": True,
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "system_prompt": "你是一个智能、友好的 AI 助手，请用中文简洁地回复用户。",
        "temperature": 0.8,
        "max_tokens": 1024,
        "timeout_seconds": 60,
        "no_ai_reply": "抱歉，我暂时没办法回答这个问题。",
        "vision_enabled": True,
        "vision_prompt": "请用中文描述这张图片的内容。",
    },

    # ---------------- 回复与消息 ----------------
    "reply": {
        "require_mention": True,
        "context_enabled": True,
        "max_history": 20,
        "max_segment_length": 2000,
        "max_queue_size": 10,
        "filter_meaningless": True,
        "strip_markdown": True,
        "quote_reply": False,
    },

    # ---------------- 过滤与安全 ----------------
    "filters": {
        "keywords_enabled": True,
        "exact_match_responses": {},
        "fuzzy_match_responses": {},
        "sensitive_enabled": True,
        "sensitive_list": [],
        "sensitive_replacement": "***",
        "sensitive_block_input": True,
        "rate_limit_enabled": True,
        "rate_limit_seconds": 3,
    },

    # ---------------- 功能开关 ----------------
    # 禁言方式不在这里配置：请在「群管理」页对具体群/成员选择"本地"还是"官方接口"
    "features": {
        "save_received_media": True,
        "media_download_timeout": 20,
        "group_management": True,
        "auto_reply_in_group": True,
        "auto_reply_in_private": True,
        "notify_on_error": True,
    },

    # ---------------- 发送策略 ----------------
    "send": {
        "reply_style": "both",
        "message_max_length": 4000,
        # QQ 官方的软/硬限制：图片软 20MB、视频软 30MB、语音软 20MB、文件软 200MB，
        # 硬限制统一 200MB（超过软限制会降级为"文件"类型上传，超过硬限制报错）。
        # 这两个值只是"我们自己再收一收"的开关，默认取官方上限。
        "max_file_mb": 200,
        "max_image_mb": 20,
        "max_retries": 3,
        "retry_backoff_factor": 1.0,
        "request_timeout_seconds": 30,
        "token_refresh_buffer_seconds": 60,
        "upload_keep_days": 7,
        "_reply_style_note": "both=同时带 msg_id+msg_seq 与 message_reference；msg_id=仅被动回复；message_reference=仅引用字段。",
    },

    # ---------------- 存储与留存 ----------------
    "storage": {
        "enabled": True,
        "db_path": "data/messages.db",
        "media_dir": "data/media",
        "max_messages": 20000,
        "max_conversations": 500,
        "messages_per_page": 200,
        "retention_days": 30,
        "trim_every_writes": 100,
        "group_members_refresh_hours": 6,
    },

    # ---------------- 连接与重连 ----------------
    "receiver": {
        "reconnect_interval_seconds": 5,
        "max_reconnect_interval_seconds": 30,
        "heartbeat_timeout_factor": 3,
    },

    # ---------------- 定时任务 ----------------
    "scheduler": {
        "enabled": False,
        "tasks": [],
        "_tasks_note": "格式：[{\"time\":\"08:00\",\"bot_id\":\"bot1\",\"target_type\":\"group\",\"target_id\":\"群openid\",\"content\":\"早安\"}]",
    },

    # ---------------- 指令面板 ----------------
    "panels": {
        "enabled": True,
        "commands": [
            {"type": "command", "name": "/帮助", "desc": "显示可用指令"},
            {"type": "command", "name": "/清空上下文", "desc": "清空本会话记忆"},
        ],
        "c2c_target_type": "all",
        "c2c_openids": [],
        "group_target_type": "all",
        "group_openids": [],
        "remark": "QQBotMerged 指令面板",
    },

    # ---------------- 插件系统 ----------------
    "plugins": {
        "enabled": True,
        "dir": "plugins",
        # 按机器人隔离插件（在「插件管理」页勾选，或在这里手写插件名）
        "enabled_names": [],        # 该机器人的插件白名单；空 = 不限制
        "disabled_names": [],       # 该机器人的插件黑名单（优先级高于白名单）
    },

    # ---------------- 云同步 ----------------
    "cloud_sync": {
        "enabled": False,
        "provider": "cloudflare_d1",
        "account_id": "",
        "database_id": "",
        "api_token": "",
        "interval_seconds": 300,
        "pull_on_start": True,
        "upload_logs": False,
        "skip_secret_files": True,
        "max_file_mb": 10,
        "tombstone_days": 30,
        "apply_remote_deletes": False,
        "error_pause_minutes": 30,
        "startup_buffer": True,
        "startup_buffer_minutes": 3,
        "startup_buffer_max_mb": 8,
        "startup_buffer_keep_remote": True,
    },

    # ---------------- 日志 ----------------
    "logging": {
        "level": "INFO",
        "max_size_mb": 10,
        "console_color": True,
        "keep_days": 30,
    },

    # ---------------- 界面与动画 ----------------
    "ui": {
        "theme": "auto",
        "animation": "full",
        "poll_interval_ms": 2000,
        "message_bubbles": True,
        "compact_mode": False,
        "show_avatar": True,
        "image_lightbox": True,
    },

    # ---------------- 权限与告警 ----------------
    "security": {
        "admin_openids": [],
        "alert_enabled": True,
        "alert_owner_openid": "",
    },

    # ---------------- 上下文（保留兼容顶层便捷项） ----------------
    "context": {
        "max_history": 20,
        "enabled": True,
    },
}


# ======================================================================================
# 字段元信息：(类型, 中文名, 说明, 单位/选项)
#   type: bool | int | float | str | secret | textarea | list | dict
#         | enum | bots | scheduler_tasks | panel_commands
#   —— 类型名与前端渲染器一一对应（renderField 按 type 分派控件）
# ======================================================================================
def _f(kind: str, label: str, hint: str = "", **extra) -> Dict[str, Any]:
    item = {"type": kind, "label": label, "hint": hint}
    item.update(extra)
    return item


FIELDS: Dict[str, Dict[str, Any]] = {
    # ---------------- web ----------------
    "web.host": _f("str", "监听地址", "127.0.0.1=仅本机；0.0.0.0=局域网可访问（务必设置令牌）"),
    "web.port": _f("int", "监听端口", "默认 8666", min=1, max=65535),
    "web.token": _f("secret", "访问令牌", "留空不校验；填了则用 ?token=xxx 访问"),
    "web.public_base_url": _f("str", "对外访问地址",
                              "发文件下载链接时用；例 http://1.2.3.4:8666 或 https://bot.example.com。"
                              "留空自动取监听地址（0.0.0.0 会换成局域网 IP）"),
    "web.debug": _f("bool", "调试模式", "仅本机调试用，不要对公网开启"),
    "web.poll_interval_ms": _f("int", "网页刷新间隔", "毫秒", min=500, max=60000),
    "web.log_access_requests": _f("bool", "记录 HTTP 访问日志", "关闭可减少日志刷屏"),
    "web.log_polling_requests": _f("bool", "记录轮询请求日志", "默认关闭，避免刷屏"),
    "web.page_size": _f("int", "单页消息条数", "聊天记录每次加载条数", min=20, max=2000),

    # ---------------- bots（列表项，网页用专用卡片渲染） ----------------
    "bots": _f("bots", "机器人账号列表", "每个启用的账号会建立一条独立 WebSocket 连接"),

    # ---------------- ai ----------------
    "ai.enabled": _f("bool", "启用内置 AI", "关闭后普通消息交给插件/关键词回复"),
    "ai.api_key": _f("secret", "API 密钥", "任意 OpenAI 兼容服务"),
    "ai.base_url": _f("str", "接口地址", "如 https://api.deepseek.com 或 https://api.openai.com/v1"),
    "ai.model": _f("str", "模型名称", "如 deepseek-chat / gpt-4o-mini"),
    "ai.temperature": _f("float", "温度", "0~2，越大越随机", min=0, max=2, step=0.1),
    "ai.max_tokens": _f("int", "最大回复长度", "tokens", min=16, max=32768),
    "ai.timeout_seconds": _f("int", "请求超时", "秒", min=5, max=600),
    "ai.no_ai_reply": _f("str", "兜底回复", "没配 AI 且没人接管时的回复，留空=不回复"),
    "ai.system_prompt": _f("textarea", "AI 人设（全局提示词）", "所有对话都会带上这段系统提示"),
    "ai.vision_enabled": _f("bool", "图片识别", "收到图片时交给多模态模型描述"),
    "ai.vision_prompt": _f("str", "图片识别提示词", "发给多模态模型的默认提示"),

    # ---------------- reply ----------------
    "reply.require_mention": _f("bool", "群聊需 @ 才回复", "关闭后回复群内所有消息（需平台权限）"),
    "reply.context_enabled": _f("bool", "启用上下文记忆", "按用户/群分别保存历史"),
    "reply.max_history": _f("int", "上下文条数", "每个用户/群最多记住多少条", min=0, max=200),
    "reply.max_segment_length": _f("int", "单条消息长度上限", "超出自动分段", min=100, max=5000),
    "reply.max_queue_size": _f("int", "消息队列上限", "繁忙时的最大排队数", min=1, max=500),
    "reply.filter_meaningless": _f("bool", "过滤无意义消息", "纯数字/符号/表情不回复"),
    "reply.strip_markdown": _f("bool", "清理 Markdown 符号", "AI 输出去掉 **、#、表格等符号，QQ 显示更干净"),
    "reply.quote_reply": _f("bool", "回复时引用原消息", "私聊/群聊回复都带上引用"),

    # ---------------- filters ----------------
    "filters.keywords_enabled": _f("bool", "启用关键词回复", "命中关键词直接回复，不调用 AI"),
    "filters.exact_match_responses": _f("dict", "精确匹配回复", "键=完全一致的关键词，值=回复内容"),
    "filters.fuzzy_match_responses": _f("dict", "模糊匹配回复", "键=包含即触发，值=回复内容"),
    "filters.sensitive_enabled": _f("bool", "启用敏感词过滤", ""),
    "filters.sensitive_list": _f("list", "敏感词列表", "每行一个词，子串匹配"),
    "filters.sensitive_replacement": _f("str", "替换符号", "命中后替换成什么"),
    "filters.sensitive_block_input": _f("bool", "直接拦截输入", "开启=含敏感词的消息不回复；关闭=打码后正常回复"),
    "filters.rate_limit_enabled": _f("bool", "回复限速", "同一用户频繁提问自动降频"),
    "filters.rate_limit_seconds": _f("float", "限速间隔", "秒", min=0.5, max=600, step=0.5),

    # ---------------- features ----------------
    "features.save_received_media": _f("bool", "自动留存收到的图片/表情包",
                                       "收到即下载到本地并入库，避免 QQ 链接过期后失效"),
    "features.media_download_timeout": _f("int", "媒体下载超时", "秒", min=3, max=120),
    "features.group_management": _f("bool", "启用群管理页",
                                    "成员列表、禁言（禁言方式在群管理页里按群选择）、群配置"),
    "features.auto_reply_in_group": _f("bool", "群聊自动回复", "总开关"),
    "features.auto_reply_in_private": _f("bool", "私聊自动回复", "总开关"),
    "features.notify_on_error": _f("bool", "出错告警", "连接失败等异常时私聊通知主人"),

    # ---------------- send ----------------
    "send.reply_style": _f("enum", "引用消息字段", "平台报参数错误时可切换",
                           options=[["both", "两种都带（兼容性最好）"], ["msg_id", "仅 msg_id"], ["message_reference", "仅引用字段"]]),
    "send.message_max_length": _f("int", "发送文本上限", "字符", min=100, max=20000),
    "send.max_file_mb": _f("float", "发送文件上限", "MB（上传给 QQ）", min=0.1, max=200),
    "send.max_image_mb": _f("float", "图片大小上限", "MB", min=0.1, max=100),
    "send.max_retries": _f("int", "失败重试次数", "", min=1, max=10),
    "send.retry_backoff_factor": _f("float", "重试退避系数", "指数退避基础秒数", min=0, max=10, step=0.5),
    "send.request_timeout_seconds": _f("float", "请求超时", "秒", min=3, max=300),
    "send.token_refresh_buffer_seconds": _f("float", "Token 提前刷新", "秒", min=0, max=3600),
    "send.upload_keep_days": _f("int", "上传文件保留天数", "网页上传的临时文件清理周期", min=0, max=365),

    # ---------------- storage ----------------
    "storage.enabled": _f("bool", "使用 SQLite 持久化", "关闭则改用内存存储（重启清空）"),
    "storage.db_path": _f("str", "数据库路径", "相对程序根目录"),
    "storage.media_dir": _f("str", "媒体目录", "收到的图片/表情包保存位置"),
    "storage.max_messages": _f("int", "最多保留消息数", "超出后裁剪最旧的", min=0, max=1000000),
    "storage.max_conversations": _f("int", "最多会话数", "", min=0, max=100000),
    "storage.messages_per_page": _f("int", "单次读取条数", "", min=20, max=5000),
    "storage.retention_days": _f("int", "消息保留天数", "0=永久保留", min=0, max=3650),
    "storage.trim_every_writes": _f("int", "每写入多少条裁剪一次", "", min=10, max=10000),
    "storage.group_members_refresh_hours": _f("int", "群成员缓存时长", "小时，到期自动刷新", min=1, max=720),

    # ---------------- receiver ----------------
    "receiver.reconnect_interval_seconds": _f("float", "重连间隔", "秒", min=1, max=600),
    "receiver.max_reconnect_interval_seconds": _f("float", "最大重连间隔", "秒（指数退避上限）", min=1, max=3600),
    "receiver.heartbeat_timeout_factor": _f("float", "心跳超时倍数", "超过 N 个心跳周期无响应则重连", min=1, max=20),

    # ---------------- scheduler ----------------
    "scheduler.enabled": _f("bool", "启用定时任务", "按北京时间 HH:MM 触发"),
    "scheduler.tasks": _f("scheduler_tasks", "定时任务列表", "时间 + 目标会话 + 内容"),

    # ---------------- panels ----------------
    "panels.enabled": _f("bool", "注册指令面板", "在 QQ 聊天界面显示可点指令"),
    "panels.remark": _f("str", "面板备注", ""),
    "panels.commands": _f("panel_commands", "指令列表", "type=command 需填名称与描述；type=link 填名称与链接"),
    "panels.c2c_target_type": _f("enum", "私聊面板范围", "", options=[["all", "所有用户"], ["specific", "指定用户"]]),
    "panels.c2c_openids": _f("list", "私聊面板指定用户", "每行一个 user openid"),
    "panels.group_target_type": _f("enum", "群聊面板范围", "", options=[["all", "所有群"], ["specific", "指定群"]]),
    "panels.group_openids": _f("list", "群聊面板指定群", "每行一个 group openid"),

    # ---------------- plugins ----------------
    "plugins.enabled": _f("bool", "启用插件系统", "关闭后不加载任何插件（全局设置）"),
    "plugins.dir": _f("str", "插件目录",
                      "相对程序根目录；里面放 AstrBot 插件目录（metadata.yaml + main.py）（全局设置）"),
    "plugins.enabled_names": _f("list", "本机器人启用的插件",
                                "每行一个插件名（插件管理页的名字）；留空=该机器人启用所有已启用插件"),
    "plugins.disabled_names": _f("list", "本机器人停用的插件",
                                 "每行一个插件名；该机器人不运行这些插件（优先级高于上面的白名单）"),

    # ---------------- cloud_sync ----------------
    "cloud_sync.enabled": _f("bool", "启用云同步", "把 data/ 下的用户数据同步到 Cloudflare D1"),
    "cloud_sync.account_id": _f("str", "Cloudflare 账户 ID", ""),
    "cloud_sync.database_id": _f("str", "D1 数据库 ID", ""),
    "cloud_sync.api_token": _f("secret", "Cloudflare API 令牌", "权限需包含 D1 编辑"),
    "cloud_sync.interval_seconds": _f("int", "同步间隔", "秒", min=30, max=86400),
    "cloud_sync.pull_on_start": _f("bool", "启动时拉取云端数据", "换机器恢复就靠它"),
    "cloud_sync.upload_logs": _f("bool", "日志也上云", "日志量大，默认关闭"),
    "cloud_sync.skip_secret_files": _f("bool", "含密钥的文件不上传",
                                       "上传前按字段名扫一遍：出现 api_key/secret/token/password "
                                       "等字段就跳过上传（本地文件不动）"),
    "cloud_sync.max_file_mb": _f("float", "单文件大小上限", "MB，超过不同步", min=0.1, max=200),
    "cloud_sync.tombstone_days": _f("int", "删除标记保留天数", "0=永久", min=0, max=3650),
    "cloud_sync.apply_remote_deletes": _f("bool", "跟随云端删除本地", "关闭则本地永不因云端删除而消失"),
    "cloud_sync.error_pause_minutes": _f("int", "连续失败暂停", "分钟；0=只能手动恢复", min=0, max=1440),
    "cloud_sync.startup_buffer": _f("bool", "启动写缓存", "首次同步完成前的改动先缓存在内存"),
    "cloud_sync.startup_buffer_minutes": _f("int", "写缓存等待上限", "分钟；0=一直等", min=0, max=120),
    "cloud_sync.startup_buffer_max_mb": _f("float", "写缓存容量上限", "MB", min=1, max=512),
    "cloud_sync.startup_buffer_keep_remote": _f("bool", "冲突时以云端为准", ""),

    # ---------------- logging ----------------
    "logging.level": _f("enum", "日志级别", "", options=[["DEBUG", "DEBUG"], ["INFO", "INFO"],
                                                        ["WARNING", "WARNING"], ["ERROR", "ERROR"]]),
    "logging.max_size_mb": _f("float", "单个日志文件上限", "MB，超出自动分割", min=0.5, max=500),
    "logging.console_color": _f("bool", "控制台彩色输出", "日志文件始终保持纯文本"),
    "logging.keep_days": _f("int", "日志保留天数", "0=不自动清理", min=0, max=3650),

    # ---------------- ui ----------------
    "ui.theme": _f("enum", "主题", "", options=[["auto", "跟随系统"], ["light", "浅色"], ["dark", "深色"]]),
    "ui.animation": _f("enum", "动画强度", "影响页面切换与消息出现的过渡效果",
                       options=[["full", "完整动画"], ["lite", "精简（省电）"], ["off", "关闭动画"]]),
    "ui.poll_interval_ms": _f("int", "消息轮询间隔", "毫秒", min=500, max=60000),
    "ui.message_bubbles": _f("bool", "气泡样式", "关闭则用平铺列表"),
    "ui.compact_mode": _f("bool", "紧凑模式", "缩小间距，一屏显示更多"),
    "ui.show_avatar": _f("bool", "显示头像", ""),
    "ui.image_lightbox": _f("bool", "图片点击放大", ""),

    # ---------------- security ----------------
    "security.admin_openids": _f("list", "管理员 openid", "每行一个，拥有机器人指令的管理权限"),
    "security.alert_enabled": _f("bool", "启用出错告警", ""),
    "security.alert_owner_openid": _f("str", "告警接收人 openid", "主人 openid"),

    # ---------------- context（兼容项） ----------------
    "context.enabled": _f("bool", "启用上下文（兼容项）", "与「回复与消息」里的开关同步"),
    "context.max_history": _f("int", "上下文条数（兼容项）", "与「回复与消息」里的同步", min=0, max=200),
}

# 需要按“每行一项”解析成列表的字段
LIST_FIELDS = {"filters.sensitive_list", "panels.c2c_openids", "panels.group_openids",
               "security.admin_openids"}
# 需要按 JSON 解析的字段
JSON_FIELDS = {"filters.exact_match_responses", "filters.fuzzy_match_responses"}
# 特殊控件（由前端专用渲染器处理）
BOTS_FIELD = "bots"
TASKS_FIELD = "scheduler.tasks"
COMMANDS_FIELD = "panels.commands"
# 需要重启才生效的字段（连接类、端口、存储引擎）
RESTART_REQUIRED = {
    "web.host", "web.port", "web.debug",
    "bots", "storage.enabled", "storage.db_path", "storage.media_dir",
    "receiver.reconnect_interval_seconds", "receiver.max_reconnect_interval_seconds",
}
# 密码类字段：网页留空=不修改
SECRET_FIELDS = {"web.token", "ai.api_key", "cloud_sync.api_token", "security.alert_owner_openid"}
# 密钥类字段（用于打码显示）
MASKED_FIELDS = {"web.token", "ai.api_key", "cloud_sync.api_token"}
SECRET_MASK = "********"


# 每个机器人自己的设置（在「设置」页里，这些分组作用于**当前选中的机器人**）
# 未列出的分组都是全局设置（网页后台 / 存储 / 日志 / 界面 / 云同步 …）
PER_BOT_SECTIONS = ("ai", "reply", "filters", "features", "send", "scheduler", "panels", "plugins")

# 分组虽然按机器人隔离，但这两项是"整台程序"的开关/目录：按机器人覆盖它们没有意义
# （插件只加载一次），所以在上面统一判定的基础上单独放行成全局项。
GLOBAL_ONLY_PATHS = {"plugins.enabled", "plugins.dir"}

# 各分组在「设置」页里的作用域说明
SCOPE_HINTS: Dict[str, str] = {
    "bots": "这里配置每个机器人的账号与连接参数；选中的机器人会作用于整个后台",
    "ai": "以下设置只作用于**当前选中的机器人**",
    "reply": "以下设置只作用于**当前选中的机器人**",
    "filters": "以下设置只作用于**当前选中的机器人**",
    "features": "以下设置只作用于**当前选中的机器人**",
    "send": "以下设置只作用于**当前选中的机器人**",
    "scheduler": "定时任务只由**当前选中的机器人**执行",
    "panels": "指令面板只注册到**当前选中的机器人**",
    "web": "全局设置（所有机器人共用）",
    "storage": "全局设置（所有机器人共用）",
    "receiver": "全局设置（所有机器人共用）",
    "plugins": "「本机器人启用/停用的插件」只作用于**当前选中的机器人**；"
               "插件总开关与插件目录是全局设置（所有机器人共用）",
    "cloud": "全局设置（所有机器人共用）",
    "security": "全局设置（所有机器人共用；管理员按 openid 判定）",
    "logging": "全局设置（所有机器人共用）",
    "ui": "全局设置（所有机器人共用）",
    "context": "只作用于**当前选中的机器人**",
}


def per_bot_paths() -> set:
    """所有属于"每个机器人自己"的配置路径。"""
    return {path for path in FIELDS if is_per_bot(path)}


def is_per_bot(path: str) -> bool:
    path = str(path or "")
    if path in GLOBAL_ONLY_PATHS:
        return False
    return path.split(".")[0] in PER_BOT_SECTIONS


def default_config() -> Dict[str, Any]:
    import copy
    return copy.deepcopy(DEFAULT_CONFIG)


def field_meta() -> Dict[str, Dict[str, Any]]:
    return {path: dict(meta) for path, meta in FIELDS.items()}


def section_labels() -> Dict[str, Dict[str, str]]:
    return {key: dict(value) for key, value in SECTION_LABELS.items()}


def flatten(data: Any, prefix: str = "") -> Dict[str, Any]:
    """把嵌套配置拍平成 {"a.b.c": value}，跳过以 `_` 开头的说明键。"""
    flat: Dict[str, Any] = {}
    for key, value in (data or {}).items():
        if str(key).startswith("_"):
            continue
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, path + "."))
        elif isinstance(value, list) and key != "bots":
            flat[path] = value
        else:
            flat[path] = value
    return flat


def field_paths() -> List[str]:
    """所有可编辑的配置路径（列表项 bots 单独处理）。"""
    return sorted(FIELDS.keys())


# 每个分组里展示哪些字段（顺序即页面顺序）
SECTION_FIELDS: Dict[str, List[str]] = {
    "bots": ["bots"],
    "web": ["web.host", "web.port", "web.token", "web.public_base_url", "web.debug",
            "web.poll_interval_ms", "web.page_size", "web.log_access_requests",
            "web.log_polling_requests"],
    "ai": ["ai.enabled", "ai.api_key", "ai.base_url", "ai.model", "ai.temperature", "ai.max_tokens",
           "ai.timeout_seconds", "ai.no_ai_reply", "ai.system_prompt",
           "ai.vision_enabled", "ai.vision_prompt"],
    "reply": ["reply.require_mention", "reply.context_enabled", "reply.max_history",
              "reply.max_segment_length", "reply.max_queue_size", "reply.filter_meaningless",
              "reply.strip_markdown", "reply.quote_reply"],
    "filters": ["filters.keywords_enabled", "filters.exact_match_responses",
                "filters.fuzzy_match_responses", "filters.sensitive_enabled",
                "filters.sensitive_list", "filters.sensitive_replacement",
                "filters.sensitive_block_input", "filters.rate_limit_enabled",
                "filters.rate_limit_seconds"],
    "features": ["features.save_received_media", "features.media_download_timeout",
                 "features.group_management",
                 "features.auto_reply_in_group", "features.auto_reply_in_private",
                 "features.notify_on_error"],
    "send": ["send.reply_style", "send.message_max_length", "send.max_file_mb", "send.max_image_mb",
             "send.max_retries", "send.retry_backoff_factor", "send.request_timeout_seconds",
             "send.token_refresh_buffer_seconds", "send.upload_keep_days"],
    "storage": ["storage.enabled", "storage.db_path", "storage.media_dir", "storage.max_messages",
                "storage.max_conversations", "storage.messages_per_page", "storage.retention_days",
                "storage.trim_every_writes", "storage.group_members_refresh_hours"],
    "receiver": ["receiver.reconnect_interval_seconds", "receiver.max_reconnect_interval_seconds",
                 "receiver.heartbeat_timeout_factor"],
    "scheduler": ["scheduler.enabled", "scheduler.tasks"],
    "panels": ["panels.enabled", "panels.remark", "panels.commands", "panels.c2c_target_type",
               "panels.c2c_openids", "panels.group_target_type", "panels.group_openids"],
    "plugins": ["plugins.enabled", "plugins.enabled_names", "plugins.disabled_names",
                "plugins.dir"],
    "cloud": ["cloud_sync.enabled", "cloud_sync.account_id", "cloud_sync.database_id",
              "cloud_sync.api_token", "cloud_sync.interval_seconds", "cloud_sync.pull_on_start",
              "cloud_sync.upload_logs", "cloud_sync.skip_secret_files", "cloud_sync.max_file_mb",
              "cloud_sync.tombstone_days",
              "cloud_sync.apply_remote_deletes", "cloud_sync.error_pause_minutes",
              "cloud_sync.startup_buffer", "cloud_sync.startup_buffer_minutes",
              "cloud_sync.startup_buffer_max_mb", "cloud_sync.startup_buffer_keep_remote"],
    "security": ["security.admin_openids", "security.alert_enabled",
                 "security.alert_owner_openid"],
    "logging": ["logging.level", "logging.max_size_mb", "logging.console_color",
                "logging.keep_days"],
    "ui": ["ui.theme", "ui.animation", "ui.poll_interval_ms", "ui.message_bubbles",
           "ui.compact_mode", "ui.show_avatar", "ui.image_lightbox"],
    "context": ["context.enabled", "context.max_history"],
}

SECTION_ORDER = ["bots", "web", "ai", "reply", "filters", "features", "send", "storage",
                 "receiver", "scheduler", "panels", "plugins", "cloud", "security",
                 "logging", "ui", "context"]


def sections_payload() -> List[Dict[str, Any]]:
    """给设置页的分组结构：[{key, title, icon, tip, fields, per_bot}]"""
    out = []
    for key in SECTION_ORDER:
        fields = [path for path in SECTION_FIELDS.get(key, []) if path in FIELDS]
        if not fields:
            continue
        info = SECTION_LABELS.get(key, {})
        out.append({
            "key": key,
            "title": info.get("label", key),
            "icon": info.get("icon", ""),
            "tip": info.get("desc", ""),
            "scope_hint": SCOPE_HINTS.get(key, ""),
            # 只要分组里有"按机器人"的字段就算按机器人隔离（plugins 里两项是全局的，
            # 但它们只是同一分组下的例外，见 GLOBAL_ONLY_PATHS）
            "per_bot": any(is_per_bot(path) for path in fields),
            "fields": fields,
        })
    return out
