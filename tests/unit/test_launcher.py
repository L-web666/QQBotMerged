# -*- coding: utf-8 -*-
"""单元测试：qqbot-cli 启动器

覆盖三件事，都不需要真的装依赖：

  A. **下载链路的安全边界**：主机白名单、路径穿越、Windows 设备名、
     绝对路径、压缩包体积/成员数上限

  B. **GitHub tarball 的壳目录处理**：这个坑踩过两次 ——
     ① 顶层目录名形如 `L-web666-QQBotMerged-<sha>`（含 `-`），
        不能简单按"第一段"猜；
     ② `_safe_member()` 会去掉末尾的 `/`，导致目录条目认不出来，
        壳目录剥不掉、所有文件被埋到 `xxx-<sha>/` 下面。

  C. **向导生成的 config.json 主程序能读**：只写了少数字段，
     其余必须由主程序的 schema 自动补全。

运行：`python tests/unit/test_launcher.py`
"""

import builtins
import json
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from qqbot_cli import cli, fetch, paths, runner, wizard   # noqa: E402

original_input = builtins.input

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def main():
    print("=" * 70)
    print("  qqbot-cli 启动器")
    print("=" * 70)

    # ================= A. 下载安全 =================
    print("\n[A1] 主机白名单")
    for url, allowed in [
        ("https://api.github.com/repos/x/y", True),
        ("https://codeload.github.com/x/y/tar.gz/refs/tags/v1.0.0", True),
        ("https://evil.example.com/x.tar.gz", False),
        ("file:///etc/passwd", False),
        ("http://127.0.0.1:8080/x.tar.gz", False),
        ("ftp://github.com/x", False),
    ]:
        try:
            fetch._check_host(url)
            got = True
        except fetch.FetchError:
            got = False
        check(f"{'放行' if allowed else '拒绝'} {url[:46]}", got == allowed, str(got))

    print("\n[A2] 压缩包成员名：路径穿越与非法路径一律丢弃")
    for name, expected in [
        ("qqbot-1.0/run.py", "qqbot-1.0/run.py"),
        ("a\\b.py", "a/b.py"),
        ("a/./b.py", "a/b.py"),
        ("../../../etc/passwd", ""),
        ("/etc/passwd", ""),
        ("C:\\Windows\\x", ""),
        ("~/secret", ""),
        ("", ""),
    ]:
        got = fetch._safe_member(name)
        check(f"{name!r} -> {expected!r}", got == expected, repr(got))

    # ================= B. 壳目录 =================
    print("\n[B1] 识别 GitHub tarball 的壳目录")
    cases = [
        (["L-web666-QQBotMerged-abc/",
          "L-web666-QQBotMerged-abc/run.py",
          "L-web666-QQBotMerged-abc/core/x.py"],
         "L-web666-QQBotMerged-abc", "真实 GitHub 结构（名字含 -）"),
        (["QQBotMerged-1.4.4/", "QQBotMerged-1.4.4/run.py"],
         "QQBotMerged-1.4.4", "简单壳目录"),
        (["run.py", "core/x.py"], "", "没有壳目录（不能把 core 当壳）"),
        (["core/", "run.py", "core/x.py"], "", "没有壳目录但有子目录条目"),
        (["a/x.py", "b/y.py"], "", "多个顶层"),
        (["x.py"], "", "单文件"),
        ([], "", "空包"),
    ]
    for names, expected, desc in cases:
        got = fetch._strip_root(names)
        check(f"{desc}", got == expected, "得到 %r" % got)

    print("\n[B2] 真实 tarball 解压后壳目录必须被剥掉")
    work = os.path.join(PROJECT, "data", "_selftest", "unit_launcher")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    try:
        # 造一个和 GitHub 一样带壳目录的 tar.gz（不联网）
        import io
        import tarfile
        sample = os.path.join(work, "sample.tar.gz")
        with tarfile.open(sample, "w:gz") as tar:
            for name, content in [
                ("L-web666-QQBotMerged-abc/", None),
                ("L-web666-QQBotMerged-abc/run.py", b"print(1)\n"),
                ("L-web666-QQBotMerged-abc/core/", None),
                ("L-web666-QQBotMerged-abc/core/x.py", b"X = 1\n"),
            ]:
                if content is None:
                    info = tarfile.TarInfo(name)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tar.addfile(info)
                else:
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
        target = os.path.join(work, "extracted")
        fetch.extract(sample, target)
        entries = sorted(os.listdir(target))
        check("壳目录已剥离", "L-web666-QQBotMerged-abc" not in entries, str(entries))
        check("run.py 在根下", "run.py" in entries, str(entries))
        check("core/ 在根下", "core" in entries, str(entries))
        check("子文件内容正确",
              open(os.path.join(target, "core", "x.py"), encoding="utf-8").read() == "X = 1\n", "")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # ================= C. 向导产出的配置 =================
    print("\n[C1] 向导生成的 config.json 主程序能读")
    work = os.path.join(PROJECT, "data", "_selftest", "unit_wizard")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    original = builtins.input
    try:
        feed = iter(["APPID_X", "SECRET_Y", "我的机器人", "",
                     "https://api.deepseek.com", "deepseek-chat", "18666"])
        builtins.input = lambda prompt="": next(feed)
        config = wizard.run_wizard(work, "v1.4.4", {})
        builtins.input = original
        path = wizard.write_config(work, config)
        check("config.json 已写入", os.path.exists(path), path)

        data = json.load(open(path, encoding="utf-8"))
        check("端口写对了", data["web"]["port"] == 18666, str(data["web"]["port"]))
        check("令牌已自动生成", len(data["web"]["token"]) >= 20,
              str(len(data["web"]["token"])))
        check("AppID 写对了", data["bots"][0]["app_id"] == "APPID_X", "")
        check("填了凭据就启用", data["bots"][0]["enabled"] is True, "")

        # 关键：让主程序读一遍，确认能加载、缺项自动补全
        script = (
            "import sys, json\n"
            "sys.path.insert(0, %r)\n"
            "from core.config_manager import load_config\n"
            "cfg = load_config('config.json')\n"
            "print(json.dumps({\n"
            "  'port': cfg.config.int_of('web', 'port', default=0),\n"
            "  'bots': len(cfg.config.get('bots') or []),\n"
            "  'model': cfg.config.str_of('ai', 'model', default=''),\n"
            "  'filled': cfg.config.int_of('reply', 'max_segment_length', default=-1),\n"
            "}))\n" % PROJECT)
        completed = subprocess.run([sys.executable, "-c", script], cwd=work,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=120)
        check("主程序能加载这份配置", completed.returncode == 0,
              (completed.stderr or "")[-300:])
        if completed.returncode == 0:
            info = json.loads(completed.stdout.strip().splitlines()[-1])
            check("端口被正确读出", info["port"] == 18666, str(info["port"]))
            check("只有 1 个机器人", info["bots"] == 1, str(info["bots"]))
            check("AI 模型正确", info["model"] == "deepseek-chat", info["model"])
            check("未填的项被 schema 自动补全", info["filled"] > 0, str(info["filled"]))
    except Exception as exc:
        builtins.input = original
        check("向导流程没抛异常", False, str(exc))
    finally:
        builtins.input = original
        shutil.rmtree(work, ignore_errors=True)

    print("\n[C2] 留空也能装完（不填 AppID）")
    work = os.path.join(PROJECT, "data", "_selftest", "unit_wizard2")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    try:
        feed = iter(["", "", "", "", "", "", ""])
        builtins.input = lambda prompt="": next(feed)
        config = wizard.run_wizard(work, "v1.4.4", {})
        builtins.input = original
        check("没填 AppID 时机器人为未启用",
              config["bots"][0]["enabled"] is False, "")
        check("仍然生成了令牌", len(config["web"]["token"]) >= 20, "")
        check("端口有默认值", config["web"]["port"] == 8666, str(config["web"]["port"]))
    except Exception as exc:
        builtins.input = original
        check("留空流程没抛异常", False, str(exc))
    finally:
        builtins.input = original
        shutil.rmtree(work, ignore_errors=True)

    # ================= D. 安装判定 =================
    print("\n[D1] 安装目录判定")
    work = os.path.join(PROJECT, "data", "_selftest", "unit_marker")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    try:
        check("空目录不算已安装", runner.is_installed(work) is False, "")
        open(os.path.join(work, "run.py"), "w", encoding="utf-8").close()
        check("只有 run.py 不算已安装", runner.is_installed(work) is False, "")
        runner.write_marker(work, {"tag": "v1.4.4", "sha256": "abc"})
        check("有 marker 才算已安装", runner.is_installed(work) is True, "")
        marker = runner.read_marker(work)
        check("marker 内容正确", marker.get("tag") == "v1.4.4", str(marker))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n[D2] 路径展开与虚拟环境位置")
    check("默认目录以 QQBotMerged 结尾",
          paths.default_install_dir().endswith("QQBotMerged"), paths.default_install_dir())
    expanded = paths.expand_dir("~/somewhere")
    check("~ 被展开", not expanded.startswith("~"), expanded)
    check("结果是绝对路径", os.path.isabs(expanded), expanded)
    check("空输入回落到默认目录",
          paths.expand_dir("") == paths.default_install_dir(), "")
    venv_python = paths.venv_python("/tmp/qq")
    check("虚拟环境 python 路径含 .venv", ".venv" in venv_python, venv_python)

    # ================= E. 安装目录可选 =================
    print("\n[E1] 安装目录由用户自己选（向导会问）")
    original_dir = paths.default_install_dir()
    work = os.path.join(PROJECT, "data", "_selftest", "unit_askdir")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    try:
        from qqbot_cli import cli

        def feed_input(value):
            builtins.input = lambda prompt="": value

        feed_input("")
        check("直接回车 → 用默认目录",
              cli._ask_install_dir("") == original_dir, "")

        absolute = os.path.join(work, "myQQBot")
        feed_input(absolute)
        check("绝对路径原样采用",
              cli._ask_install_dir("") == os.path.abspath(absolute), "")

        feed_input("qqbot_sub")
        relative = cli._ask_install_dir("")
        check("相对路径转成绝对路径", os.path.isabs(relative), relative)

        feed_input('"%s"' % absolute)
        check("带引号的路径（资源管理器粘贴）能去掉引号",
              cli._ask_install_dir("") == os.path.abspath(absolute), "")

        # 上级目录不存在时要求重问
        replies = iter([os.path.join(work, "no_such_parent", "x"), absolute])
        builtins.input = lambda prompt="": next(replies)
        check("上级目录不存在时会重问",
              cli._ask_install_dir("") == os.path.abspath(absolute), "")
        builtins.input = original_input
    except Exception as exc:
        builtins.input = original_input
        check("询问安装目录没抛异常", False, str(exc))
    finally:
        builtins.input = original_input

    print("\n[E2] 目录里已有别的东西时要确认")
    try:
        crowded = os.path.join(work, "crowded")
        os.makedirs(crowded, exist_ok=True)
        with open(os.path.join(crowded, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        feed_input("n")
        check("用户答 n → 不装", cli._confirm_target_dir(crowded) is False, "")
        feed_input("y")
        check("用户答 y → 继续", cli._confirm_target_dir(crowded) is True, "")
        empty = os.path.join(work, "empty_dir")
        os.makedirs(empty, exist_ok=True)
        check("空目录不必确认", cli._confirm_target_dir(empty) is True, "")
        builtins.input = original_input
    except Exception as exc:
        builtins.input = original_input
        check("目录确认没抛异常", False, str(exc))
    finally:
        builtins.input = original_input

    print("\n[E3] 记住装在哪（下次不带 --dir 也能启动）")
    try:
        remember_target = os.path.join(work, "remembered")
        os.makedirs(remember_target, exist_ok=True)
        # 先清掉可能存在的记录
        for one in paths._state_files(remember_target):
            try:
                os.remove(one)
            except OSError:
                pass
        written = paths.remember_install_dir(remember_target)
        check("至少写入一处位置", written is True, str(written))
        check("能读回来", paths.remembered_install_dir() == remember_target,
              paths.remembered_install_dir())
        check("resolve 用它",
              paths.resolve_install_dir() == remember_target,
              paths.resolve_install_dir())
        override = os.path.join(work, "override")
        check("--dir 优先于记住的",
              paths.resolve_install_dir(override) == os.path.abspath(override),
              paths.resolve_install_dir(override))
        # 记录的位置被删掉后不能再用它
        shutil.rmtree(remember_target, ignore_errors=True)
        check("记住的目录没了就回落", paths.remembered_install_dir() == "",
              paths.remembered_install_dir())
    except Exception as exc:
        check("记住目录流程没抛异常", False, str(exc))
    finally:
        for one in paths._state_files(os.path.join(work, "remembered")):
            try:
                os.remove(one)
            except OSError:
                pass
        # 注意：这里不删 work，[E4] 还要用；最后统一清理

    print("\n[E4] 向导被取消时仍要记成「已安装」（否则下次会重新下载）")
    try:
        cancelled = os.path.join(work, "cancelled")
        os.makedirs(cancelled, exist_ok=True)
        with open(os.path.join(cancelled, "run.py"), "w", encoding="utf-8") as fh:
            fh.write("print('hi')\n")
        os.makedirs(os.path.join(cancelled, ".venv", "Scripts"), exist_ok=True)
        with open(os.path.join(cancelled, ".venv", "Scripts", "python.exe"), "w") as fh:
            fh.write("")
        check("取消前：判为未安装", runner.is_installed(cancelled) is False, "")
        # 取消分支现在会做的事
        runner.write_marker(cancelled, {
            "tag": "v1.5.0", "sha256": "x", "cli_version": "1.0.0",
            "needs_config": True,
        })
        check("取消后：仍判为已安装（不会再下载一遍）",
              runner.is_installed(cancelled) is True, "")
        marker = runner.read_marker(cancelled)
        check("marker 里记了 needs_config", marker.get("needs_config") is True, "")
        # auto 分支据此判断要不要先补配置
        config_path = os.path.join(cancelled, "config.json")
        needs = marker.get("needs_config") and not os.path.exists(config_path)
        check("缺 config.json 时会被要求补配置", bool(needs) is True, "")
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.write("{}")
        needs_after = marker.get("needs_config") and not os.path.exists(config_path)
        check("有 config.json 后不再要求补配置", bool(needs_after) is False, "")
        # 正常安装不该带这个标记
        normal = os.path.join(work, "normal")
        os.makedirs(normal, exist_ok=True)
        with open(os.path.join(normal, "run.py"), "w", encoding="utf-8") as fh:
            fh.write("print('hi')\n")
        runner.write_marker(normal, {"tag": "v1.5.0", "sha256": "y",
                                     "cli_version": "1.0.0"})
        check("正常安装没有 needs_config", 
              runner.read_marker(normal).get("needs_config") is None, "")
    except Exception as exc:
        check("取消流程没抛异常", False, str(exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
