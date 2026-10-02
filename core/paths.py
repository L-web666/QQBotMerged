# -*- coding: utf-8 -*-
"""路径与运行环境工具：统一程序根目录、资源目录、数据目录，兼容 PyInstaller 打包。

打包（exe）后的约定：
- **程序根目录** = exe 所在目录（`config.json`、`data/`、`plugins/` 都放这里，用户看得见、能改）；
- **资源目录** = 打进 exe 内部的那份（`sys._MEIPASS`，只有前端 `web/`）；
- 前端资源优先用 exe 旁边的 `web/`（想改样式/脚本就直接改），没有才用 exe 内置的那份；
- **插件不随 exe 附带**：`plugins/` 只会被创建成空目录，插件由用户自己放进去。
"""

import os
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
    """创建运行所需目录（幂等）。

    `plugins/` 只会被建成**空目录**：插件由用户自己放进 `plugins/<插件名>/`，
    程序不附带、也不自动释放任何插件。
    """
    for path in (DATA_DIR, LOG_DIR, MEDIA_DIR, UPLOAD_DIR, PLUGIN_DIR):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass


def rel(path: str) -> str:
    """把绝对路径转成相对程序根目录的展示路径。"""
    try:
        return os.path.relpath(path, BASE_DIR)
    except ValueError:
        return path
