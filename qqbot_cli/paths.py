# -*- coding: utf-8 -*-
"""启动器自己的路径与常量。

**刻意不导入主程序的 `core`**：主程序在 GitHub 上随时可能变，启动器要是
依赖它的模块结构，一次重构就把老用户卡住了。这里只用最小上限度的信息。
"""

import os
import sys

# GitHub 仓库与接口（和主程序 core/version.py 里的一致，但这里独立维护）
REPO_SLUG = "L-web666/QQBotMerged"
RELEASES_API = "https://api.github.com/repos/%s/releases?per_page=20" % REPO_SLUG
TAGS_API = "https://api.github.com/repos/%s/tags?per_page=20" % REPO_SLUG

# 只从这些主机下载（和主程序的允许清单一致，防止被指向任意地址）
ALLOWED_HOSTS = (
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
)

# 安装目录里认这个文件来判断"这里已经装过了"
MARKER_FILE = ".qqbot-install.json"

# 虚拟环境目录名（放在安装目录下）
VENV_DIR = ".venv"

# 主程序需要的依赖（和主程序 requirements.txt 保持一致；
# 启动器自己只需要 requests）
PROGRAM_REQUIRES = (
    "flask>=2.0",
    "Werkzeug>=3.1.9",
    "requests>=2.28.0",
    "websocket-client>=1.5.0",
)

# 最低 Python 版本：Werkzeug 3.1.9 要求 >=3.9
MIN_PYTHON = (3, 9)


def default_install_dir() -> str:
    """默认安装目录：`~/QQBotMerged`。"""
    return os.path.join(os.path.expanduser("~"), "QQBotMerged")


def expand_dir(value: str) -> str:
    """把用户输入（可能带 ~ 或环境变量）展开成绝对路径。"""
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        return default_install_dir()
    text = os.path.expandvars(os.path.expanduser(text))
    return os.path.abspath(text)


def _platform() -> str:
    """当前平台（集中一处判断，方便测试时替换）。

    返回 "windows" / "macos" / "linux" / 其它 `sys.platform` 值。
    **不要把 `os.name` / `sys.platform` 散落在各处**：所有平台分支都走这里，
    测试里只要替换这一个函数就能验证另一套逻辑。
    """
    override = os.environ.get("QQBOT_CLI_FORCE_PLATFORM")
    if override:
        return override
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def is_windows() -> bool:
    return _platform() == "windows"


def is_linux() -> bool:
    return _platform() == "linux"


def is_macos() -> bool:
    return _platform() == "macos"


def platform_label() -> str:
    """给人看的平台名（提示信息里用）。"""
    table = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}
    return table.get(_platform(), _platform())


def install_help_command(install_dir: str) -> str:
    """打印「怎么进安装目录」时要给的命令（Windows 用 cd /d 才能跨盘符）。"""
    if is_windows():
        return 'cd /d "%s"' % install_dir
    return 'cd "%s"' % install_dir


def venv_activate_hint(install_dir: str) -> str:
    """怎么手动激活虚拟环境（排错时有用）。"""
    if is_windows():
        return r'%s\.venv\Scripts\activate.bat' % install_dir
    return 'source "%s/.venv/bin/activate"' % install_dir


def venv_python(install_dir: str) -> str:
    """虚拟环境里的 python 可执行文件路径。"""
    if is_windows():
        return os.path.join(install_dir, VENV_DIR, "Scripts", "python.exe")
    return os.path.join(install_dir, VENV_DIR, "bin", "python")


def current_python() -> str:
    """当前解释器（用于创建 venv）。"""
    return sys.executable


# ---------------------------------------------------------------- 记住装在哪
def _state_files(install_dir: str = "") -> list:
    """启动器记住"装到哪"的几个候选位置，按优先级排列。

    **为什么要好几个**：`%APPDATA%` / `$XDG_CONFIG_HOME` 在有些环境里不可写
    （受限的用户目录、容器、CI）；用户主目录也可能只读（只读家目录、企业策略）。
    只记一处，用户在向导里选的目录下次就找不到 —— `qqbot` 会默默回落到默认目录，
    看起来像"装丢了"。

    所以依次尝试几个位置，任何一处写成功就够用：
      ① 用户配置目录（最"正规"的位置）
      ② 用户主目录下的隐藏文件（配置目录写不了时的兜底）
      ③ **当前工作目录**下的隐藏文件（前两处都写不了时用；用户一般在自己
         常用的目录里运行 `qqbot`，下次也在那儿跑，所以找得回来）
    """
    home = os.path.expanduser("~")
    if os.name == "nt":
        base = os.environ.get("APPDATA") or home
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    candidates = [
        os.path.join(base, "qqbot-cli", "state.json"),
        os.path.join(home, ".qqbot-cli.json"),
    ]
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    if cwd:
        candidates.append(os.path.join(cwd, ".qqbot-cli.json"))
    if install_dir:
        parent = os.path.dirname(os.path.abspath(install_dir).rstrip("/\\")) or "."
        marker = os.path.join(parent, ".qqbot-cli.json")
        if marker not in candidates:
            candidates.append(marker)
    return candidates


def _state_file() -> str:
    """主状态文件路径（读的时候会依次尝试 `_state_files()`）。"""
    return _state_files()[0]


def remember_install_dir(install_dir: str) -> bool:
    """记下这次装到了哪，下次 `qqbot` 不带参数就能直接启动。

    返回是否至少有一处写成功（写失败不影响本次使用，只是下次要带 --dir）。
    """
    import json
    payload = {"install_dir": install_dir}
    written = False
    for path in _state_files(install_dir):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            written = True
        except OSError:
            continue
    return written


def remembered_install_dir(install_dir: str = "") -> str:
    """读回上次装的目录；没有就返回空串。多个位置依次尝试。"""
    import json
    for path in _state_files(install_dir):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            value = str((data or {}).get("install_dir") or "")
            if value and os.path.isdir(value):
                return value
        except Exception:
            continue
    return ""


def resolve_install_dir(cli_value: str = "", hint: str = "") -> str:
    """确定要操作的安装目录。

    优先级：命令行 --dir > 上次记住的位置 > 默认目录
    （`hint` 用于让"记住的位置"多查一个候选项，比如上次的安装目录）
    """
    if str(cli_value or "").strip():
        return expand_dir(cli_value)
    remembered = remembered_install_dir(hint)
    if remembered:
        return remembered
    return default_install_dir()
