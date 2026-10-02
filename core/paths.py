# -*- coding: utf-8 -*-
"""路径与运行环境工具：统一程序根目录、资源目录、数据目录，兼容 PyInstaller 打包。

打包（exe）后的约定：
- **程序根目录** = exe 所在目录（`config.json`、`data/`、`plugins/` 都放这里，用户看得见、能改）；
- **资源目录** = 打进 exe 内部的那份（`sys._MEIPASS`，含 web/ 与示例插件）；
- 前端资源优先用 exe 旁边的 `web/`（想改样式/脚本就直接改），没有才用 exe 内置的那份。
"""

import os
import shutil
import sys


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def get_base_dir() -> str:
    """程序根目录：开发环境=本文件上两级目录；打包后=exe 所在目录。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_resource_dir() -> str:
    """打包进 exe 的资源目录（PyInstaller 解包目录）；开发环境=程序根目录。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", get_base_dir())
    return get_base_dir()


def resource_path(*parts: str) -> str:
    """资源路径：exe 旁边有同名目录就用它（方便自己改前端），否则用 exe 内置的。"""
    external = os.path.join(BASE_DIR, *parts)
    if os.path.exists(external):
        return external
    return os.path.join(RESOURCE_DIR, *parts)


BASE_DIR = get_base_dir()
RESOURCE_DIR = get_resource_dir()

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CONFIG_DOC_FILE = os.path.join(BASE_DIR, "config配置说明文件.txt")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
DB_FILE = os.path.join(DATA_DIR, "messages.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
PLUGIN_DIR = os.path.join(BASE_DIR, "plugins")
STATIC_DIR = resource_path("web", "static")
TEMPLATE_DIR = resource_path("web", "templates")


def ensure_dirs():
    """创建运行所需目录（幂等），并把内置的示例插件释放到 plugins/。"""
    for path in (DATA_DIR, LOG_DIR, MEDIA_DIR, UPLOAD_DIR, PLUGIN_DIR):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
    try:
        seed_default_plugins()
    except OSError:
        pass


def seed_default_plugins() -> int:
    """首次运行时，把打进 exe 的示例插件复制到 exe 旁边的 plugins/（已存在的不动）。

    返回复制的插件个数（开发环境或没有内置插件时返回 0）。
    """
    if not is_frozen():
        return 0
    source = os.path.join(RESOURCE_DIR, "plugins")
    target = PLUGIN_DIR
    if not os.path.isdir(source) or os.path.abspath(source) == os.path.abspath(target):
        return 0
    copied = 0
    try:
        names = sorted(os.listdir(source))
    except OSError:
        return 0
    for name in names:
        src = os.path.join(source, name)
        dst = os.path.join(target, name)
        if not os.path.isdir(src) or os.path.exists(dst):
            continue
        try:
            shutil.copytree(src, dst)
            copied += 1
        except OSError:
            continue
    return copied


def rel(path: str) -> str:
    """把绝对路径转成相对程序根目录的展示路径。"""
    try:
        return os.path.relpath(path, BASE_DIR)
    except ValueError:
        return path
