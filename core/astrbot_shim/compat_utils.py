# -*- coding: utf-8 -*-
"""小工具（_module / 版本比较 / YAML / schema 默认值）。

本模块是 `core/astrbot_shim/` 包的一部分（由 `core/astrbot_compat.py` 拆分而来）；
`core/astrbot_compat.py` 只是兼容再导出层，`from core.astrbot_compat import X` 照旧可用。
"""

import asyncio
import copy
import importlib
import importlib.util
import inspect
import json
import logging
import os
import sys
import threading
import time
import types
from enum import Enum, IntFlag
from typing import Any, Callable, Dict, List, Optional, Tuple
from core.astrbot_shim.base import logger
from core.astrbot_shim.base import logger




# ======================================================================================
# 垫片安装
# ======================================================================================
def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


# ======================================================================================
# 插件加载器
# ======================================================================================
def _deep_update(base: Dict[str, Any], extra: Dict[str, Any]):
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def _schema_defaults(schema: Dict[str, Any]) -> Dict[str, Any]:
    """按官方 `_conf_schema.json` 递归取默认值（object 类型钻进 items）。"""
    out: Dict[str, Any] = {}
    for key, spec in (schema or {}).items():
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("type") or "")
        if kind == "object" and isinstance(spec.get("items"), dict):
            out[key] = _schema_defaults(spec["items"])
        elif "default" in spec:
            out[key] = copy.deepcopy(spec["default"])
        elif kind == "int":
            out[key] = 0
        elif kind == "float":
            out[key] = 0.0
        elif kind == "bool":
            out[key] = False
        elif kind == "list" or kind == "file":
            out[key] = []
        elif kind == "dict" or kind == "template_list":
            out[key] = {} if kind == "dict" else []
        else:
            out[key] = ""
    return out


def _version_tuple(text: str) -> Tuple[int, ...]:
    parts = []
    for chunk in str(text or "").replace("v", "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or [0])


def _version_satisfies(current: str, requirement: str) -> bool:
    """极简的版本范围判断：支持 `>=4.17.0`、`>4.0`、`<=x`、`<x`、`==x`、`4.17.0`。"""
    req = str(requirement or "").strip()
    if not req:
        return True
    for op in (">=", "<=", "==", ">", "<"):
        if req.startswith(op):
            target = _version_tuple(req[len(op):])
            now = _version_tuple(current)
            size = max(len(target), len(now))
            now = now + (0,) * (size - len(now))
            target = target + (0,) * (size - len(target))
            if op == ">=":
                return now >= target
            if op == "<=":
                return now <= target
            if op == "==":
                return now == target
            if op == ">":
                return now > target
            return now < target
    # 没写运算符时按"最低版本"理解
    now, target = _version_tuple(current), _version_tuple(req)
    size = max(len(target), len(now))
    return now + (0,) * (size - len(now)) >= target + (0,) * (size - len(target))


def _load_yaml(path: str) -> Dict[str, Any]:
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}
    except ImportError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning("解析 %s 失败：%s", path, exc)
        return {}
    # 没有 PyYAML 时的极简兜底：只认 "key: value"
    out: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                out[key.strip()] = value.strip().strip("'\"")
    except OSError:
        return {}
    return out
