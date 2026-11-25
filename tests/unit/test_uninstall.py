# -*- coding: utf-8 -*-
"""单元测试：卸载 / 数据导出 / 跨平台

  A. **导出**：`config.json` + `data/` 打成 zip，**不带**程序代码与缓存；
     拒绝导到 `data/` 里面、上级目录不存在时报错。

  B. **卸载**：只删「看起来确实是本程序」的目录（有 run.py / 安装记录 / config.json），
     删不掉的文件单独列出来；卸载后清掉「记住装在哪」的记录。

  C. **跨平台**：用 `QQBOT_CLI_FORCE_PLATFORM` 在子进程里模拟 Windows / Linux / macOS，
     验证 `cd /d` 与 `cd`、`Scripts/python.exe` 与 `bin/python`、`activate.bat` 与 `source`。
     **路径分隔符不在此断言** —— `os.path.join` 跟随真实运行系统，模拟时它是反斜杠，
     这没问题；只看目录结构对不对。

运行：`python tests/unit/test_uninstall.py`
"""

import builtins
import json
import os
import shutil
import subprocess
import sys
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from qqbot_cli import cli, paths, runner, uninstall      # noqa: E402

PASSED = []
FAILED = []
ORIGINAL_INPUT = builtins.input


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def make_install(root, with_data=True):
    """造一个像样的安装目录。"""
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "run.py"), "w", encoding="utf-8") as fh:
        fh.write("print('hi')\n")
    os.makedirs(os.path.join(root, "core"), exist_ok=True)
    with open(os.path.join(root, "core", "x.py"), "w", encoding="utf-8") as fh:
        fh.write("X = 1\n")
    os.makedirs(os.path.join(root, ".venv", "Scripts"), exist_ok=True)
    with open(os.path.join(root, ".venv", "Scripts", "python.exe"), "w") as fh:
        fh.write("")
    with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"web": {"port": 8666, "token": "T"}}, fh)
    if with_data:
        for sub in ("logs", "media"):
            os.makedirs(os.path.join(root, "data", sub), exist_ok=True)
        with open(os.path.join(root, "data", "messages.db"), "wb") as fh:
            fh.write(b"B" * 5000)
        with open(os.path.join(root, "data", "logs", "app.log"), "w") as fh:
            fh.write("log\n")
        with open(os.path.join(root, "data", "media", "a.jpg"), "wb") as fh:
            fh.write(b"J" * 3000)
        os.makedirs(os.path.join(root, "data", "__pycache__"), exist_ok=True)
        with open(os.path.join(root, "data", "__pycache__", "c.pyc"), "wb") as fh:
            fh.write(b"c")
    runner.write_marker(root, {"tag": "v1.4.4"})
    return root


