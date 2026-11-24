# -*- coding: utf-8 -*-
"""建虚拟环境、装依赖、启动主程序。

**为什么用虚拟环境**：主程序需要 `Werkzeug>=3.1.9`（安全修复版）等固定版本。
直接装进用户系统 Python 可能和他们已有的包冲突，装了也可能起不来。
每个安装目录一个 `.venv`，升级、删除都不影响系统环境。
"""

import json
import os
import subprocess
import sys
from typing import Callable, Dict, List, Optional

from . import paths


class SetupError(RuntimeError):
    """建环境/装依赖失败。"""


def _run(cmd: List[str], cwd: Optional[str] = None, timeout: int = 900,
         progress: Optional[Callable[[str], None]] = None) -> subprocess.CompletedProcess:
    """跑一个子进程并收集输出（失败时把最后几行带进异常）。"""
    if progress:
        progress(" ".join(cmd[:4]) + (" …" if len(cmd) > 4 else ""))
    try:
        completed = subprocess.run(
            cmd, cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise SetupError("找不到命令：%s" % cmd[0])
    except subprocess.TimeoutExpired:
        raise SetupError("命令超时（%d 秒）：%s" % (timeout, " ".join(cmd[:3])))
    if completed.returncode != 0:
        tail = "\n".join((completed.stdout or "").strip().splitlines()[-12:])
        raise SetupError("命令失败（退出码 %d）：%s\n%s"
                         % (completed.returncode, " ".join(cmd[:3]), tail))
    return completed


def check_python() -> None:
    """启动器自己所在解释器要 >=3.9（和主程序一致）。"""
    if sys.version_info < paths.MIN_PYTHON:
        raise SetupError(
            "需要 Python %d.%d 或更高版本，当前是 %d.%d"
            % (paths.MIN_PYTHON[0], paths.MIN_PYTHON[1],
               sys.version_info[0], sys.version_info[1]))


def ensure_venv(install_dir: str, progress: Optional[Callable[[str], None]] = None) -> str:
    """建好 `<安装目录>/.venv`，返回里面的 python 路径。已存在就直接用。"""
    python = paths.venv_python(install_dir)
    if os.path.exists(python):
        return python
    if progress:
        progress("创建虚拟环境 …")
    try:
        _run([paths.current_python(), "-m", "venv",
              os.path.join(install_dir, paths.VENV_DIR)], timeout=300)
    except SetupError as exc:
        raise SetupError("创建虚拟环境失败：%s\n"
                         "（Linux 上可能需要装 python3-venv）" % exc)
    if not os.path.exists(python):
        raise SetupError("虚拟环境建好了但找不到 python：%s" % python)
    return python


def install_requirements(install_dir: str, python: str,
                         progress: Optional[Callable[[str], None]] = None) -> None:
    """装主程序的依赖。优先用装好的 requirements.txt，没有就用内置清单。"""
    if progress:
        progress("安装依赖（首次可能要一两分钟）…")
    requirement_file = os.path.join(install_dir, "requirements.txt")
    if os.path.exists(requirement_file):
        cmd = [python, "-m", "pip", "install", "-q", "-r", requirement_file]
    else:
        cmd = [python, "-m", "pip", "install", "-q"] + list(paths.PROGRAM_REQUIRES)
    try:
        _run(cmd, timeout=1800)
    except SetupError as exc:
        raise SetupError("安装依赖失败：%s\n"
                         "（如果是网络问题，可以配国内镜像后重试：\n"
                         "  pip config set global.index-url "
                         "https://pypi.tuna.tsinghua.edu.cn/simple）" % exc)


def verify_dependencies(python: str) -> Dict[str, str]:
    """确认关键依赖真的能导入；返回 {包名: 版本}。"""
    script = (
        "import json\n"
        "from importlib.metadata import version\n"
        "out = {}\n"
        "for name in ('Flask', 'Werkzeug', 'requests', 'websocket-client'):\n"
        "    try:\n"
        "        out[name] = version(name)\n"
        "    except Exception:\n"
        "        out[name] = ''\n"
        "print(json.dumps(out))\n"
    )
    completed = subprocess.run([python, "-c", script], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=120)
    if completed.returncode != 0:
        raise SetupError("依赖自检失败：%s" % (completed.stderr or "").strip()[:300])
    try:
        data = json.loads(completed.stdout.strip().splitlines()[-1])
    except Exception:
        raise SetupError("依赖自检返回内容无法解析")
    missing = [name for name, ver in data.items() if not ver]
    if missing:
        raise SetupError("这些依赖没装上：%s" % ", ".join(missing))
    return data


def launch(install_dir: str, python: str, extra_args: Optional[List[str]] = None) -> int:
    """在当前进程里直接跑主程序（前台运行，Ctrl+C 退出）。"""
    entry = os.path.join(install_dir, "run.py")
    if not os.path.exists(entry):
        raise SetupError("安装目录里找不到 run.py：%s" % install_dir)
    cmd = [python, entry] + list(extra_args or [])
    try:
        # 前台交接：让主程序直接接管这个终端（能看到彩色日志、能 Ctrl+C）
        return subprocess.call(cmd, cwd=install_dir)
    except KeyboardInterrupt:
        return 0


def is_installed(install_dir: str) -> bool:
    """判断这个目录里是否已经装好了主程序。"""
    marker = os.path.join(install_dir, paths.MARKER_FILE)
    entry = os.path.join(install_dir, "run.py")
    return os.path.exists(marker) and os.path.exists(entry)


def read_marker(install_dir: str) -> Dict:
    marker = os.path.join(install_dir, paths.MARKER_FILE)
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_marker(install_dir: str, data: Dict) -> None:
    marker = os.path.join(install_dir, paths.MARKER_FILE)
    payload = dict(data or {})
    payload["install_dir"] = install_dir
    with open(marker, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
