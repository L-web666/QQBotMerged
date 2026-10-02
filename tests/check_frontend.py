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

    # ---- 聊天页：@ 提及已下线 / 去掉转发图片 ----
    mention_problems = []
    if "data-act=\"forward\"" in chat_js or "forwardImage" in chat_js:
        mention_problems.append("消息操作框里还留着「转发图片到其他会话」")
    for gone in ("mentionBtn", "mentionPicker", "mentionList", "insertMention",
                 "toggleMentionPicker"):
        if f"'{gone}'" in chat_js or f"function {gone}(" in chat_js:
            mention_problems.append(f"@ 提及相关代码没删干净：{gone}")
        if f'id="{gone}"' in html:
            mention_problems.append(f"模板里还有 {gone}")
    if "/api/groups/members" in chat_js:
        mention_problems.append("还在调用只给 @ 选择器用的群成员接口")
    if "qqbot-at-user" not in chat_js:
        mention_problems.append("收到的 @ 标记不再渲染成可读标签")
    print("\n聊天页 @ 相关：")
    if mention_problems:
        print("❌ " + "；".join(mention_problems))
        FAILED.append(f"@ 功能清理不彻底：{mention_problems}")
    else:
        print("✅ @ 发送功能已移除；收到的 @ 标记仍会显示成可读标签；转发图片入口已删除")

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

    print("\n" + "=" * 68)
    if FAILED:
        print(f"  校验未通过：{len(FAILED)} 类问题")
        return 1
    print("  校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
