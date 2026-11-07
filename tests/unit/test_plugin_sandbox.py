# -*- coding: utf-8 -*-
"""单元测试：插件进程内软限制（`core/plugin_sandbox.py`）

**软限制的定位先说清楚**：插件仍在主进程里运行，这是"降低误伤范围 + 拦住明显越界写法"，
**不是**可以随意运行不可信代码的保证（真正的隔离需要独立进程 + 独立低权限账户）。
本文件只断言它**声称要做的事**真的做到了：

  · loose（默认）：拦住"执行系统命令 / 起子进程"，其余放行（不影响现有插件）
  · strict：另加导入白名单、只允许写插件数据目录（与插件自身目录）、联网域名白名单
  · off：完全不干预
  · 离开上下文后所有补丁**必须还原**（不能污染宿主与其它插件）

分层约定见 `PROGRESS.md`：不连网、不碰真实 `data/`，临时文件只写 `data/_selftest/`。

运行：`python tests/unit/test_plugin_sandbox.py`
"""

import builtins
import io
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import plugin_sandbox                                    # noqa: E402
from core.plugin_sandbox import (PluginSandbox, SandboxViolation,    # noqa: E402
                                 normalize_mode)

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def blocked(func) -> bool:
    """执行 func，返回它是否被沙箱拦下。"""
    try:
        func()
        return False
    except SandboxViolation:
        return True


def try_import(module: str):
    """按名字导入模块，返回 (是否成功, 说明)。

    注意不要写成 `blocked(lambda: __import__("json"))`：`__import__` 在 lambda
    闭包里走的是 `builtins.__import__` 的**新**绑定，和 `import json` 语句的查找
    时机不同，容易测出与真实插件行为不符的结果。这里直接用 importlib 走同一条路。
    """
    import importlib
    try:
        importlib.import_module(module)
        return True, ""
    except SandboxViolation as exc:
        return False, str(exc)
    except Exception as exc:            # 模块不存在等
        return False, repr(exc)


