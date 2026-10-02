# -*- coding: utf-8 -*-
"""日志模块（合并版）

- 控制台彩色输出（时间灰、WARNING 黄、ERROR 红），日志文件始终纯文本；
- 按大小自动分割（`data/logs/YYYYMMDD_HHMMSS.txt`）；
- 支持按级别、按文件读取，供后台「日志查看」页使用；
- 保留天数自动清理。

来源：合并 `API_qqbot/core/logger.py` 的着色与分割能力，
并补上后台日志页需要的读取/筛选接口。
"""

import logging
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

from core import paths

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _supports_color() -> bool:
    """是否给控制台上色（重定向到文件/管道或非 TTY 时不上色）。"""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return bool(sys.stdout) and sys.stdout.isatty()
    except Exception:
        return False


class _ColorFormatter(logging.Formatter):
    """控制台格式：时间灰、级别按严重程度着色，正文不着色。"""

    GREY = "\x1b[90m"
    YELLOW = "\x1b[33m"
    RED = "\x1b[31m"
    BOLD_RED = "\x1b[1;31m"
    RESET = "\x1b[0m"

    def __init__(self, use_color: bool = True):
        super().__init__("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.use_color:
            return text
        stamp = self.formatTime(record, self.datefmt)
        color = ""
        if record.levelno >= logging.CRITICAL:
            color = self.BOLD_RED
        elif record.levelno >= logging.ERROR:
            color = self.RED
        elif record.levelno >= logging.WARNING:
            color = self.YELLOW
        # 时间灰色 + 级别着色，正文保持原色
        return (f"{self.GREY}{stamp}{self.RESET} "
                f"{color}[{record.levelname}]{self.RESET} {record.getMessage()}"
                + (f"\n{self.GREY}{record.exc_text}{self.RESET}" if record.exc_text else ""))


class _PlainFormatter(logging.Formatter):
    """文件格式：纯文本，永远不带 ANSI 颜色。"""

    def __init__(self):
        super().__init__("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


class _SizedFileHandler(logging.FileHandler):
    """写入后检查大小的文件 handler：超过上限就让 Logger 换个新文件。

    （原实现里"按大小分割"的检查函数从未被调用，日志文件会一直涨；这里真正执行。）
    """

    def __init__(self, filename: str, owner, max_bytes: float):
        super().__init__(filename, encoding="utf-8")
        self._owner = owner
        self._max_bytes = max_bytes

    def emit(self, record: logging.LogRecord):
        super().emit(record)
        try:
            if self.stream and self.stream.tell() >= self._max_bytes:
                self._owner.request_roll()
        except Exception:
            pass


class _LibraryRelay(logging.Handler):
    """把"非本程序命名空间"的日志转发给主 logger。

    为什么需要：`core/` 下的模块用 `logging.getLogger(__name__)`（如 `core.storage`），
    它们不会走主 logger。最初的实现是把文件 handler 同时挂到根 logger 上，
    但那样"再创建一个 Logger 实例"就会把前一个实例的文件 handler 顶掉，
    出现"日志文件是空的"这种怪现象。

    改成：主 logger 独占自己的 handler；根 logger 上只放这个转发器，
    把第三方/子模块的记录交给主 logger 统一落盘。
    """

    def __init__(self, target: logging.Logger):
        super().__init__(level=logging.NOTSET)
        self._target = target

    def emit(self, record: logging.LogRecord):
        name = record.name or ""
        if name.startswith("qqbotmerged"):
            return                       # 主 logger 自己写，避免重复
        if record.name in ("werkzeug", "urllib3", "requests"):
            record = logging.makeLogRecord(record.__dict__)   # 复制一份再改，避免影响其它 handler
        self._target.handle(record)


class _AccessLogGate(logging.Filter):
    """HTTP 访问日志总闸（按配置实时生效，避免轮询把控制台刷屏）。

    - `web.log_access_requests=false` → 丢弃所有 HTTP 访问日志；
    - `web.log_polling_requests=false`（默认）→ 丢弃轮询/静态资源请求的日志。

    开关每次记录时都会读取当前配置，所以在网页上改完立即生效。
    """

    _ACCESS_RE = re.compile(r'"[A-Z]+ [^"]* HTTP/1\.\d"')
    _POLLING_MARKERS = ('"GET /api/chat/stream', '"GET /api/chat/conversations',
                        '"GET /api/chat/messages', '"GET /api/chat/read',
                        '"GET /api/admin/status', '"GET /api/admin/stats',
                        '"GET /api/admin/logs', '"GET /admin/logs',
                        '"GET /health', '"GET /api/chat/image', '"GET /media/',
                        '"GET /static/')

    def __init__(self, config_source):
        super().__init__()
        self._config_source = config_source

    def _flags(self):
        try:
            config = self._config_source()
            return (config.bool_of("web", "log_access_requests", default=False),
                    config.bool_of("web", "log_polling_requests", default=False))
        except Exception:
            return (False, False)

    def filter(self, record: logging.LogRecord) -> bool:
        log_access, log_polling = self._flags()
        try:
            message = _ANSI_RE.sub("", record.getMessage())
        except Exception:
            return True
        if not self._ACCESS_RE.search(message):
            return True                    # 非访问日志（启动信息、异常）照常输出
        if not log_access:
            return False
        if not log_polling and any(marker in message for marker in self._POLLING_MARKERS):
            return False
        return True


class Logger:
    """日志管理器：控制台 + 文件（按大小分割），并提供后台读取接口。

    说明：`core/` 下的模块都用 `logging.getLogger(__name__)` 记录日志，
    因此这里同时把**根 logger** 配置好，子 logger 才能把 INFO 级别的日志
    真正写进文件（否则按 Python 默认只输出 WARNING 以上，排查问题时看不到关键信息）。
    """

    def __init__(self, max_size_mb: float = 10, console_color: bool = True, level: str = "INFO",
                 log_dir: str = None, keep_days: int = 30, config_source=None):
        self.log_dir = log_dir or paths.LOG_DIR
        self.keep_days = int(keep_days or 0)
        self.max_size = max(0.5, float(max_size_mb or 10)) * 1024 * 1024
        os.makedirs(self.log_dir, exist_ok=True)

        self.logger = logging.getLogger("qqbotmerged")
        self.logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        self.logger.propagate = False
        self._handler = None
        self._file_path = None
        self._lock = threading.RLock()
        self._rolling = False

        # 根 logger 也交给同一套 handler：子 logger（core.storage 等）才能落盘
        self._root = logging.getLogger()
        self._root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        self._console_handler = None

        self._install_handlers(console_color)
        self._install_access_log_gate(config_source)
        self._cleanup_old_logs()

    def _install_access_log_gate(self, config_source):
        """给 werkzeug 的访问日志装总闸（否则每个轮询请求都会往控制台刷一行）。"""
        self._access_gate = None
        if config_source is None:
            return
        self._access_gate = _AccessLogGate(config_source)
        werkzeug_logger = logging.getLogger("werkzeug")
        # 先摘掉自己以前装的，避免重复添加
        for existing in list(werkzeug_logger.filters):
            if isinstance(existing, _AccessLogGate):
                werkzeug_logger.removeFilter(existing)
        werkzeug_logger.addFilter(self._access_gate)

    # ------------------------------------------------------------------ 内部
    def _install_handlers(self, console_color: bool):
        # 主 logger：控制台 + 文件（重建时先清掉自己以前装的）
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(_ColorFormatter(_supports_color() and console_color))
        console._qqbotmerged = True          # type: ignore[attr-defined]
        self._console_handler = console
        self.logger.addHandler(console)

        self._roll_file()

        # 根 logger：只放一个转发器，把子模块/第三方日志交给主 logger
        for handler in list(self._root.handlers):
            if getattr(handler, "_qqbotmerged", False):
                self._root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        relay = _LibraryRelay(self.logger)
        relay._qqbotmerged = True            # type: ignore[attr-defined]
        self._root.addHandler(relay)

    def _roll_file(self):
        """创建新的日志文件（按启动时间和大小滚动）。"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.log_dir, f"{stamp}.txt")
        index = 1
        while os.path.exists(path):
            path = os.path.join(self.log_dir, f"{stamp}_{index}.txt")
            index += 1
        handler = _SizedFileHandler(path, self, self.max_size)
        handler.setFormatter(_PlainFormatter())
        handler._qqbotmerged = True          # type: ignore[attr-defined]
        with self._lock:
            if self._handler is not None:
                self.logger.removeHandler(self._handler)
                try:
                    self._handler.close()
                except Exception:
                    pass
            self.logger.addHandler(handler)
            self._handler = handler
            self._file_path = path

    def request_roll(self):
        """由 _SizedFileHandler 在超过大小上限时调用（日志线程内，需防重入）。"""
        with self._lock:
            if self._rolling:
                return
            self._rolling = True
        try:
            self._roll_file()
            self.logger.info("日志文件已达大小上限，已切换到新文件：%s",
                             os.path.basename(self._file_path or ""))
        finally:
            self._rolling = False

    def _maybe_roll(self, record: logging.LogRecord):
        try:
            if self._file_path and os.path.exists(self._file_path) \
                    and os.path.getsize(self._file_path) >= self.max_size:
                self._roll_file()
        except OSError:
            pass

    def _cleanup_old_logs(self):
        if self.keep_days <= 0:
            return
        cutoff = time.time() - self.keep_days * 86400
        try:
            for name in os.listdir(self.log_dir):
                if not name.endswith(".txt"):
                    continue
                full = os.path.join(self.log_dir, name)
                try:
                    if os.path.getmtime(full) < cutoff:
                        os.remove(full)
                except OSError:
                    pass
        except OSError:
            pass

    # ------------------------------------------------------------------ 对外
    def get_logger(self) -> logging.Logger:
        return self.logger

    @property
    def current_file(self) -> Optional[str]:
        return self._file_path

    def set_level(self, level: str):
        numeric = getattr(logging, str(level).upper(), logging.INFO)
        self.logger.setLevel(numeric)
        self._root.setLevel(numeric)

    def set_console_color(self, enabled: bool):
        for handler in self.logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setFormatter(_ColorFormatter(_supports_color() and enabled))

    def log(self, level: str, message: str, *args):
        """带级别名写入日志（后台「日志」页可筛选）。"""
        self.logger.log(getattr(logging, str(level).upper(), logging.INFO), message, *args)

    # ------------------------------------------------------- 后台读取（日志页）
    def list_files(self) -> List[Dict[str, object]]:
        """列出日志文件（新的在前）。"""
        items = []
        try:
            for name in os.listdir(self.log_dir):
                if not name.endswith(".txt"):
                    continue
                full = os.path.join(self.log_dir, name)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                items.append({
                    "name": name,
                    "size": stat.st_size,
                    "size_text": _human_size(stat.st_size),
                    "mtime": stat.st_mtime,
                    "time_text": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "current": full == self._file_path,
                })
        except OSError:
            pass
        items.sort(key=lambda item: item["mtime"], reverse=True)
        return items

    def read_tail(self, name: str = "", limit: int = 500,
                  level: str = "", keyword: str = "") -> Dict[str, object]:
        """读取日志尾部若干行，可按级别与关键词过滤（默认读当前文件）。"""
        path = self._resolve(name)
        if not path:
            return {"lines": [], "file": "", "total": 0, "matched": 0}
        limit = max(1, min(int(limit or 500), 5000))
        level = (level or "").upper().strip()
        keyword = (keyword or "").strip().lower()

        lines: List[str] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                raw_lines = handle.readlines()
        except OSError:
            raw_lines = []

        total = len(raw_lines)
        for line in raw_lines:
            plain = _ANSI_RE.sub("", line.rstrip("\n"))
            if level and level not in LEVEL_NAMES:
                level = ""
            if level and f"[{level}]" not in plain:
                continue
            if keyword and keyword not in plain.lower():
                continue
            lines.append(plain)

        matched = len(lines)
        return {
            "file": os.path.basename(path),
            "lines": lines[-limit:],
            "total": total,
            "matched": matched,
            "level": level,
            "keyword": keyword,
        }

    def read_file(self, name: str) -> Optional[str]:
        """读出整个日志文件（供下载）。"""
        path = self._resolve(name)
        if not path:
            return None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return None

    def clear_older_than(self, days: int) -> int:
        """删除 N 天前的日志文件，返回删除数量。"""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        removed = 0
        for item in self.list_files():
            if item["current"] or item["mtime"] >= cutoff:
                continue
            try:
                os.remove(os.path.join(self.log_dir, item["name"]))
                removed += 1
            except OSError:
                pass
        return removed

    def _resolve(self, name: str) -> Optional[str]:
        """把文件名解析成日志目录内的真实路径（防目录穿越）。"""
        if not name:
            return self._file_path
        safe = os.path.basename(name)
        full = os.path.join(self.log_dir, safe)
        if not os.path.isfile(full):
            return self._file_path if not name else None
        return full


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def mask_transfer_code(text: str) -> str:
    """日志脱敏：把 6 位数字转移码替换成 ******（兼容旧模块的调用）。"""
    if not text:
        return text
    return re.sub(r"(?<!\d)(\d{6})(?!\d)", "******", str(text))
