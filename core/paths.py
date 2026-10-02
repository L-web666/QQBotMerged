# -*- coding: utf-8 -*-
"""路径与运行环境工具：统一程序根目录、数据目录、打包（PyInstaller）兼容。"""

import os
import sys


def get_base_dir() -> str:
    """程序根目录：开发环境=本文件上两级目录；打包后=exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = get_base_dir()

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CONFIG_DOC_FILE = os.path.join(BASE_DIR, "config配置说明文件.txt")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
DB_FILE = os.path.join(DATA_DIR, "messages.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
PLUGIN_DIR = os.path.join(BASE_DIR, "plugins")
STATIC_DIR = os.path.join(BASE_DIR, "web", "static")
TEMPLATE_DIR = os.path.join(BASE_DIR, "web", "templates")


def ensure_dirs():
    """创建运行所需目录（幂等）。"""
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
