# -*- coding: utf-8 -*-
"""建虚拟环境、装依赖、启动主程序。

**为什么用虚拟环境**：主程序需要 `Werkzeug>=3.1.9`（安全修复版）等固定版本。
直接装进用户系统 Python 可能和他们已有的包冲突，装了也可能起不来。
每个安装目录一个 `.venv`，升级、删除都不影响系统环境。
"""

import json
import os
import re
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


# pip 的安全下限。低于它的 pip 内置 certifi 仍信任一批已出事的根/中间证书
# （CVE-2026-133949 / GHSA-jg88-vh4g-v698），在攻击者能站在网络路径上时
# 可能被伪造证书骗去装篡改过的包。这不是本项目特有的问题，但**首次装依赖
# 之前**至少要把它报出来，让用户知道风险、可以自己决定怎么办。
MIN_PIP_VERSION = (26, 2, 2)


def _parse_version(text: str) -> tuple:
    """从 `pip 24.3.1 from ...` 里取出版本号元组；取不到返回空元组。"""
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    if not match:
        return ()
    parts = [int(match.group(1)), int(match.group(2))]
    parts.append(int(match.group(3)) if match.group(3) else 0)
    return tuple(parts)


def pip_version(python: str) -> tuple:
    """读虚拟环境里的 pip 版本；读不到返回空元组。"""
    try:
        completed = subprocess.run([python, "-m", "pip", "--version"],
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
        if completed.returncode != 0:
            return ()
        return _parse_version(completed.stdout)
    except Exception:
        return ()


def check_pip(python: str, progress: Optional[Callable[[str], None]] = None) -> dict:
    """检查 pip 版本；太旧就**提示**（不静默强行升级）。

    为什么只是提示而不是自动升级：升级 pip 本身要联网下载一个包，
    在信任链有问题的情况下"自动去装一个包"反而扩大风险面。
    这里把版本和风险说清楚，选择权交给用户。

    返回 {version, ok, message}。
    """
    found = pip_version(python)
    if not found:
        return {"version": "", "ok": True, "message": ""}
    text = ".".join(str(x) for x in found)
    if found >= MIN_PIP_VERSION:
        return {"version": text, "ok": True, "message": ""}
    wanted = ".".join(str(x) for x in MIN_PIP_VERSION)
    message = (
        "pip 版本偏旧：%s（建议 %s 或更高）。\n"
        "  低于 %s 的 pip 内置的证书库仍信任某些已出事的证书，\n"
        "  在不可信网络里装依赖时可能被中间人伪造的包索引骗过。\n"
        "  想升级可以执行：\n"
        "      \"%s\" -m pip install --upgrade pip\n"
        "  （不自动升级是刻意的：升级本身也要联网，把这个选择留给你）"
        % (text, wanted, wanted, python))
    if progress:
        progress(message)
    return {"version": text, "ok": False, "message": message}


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
    """确认关键依赖真的能导入；返回 {包名: 版本}。

    PyYAML 也要查：插件的 metadata.yaml 靠它解析，缺了不报错但会**静默解析错**
    （退回"只认 key: value"的兜底，YAML 多行块会读成字面量）。
    """
    script = (
        "import json\n"
        "from importlib.metadata import version\n"
        "out = {}\n"
        "for name in ('Flask', 'Werkzeug', 'requests', 'websocket-client', 'PyYAML'):\n"
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
    """在当前进程里直接跑主程序（前台运行，Ctrl+C 退出）。

    **执行前再核一遍路径**：`python`（虚拟环境解释器）和 `run.py` 都必须
    真实落在 `install_dir` 里面。这样即便前面某处判断被绕过（比如状态文件、
    marker 被伪造），也不会去执行安装目录外的程序。
    """
    entry = os.path.join(install_dir, "run.py")
    if not os.path.exists(entry):
        raise SetupError("安装目录里找不到 run.py：%s" % install_dir)
    root = os.path.realpath(os.path.abspath(install_dir))
    for label, target in (("run.py", entry), ("虚拟环境解释器", python)):
        real = os.path.realpath(os.path.abspath(target))
        try:
            inside = os.path.commonpath([root, real]) == root
        except ValueError:
            inside = False
        if not inside:
            raise SetupError(
                "%s 不在安装目录里，拒绝执行：\n  安装目录 %s\n  实际路径 %s"
                % (label, root, real))
    cmd = [python, entry] + list(extra_args or [])
    try:
        # 前台交接：让主程序直接接管这个终端（能看到彩色日志、能 Ctrl+C）
        return subprocess.call(cmd, cwd=install_dir)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        # 解释器文件存在但跑不起来（损坏、架构不符、权限不足）
        raise SetupError("启动失败：%s\n  解释器：%s" % (exc, python))


def _same_path(left: str, right: str) -> bool:
    """两个路径是否指向同一个真实位置（消掉符号链接/大小写/相对路径差异）。"""
    try:
        a = os.path.realpath(os.path.abspath(left))
        b = os.path.realpath(os.path.abspath(right))
    except OSError:
        return False
    if os.name == "nt":
        return a.lower() == b.lower()
    return a == b


def is_installed(install_dir: str) -> bool:
    """判断这个目录里是否已经装好了主程序。

    **只检查文件存在是不够的**：marker 里记了 `install_dir`，必须和实际路径
    对得上才算数。否则任何人拿一份 marker + run.py 放到一个目录里，
    就会被当成"已安装" —— 自动模式随后会执行那个目录的 run.py（本地代码执行）。
    """
    marker = os.path.join(install_dir, paths.MARKER_FILE)
    entry = os.path.join(install_dir, "run.py")
    if not (os.path.exists(marker) and os.path.exists(entry)):
        return False
    # marker 与 run.py 本身是符号链接 → 不信任
    if os.path.islink(marker) or os.path.islink(entry):
        return False
    data = read_marker(install_dir)
    recorded = str(data.get("install_dir") or "")
    if not recorded:
        return False
    return _same_path(recorded, install_dir)


def read_marker(install_dir: str) -> Dict:
    marker = os.path.join(install_dir, paths.MARKER_FILE)
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_marker(install_dir: str, data: Dict) -> None:
    """写安装记录。记录里存**规范化后的绝对路径**，供 `is_installed()` 校验。"""
    marker = os.path.join(install_dir, paths.MARKER_FILE)
    payload = dict(data or {})
    payload["install_dir"] = os.path.realpath(os.path.abspath(install_dir))
    with open(marker, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
