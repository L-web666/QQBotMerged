# -*- coding: utf-8 -*-
"""卸载与数据导出。

**两件事分开做**：
  · **导出**：把 `config.json` + `data/` 打包成一个 zip，放哪儿都行；
  · **卸载**：删掉安装目录。删之前先问要不要导出 —— 不导出就删，数据就真没了。

`data/` 里的东西是用户自己的（消息库、媒体、日志），所以默认行为是**先问**，
而不是默认删除。导出用 zip，Windows / Linux / macOS 都能双击打开。
"""

import os
import shutil
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

from . import paths

# 导出时一定要带上的（存在才带）
EXPORT_ITEMS = ("config.json", "data")
# 不算「数据」的东西，导出时不带（体积大、可重建、或纯缓存）
EXPORT_SKIP_DIRS = {"__pycache__", ".venv", ".git", "node_modules"}
EXPORT_SKIP_SUFFIXES = (".pyc", ".pyo", ".log.1", ".db-wal", ".db-shm")


class UninstallError(RuntimeError):
    """卸载/导出过程中的可预期错误。"""


# ---------------------------------------------------------------- 统计
def measure(install_dir: str) -> Dict:
    """统计安装目录里有什么、多大、有多少条消息，卸载前给用户看。

    返回 {exists, total_bytes, total_files, data_bytes, data_files,
          has_config, has_data, extra}，`extra` 是既不属于程序也不属于数据的
    条目（用户自己放进去的东西）。
    """
    info = {
        "exists": os.path.isdir(install_dir),
        "total_bytes": 0, "total_files": 0,
        "data_bytes": 0, "data_files": 0,
        "has_config": False, "has_data": False,
        "extra": [],
    }
    if not info["exists"]:
        return info
    info["has_config"] = os.path.exists(os.path.join(install_dir, "config.json"))
    info["has_data"] = os.path.isdir(os.path.join(install_dir, "data"))
    for name in os.listdir(install_dir):
        full = os.path.join(install_dir, name)
        if name == "data":
            size, count = _walk_size(full)
            info["data_bytes"] = size
            info["data_files"] = count
        if os.path.isfile(full):
            try:
                info["total_bytes"] += os.path.getsize(full)
                info["total_files"] += 1
            except OSError:
                pass
        elif os.path.isdir(full):
            size, count = _walk_size(full)
            info["total_bytes"] += size
            info["total_files"] += count
    # 既不是程序文件也不是数据的条目
    program_items = set(EXPORT_ITEMS) | {
        "run.py", "core", "web", "plugins", "docs", "tools", "tests",
        "build_exe.py", "build_exe.bat", "requirements.txt", "README.md",
        "AGENTS.md", "LICENSE", "PACKAGING.md", "start.bat", "start.sh",
        paths.MARKER_FILE, paths.VENV_DIR, ".gitattributes", ".gitignore",
        "config配置说明文件.txt", ".qqbot-download",
    }
    for name in os.listdir(install_dir):
        if name not in program_items:
            info["extra"].append(name)
    return info


def _walk_size(root: str) -> Tuple[int, int]:
    total = 0
    count = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXPORT_SKIP_DIRS]
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
                count += 1
            except OSError:
                continue
    return total, count


def human_size(num: int) -> str:
    size = float(max(0, num))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return "%d %s" % (int(size), unit)
            return "%.1f %s" % (size, unit)
        size /= 1024
    return "%.1f GB" % size


# ---------------------------------------------------------------- 导出
def default_export_path(install_dir: str) -> str:
    """默认导出到哪：安装目录**外面**，避免连着一起被删。"""
    parent = os.path.dirname(os.path.abspath(install_dir).rstrip("/\\")) or "."
    base = os.path.basename(os.path.abspath(install_dir).rstrip("/\\")) or "qqbot"
    stamp = _timestamp()
    return os.path.join(parent, "%s-backup-%s.zip" % (base, stamp))


def _timestamp() -> str:
    import time
    return time.strftime("%Y%m%d_%H%M%S")


