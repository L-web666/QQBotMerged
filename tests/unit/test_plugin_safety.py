# -*- coding: utf-8 -*-
"""单元测试：插件安全扫描（`core/plugin_safety.py`）

分层约定（见 `PROGRESS.md`）：
- `tests/unit/`：快、只依赖被测模块本身；
- `tests/test_offline.py`：集成/端到端（跑全链路）；
- `tests/dom_test_*.js`：前端行为。

本文件不启动程序、不连 QQ、不读真实 `data/`（临时文件只写 `data/_selftest/`）。

运行：`python tests/unit/test_plugin_safety.py`
"""

import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import plugin_safety                                    # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def ids(findings):
    return sorted({item["id"] for item in findings})


def main():
    print("=" * 70)
    print("  单元测试：插件安全扫描")
    print("=" * 70)

    # ---------- 1. 规则命中 ----------
    print("\n[1] 规则命中（各种高危写法都能被静态扫出来）")
    risky = "\n".join([
        "import os, subprocess, threading, ctypes",
        "import requests",
        "def go():",
        "    os.system('whoami')",
        "    subprocess.run(['cmd', '/c', 'dir'])",
        "    open('config.json', 'r', encoding='utf-8').read()",
        "    requests.post('http://example.com', json={'api_key': 'x'})",
        "    threading.Thread(target=go).start()",
        "    time.sleep(1)",
        "    eval('1 + 1')",
        "    return pickle.loads(b'')",
    ])
    found = plugin_safety.scan_source(risky, "main.py")
    found_ids = ids(found)
    for rule_id, label in (("process", "执行系统命令"),
                           ("dynamic_code", "动态执行代码"),
                           ("secrets", "读配置/密钥文件"),
                           ("filesystem", "读写文件"),
                           ("network", "联网"),
                           ("thread", "自起线程"),
                           ("time_control", "休眠"),
                           ("reflection", "底层/反射")):
        check(f"{label} 会被标出来（规则 {rule_id}）", rule_id in found_ids, str(found_ids))
    check("命中的项都带等级/说明/证据（网页要展示）",
          all(item.get("level") in ("high", "medium", "info")
              and item.get("label") and item.get("hint") and item.get("evidence")
              for item in found),
          str(found[:1])[:200])
    check("命中项按规则带了文件名", all(item.get("file") == "main.py" for item in found),
          str([item.get("file") for item in found]))

    # ---------- 2. 正常插件不误报 ----------
    print("\n[2] 正常插件（只用 astrbot.api）不该报高风险")
    clean = "\n".join([
        "from astrbot.api.event import filter, AstrMessageEvent",
        "from astrbot.api.star import Context, Star",
        "",
        "",
        "class MyPlugin(Star):",
        "    def __init__(self, context: Context, config=None):",
        "        super().__init__(context)",
        "        self.config = config or {}",
        "",
        "    @filter.command('hello')",
        "    async def hello(self, event: AstrMessageEvent):",
        "        yield event.plain_result('hi')",
    ])
    clean_result = plugin_safety.summarize(plugin_safety.scan_source(clean, "main.py"),
                                          ["main.py"])
    check("干净的插件扫出来是 ok", clean_result["level"] == "ok",
          f"{clean_result['level']} {[item['id'] for item in clean_result['findings']]}")
    check("ok 时摘要文案不为空", bool(clean_result["summary"]), clean_result["summary"])

    # ---------- 3. 解析失败不抛异常 ----------
    print("\n[3] 源码有语法错误时不抛异常（插件本来就加载不了）")
    check("语法错误 → 返回空列表",
          plugin_safety.scan_source("def broken(:\n  pass", "main.py") == [], "")
    check("空源码 → 返回空列表", plugin_safety.scan_source("", "main.py") == [], "")
    check("目录不存在 → 不抛异常且结论是 ok",
          plugin_safety.scan_plugin_dir(os.path.join("data", "_selftest", "no_such_dir_9f3"))
          ["level"] == "ok", "")

    # ---------- 4. 只解析、不执行 ----------
    print("\n[4] 扫描只做 AST 解析，绝不执行插件代码")
    test_root = os.path.join(PROJECT, "data", "_selftest")
    os.makedirs(test_root, exist_ok=True)
    run_id = str(int(time.time() * 1000) % 100000000)
    sandbox = os.path.join(test_root, f"unit_safety_{run_id}")
    shutil.rmtree(sandbox, ignore_errors=True)
    os.makedirs(sandbox, exist_ok=True)
    sentinel = os.path.join(sandbox, "executed.txt").replace("\\", "/")
    try:
        plugin_dir = os.path.join(sandbox, "evil_plugin")
        os.makedirs(plugin_dir, exist_ok=True)
        with open(os.path.join(plugin_dir, "main.py"), "w", encoding="utf-8") as handle:
            handle.write("import os, subprocess\n"
                         f"open(r'{sentinel}', 'w').write('boom')\n"
                         "subprocess.run(['cmd', '/c', 'echo hi'])\n"
                         "os.system('echo hi')\n")

        result = plugin_safety.scan_plugin_dir(plugin_dir)
        check("扫到了高风险（会执行命令/写文件）", result["level"] == "high",
              f"{result['level']} {[item['id'] for item in result['findings']]}")
        check("扫描过程没有执行插件代码（哨兵文件不存在）",
              not os.path.exists(sentinel), sentinel)
        check("结果里记录了扫描到的文件", "main.py" in result["files"], str(result["files"]))
        check("counts 统计与 findings 一致",
              sum(result["counts"].values()) == len(result["findings"]),
              f"{result['counts']} vs {len(result['findings'])}")

        # ---------- 5. 扫"插件根目录" ----------
        print("\n[5] 扫插件根目录：每个插件的命中项带上是哪个插件")
        second = os.path.join(sandbox, "second_plugin")
        os.makedirs(second, exist_ok=True)
        with open(os.path.join(second, "main.py"), "w", encoding="utf-8") as handle:
            handle.write("from astrbot.api.star import Star\n\n\nclass Ok(Star):\n    pass\n")
        container = plugin_safety.scan_container(sandbox)
        check("根目录扫描会合并两个子目录的结果",
              len(container["files"]) == 2, str(container["files"]))
        check("命中项带上了插件名与相对路径",
              all(item.get("plugin") and item.get("file", "").startswith(item["plugin"] + "/")
                  for item in container["findings"]),
              str(container["findings"][:2]))
        check("_ 开头/点开头的目录被跳过",
              plugin_safety.scan_container(os.path.join("plugins", "_not_exist"))["level"] == "ok",
              "")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    # ---------- 6. 自带插件不该出现高风险 ----------
    print("\n[6] 仓库自带插件（plugins/）不该出现高风险项")
    bundled = plugin_safety.scan_container(os.path.join(PROJECT, "plugins"))
    high = [item for item in bundled["findings"] if item["level"] == "high"]
    check("自带插件没有高风险命中（有的话说明规则误报或插件确实可疑）",
          not high, str([(item.get("plugin"), item["id"], item["evidence"]) for item in high])[:240])
    check("自带插件确实被扫到了（不是空扫描）", bool(bundled["files"]), str(bundled["files"]))

    # ---------- 7. 命令行入口 ----------
    print("\n[7] 命令行入口")
    check("main(['plugins']) 返回 0（自带插件无高风险）",
          plugin_safety.main([os.path.join(PROJECT, "plugins")]) == 0, "")
    check("main(['不存在的目录']) 返回 2",
          plugin_safety.main([os.path.join("data", "_selftest", "no_such_dir_9f4")]) == 2, "")

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
