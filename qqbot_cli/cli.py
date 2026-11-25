# -*- coding: utf-8 -*-
"""命令行入口：`qqbot`。

    qqbot               没装过 → 走向导；装过了 → 直接启动
    qqbot init          强制重新走向导（不会删 data/ 里的数据）
    qqbot run           只启动
    qqbot --dir <路径>  指定安装目录（支持在一个机器上装多份）
    qqbot --upgrade     重新下载最新版（保留 config.json 和 data/）
"""

import argparse
import os
import sys
from typing import Dict, List, Optional

from . import __version__, fetch, paths, runner, uninstall, wizard


# ---------------------------------------------------------------- 输出
def _ok(message: str) -> None:
    print("  ✓ %s" % message)


def _info(message: str) -> None:
    print("  · %s" % message)


def _fail(message: str) -> None:
    print()
    print("  ✗ %s" % message)
    print()


def _progress_printer():
    """下载进度（只在终端里刷新一行）。"""
    state = {"last": -1}

    def show(kind: str, done: int, total: int) -> None:
        if kind != "downloading":
            return
        if total > 0:
            percent = int(done * 100 / total)
            if percent == state["last"]:
                return
            state["last"] = percent
            bar = "#" * (percent // 4)
            print("\r  · 下载中 [%-25s] %3d%%" % (bar, percent), end="", flush=True)
        else:
            if done // (512 * 1024) == state["last"]:
                return
            state["last"] = done // (512 * 1024)
            print("\r  · 下载中 %d KB" % (done // 1024), end="", flush=True)

    return show


def _finish_progress() -> None:
    print()


# ---------------------------------------------------------------- 安装
def _ask_install_dir(suggested: str) -> str:
    """问用户装到哪个目录。

    默认值给一个**用户看得见、自己也好找**的位置。回车即用默认值；
    输入相对路径会按当前目录解析成绝对路径。
    """
    default = suggested or paths.default_install_dir()
    print()
    print("=" * 60)
    print("  QQBotMerged 安装向导")
    print("=" * 60)
    print()
    print("  程序会装到一个独立目录里，配置、数据、日志都在里面，")
    print("  想查看或改东西直接打开这个目录即可（等于手动 cd 进去）。")
    print()
    while True:
        try:
            answer = input("  安装目录 [%s]: " % default).strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            print()
            raise
        if not answer:
            return paths.expand_dir(default)
        chosen = paths.expand_dir(answer)
        # 相对路径：按当前工作目录解析（用户写了 `qqbot` 就装到 ./qqbot）
        if not os.path.isabs(os.path.expanduser(answer)):
            chosen = os.path.abspath(os.path.expanduser(answer))
        parent = os.path.dirname(chosen.rstrip("/\\")) or "."
        if not os.path.isdir(parent):
            print("  上级目录不存在：%s" % parent)
            print("  请换一个已有目录下的路径，或先把上级目录建好。")
            continue
        return chosen


def _confirm_target_dir(install_dir: str) -> bool:
    """目录已存在且有内容、但又不是本程序 —— 让用户确认一下。"""
    try:
        entries = [name for name in os.listdir(install_dir)
                   if name not in (".", "..")]
    except OSError:
        return True
    if not entries:
        return True
    print()
    print("  注意：%s 里已经有 %d 个文件/文件夹，看起来不像是装的这个程序。"
          % (install_dir, len(entries)))
    preview = "、".join(entries[:5])
    if len(entries) > 5:
        preview += " 等"
    print("  里面有：%s" % preview)
    print("  （程序只会写入自己的文件，不会删除你原有的东西，"
          "但同名文件会被覆盖）")
    try:
        return wizard.ask_yes("  仍然装到这个目录吗？", default=False)
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _finish_config_only(install_dir: str, marker: Dict) -> int:
    """装好的目录只差配置（上次向导被取消）：跑向导 + 出结果，不重新下载。

    程序文件、虚拟环境、依赖都已经就位，重跑一遍下载既慢又浪费；
    这里只补 `config.json`，然后照常打印地址/令牌并问要不要启动。
    """
    print()
    _info("这个目录已经装好了，只差配置文件（不会重新下载）。")
    tag = str(marker.get("tag") or "未知")
    config_path = os.path.join(install_dir, "config.json")
    existing: Dict = {}
    try:
        config = wizard.run_wizard(install_dir, tag, existing)
    except (EOFError, KeyboardInterrupt):
        print()
        _info("向导已取消，配置还没写。之后运行 `qqbot init` 可以接着补。")
        return 0
    wizard.write_config(install_dir, config)
    _ok("已生成 config.json")

    # 清掉 needs_config：配置有了，下次 `qqbot` 直接启动
    payload = dict(marker)
    payload.pop("needs_config", None)
    runner.write_marker(install_dir, payload)
    paths.remember_install_dir(install_dir)

    python = paths.venv_python(install_dir)
    versions: Dict = {}
    if os.path.exists(python):
        try:
            versions = runner.verify_dependencies(python)
        except runner.SetupError:
            versions = {}
    wizard.show_summary(install_dir, config, versions)

    try:
        if wizard.ask_yes("现在启动吗？", default=True):
            return do_run(install_dir)
    except (EOFError, KeyboardInterrupt):
        print()
    print()
    _info("之后运行 `qqbot` 就能启动。")
    return 0


def do_install(install_dir: str = "", force_wizard: bool = False,
               upgrade: bool = False, dir_given: bool = False) -> int:
    """下载 + 建环境 + 向导。返回进程退出码。

    `dir_given` 表示用户用 `--dir` 明确指定了目录 —— 那种情况不再重复询问。
    """
    runner.check_python()

    # ---- 先问装到哪（目录要先定下来才能下载）
    if not dir_given and not upgrade:
        install_dir = _ask_install_dir(install_dir)
    if not install_dir:
        install_dir = paths.default_install_dir()

    already = runner.is_installed(install_dir)
    if already and not upgrade and not force_wizard:
        return do_run(install_dir)

    # **装好了但还缺配置**（上次向导被取消）→ 只补配置，不重新下载。
    # 这也是 `qqbot` auto 模式在那种情况下的目标路径。
    if already and not upgrade:
        marker = runner.read_marker(install_dir)
        config_path = os.path.join(install_dir, "config.json")
        if marker.get("needs_config") and not os.path.exists(config_path):
            return _finish_config_only(install_dir, marker)

    # 目录里有东西但不是本程序 → 问清楚再动，避免覆盖用户别的文件
    if not already and not upgrade:
        if not _confirm_target_dir(install_dir):
            _info("已取消，没有改动任何文件。")
            return 0

    if already and upgrade:
        print()
        print("  正在更新 %s" % install_dir)
        _info("config.json 和 data/ 会保留，只替换程序文件")
    else:
        print()
        print("  安装到 %s" % install_dir)

    # ---- 选版本
    print()
    _info("查询最新版本 …")
    try:
        releases = fetch.list_releases()
        release = fetch.pick_latest(releases)
    except fetch.FetchError as exc:
        _fail(str(exc))
        _info("如果网络不通，可以手动下载：")
        _info("  https://github.com/%s/releases" % paths.REPO_SLUG)
        _info("解压后在该目录里运行 qqbot --dir <该目录>")
        return 1
    if not release:
        _fail("没找到可用的发布版本")
        return 1
    tag = release["tag"]
    _ok("最新版本：%s" % tag)

    # ---- 下载解压
    try:
        result = fetch.install_release(release, install_dir,
                                       progress=_progress_printer())
        _finish_progress()
        _ok("下载并解压完成（sha256 %s…）" % (result.get("sha256") or "")[:12])
    except fetch.FetchError as exc:
        _finish_progress()
        _fail(str(exc))
        return 1

    # ---- 虚拟环境 + 依赖
    try:
        print()
        python = runner.ensure_venv(install_dir, progress=_info)
        _ok("虚拟环境就绪")
        # 装依赖之前先看 pip 版本：太旧的 pip 可能信任已出事的证书，
        # 在不可信网络里装包有被中间人顶替的风险（见 runner.MIN_PIP_VERSION）。
        pip_info = runner.check_pip(python)
        if pip_info.get("ok"):
            if pip_info.get("version"):
                _ok("pip 版本 %s" % pip_info["version"])
        else:
            _info("注意：pip 版本偏低，见上面的说明（不影响继续安装）")
        runner.install_requirements(install_dir, python, progress=_info)
        versions = runner.verify_dependencies(python)
        _ok("依赖安装完成")
    except runner.SetupError as exc:
        _fail(str(exc))
        return 1

    # ---- 向导
    config_path = os.path.join(install_dir, "config.json")
    existing: Dict = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8-sig") as handle:
                loaded = _load_json(handle)
            existing = loaded
        except Exception:
            existing = {}

    if force_wizard or not os.path.exists(config_path):
        try:
            config = wizard.run_wizard(install_dir, tag, existing)
        except (EOFError, KeyboardInterrupt):
            # 用户中途按了 Ctrl+C / 输入被关掉（管道里跑、CI）。
            # **程序文件已经装好了，必须照常写 marker**，否则 `is_installed()`
            # 判为没装，下次 `qqbot` 会重新下载一遍 —— 白费一次下载和装依赖。
            # 但 config.json 没写成，所以一个劲儿启动也没用，改为提示补配置。
            print()
            runner.write_marker(install_dir, {
                "tag": tag,
                "sha256": result.get("sha256") or "",
                "cli_version": __version__,
                "needs_config": True,
                "python": "%d.%d.%d" % sys.version_info[:3],
                "pip": pip_info.get("version") or "",
                "dependencies": versions,
            })
            paths.remember_install_dir(install_dir)
            _info("向导已取消，程序文件已经装好了。")
            _info("还没写配置，所以暂时不能直接启动。")
            _info("运行 `qqbot init` 补完配置即可（不会重新下载）。")
            return 0
        wizard.write_config(install_dir, config)
        _ok("已生成 config.json")
    else:
        config = existing
        _ok("沿用已有的 config.json")

    runner.write_marker(install_dir, {
        "tag": tag,
        "sha256": result.get("sha256") or "",
        "cli_version": __version__,
        # 把运行环境记下来：审计和安全排查时不用再猜装的是什么版本
        "python": "%d.%d.%d" % sys.version_info[:3],
        "pip": pip_info.get("version") or "",
        "dependencies": versions,
    })
    # 记下装到了哪：下次 `qqbot` 不带参数就知道去哪启动
    paths.remember_install_dir(install_dir)
    wizard.show_summary(install_dir, config, versions)

    # ---- 问一句要不要现在启动
    try:
        if wizard.ask_yes("现在启动吗？", default=True):
            return do_run(install_dir)
    except (EOFError, KeyboardInterrupt):
        print()
    print()
    _info("之后运行 `qqbot` 就能启动。")
    return 0


def _load_json(handle) -> Dict:
    """读 config.json（容忍 BOM）。"""
    import json as _json
    data = _json.load(handle)
    return data if isinstance(data, dict) else {}


def do_run(install_dir: str, extra: Optional[List[str]] = None) -> int:
    """启动主程序。"""
    if not runner.is_installed(install_dir):
        _fail("这个目录里还没装程序：%s" % install_dir)
        _info("先运行 `qqbot init` 走向导。")
        return 1
    python = paths.venv_python(install_dir)
    if not os.path.exists(python):
        _fail("找不到虚拟环境：%s" % python)
        _info("重新运行 `qqbot init` 可以重建。")
        return 1
    marker = runner.read_marker(install_dir)
    print()
    _info("启动 QQBotMerged（版本 %s）" % (marker.get("tag") or "未知"))
    _info("按 Ctrl+C 停止")
    print()
    try:
        return runner.launch(install_dir, python, extra)
    except runner.SetupError as exc:
        _fail(str(exc))
        return 1


# ---------------------------------------------------------------- 导出 / 卸载
def do_export(install_dir: str, target: str = "") -> int:
    """把 config.json + data/ 导出成 zip。"""
    if not os.path.isdir(install_dir):
        _fail("安装目录不存在：%s" % install_dir)
        return 1
    target = target or uninstall.default_export_path(install_dir)
    print()
    _info("导出 config.json 与 data/ …")
    try:
        result = uninstall.export_data(install_dir, target, progress=_info)
    except uninstall.UninstallError as exc:
        _fail(str(exc))
        return 1
    print()
    _ok("导出完成：%s" % result["path"])
    _info("大小 %s，共 %d 个文件"
          % (uninstall.human_size(result["bytes"]), result["files"]))
    if result["skipped"]:
        _info("有 %d 个文件被占用/读不了，已跳过：" % len(result["skipped"]))
        for name in result["skipped"][:5]:
            _info("  %s" % name)
    return 0


def do_uninstall(install_dir: str, export: Optional[bool] = None,
                 target: str = "", assume_yes: bool = False) -> int:
    """卸载：可选先导出数据，然后删掉安装目录。

    `export` 为 None 时问用户；True/False 直接按给的来（--export / --no-export）。
    `assume_yes` 跳过最后那道「确认删除」的询问（脚本里用 `-y`）。
    """
    if not os.path.isdir(install_dir):
        _fail("安装目录不存在：%s" % install_dir)
        _info("用 `qqbot --dir <路径> uninstall` 指定要卸载的目录。")
        return 1

    info = uninstall.measure(install_dir)
    print()
    print("=" * 60)
    print("  卸载 QQBotMerged")
    print("=" * 60)
    print("  安装目录 : %s" % install_dir)
    print("  占用大小 : %s" % uninstall.human_size(info["total_bytes"]))
    if info["has_data"]:
        print("  data/    : %s（%d 个文件）"
              % (uninstall.human_size(info["data_bytes"]), info["data_files"]))
    else:
        print("  data/    : 没有")
    print("  config.json : %s" % ("有" if info["has_config"] else "没有"))
    if info["extra"]:
        preview = "、".join(info["extra"][:5])
        print("  其它文件 : %s%s（也会一起删）"
              % (preview, " 等" if len(info["extra"]) > 5 else ""))
    print("=" * 60)

    # ---- 问要不要先导出数据
    do_export_first = export
    if do_export_first is None:
        if not (info["has_data"] or info["has_config"]):
            do_export_first = False
        else:
            print()
            print("  卸载前可以把 config.json 和 data/（消息库、媒体、日志）")
            print("  导出成一个 zip 备份，以后重装时可以放回去继续用。")
            try:
                do_export_first = wizard.ask_yes("  先导出数据吗？", default=True)
            except (EOFError, KeyboardInterrupt):
                print()
                _info("已取消，没有删除任何东西。")
                return 0

    archive = ""
    if do_export_first:
        chosen = target or uninstall.default_export_path(install_dir)
        if not target:
            try:
                answer = input("  导出到 [%s]: " % chosen).strip().strip('"').strip("'")
            except (EOFError, KeyboardInterrupt):
                print()
                _info("已取消，没有删除任何东西。")
                return 0
            if answer:
                chosen = paths.expand_dir(answer) if os.path.isabs(answer) else \
                    os.path.abspath(answer)
        print()
        _info("正在导出 …")
        try:
            result = uninstall.export_data(install_dir, chosen, progress=_info)
        except uninstall.UninstallError as exc:
            _fail(str(exc))
            _info("导出失败，为保证数据不丢，**没有删除**任何东西。")
            _info("可以重试：qqbot --dir \"%s\" export" % install_dir)
            return 1
        archive = result["path"]
        _ok("已导出到 %s（%s，%d 个文件）"
            % (archive, uninstall.human_size(result["bytes"]), result["files"]))
        if result["skipped"]:
            _info("注意：%d 个文件被占用，没进备份：" % len(result["skipped"]))
            for name in result["skipped"][:5]:
                _info("  %s" % name)
        # 导出成功后记一下，万一删失败用户也知道备份在哪
        print()
    else:
        print()
        _info("不导出数据 —— 删除后这些内容无法恢复。")

    # ---- 最后确认
    if not assume_yes:
        try:
            if not wizard.ask_yes("  确认删除 %s 吗？" % install_dir, default=False):
                _info("已取消，没有删除任何东西。")
                if archive:
                    _info("备份仍然保留在：%s" % archive)
                return 0
        except (EOFError, KeyboardInterrupt):
            print()
            _info("已取消，没有删除任何东西。")
            if archive:
                _info("备份仍然保留在：%s" % archive)
            return 0
    else:
        _info("（-y 已跳过确认）")

    print()
    try:
        result = uninstall.uninstall(install_dir, progress=_info)
    except uninstall.UninstallError as exc:
        _fail(str(exc))
        return 1

    print()
    if result["removed"]:
        _ok("已删除 %s" % result["path"])
    else:
        _fail("有 %d 个文件删不掉（可能被程序或编辑器占用）："
              % result["failed_count"])
        for name in result["failed"][:5]:
            _info("  %s" % name)
        _info("关掉正在使用这些文件的程序后再运行一次 `qqbot uninstall`。")

    forgotten = uninstall.forget_state(install_dir)
    if forgotten:
        _info("已清掉「上次装在哪」的记录")
    print()
    if archive:
        _info("备份在：%s" % archive)
        _info("以后想恢复：新建一个目录解压进去，再运行 qqbot --dir <那个目录> run")
    _info("启动器本身还在，想彻底移除：pipx uninstall qqbot-cli")
    return 0 if result["removed"] else 1


# ---------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    # 完整说明随包一起装着（qqbot_cli/PACKAGING.md），把路径打在帮助末尾，
    # 用户不用回 GitHub 找。
    doc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "PACKAGING.md")
    doc_hint = ""
    if os.path.exists(doc_path):
        doc_hint = "\n完整说明：%s" % doc_path
    parser = argparse.ArgumentParser(
        prog="qqbot",
        description="QQBotMerged 启动器：下载程序、建环境、走向导、启动、卸载。",
        epilog="不给子命令时：没装过就走向导，装过了就直接启动。" + doc_hint,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="auto",
                        choices=["auto", "init", "run", "export", "uninstall",
                                 "version"],
                        help="auto=智能（默认）/ init=重新走向导 / run=只启动 / "
                             "export=导出 config.json 与 data/ / uninstall=卸载")
    parser.add_argument("--dir", dest="install_dir", default="",
                        help="安装目录（默认 %s）" % paths.default_install_dir())
    parser.add_argument("--upgrade", action="store_true",
                        help="重新下载最新版（保留 config.json 和 data/）")
    parser.add_argument("--output", dest="output", default="",
                        help="export/uninstall 时备份 zip 的保存位置")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--export", dest="export", action="store_true",
                       default=None, help="卸载时先导出数据（不再询问）")
    group.add_argument("--no-export", dest="export", action="store_false",
                       help="卸载时不导出数据（不再询问，直接删）")
    parser.add_argument("-y", "--yes", dest="assume_yes", action="store_true",
                        help="卸载时跳过最后的确认（脚本里用）")
    parser.add_argument("-v", "--version", action="store_true",
                        help="显示启动器版本")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version or args.command == "version":
        print("qqbot-cli %s" % __version__)
        return 0

    # 用户有没有显式给 --dir：给了就不再追问装到哪
    dir_given = bool(str(args.install_dir or "").strip())
    # **没给 --dir 时不要立刻回落到默认目录**：先问用户装哪（首次安装），
    # 或者用上次记住的目录（之后启动）。所以这里只在给了 --dir 时才展开。
    install_dir = paths.expand_dir(args.install_dir) if dir_given else ""

    try:
        if args.command == "run":
            return do_run(paths.resolve_install_dir(args.install_dir))
        if args.command == "export":
            target_dir = paths.resolve_install_dir(args.install_dir)
            if not dir_given and not os.path.isdir(target_dir):
                target_dir = _ask_install_dir("")
            return do_export(target_dir, args.output)
        if args.command == "uninstall":
            target_dir = paths.resolve_install_dir(args.install_dir)
            if not dir_given:
                # 没指定就确认一下 —— 卸载删错目录的代价太大
                print()
                _info("将卸载：%s" % target_dir)
                try:
                    if not wizard.ask_yes("  是这个目录吗？", default=True):
                        target_dir = _ask_install_dir("")
                except (EOFError, KeyboardInterrupt):
                    print()
                    _info("已取消。")
                    return 0
            return do_uninstall(target_dir, export=args.export,
                                target=args.output, assume_yes=args.assume_yes)
        if args.command == "init":
            if not dir_given:
                install_dir = _ask_install_dir(paths.resolve_install_dir(""))
                dir_given = True
            return do_install(install_dir, force_wizard=True,
                              upgrade=runner.is_installed(install_dir),
                              dir_given=True)
        # auto：先看记住的那份装没装
        remembered = paths.remembered_install_dir()
        if not dir_given and remembered and runner.is_installed(remembered) \
                and not args.upgrade:
            # 装过但上次向导被取消了（没写 config.json）→ 补配置，而不是启动个空程序
            marker = runner.read_marker(remembered)
            if marker.get("needs_config") and \
                    not os.path.exists(os.path.join(remembered, "config.json")):
                _info("上次没写配置，先补一下（不会重新下载）。")
                return do_install(remembered, force_wizard=True,
                                  upgrade=False, dir_given=True)
            return do_run(remembered)
        return do_install(install_dir, upgrade=args.upgrade, dir_given=dir_given)
    except KeyboardInterrupt:
        print()
        _info("已取消。")
        return 130
    except Exception as exc:                            # 兜底：不把栈甩给用户
        _fail("出错了：%s" % exc)
        _info("如果一直失败，可以到 GitHub 提 issue：")
        _info("  https://github.com/%s/issues" % paths.REPO_SLUG)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
