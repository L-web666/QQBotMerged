# -*- coding: utf-8 -*-
"""前端契约校验（离线、无浏览器）

检查三件事，防止"点开页面才发现取不到元素"这类哑巴错误：
1. JS 里引用的元素 id（Util.el('xxx') / getElementById('xxx')）是否都存在于模板中；
2. 模板的 id 是否有 JS 从未使用（提示冗余，仅供参考）；
3. 设置页的字段类型是否都有对应的渲染分支（config_schema 的 type ↔ settings.js 的控件）。
"""

import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(BASE)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

TEMPLATE = os.path.join(PROJECT, "web", "templates", "index.html")
JS_DIR = os.path.join(PROJECT, "web", "static", "js")

from core import config_schema as schema   # noqa: E402

FAILED = []


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def main():
    html = read(TEMPLATE)
    template_ids = set(re.findall(r'\bid="([^"]+)"', html))

    used_ids = set()
    for name in sorted(os.listdir(JS_DIR)):
        if not name.endswith(".js"):
            continue
        source = read(os.path.join(JS_DIR, name))
        used_ids |= set(re.findall(r"U\.el\(\s*'([^']+)'\s*\)", source))
        used_ids |= set(re.findall(r"U\.el\(\s*\"([^\"]+)\"\s*\)", source))
        used_ids |= set(re.findall(r"getElementById\(\s*'([^']+)'\s*\)", source))
        # 查询选择器里的 #id
        used_ids |= set(re.findall(r"querySelector(?:All)?\(\s*'#([A-Za-z0-9_\-]+)", source))

    print("=" * 68)
    print("  前端契约校验")
    print("=" * 68)
    print(f"模板 id：{len(template_ids)} 个 · JS 引用 id：{len(used_ids)} 个")

    # 动态创建的 id（由 JS 注入 DOM 后再自己取）白名单
    dynamic_ids = {"rmPending", "muteMinutes", "clearConfirmChk", "clearAllChk",
                   "ctxAllChk", "mediaDays", "gmMinutes",
                   "aliasInput", "aliasSave", "pluginsForce",
                   # 群管理详情卡片与"禁言方式"选择器由 JS 渲染后才存在
                   "muteModeSel", "muteMinutesInput",
                   # 群管理详情卡片由 JS 渲染后才存在
                   "grpRefreshName", "grpRefreshMembers", "grpOpenChat",
                   "grpRefreshMutes", "grpReloadMsgs",
                   "grpSaveSettings", "grpResetSettings"}
    missing = sorted(
        used_ids - template_ids
        - dynamic_ids
        # 设置页动态生成的控件 id/属性名不算
        - {"groupDetail", "settingsForm"}
    )
    # 过滤掉明显是属性选择器/数据属性的误报
    missing = [name for name in missing if not name.startswith("data-")]

    if missing:
        print("\n❌ JS 引用了模板里不存在的元素 id：")
        for name in missing:
            print("   -", name)
        FAILED.append(f"缺失元素 id：{missing}")
    else:
        print("\n✅ JS 引用的元素 id 全部存在于模板中")

    unused = sorted(template_ids - used_ids)
    if unused:
        print(f"\nℹ️  模板中未被 JS 直接引用的 id（{len(unused)} 个，可能由 CSS/布局使用）：")
        print("   ", "、".join(unused))

    # ---- 设置页控件覆盖 ----
    settings_js = read(os.path.join(JS_DIR, "settings.js"))
    handled = set(re.findall(r"type === '([a-z_]+)'", settings_js))
    # str 由默认分支（普通文本框）处理
    handled.add("str")
    declared = {meta["type"] for meta in schema.field_meta().values()}
    unhandled = sorted(declared - handled)
    print(f"\n设置页字段类型：声明 {len(declared)} 种 -> {sorted(declared)}")
    if unhandled:
        print(f"❌ 以下类型在 settings.js 里没有渲染分支：{unhandled}")
        FAILED.append(f"未处理的字段类型：{unhandled}")
    else:
        print("✅ 所有字段类型都有渲染分支")

    # ---- 配置分组完整性 ----
    payload_sections = schema.sections_payload()
    section_fields = set()
    for section in payload_sections:
        section_fields |= set(section["fields"])
    declared_fields = set(schema.field_meta().keys())
    orphan = sorted(declared_fields - section_fields - {"bots"})
    if orphan:
        print(f"\n❌ 这些配置项没有出现在设置页的任何分组里：{orphan}")
        FAILED.append(f"未分组的配置项：{orphan}")
    else:
        print("✅ 所有配置项都已归入设置页分组")

    # ---- 切换机器人后，按机器人的页面必须自动刷新 ----
    def has_onchange(source: str, call: str) -> bool:
        """页面里注册了 U.ActiveBot.onChange(...)，且回调里调用了指定函数。"""
        match = re.search(r"U\.ActiveBot\.onChange\(function\s*\([^)]*\)\s*\{(.*?)\n\s*\}\)",
                          source, re.S)
        return bool(match) and call in match.group(1)

    admin_js = read(os.path.join(JS_DIR, "admin.js"))
    groups_js = read(os.path.join(JS_DIR, "groups.js"))
    chat_js = read(os.path.join(JS_DIR, "chat.js"))
    app_js = read(os.path.join(JS_DIR, "app.js"))
    refresh_problems = []
    if not has_onchange(admin_js, "loadContext("):
        refresh_problems.append("上下文页（admin.js → loadContext）")
    if not has_onchange(admin_js, "loadPanels("):
        refresh_problems.append("指令面板页（admin.js → loadPanels）")
    if not has_onchange(groups_js, "load("):
        refresh_problems.append("群管理页（groups.js → load）")
    if "onBotChanged" not in chat_js or "onBotChanged" not in app_js:
        refresh_problems.append("聊天页（chat.js onBotChanged）")
    if not re.search(r"data\.active\s*!==\s*active", app_js):
        refresh_problems.append("浏览器与服务端的当前机器人保持一致（app.js）")
    print("\n切换机器人后的自动刷新：")
    if refresh_problems:
        print("❌ 这些页面切换机器人后不会自动刷新：" + "、".join(refresh_problems))
        FAILED.append(f"切换机器人后未自动刷新：{refresh_problems}")
    else:
        print("✅ 上下文 / 群管理 / 聊天 都会随机器人切换自动刷新")

    # ---- 指令面板页必须带机器人维度 ----
    panel_problems = []
    if not re.search(r"U\.qs\(\s*'/api/admin/panels'\s*\)", admin_js):
        panel_problems.append("面板列表请求没带机器人（应该写成 U.qs('/api/admin/panels')）")
    delete_match = re.search(r"postJSON\(\s*'/api/admin/panels/delete'\s*,\s*\{([^}]*)\}",
                             admin_js, re.S)
    if not delete_match:
        panel_problems.append("找不到 /api/admin/panels/delete 的调用")
    elif "bot_id" not in delete_match.group(1):
        panel_problems.append("删除面板时没有显式带上 bot_id（容易删到别的机器人）")
    print("\n指令面板页的机器人隔离：")
    if panel_problems:
        print("❌ " + "；".join(panel_problems))
        FAILED.append(f"指令面板页没有按机器人隔离：{panel_problems}")
    else:
        print("✅ 面板列表 / 删除都带上了当前机器人，切换机器人后会重新加载")

    # ---- getJSON 的非 2xx 错误必须带出后端文案与响应体 ----
    util_js = read(os.path.join(JS_DIR, "util.js"))
    getjson_match = re.search(r"function getJSON\(path\)\s*\{(.*?)\n  \}", util_js, re.S)
    getjson_body = getjson_match.group(1) if getjson_match else ""
    net_problems = []
    if not getjson_match:
        net_problems.append("util.js 里找不到 getJSON 函数")
    else:
        if "error.payload = data" not in getjson_body:
            net_problems.append("getJSON 没把响应体挂到 error.payload")
        if "data && data.message ? data.message" not in getjson_body:
            net_problems.append("getJSON 的报错文案没有优先用后端的 data.message")
        if "访问令牌无效" not in getjson_body:
            net_problems.append("getJSON 丢了 401 的「访问令牌无效」专门提示")
    print("\ngetJSON 的错误信息：")
    if net_problems:
        print("❌ " + "；".join(net_problems))
        FAILED.append(f"getJSON 错误信息不完整：{net_problems}")
    else:
        print("✅ 非 2xx 优先抛后端 data.message，响应体放在 error.payload（401 专门提示保留）")

    # ---- 插件页必须能按机器人启用不同的插件 ----
    plugin_bot_problems = []
    if not has_onchange(admin_js, "loadPlugins("):
        plugin_bot_problems.append("切换机器人后没有重新加载插件列表（admin.js → loadPlugins）")
    if not re.search(r"U\.qs\(\s*'/api/admin/plugins'\s*\)", admin_js):
        plugin_bot_problems.append("插件列表请求没带机器人（应该写成 U.qs('/api/admin/plugins')）")
    if "data-plugin-bot" not in admin_js:
        plugin_bot_problems.append("插件表格里没有「当前机器人」勾选框（data-plugin-bot）")
    toggle_match = re.search(r"postJSON\(\s*'/api/admin/plugins/bot'\s*,\s*\{([^}]*)\}",
                             admin_js, re.S)
    if not toggle_match:
        plugin_bot_problems.append("找不到 /api/admin/plugins/bot 的调用")
    elif "bot_id" not in toggle_match.group(1) or "allowed" not in toggle_match.group(1):
        plugin_bot_problems.append("按机器人保存时没同时带上 bot_id 与 allowed（全量插件名列表）")
    if "pluginsBotScope" not in html:
        plugin_bot_problems.append("模板里没有 #pluginsBotScope（勾选框的说明行）")
    print("\n插件页的机器人隔离：")
    if plugin_bot_problems:
        print("❌ " + "；".join(plugin_bot_problems))
        FAILED.append(f"插件页没有按机器人隔离：{plugin_bot_problems}")
    else:
        print("✅ 插件页每个插件都有「当前机器人」勾选框，切换机器人后重新加载")

    # ---- 全局命名空间要被"锁成只读"，防止被别的脚本整个替换掉 ----
    global_problems = []
    if "function protectGlobal" not in util_js or "__qbmProtect" not in util_js:
        global_problems.append("util.js 里没有 protectGlobal / __qbmProtect 助手")
    for fname, gname in (("effects.js", "Effects"), ("chat.js", "Chat"),
                         ("settings.js", "Settings"), ("admin.js", "Admin"),
                         ("groups.js", "Groups"), ("app.js", "App")):
        source = read(os.path.join(JS_DIR, fname))
        if "__qbmProtect" not in source or f"'{gname}'" not in source:
            global_problems.append(f"{fname} 没有保护全局 {gname}")
    if "Object.freeze" in admin_js:
        # 这些命名空间里有 `Admin.pluginChanges = {}` 这类整字段赋值，冻结会直接打断页面
        global_problems.append("admin.js 里出现了 Object.freeze（会让整字段赋值失效）")
    print("\n全局命名空间保护：")
    if global_problems:
        print("❌ " + "；".join(global_problems))
        FAILED.append(f"全局命名空间没有保护：{global_problems}")
    else:
        print("✅ Util/Effects/Chat/Settings/Admin/Groups/App 都锁成只读全局（内部字段仍可改）")

    # ---- 插件页要展示安全扫描结果（core/plugin_safety.py） ----
    safety_problems = []
    if "plugin.safety" not in admin_js:
        safety_problems.append("插件行没有用到后端返回的 plugin.safety（安全扫描结果）")
    if "diag.safety" not in admin_js:
        safety_problems.append("插件页顶部没有展示安全扫描汇总（diag.safety）")
    if "安全提示" not in admin_js:
        safety_problems.append("没有面向用户的「安全提示」字样，光有字段用户看不懂")
    print("\n插件页的安全扫描提示：")
    if safety_problems:
        print("❌ " + "；".join(safety_problems))
        FAILED.append(f"插件页没有展示安全扫描结果：{safety_problems}")
    else:
        print("✅ 插件行与页顶都会展示 AST 安全扫描结果（高风险/中风险有徽章，可在 title 看命中项）")

    # ---- 插件页：保存/刷新后必须重算"待保存"提示 ----
    pending_problems = []
    if "updatePluginPending()" not in admin_js:
        pending_problems.append("插件页没有 updatePluginPending")
    else:
        for func_name in ("loadPlugins", "savePlugins", "reloadPlugins"):
            match = re.search(r"function %s\(\)\s*\{(.*?)\n  \}" % func_name, admin_js, re.S)
            body = match.group(1) if match else ""
            if "updatePluginPending()" not in body:
                pending_problems.append(f"{func_name}() 之后没刷新待保存提示")
    print("\n插件页「待保存」提示：")
    if pending_problems:
        print("❌ " + "、".join(pending_problems))
        FAILED.append(f"插件页待保存提示不会刷新：{pending_problems}")
    else:
        print("✅ 保存 / 重新加载插件后，「有 N 项修改待保存」会立刻清掉")

    # ---- 聊天页：群 @ 某人（官方 qqbot-at-user 标签）+ 转发图片入口仍然没有 ----
    mention_problems = []
    if "data-act=\"forward\"" in chat_js or "forwardImage" in chat_js:
        mention_problems.append("消息操作框里还留着「转发图片到其他会话」")
    if 'id="mentionPicker"' not in html:
        mention_problems.append("模板里没有 id=\"mentionPicker\"（群 @ 选择器）")
    if 'id="mentionHint"' not in html:
        mention_problems.append("模板里没有 id=\"mentionHint\"（「将 @ 谁」提示）")
    if "<qqbot-at-user" not in chat_js:
        mention_problems.append("chat.js 里没有官方 @ 标签 <qqbot-at-user id=\"…\" />")
    for func in ("function insertMention(", "function loadMentionCandidates(",
                 "function atUserIds("):
        if func not in chat_js:
            mention_problems.append(f"chat.js 缺少 {func}) 相关实现")
    if "loadMentionCandidates();" not in chat_js:
        mention_problems.append("切换会话后没有刷新 @ 候选名单")
    if "/api/chat/mentions" not in chat_js:
        mention_problems.append("chat.js 没有调用 /api/chat/mentions")
    _server_src = read(os.path.join(PROJECT, "web", "server.py"))
    if "/api/chat/mentions" not in _server_src:
        mention_problems.append("web/server.py 里没有 /api/chat/mentions 路由")
    if "qqbot-at-user" not in chat_js:
        mention_problems.append("收到的 @ 标记不再渲染成可读标签")
    print("\n聊天页 @ 提及（群 @ 某人）：")
    if mention_problems:
        print("❌ " + "；".join(mention_problems))
        FAILED.append(f"群 @ 功能没接好：{mention_problems}")
    else:
        print("✅ 群 @ 选择器会插入官方 qqbot-at-user 标签、切会话刷新候选名单；"
              "收到的 @ 标记仍显示成可读标签；转发图片入口仍是移除状态")

    # ---- 字段类型与默认值一致性 ----
    mismatches = []
    for path, meta in schema.field_meta().items():
        if path == "bots":
            continue
        node = schema.DEFAULT_CONFIG
        try:
            for part in path.split("."):
                node = node[part]
        except (KeyError, TypeError):
            mismatches.append(f"{path}（默认配置里不存在）")
            continue
        kind = meta["type"]
        ok = True
        if kind == "bool":
            ok = isinstance(node, bool)
        elif kind == "int":
            ok = isinstance(node, int) and not isinstance(node, bool)
        elif kind == "float":
            ok = isinstance(node, (int, float)) and not isinstance(node, bool)
        elif kind in ("list",):
            ok = isinstance(node, list)
        elif kind in ("dict",):
            ok = isinstance(node, dict)
        elif kind in ("str", "secret", "textarea", "enum"):
            ok = isinstance(node, str)
        elif kind in ("scheduler_tasks", "panel_commands"):
            ok = isinstance(node, list)
        if not ok:
            mismatches.append(f"{path}: 类型声明 {kind} 与默认值 {type(node).__name__} 不符")
    if mismatches:
        print("\n❌ 字段类型与默认值不一致：")
        for item in mismatches:
            print("   -", item)
        FAILED.append(f"类型不一致 {len(mismatches)} 处")
    else:
        print("✅ 字段类型与默认值一致")

    # ---- Markdown / 纯文本切换（发送格式） ----
    format_problems = []
    if 'id="sendFormat"' not in html:
        format_problems.append("模板 index.html 里没有 id=\"sendFormat\"（格式选择控件）")
    if 'value="markdown"' not in html:
        format_problems.append("模板的格式选择里没有 value=\"markdown\" 选项")
    if 'value="text"' not in html:
        format_problems.append("模板的格式选择里没有 value=\"text\" 选项")
    if "body.format" not in chat_js:
        format_problems.append("chat.js 发送文本时没有带上 body.format")
    if "qbm-send-format" not in chat_js:
        format_problems.append("chat.js 没有把格式选择记忆到 localStorage（键 qbm-send-format）")
    if "Markdown 文本发送成功" not in chat_js:
        format_problems.append("chat.js 里没有 markdown 的成功提示「Markdown 文本发送成功」")
    if "文本发送成功" not in chat_js:
        format_problems.append("chat.js 里丢了纯文本的成功提示「文本发送成功」")

    server_py = read(os.path.join(PROJECT, "web", "server.py"))
    send_match = re.search(r"def chat_send\(\):(.*?)\n    @app\.route", server_py, re.S)
    send_body = send_match.group(1) if send_match else ""
    if not send_match:
        format_problems.append("web/server.py 里找不到 /api/chat/send 的 chat_send 接口")
    else:
        if 'field("format")' not in send_body:
            format_problems.append("chat_send 没有读取 format 字段")
        if "format 只能是 text 或 markdown" not in send_body:
            format_problems.append("chat_send 没有对非法 format 返回 400 文案"
                                   "（format 只能是 text 或 markdown）")
        if "fmt=send_format" not in send_body:
            format_problems.append("chat_send 的文本分支没有把格式传给 rt.send_text(fmt=...)")
        if '"format"' not in send_body:
            format_problems.append("chat_send 的响应体里没有 format 字段")
        if '_note' not in send_body:
            format_problems.append("chat_send 没有把 info.get('_note') 放进 note（降级说明）")
    print("\nMarkdown / 纯文本切换（发送格式）：")
    if format_problems:
        print("❌ " + "；".join(format_problems))
        FAILED.append(f"发送格式切换不完整：{format_problems}")
    else:
        print("✅ 模板有 #sendFormat（纯文本/Markdown）；chat.js 发文本带 format 并记住选择；"
              "server.py 的 /api/chat/send 校验 format 并回传实际格式")

    # ---- 指令标签（qqbot-cmd-enter / qqbot-cmd-input，仅 Markdown）----
    text_py = read(os.path.join(PROJECT, "core", "message_text.py"))
    msg_py = read(os.path.join(PROJECT, "core", "runtime_pkg", "messaging.py"))
    cmd_problems = []
    for marker in ('id="cmdBtn"', 'id="cmdPanel"', 'id="cmdText"', 'id="cmdShow"',
                   'id="cmdReference"', 'id="cmdInsert"'):
        if marker not in html:
            cmd_problems.append(f"模板里没有 {marker}（指令标签面板）")
    for func in ("function buildCmdEnter(", "function buildCmdInput(", "function insertCmdTag(",
                 "function cmdTagCount(", "function toggleCmdPanel("):
        if func not in chat_js:
            cmd_problems.append(f"chat.js 缺少 {func}) 相关实现")
    if "qqbot-cmd-enter" not in chat_js or "qqbot-cmd-input" not in chat_js:
        cmd_problems.append("chat.js 里没有官方指令标签 qqbot-cmd-enter / qqbot-cmd-input")
    if "encodeURIComponent" not in chat_js:
        cmd_problems.append("chat.js 插入指令标签前没有对 text/show 做 urlencode")
    if 'class="cmd-tag"' not in chat_js:
        cmd_problems.append("收到的指令标签不再渲染成可读标签（class=\"cmd-tag\"）")
    for func in ("def build_cmd_enter(", "def build_cmd_input(", "def read_cmd_tag(",
                 "def cmd_markup_to_text("):
        if func not in text_py:
            cmd_problems.append(f"core/message_text.py 缺少 {func}) 相关实现")
    if "CMD_TEXT_MAX = 100" not in text_py:
        cmd_problems.append("core/message_text.py 里没有官方 100 字符上限 CMD_TEXT_MAX = 100")
    print("\n指令标签（qqbot-cmd-enter / qqbot-cmd-input，仅 Markdown）：")
    if cmd_problems:
        print("❌ " + "；".join(cmd_problems))
        FAILED.append(f"指令标签没接好：{cmd_problems}")
    else:
        print("✅ 发送框有「⌘ 指令」面板（回车指令/参数指令 + 显示文本 + 引用）；"
              "插入前 urlencode 并按 100 字符上限校验；收到的指令标签渲染成可读标签")

    # ---- @/指令标签自动改用 Markdown ----
    auto_problems = []
    if "markdownOnlyParts" not in chat_js or "syncFormatWithContent" not in chat_js:
        auto_problems.append("chat.js 没有「正文里有 @/指令标签就自动切 Markdown」的实现")
    if "setFormat('markdown')" not in chat_js:
        auto_problems.append("chat.js 插入 @/指令标签后没有真的把格式切成 Markdown")
    if "markdown_only_tags(" not in msg_py:
        auto_problems.append("Runtime.send_text 没有调用 markdown_only_tags（后端没兜底）")
    if "AUTO_MARKDOWN_NOTE" not in msg_py:
        auto_problems.append("Runtime.send_text 自动改格式时没有给用户说明（AUTO_MARKDOWN_NOTE）")
    if "def markdown_only_tags(" not in text_py:
        auto_problems.append("core/message_text.py 缺少 markdown_only_tags()（判定哪些正文必须用 markdown）")
    if "auto_format" not in send_body:
        auto_problems.append("chat_send 的响应里没有 auto_format（页面无法提示自动改了格式）")
    print("\n@ / 指令标签自动改用 Markdown：")
    if auto_problems:
        print("❌ " + "；".join(auto_problems))
        FAILED.append(f"自动改用 Markdown 没接好：{auto_problems}")
    else:
        print("✅ 前端插入 @/指令标签即切 Markdown；后端 send_text 在纯文本请求里检测到"
              "@某人/指令标签时也会自动改用 Markdown，并把原因写进 note")

    # ---- 群管理：禁言方式不能被群列表自动刷新覆盖 ----
    # 真实 bug：`Groups.muteMode = data.mute_mode || Groups.muteMode` 让 15 秒一次的
    # 群列表刷新把用户选的「官方接口」改回后端默认的 local，点禁言就变成本地禁言。
    mute_problems = []
    # 只看真正的代码行（注释里会出现被修掉的写法，不能把注释当实现）
    code_lines = [line.strip() for line in groups_js.splitlines()
                  if not line.strip().startswith(("//", "*", "/*"))]
    code_text = "\n".join(code_lines)
    # 必须是"未受保护"的写法：`Groups.muteMode = data.mute_mode || ...`
    # 带 muteModeReady 判断的赋值是正确实现，不算问题
    if re.search(r"Groups\.muteMode\s*=\s*data\.mute_mode\s*\|\|", code_text):
        mute_problems.append("groups.js 的 load() 仍在无条件覆盖 Groups.muteMode"
                             "（群列表自动刷新会把用户选的禁言方式改回默认）")
    if "muteModeReady" not in code_text:
        mute_problems.append("groups.js 没有 muteModeReady 标记（无法区分"
                             "「用户已选」与「服务端默认值」）")
    if "Groups.muteModeReady = true" not in code_text:
        mute_problems.append("用户改禁言方式时没有置位 muteModeReady")
    if "mode: mode" not in code_text:
        mute_problems.append("muteMember 发 /api/groups/mute 时没有带 mode")
    server_text = read(os.path.join(PROJECT, "web", "server.py"))
    if "mode=mode" not in server_text:
        mute_problems.append("server.py 的 /api/groups/mute 没有把 mode 透传给群管理")
    print("\n群管理的禁言方式：")
    if mute_problems:
        print("❌ " + "；".join(mute_problems))
        FAILED.append(f"禁言方式会被自动刷新覆盖：{mute_problems}")
    else:
        print("✅ 用户选的禁言方式不会被群列表自动刷新覆盖；请求里带 mode 并透传到群管理")

    # ---- 运行状态页：内置 AI 要跟着机器人变，且不再显示版本项 ----
    status_problems = []
    admin_code = "\n".join(line.strip() for line in admin_js.splitlines()
                           if not line.strip().startswith(("//", "*", "/*")))
    if "kv('版本'" in admin_code or 'kv("版本"' in admin_code:
        status_problems.append("运行状态页还在显示「版本」这一项（用户要求去掉）")
    if "data.ai.scope === 'bot'" in admin_code:
        status_problems.append("内置 AI 还用整体状态（scope=bot）显示，切换机器人不会变化")
    if "data.ai.bot_name" not in admin_code:
        status_problems.append("内置 AI 没有显示当前机器人（data.ai.bot_name）")
    if "rt.status(current_bot_id())" not in server_text:
        status_problems.append("/api/admin/status 没把当前机器人传给 rt.status()，"
                               "「内置 AI」会与选中的机器人无关")
    app_js = read(os.path.join(JS_DIR, "app.js"))
    switch_body = (app_js.split("function switchBot", 1)[1].split("\n  }", 1)[0]
                   if "function switchBot" in app_js else "")
    if "loadStatus" not in switch_body:
        status_problems.append("切换机器人后没有重新拉取运行状态（切完不刷新）")
    print("\n运行状态页（内置 AI 跟机器人走 / 去掉版本项）：")
    if status_problems:
        print("❌ " + "；".join(status_problems))
        FAILED.append(f"运行状态页不符合要求：{status_problems}")
    else:
        print("✅ 内置 AI 按当前机器人显示（切换后重新拉取）；页面不再显示版本项")

    # ---- 群成员身份 + 群管理页"没更新就别播动画" ----
    detail_problems = []
    groups_code = "\n".join(line.strip() for line in groups_js.splitlines()
                            if not line.strip().startswith(("//", "*", "/*")))
    groups_py = read(os.path.join(PROJECT, "core", "group_manager.py"))
    if "_normalize_role" not in groups_py:
        detail_problems.append("core/group_manager.py 没有 _normalize_role（身份解析）")
    # 任何"默认身份"都不许写成 member：认不出来就该是 unknown
    member_defaults = re.findall(r'"role":\s*"member"[^,\n}]*', groups_py)
    if member_defaults:
        detail_problems.append("group_manager.py 仍把未知身份默认写成 \"member\""
                               "（页面会一律显示「成员」）：" + str(member_defaults[:2]))
    if "ROLE_UNKNOWN" not in groups_py:
        detail_problems.append("group_manager.py 没有 ROLE_UNKNOWN 兜底值")
    if "roleLabel(" not in groups_code:
        detail_problems.append("groups.js 没有 roleLabel（身份显示）")
    if "'成员')" in groups_code and "member.role === 'member'" not in groups_code:
        detail_problems.append("groups.js 仍把非 owner/admin 一律显示成「成员」")
    # 详情重绘必须受指纹控制，否则 20 秒一次的刷新会让卡片动画反复重播
    if "detailSignature" not in groups_code:
        detail_problems.append("groups.js 没有 detailSignature（详情没变就不该重绘）")
    if not re.search(r"if\s*\(\s*signature\s*!==\s*Groups\.detailSignature", groups_code):
        detail_problems.append("openGroup 没有用指纹判断是否重绘详情（动画会一直重播）")
    print("\n群成员身份 / 群管理页动画：")
    if detail_problems:
        print("❌ " + "；".join(detail_problems))
        FAILED.append(f"群成员身份或动画控制有问题：{detail_problems}")
    else:
        print("✅ 身份认不出来显示「未知」（不再一律「成员」）；"
              "详情内容没变就不重绘，卡片动画不会反复重播")

    print("\n" + "=" * 68)
    if FAILED:
        print(f"  校验未通过：{len(FAILED)} 类问题")
        return 1
    print("  校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
