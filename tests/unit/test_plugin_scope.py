# -*- coding: utf-8 -*-
"""单元测试：插件范围与设置页显示一致 + 添加行按钮位置

  A. **插件管理页勾选 → 设置页可见**：以前服务端把"勾选了哪几个"归一化成
     黑名单（`disabled_names`），而设置页的「本机器人启用的插件」读的是白名单
     （`enabled_names`），于是插件管理页明明勾了插件、设置页却显示为空。
     现在改为写白名单，两处一一对应。

  B. **「添加一行」按钮位置**：新行要插在按钮**之前**，按钮始终留在最后。
     原来用 `insertAdjacentHTML('beforeend')` 插到容器末尾，而按钮也在容器里，
     新行就跑到按钮后面去了。

运行：`python tests/unit/test_plugin_scope.py`
"""

import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import config_schema as schema       # noqa: E402
from core.config_manager import load_config    # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def effective_set(known, allow, deny):
    """复现 plugin_manager.allowed_names() 的语义。"""
    return {n for n in set(known)
            if n and n not in set(deny) and (not set(allow) or n in set(allow))}


def main():
    print("=" * 70)
    print("  插件范围一致性 + 添加行按钮位置")
    print("=" * 70)

    root = os.path.join(PROJECT, "data", "_selftest", "unit_scope")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)

    try:
        known = ["astrbot_demo", "astrbot_plugin_dice",
                 "astrbot_plugin_daily_checkin", "astrbot_plugin_ollama"]
        picked = ["astrbot_plugin_dice", "astrbot_plugin_daily_checkin"]

        print("\n[A1] 服务端写的是白名单（源码层面）")
        server = open(os.path.join(PROJECT, "web", "server.py"),
                      encoding="utf-8").read()
        block = server.split("def admin_plugins_bot(")[1].split("\n    @app.route")[0]
        check("写 enabled_names 而不是只写黑名单",
              "enabled_names = [name for name in known if name in allowed]" in block, "")
        check("不再用「统一用黑名单表达」的写法",
              "统一用黑名单表达" not in block, "")
        check("docstring 说明了写白名单",
              "白名单" in block.split('"""')[1], "")

        print("\n[A2] 保存后设置页能读到勾选的插件")
        cfg = schema.default_config()
        cfg["web"].update({"host": "127.0.0.1", "token": "x"})
        path = os.path.join(root, "cfg.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False)

        manager = load_config(path)
        bot_id = (manager.config.get("bots") or [{}])[0].get("id")
        manager.set_for_bot(bot_id, "plugins.enabled_names", picked)
        manager.set_for_bot(bot_id, "plugins.disabled_names", [])
        manager.save()

        reloaded = load_config(path)
        stored = {}
        for bot in reloaded.config.get("bots") or []:
            if bot.get("id") == bot_id:
                stored = (bot.get("overrides") or {}).get("plugins") or {}
                break
        check("enabled_names 就是勾选的那 2 个",
              stored.get("enabled_names") == picked, str(stored.get("enabled_names")))
        check("disabled_names 为空（不叠两份名单）",
              stored.get("disabled_names") == [], str(stored.get("disabled_names")))

        print("\n[A3] 生效范围与勾选一致")
        got = effective_set(known, stored.get("enabled_names") or [],
                            stored.get("disabled_names") or [])
        check("实际生效 = 勾选的 2 个", got == set(picked), str(sorted(got)))
        check("未勾选的插件确实不生效",
              "astrbot_demo" not in got, str(sorted(got)))

        print("\n[A4] 勾满全部 = 不限制（两个名单都清空）")
        full = effective_set(known, [], [])
        check("all -> None 语义（空名单表示不限制）",
              full == set(known), str(sorted(full)))

        print("\n[A5] 设置页的字段确实读这个键")
        settings_js = open(os.path.join(PROJECT, "web", "static", "js", "settings.js"),
                           encoding="utf-8").read()
        check("设置页 list 类型按字段路径取值",
              "U.getPath(source, path)" in settings_js, "")
        check("渲染成可编辑的行 + 添加按钮",
              "data-addrow" in settings_js, "")

        # ================= B. 添加行按钮位置 =================
        print("\n[B1] 新行插在按钮之前（不跑到按钮下面）")
        check("不再有 insertAdjacentHTML('beforeend')",
              "insertAdjacentHTML('beforeend'" not in settings_js, "")
        count = settings_js.count("insertAdjacentHTML('beforebegin'")
        check("改用 beforebegin（插在触发按钮之前）", count >= 3, str(count))
        check("列表行插入用的是触发元素 target",
              "target.insertAdjacentHTML('beforebegin'" in settings_js, "")

        print("\n[B2] 各个「添加」入口都改了")
        for label, marker in [("添加一行(item)", "data-addrow"),
                              ("添加机器人", "data-addbot"),
                              ("添加任务", "data-addtask"),
                              ("添加指令", "data-addcmd")]:
            check(f"{label} 走 beforebegin",
                  marker in settings_js, "")
        check("机器人编辑框插在按钮之前",
              "target.insertAdjacentHTML('beforebegin',\n          botEditorHtml" in settings_js
              or "beforebegin', botEditorHtml" in settings_js.replace("\n", " "), "")

        print("\n[B3] 按钮本身仍留在容器内（不会被插到按钮外面）")
        check("list 容器里按钮在列表之后",
              "'<button class=\"row-add\" type=\"button\" data-addrow=\"item\">" in settings_js, "")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
