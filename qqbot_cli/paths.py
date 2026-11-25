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

# "记住装在哪"的状态文件名（放在用户配置目录 / 主目录 / 安装目录里）
STATE_FILENAME = ".qqbot-cli.json"

# 主程序需要的依赖（和主程序 requirements.txt 保持一致；
# 启动器自己只需要 requests）
PROGRAM_REQUIRES = (
    "flask>=2.0",
    "Werkzeug>=3.1.9",
    "requests>=2.32.4",
    "websocket-client>=1.5.0",
    # 插件的 metadata.yaml 要靠它解析；缺了会退化成只认 "key: value"，
    # 遇到 YAML 多行块（description: |）就解析错
    "PyYAML>=6.0",
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
    """启动器记住"装到哪"的位置，按优先级排列。

    **只用受保护的用户目录 + 安装目录本身，绝不读当前工作目录。**
    曾经加过 `cwd/.qqbot-cli.json` 作为兜底（前两处不可写时用），
    但那是个安全漏洞：攻击者只要放一个伪造的状态文件到一个目录里，
    用户从那儿运行 `qqbot` 就会去执行攻击者指定的目录 —— 本地代码执行。
    便利性不能换安全性，这个兜底已移除。

    现在的候选：
      ① 用户配置目录（`%APPDATA%` / `$XDG_CONFIG_HOME`）
      ② 用户主目录下的隐藏文件（配置目录写不了时兜底）
      ③ **安装目录里面**（前两处都写不了时用；安装目录一定可写，
         否则程序也装不进去）

    ③ 是"受限环境"（只读家目录、CI、容器）下唯一能成的那个。
    下一轮 `qqbot` 不带参数时怎么找到它？靠 `_find_state_by_default_dir()`
    去默认安装目录里找 —— 因为那样装的用户几乎都是装在默认位置。
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
    if install_dir:
        inside = os.path.join(os.path.abspath(install_dir), STATE_FILENAME)
        if inside not in candidates:
            candidates.append(inside)
    return candidates


def _state_file() -> str:
    """主状态文件路径（读的时候会依次尝试 `_state_files()`）。"""
    return _state_files()[0]


def _write_state_file(path: str, payload: dict) -> bool:
    """安全地写状态文件：拒绝符号链接、原子替换、POSIX 下 0600。"""
    import json
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return False
    # 已存在且是符号链接 → 拒绝（否则会写到链接指向的文件，可能覆盖别人的东西）
    if os.path.islink(path):
        return False
    temp = path + ".tmp"
    try:
        if os.path.islink(temp):
            os.remove(temp)
        if os.name != "nt":
            # 先建好再写权限，避免中间态被别人读到
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        else:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp, path)
        return True
    except OSError:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            pass
        return False


def remember_install_dir(install_dir: str) -> bool:
    """记下这次装到了哪，下次 `qqbot` 不带参数就能直接启动。

    返回是否至少有一处写成功（写失败不影响本次使用，只是下次要带 --dir）。
    """
    payload = {"install_dir": os.path.abspath(str(install_dir))}
    written = False
    for path in _state_files(install_dir):
        if _write_state_file(path, payload):
            written = True
    return written


def _scan_state_dir(root: str) -> str:
    """在 root 下找 `.qqbot-cli.json`，读回它记的安装目录；找不到返回空串。"""
    import json
    candidate = os.path.join(root, STATE_FILENAME)
    if os.path.islink(candidate):
        return ""
    try:
        with open(candidate, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return ""
    value = str((data or {}).get("install_dir") or "")
    return value if value and os.path.isdir(value) else ""


def remembered_install_dir(install_dir: str = "") -> str:
    """读回上次装的目录；没有就返回空串。多个位置依次尝试。

    **读到之后不做信任判断**：调用方还要过 `runner.is_installed()` +
    marker 校验，才会真的去执行那个目录里的东西。
    """
    import json
    for path in _state_files(install_dir):
        # 状态文件本身是符号链接 → 不信任（可能是攻击者做的跳板）
        if os.path.islink(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            value = str((data or {}).get("install_dir") or "")
            if value and os.path.isdir(value):
                return value
        except Exception:
            continue

    # 前两处都写不了时（受限环境），状态会落在**安装目录里面**（第 ③ 处）。
    # 那种情况下一轮不知道装在哪，所以直接去"默认安装目录"里看一眼 ——
    # 受限环境下用到的几乎都是默认位置。找到之后仍有 marker 校验兜底。
    try:
        fallback_dir = default_install_dir()
    except Exception:
        return ""
    found = _scan_state_dir(fallback_dir)
    if found:
        return found
    # 用户用 --dir 装到了默认位置以外时，这里找不到就会回落到默认目录，
    # 由调用方提示"要不要重新指定"（见 cli 的 auto 分支）。
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