def export_data(install_dir: str, target: str,
                progress: Optional[Callable[[str], None]] = None) -> Dict:
    """把 `config.json` + `data/` 打包成 zip。

    返回 {path, bytes, files, skipped}。`skipped` 是被跳过的文件
    （正在被占用、读不了等），不会因此中断整个导出。
    """
    if not os.path.isdir(install_dir):
        raise UninstallError("安装目录不存在：%s" % install_dir)
    config = os.path.join(install_dir, "config.json")
    data_dir = os.path.join(install_dir, "data")
    if not os.path.exists(config) and not os.path.isdir(data_dir):
        raise UninstallError("这个目录里没有 config.json 也没有 data/，没什么可导出的")

    target = os.path.abspath(target)
    if not target.lower().endswith(".zip"):
        target += ".zip"
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        raise UninstallError("导出位置的上级目录不存在：%s" % parent)
    # 不允许导到 data/ 里面（边写边打包会把自己装进去）
    try:
        inside = os.path.commonpath([os.path.abspath(target), os.path.abspath(data_dir)]) \
            == os.path.abspath(data_dir)
    except ValueError:
        inside = False
    if inside:
        raise UninstallError("导出文件不能放在 data/ 目录里面")

    if progress:
        progress("正在打包配置与数据 …")
    files = 0
    skipped: List[str] = []
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as bundle:
            if os.path.exists(config):
                try:
                    bundle.write(config, "config.json")
                    files += 1
                except OSError:
                    skipped.append("config.json")
            if os.path.isdir(data_dir):
                for base, dirs, names in os.walk(data_dir):
                    dirs[:] = [d for d in dirs if d not in EXPORT_SKIP_DIRS]
                    for name in names:
                        full = os.path.join(base, name)
                        relative = os.path.relpath(full, install_dir).replace("\\", "/")
                        try:
                            bundle.write(full, relative)
                            files += 1
                        except (OSError, ValueError):
                            # 被占用/没权限：跳过并记下来，不中断整个导出
                            skipped.append(relative)
    except OSError as exc:
        _quiet_remove(target)
        raise UninstallError("写导出文件失败：%s" % exc)
    try:
        size = os.path.getsize(target)
    except OSError:
        size = 0
    return {"path": target, "bytes": size, "files": files, "skipped": skipped}


def _quiet_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- 卸载
def uninstall(install_dir: str, progress: Optional[Callable[[str], None]] = None) -> Dict:
    """删除整个安装目录。

    **调用方必须先确认过**（问过用户要不要导出）。这里只负责删，并且：
    · 校验目录看起来确实是本程序的安装目录 —— 防止把用户随手输的路径删掉；
    · 删不掉的文件单独列出来（Windows 上文件被占用很常见）。
    """
    install_dir = os.path.abspath(install_dir)
    if not os.path.isdir(install_dir):
        raise UninstallError("安装目录不存在：%s" % install_dir)
    # 安全检查：必须是本程序的安装目录
    looks_like_ours = (
        os.path.exists(os.path.join(install_dir, "run.py"))
        or os.path.exists(os.path.join(install_dir, paths.MARKER_FILE))
        or os.path.exists(os.path.join(install_dir, "config.json")))
    if not looks_like_ours:
        raise UninstallError(
            "这个目录看起来不是 QQBotMerged 的安装目录（没有 run.py / 安装记录），"
            "为安全起见没有删除：%s" % install_dir)
    # 再挡一次：不允许删到根目录/盘符/用户主目录
    guarded = {os.path.abspath(os.sep), os.path.abspath(os.path.expanduser("~"))}
    try:
        guarded.add(os.path.abspath(os.path.dirname(os.path.expanduser("~"))))
    except Exception:
        pass
    if install_dir in guarded or len(install_dir) <= 3:
        raise UninstallError("拒绝删除这个路径（太靠近根目录）：%s" % install_dir)

    if progress:
        progress("正在删除 …")
    failed: List[str] = []
    import sys

    def _on_error(func, path, exc_info):
        failed.append(path)

    # Python 3.12 起推荐 onexc（onerror 仍存在但会发弃用警告）。
    # **不能只靠 try/except TypeError**：3.12+ 里 onerror 依然存在，
    # 传它不会报错，只会静默发警告 —— 所以按版本选参数。
    if sys.version_info >= (3, 12):
        shutil.rmtree(install_dir, onexc=_on_error)
    else:
        shutil.rmtree(install_dir, onerror=_on_error)
    removed = not os.path.exists(install_dir)
    return {"removed": removed, "failed": failed[:20],
            "failed_count": len(failed), "path": install_dir}


def forget_state(install_dir: str = "") -> int:
    """把"记住装在哪"的记录清掉（卸载后别再指向一个不存在的目录）。

    返回清掉了几处。
    """
    import json
    count = 0
    for path in paths._state_files(install_dir):
        try:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            recorded = str((data or {}).get("install_dir") or "")
            if install_dir and recorded and \
                    os.path.abspath(recorded) != os.path.abspath(install_dir):
                continue          # 这条记录指向别的安装，别动
            os.remove(path)
            count += 1
        except Exception:
            continue
    return count