def main():
    print("=" * 70)
    print("  卸载 / 数据导出 / 跨平台")
    print("=" * 70)

    work = os.path.join(PROJECT, "data", "_selftest", "unit_uninstall")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    try:
        # ================= A. 导出 =================
        print("\n[A1] 统计安装目录")
        root = make_install(os.path.join(work, "first"))
        info = uninstall.measure(root)
        check("识别出 config.json", info["has_config"] is True, "")
        check("识别出 data/", info["has_data"] is True, "")
        check("统计到数据文件（排除 __pycache__）", info["data_files"] == 3,
              str(info["data_files"]))
        check("总大小 > 0", info["total_bytes"] > 0, str(info["total_bytes"]))
        check("human_size 可读", "B" in uninstall.human_size(5000),
              uninstall.human_size(5000))

        print("\n[A2] 导出 config.json + data/")
        target = os.path.join(work, "backup.zip")
        result = uninstall.export_data(root, target)
        check("导出文件已生成", os.path.exists(result["path"]), result["path"])
        with zipfile.ZipFile(result["path"]) as bundle:
            names = bundle.namelist()
            check("含 config.json", "config.json" in names, str(names[:5]))
            check("含消息库", "data/messages.db" in names, "")
            check("含日志", any(n.startswith("data/logs/") for n in names), "")
            check("不含程序代码", not any(n.startswith("core/") for n in names), "")
            check("不含 __pycache__",
                  not any("__pycache__" in n for n in names), "")
            check("不含 .venv", not any(n.startswith(".venv") for n in names), "")
            check("消息库内容逐字节一致",
                  bundle.read("data/messages.db") == b"B" * 5000, "")
            check("配置内容完整",
                  json.loads(bundle.read("config.json").decode("utf-8"))["web"]["port"] == 8666,
                  "")

        print("\n[A3] 导出路径安全检查")
        try:
            uninstall.export_data(root, os.path.join(root, "data", "bad.zip"))
            check("拒绝导到 data/ 里面", False, "没有报错")
        except uninstall.UninstallError:
            check("拒绝导到 data/ 里面", True, "")
        try:
            uninstall.export_data(root, os.path.join(work, "no", "such", "b.zip"))
            check("上级目录不存在时报错", False, "没有报错")
        except uninstall.UninstallError:
            check("上级目录不存在时报错", True, "")
        empty = os.path.join(work, "empty")
        os.makedirs(empty, exist_ok=True)
        try:
            uninstall.export_data(empty, os.path.join(work, "e.zip"))
            check("没东西可导出时报错", False, "没有报错")
        except uninstall.UninstallError:
            check("没东西可导出时报错", True, "")

        # ================= B. 卸载 =================
        print("\n[B1] 安全边界：不是本程序的目录不删")
        stranger = os.path.join(work, "stranger")
        os.makedirs(stranger, exist_ok=True)
        with open(os.path.join(stranger, "important.docx"), "w") as fh:
            fh.write("x")
        try:
            uninstall.uninstall(stranger)
            check("拒绝删除陌生目录", False, "没有报错")
        except uninstall.UninstallError:
            check("拒绝删除陌生目录", True, "")
        check("陌生目录里的文件没被删",
              os.path.exists(os.path.join(stranger, "important.docx")), "")

        print("\n[B2] 卸载（不导出）")
        second = make_install(os.path.join(work, "second"))
        builtins.input = lambda prompt="": ""
        try:
            code = cli.do_uninstall(second, export=False, assume_yes=True)
        finally:
            builtins.input = ORIGINAL_INPUT
        check("退出码 0", code == 0, str(code))
        check("目录已删除", not os.path.exists(second), "")

        print("\n[B3] 卸载（先导出再删）")
        third = make_install(os.path.join(work, "third"))
        backup = os.path.join(work, "third-backup.zip")
        builtins.input = lambda prompt="": ""
        try:
            code = cli.do_uninstall(third, export=True, target=backup,
                                    assume_yes=True)
        finally:
            builtins.input = ORIGINAL_INPUT
        check("退出码 0", code == 0, str(code))
        check("目录已删除", not os.path.exists(third), "")
        check("备份还在", os.path.exists(backup), backup)
        if os.path.exists(backup):
            with zipfile.ZipFile(backup) as bundle:
                check("备份含配置", "config.json" in bundle.namelist(), "")
                check("备份含消息库",
                      "data/messages.db" in bundle.namelist(), "")

        print("\n[B4] 用户取消时不删")
        fourth = make_install(os.path.join(work, "fourth"))
        answers = iter(["n", "n"])     # ① 先导出吗 ② 确认删除吗
        builtins.input = lambda prompt="": next(answers)
        try:
            cli.do_uninstall(fourth, export=None, assume_yes=False)
        except StopIteration:
            pass
        finally:
            builtins.input = ORIGINAL_INPUT
        check("取消后目录还在", os.path.exists(fourth), "")

        print("\n[B5] 卸载后清掉「记住装在哪」")
        paths.remember_install_dir(fourth)
        # 传 install_dir 时一定能读回来 —— 这是"记住"本身是否生效的判据。
        # （不传参时只有默认安装目录里那份能被扫到，见 paths.remembered_install_dir）
        check("先能记住",
              paths.remembered_install_dir(fourth) == os.path.abspath(fourth),
              paths.remembered_install_dir(fourth))
        cleared = uninstall.forget_state(fourth)
        check("清掉了记录", cleared >= 1, str(cleared))
        check("读不回来了", paths.remembered_install_dir(fourth) == "",
              paths.remembered_install_dir(fourth))

        # ================= C. 跨平台 =================
        print("\n[C1] 三种平台的命令差异")
        script = (
            "import os, sys, json\n"
            "sys.path.insert(0, r'%s')\n"
            "from qqbot_cli import paths\n"
            "print(json.dumps({\n"
            "  'label': paths.platform_label(),\n"
            "  'cd': paths.install_help_command('I'),\n"
            "  'activate': paths.venv_activate_hint('I'),\n"
            "  'venv_python': paths.venv_python('I'),\n"
            "}, ensure_ascii=False))\n" % PROJECT)

        def as_platform(name):
            env = dict(os.environ)
            if name:
                env["QQBOT_CLI_FORCE_PLATFORM"] = name
            else:
                env.pop("QQBOT_CLI_FORCE_PLATFORM", None)
            done = subprocess.run([sys.executable, "-c", script], env=env,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=120)
            if done.returncode != 0:
                return None
            return json.loads(done.stdout.strip().splitlines()[-1])

        def segments(path_value):
            return [p for p in path_value.replace("\\", "/").split("/") if p]

        windows = as_platform("windows")
        if windows:
            check("Windows 平台名", windows["label"] == "Windows", windows["label"])
            check("Windows 用 cd /d", windows["cd"] == 'cd /d "I"', windows["cd"])
            check("Windows 的 venv 在 Scripts/",
                  segments(windows["venv_python"])[-2:] == ["Scripts", "python.exe"],
                  windows["venv_python"])
            check("Windows 用 activate.bat",
                  "activate.bat" in windows["activate"], windows["activate"])
        else:
            check("Windows 子进程能跑", False, "")

        linux = as_platform("linux")
        if linux:
            check("Linux 平台名", linux["label"] == "Linux", linux["label"])
            check("Linux 的 cd 不带 /d", linux["cd"] == 'cd "I"', linux["cd"])
            check("Linux 的 venv 在 bin/",
                  segments(linux["venv_python"])[-2:] == ["bin", "python"],
                  linux["venv_python"])
            check("Linux 用 source",
                  linux["activate"] == 'source "I/.venv/bin/activate"',
                  linux["activate"])
        else:
            check("Linux 子进程能跑", False, "")

        mac = as_platform("macos")
        if mac:
            check("macOS 平台名", mac["label"] == "macOS", mac["label"])
            check("macOS 的 venv 在 bin/",
                  segments(mac["venv_python"])[-2:] == ["bin", "python"],
                  mac["venv_python"])
        else:
            check("macOS 子进程能跑", False, "")

        print("\n[C2] 三种平台下所有模块都能导入")
        for platform in ("windows", "linux", "macos"):
            env = dict(os.environ)
            env["QQBOT_CLI_FORCE_PLATFORM"] = platform
            code = ("import sys; sys.path.insert(0, r'%s'); "
                    "import qqbot_cli.cli, qqbot_cli.fetch, qqbot_cli.runner, "
                    "qqbot_cli.uninstall, qqbot_cli.wizard, qqbot_cli.paths; "
                    "print('ok')" % PROJECT)
            done = subprocess.run([sys.executable, "-c", code], env=env,
                                  capture_output=True, text=True, timeout=120)
            check("%s 下导入正常" % platform,
                  done.returncode == 0 and "ok" in (done.stdout or ""),
                  (done.stderr or "")[-200:])
    finally:
        builtins.input = ORIGINAL_INPUT
        for one in paths._state_files(os.path.join(work, "fourth")):
            try:
                os.remove(one)
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