def main():
    print("=" * 70)
    print("  插件进程内软限制（sandbox）")
    print("=" * 70)

    root = os.path.join(PROJECT, "data", "_selftest", "unit_sandbox_%d" % int(time.time() * 1000))
    shutil.rmtree(root, ignore_errors=True)
    plugin_dir = os.path.join(root, "demo_plugin")
    data_dir = os.path.join(root, "demo_plugin_data")
    os.makedirs(plugin_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    try:
        # ---------- 1. 配置值归一 ----------
        print("\n[1] 档位解析（认不出时取最保守的 loose）")
        check("off / loose / strict 原样", [normalize_mode(x) for x in ("off", "loose", "strict")]
              == ["off", "loose", "strict"], "")
        check("大写与空格容错", normalize_mode(" STRICT ") == "strict", normalize_mode(" STRICT "))
        check("true → strict", normalize_mode("true") == "strict", "")
        check("false → off", normalize_mode("false") == "off", "")
        check("空值/乱写 → loose", normalize_mode("") == "loose"
              and normalize_mode("乱写") == "loose", "")

        # ---------- 2. 域名白名单 ----------
        print("\n[2] 联网白名单（strict 用）")
        for host in ("multimedia.nt.qq.com.cn", "api.deepseek.com", "127.0.0.1",
                     "qq.com.cn", "qq.com", "QQ.COM", "qq.com."):
            check(f"放行 {host}", plugin_sandbox.host_allowed(host) is True, "")
        for host in ("evil.example.com", "1.1.1.1", ""):
            check(f"拒绝 {host or '(空)'}", plugin_sandbox.host_allowed(host) is False, "")

        # ---------- 2b. 域名匹配不能被子串骗过（审计 SEC-01） ----------
        print("\n[2b] 域名白名单只做精确/子域匹配，不接受子串包含")
        for host in ("qq.com.evil.com", "evilqq.com", "myqq.com", "notqq.com.cn",
                     "api.deepseek.com.evil.net", "qq.com.tunnel.evil"):
            check(f"拒绝子串伪造 {host}", plugin_sandbox.host_allowed(host) is False, host)

        # ---------- 2c. 标准库白名单：该放的放、该拦的拦 ----------
        print("\n[2c] 导入白名单：常见标准库不误伤，危险模块不放行")
        box0 = PluginSandbox("strict", plugin_dir, data_dir, "demo")
        for mod in ("json", "re", "os", "sys", "time", "datetime", "pathlib",
                    "copyreg", "threading", "collections", "logging", "asyncio"):
            check(f"放行标准库 {mod}", box0._module_allowed(mod) is True, mod)
        for mod in ("subprocess", "ctypes", "pickle", "multiprocessing", "runpy", "marshal"):
            check(f"拦危险模块 {mod}", box0._module_allowed(mod) is False, mod)

        # ---------- 3. loose：只拦执行命令 ----------
        print("\n[3] loose（默认）：拦执行命令/起子进程，其余放行")
        with PluginSandbox("loose", plugin_dir, data_dir, "demo") as box:
            check("os.system 被拦", blocked(lambda: os.system("echo hi")), "")
            check("os.popen 被拦", blocked(lambda: os.popen("dir")), "")
            check("import subprocess 放行（不影响现有插件）",
                  not blocked(lambda: __import__("subprocess")), "")
            check("写插件数据目录放行",
                  not blocked(lambda: open(os.path.join(data_dir, "a.txt"), "w").write("x")), "")
        check("记下了拦截项", bool(box.violations), str(box.violations[:2]))

        # ---------- 4. strict：导入白名单 ----------
        print("\n[4] strict：导入白名单")
        with PluginSandbox("strict", plugin_dir, data_dir, "demo") as box:
            # 已经 import 过的模块会直接从 sys.modules 命中，不会再走导入钩子，
            # 所以先清掉再测（否则"拦 subprocess"会是假通过）。
            import sys as _sys
            banned = ("subprocess", "ctypes", "pickle", "multiprocessing")
            saved = {name: _sys.modules.pop(name, None) for name in banned}
            try:
                for mod in banned:
                    ok, why = try_import(mod)
                    check(f"拦 import {mod}", not ok, why)
            finally:
                for name, module in saved.items():
                    if module is not None:
                        _sys.modules[name] = module
            for mod in ("json", "re", "os", "sys", "datetime", "collections"):
                ok, why = try_import(mod)
                check(f"放行 import {mod}", ok, why)

        # ---------- 5. strict：文件写入范围 ----------
        print("\n[5] strict：只允许写插件目录/数据目录")
        outside = os.path.join(root, "outside.txt")
        with PluginSandbox("strict", plugin_dir, data_dir, "demo") as box:
            check("拦写插件目录外的文件",
                  blocked(lambda: open(outside, "w").write("x")), outside)
            check("放行写插件数据目录",
                  not blocked(lambda: open(os.path.join(data_dir, "ok.txt"), "w").write("ok")), "")
            check("放行写插件自身目录",
                  not blocked(lambda: open(os.path.join(plugin_dir, "res.txt"), "w").write("r")), "")
        check("确实没在目录外生成文件", not os.path.isfile(outside), "")

        # ---------- 6. strict：联网 ----------
        print("\n[6] strict：联网域名白名单")
        with PluginSandbox("strict", plugin_dir, data_dir, "demo") as box:
            import socket
            check("拦连接 evil.example.com",
                  blocked(lambda: socket.create_connection(("evil.example.com", 80), timeout=1)),
                  "")

        # ---------- 7. off：完全不干预 ----------
        print("\n[7] off：不装任何补丁")
        with PluginSandbox("off", plugin_dir, data_dir, "demo") as box:
            check("import subprocess 放行", not blocked(lambda: __import__("subprocess")), "")
            check("没有记录拦截", not box.violations, str(box.violations[:2]))

        # ---------- 8. 离开上下文必须还原（最要紧的一条） ----------
        print("\n[8] 离开上下文后补丁全部还原（不污染宿主）")
        original_open = builtins.open
        original_system = os.system
        with PluginSandbox("strict", plugin_dir, data_dir, "demo"):
            check("上下文内 builtins.open 已被包装", builtins.open is not original_open, "")
            check("上下文内 os.system 已被包装", os.system is not original_system, "")
        check("builtins.open 已还原", builtins.open is original_open, "")
        check("io.open 与 builtins.open 一致（没有残留包装）", io.open is original_open, "")
        check("os.system 已还原", os.system is original_system, "")
        check("宿主仍能正常 import subprocess", not blocked(lambda: __import__("subprocess")), "")

        # ---------- 9. 插件自己的模块要能 import ----------
        print("\n[9] 插件目录内的模块必须能正常导入")
        with open(os.path.join(plugin_dir, "helpers.py"), "w", encoding="utf-8") as handle:
            handle.write("VALUE = 42\n")
        with PluginSandbox("strict", plugin_dir, data_dir, "demo") as box:
            check("拦不在白名单的第三方包", blocked(lambda: __import__("numpy")), "")
            check("本地模块名在白名单里被识别",
                  "helpers" in box._local_modules(), str(box._local_modules()))

        # ---------- 10. 线程作用域（审计 NEW3-02） ----------
        print("\n[10] 沙箱只约束安装它的线程，宿主其它线程不受影响")
        import threading
        sibling = os.path.join(root, "sibling_dir")     # 与 plugin_dir 同前缀的兄弟目录
        os.makedirs(sibling, exist_ok=True)
        host_target = os.path.join(sibling, "host_own.txt")
        cross = {}
        with PluginSandbox("strict", plugin_dir, data_dir, "demo"):
            # 安装线程：写目录外必须被拦
            check("安装线程（插件）写目录外被拦",
                  blocked(lambda: builtins.open(host_target, "w").write("x")), host_target)

            # 无关线程：写宿主自己的文件必须放行
            def other_thread():
                try:
                    handle = builtins.open(host_target, "w")
                    handle.write("host")
                    handle.close()
                    cross["ok"] = True
                except SandboxViolation as exc:
                    cross["blocked"] = str(exc)

            worker = threading.Thread(target=other_thread)
            worker.start()
            worker.join(timeout=10)
            check("无关线程写宿主文件不受沙箱影响", cross.get("ok") is True,
                  str(cross.get("blocked", ""))[:100])

        # ---------- 11. 目录边界判断（不能靠字符串前缀） ----------
        print("\n[11] 路径判断按目录边界，不能被同前缀的兄弟目录骗过")
        box = PluginSandbox("strict", plugin_dir, data_dir, "demo")
        check("插件目录内放行", box._path_allowed(os.path.join(plugin_dir, "a.txt")) is True, "")
        check("数据目录内放行", box._path_allowed(os.path.join(data_dir, "b.txt")) is True, "")
        check("同前缀兄弟目录不被误判为插件目录",
              box._path_allowed(os.path.join(sibling, "c.txt")) is False, sibling)
        check("prefix 伪造（plugin 后面接别的名字）被拒",
              box._path_allowed(plugin_dir + "_evil/x.txt") is False, "")
        check("目录本身放行", box._path_allowed(plugin_dir) is True, "")

        # ---------- 12. 安装失败必须原子回滚（审计 NEW4-02） ----------
        print("\n[12] 安装中途失败：回滚已打补丁 + 状态可见")
        before_open = builtins.open
        broken = PluginSandbox("strict", plugin_dir, data_dir, "demo")

        def _boom():
            raise RuntimeError("模拟补丁失败")

        broken._patch_network = _boom      # 让第二个补丁失败
        broken.__enter__()
        check("安装失败时 _active=False", broken._active is False, str(broken._active))
        check("安装失败时 installed=False", broken.installed is False, str(broken.installed))
        check("记录了下失败原因", bool(broken.install_error), broken.install_error)
        check("失败后已打的补丁被回滚（open 仍是内置）",
              builtins.open is before_open, builtins.open.__name__)
        broken.__exit__(None, None, None)
        check("退出后仍然是内置 open", builtins.open is before_open, builtins.open.__name__)
        note = broken.status_note()
        check("status_note 明确说未生效", "未生效" in note, note)

        # ---------- 13. 正常安装：状态可见且能还原 ----------
        print("\n[13] 正常安装：effective=True，退出后还原")
        good = PluginSandbox("strict", plugin_dir, data_dir, "demo")
        before_good = builtins.open
        good.__enter__()
        check("installed=True", good.installed is True, "")
        check("effective=True", good.effective is True, "")
        check("status_note 说已生效", "生效" in good.status_note(), good.status_note())
        inside_name = builtins.open.__name__
        good.__exit__(None, None, None)
        check("安装期 open 被包装", inside_name == "guarded_open", inside_name)
        check("退出后已还原", builtins.open is before_good, builtins.open.__name__)

        # ---------- 14. off 档不谎报生效 ----------
        print("\n[14] off 档：不安装、也不谎报生效")
        off_box = PluginSandbox("off", plugin_dir, data_dir, "demo")
        off_box.__enter__()
        check("off 档 effective=False", off_box.effective is False, "")
        check("off 档 status_note 说明已关闭",
              "关闭" in off_box.status_note(), off_box.status_note())
        off_box.__exit__(None, None, None)
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
