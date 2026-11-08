# -*- coding: utf-8 -*-
"""审计日志：记录"谁在什么时候做了什么敏感操作"。

**记什么**：关闭程序、安装更新、修改配置、清空数据、群发禁言等会改变状态或
有破坏性的操作。**不记**普通浏览（聊天记录、刷新状态）——那会把日志淹没。

**为什么单独一个文件**：程序日志按大小轮转、会被清理、还会被云同步上传；
审计记录需要更长的保留期，也不该混在调试信息里。

格式：每行一条 JSON（便于脚本分析），字段：
    {ts, time, action, target, client, result, detail}

**脱敏**：`detail` 里绝不写令牌/密钥原文，超过 200 字符截断。
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

from core import paths

logger = logging.getLogger(__name__)

MAX_DETAIL = 200            # 单条 detail 最大长度
MAX_FILE_BYTES = 8 * 1024 * 1024   # 超过 8MB 轮转一份
_lock = threading.Lock()

# detail 里出现这些键名时打码
_SENSITIVE_KEYS = ("token", "secret", "password", "passwd", "api_key", "apikey",
                   "access_token", "api_token", "authorization", "credential")


def audit_path() -> str:
    """审计日志路径（放 data/logs/ 下，与普通日志同级但独立文件）。"""
    return os.path.join(paths.LOG_DIR, "audit.log")


def _redact(value: Any, depth: int = 0) -> Any:
    """把字典里像密钥的字段替换成掩码（递归，限制深度）。"""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            name = str(key).lower()
            if any(marker in name for marker in _SENSITIVE_KEYS):
                cleaned[key] = "***"
            else:
                cleaned[key] = _redact(item, depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value[:MAX_DETAIL] + ("…" if len(value) > MAX_DETAIL else "")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:MAX_DETAIL]


def record(action: str, target: str = "", client: str = "",
           result: str = "ok", detail: Optional[Any] = None) -> str:
    """写一条审计记录，返回记录行的 JSON 文本（便于测试断言）。

    写失败**不影响业务**：只记一条 warning，绝不因为审计日志出问题就让操作失败。
    """
    entry: Dict[str, Any] = {
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": str(action or "")[:80],
        "target": str(target or "")[:120],
        "client": str(client or "")[:80],
        "result": str(result or "ok")[:20],
        "detail": _redact(detail) if detail is not None else {},
    }
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _lock:
            directory = os.path.dirname(audit_path())
            os.makedirs(directory, exist_ok=True)
            _rotate_if_needed()
            with open(audit_path(), "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError as exc:
        logger.warning("写审计日志失败（不影响操作）：%s", exc)
    except (ValueError, TypeError) as exc:
        # 路径里有非法字符、编码问题之类：同样不能让业务失败
        logger.warning("审计日志路径异常（不影响操作）：%s", exc)
    logger.info("[审计] %s %s by %s -> %s", entry["action"], entry["target"],
                entry["client"] or "?", entry["result"])
    return line


def _rotate_if_needed():
    """超过上限就改名留一份（只留最近一份，避免占空间）。"""
    try:
        path = audit_path()
        if os.path.isfile(path) and os.path.getsize(path) > MAX_FILE_BYTES:
            backup = path + ".1"
            try:
                if os.path.isfile(backup):
                    os.remove(backup)
            except OSError:
                pass
            os.replace(path, backup)
    except OSError:
        pass


def read_tail(limit: int = 200) -> list:
    """读最近的若干条（给诊断/排查用），返回解析后的列表。"""
    limit = max(1, min(int(limit or 200), 2000))
    path = audit_path()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    out = []
    for raw in lines[-limit:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except ValueError:
            out.append({"raw": raw})
    return out


def client_of(request) -> str:
    """从 Flask 请求里取"操作来源"，用于审计记录。"""
    try:
        forwarded = ""
        try:
            forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        except Exception:
            forwarded = ""
        address = request.remote_addr or ""
        return forwarded or address or "unknown"
    except Exception:
        return "unknown"
