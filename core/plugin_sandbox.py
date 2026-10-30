# -*- coding: utf-8 -*-
"""插件**进程内软限制**（soft sandbox）。

## 这是什么，不是什么

**软限制**：在**同一个 Python 进程**里，限制插件能做什么 —— 收紧导入白名单、拦住危险的
内置函数、把文件访问限制在插件自己的数据目录、把网络出口限制在已知域名。
目的是挡住"手滑装了来路不明/写得很糟的插件"这类**现实场景**，以及**降低误伤范围**。

**不是硬沙箱**：插件仍然运行在主进程里，一个刻意要绕过的插件总能找到办法
（比如通过进程内已有对象的属性链）。真正的隔离需要独立进程 + 独立低权限账户，
不在本模块范围内。请不要把它当成"可以随意运行不可信代码"的保证。

## 三个开关（`plugins.sandbox`）

- `off`：完全不干预（排查问题时用）
- `loose`（默认）：只拦**最危险**的操作（起子进程/执行命令），其余放行并记一条告警
- `strict`：导入白名单 + 文件限制在插件数据目录 + 网络白名单（只允许 QQ / AI 服务域名）

默认取 `loose`：既不影响现有插件，又能挡住"插件里 `os.system('rm -rf')`"这种最坏情况。
"""

import builtins
import logging
import os
import sys
from typing import Any, Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)

MODES = ("off", "loose", "strict")

# ---------------------------------------------------------------------------
# 导入白名单（strict 模式）
# ---------------------------------------------------------------------------
# 说明：白名单是"允许清单"，不在这里的一律拒绝。为避免误伤，把**纯计算/数据处理**的
# 标准库尽量放进来；凡是能直接执行命令、读写系统、调试注入、反序列化执行代码的模块都不放。
ALLOWED_MODULES: Set[str] = {
    # 基础
    "abc", "aifc", "annotations", "array", "ast", "asyncio", "base64", "binascii", "bisect",
    "calendar", "cmath", "codecs", "collections", "colorsys", "contextlib", "contextvars",
    "copy", "csv", "dataclasses", "datetime", "decimal", "difflib", "dis", "email", "enum",
    "fnmatch", "fractions", "functools", "graphlib", "gzip", "hashlib", "heapq", "hmac",
    "html", "http", "inspect", "io", "ipaddress", "itertools", "json", "keyword", "linecache",
    "locale", "logging", "math", "mimetypes", "numbers", "operator", "pathlib", "pprint",
    "queue", "random", "re", "secrets", "shlex", "shutil", "socket", "sqlite3", "statistics",
    "string", "stringprep", "struct", "textwrap", "time", "timeit", "traceback", "types",
    "typing", "unicodedata", "unittest", "urllib", "uuid", "warnings", "weakref", "zoneinfo",
    "tempfile", "glob",
    # `os` 必须放行：插件普遍用 os.path / os.environ，沙箱自身装补丁也要 import os。
    # 真正危险的是 os.system / os.popen / os.exec* / os.spawn*，它们在上面的补丁里被单独拦掉。
    "os", "os.path", "sys", "importlib",
    # 解释器内部/实现模块：插件与宿主都会用到，拦了会让沙箱自己装不上补丁
    "_io", "_thread", "_collections", "_collections_abc", "_functools", "_datetime",
    "_json", "_socket", "_ssl", "_hashlib", "_struct", "_random", "_bisect", "_heapq",
    "_operator", "_contextvars", "_asyncio", "_signal", "encodings", "codecs", "builtins",
    "sysconfig", "posix", "nt", "_winapi", "errno", "stat",
    # 网络客户端（插件常见需求：调 AI 服务、调第三方 API）
    "requests", "http.client", "ssl",
    # 本程序的既有 API（插件通过它们与宿主交互）
    "astrbot", "core",
}

# 明确禁止的模块前缀（即使上面误加了也拦）
# 注意只列**真正危险**的：`importlib` 本身要放行（插件导入依赖会用到），
# 禁的是它的动态加载子模块；`sys` 也放行（很多插件用 sys.platform 判断系统）。
DENIED_MODULES: tuple = (
    "subprocess", "multiprocessing", "ctypes", "ctypes.util", "cffi", "pty", "tty",
    "importlib.machinery", "importlib.abc", "imp", "runpy", "pdb", "bdb", "code", "codeop",
    "pickle", "cPickle", "marshal", "shelve", "dill", "resource", "gc",
    "atexit", "faulthandler", "tracemalloc", "platform", "venv", "ensurepip", "pip",
    "setuptools",
)
# 注意：`json`（含 `json.decoder` 之类）**不是**禁项。判断要按"顶层模块名"做，
# 不能用 `name.startswith(denied)`——那样 `json.dumps` 里的 "code" 之类会误伤。

# 允许联网的域名后缀（strict 模式）——QQ 官方接口与常见 AI 服务
ALLOWED_HOSTS: tuple = (
    "qq.com", "qpic.cn", "gtimg.cn", "qqusercontent.com", "myqcloud.com",
    "deepseek.com", "openai.com", "anthropic.com", "aliyuncs.com", "qianwen", "dashscope",
    "bigmodel.cn", "moonshot.cn", "siliconflow.cn", "ollama.com", "127.0.0.1", "localhost",
)


def normalize_mode(value: Any) -> str:
    """把配置值归一成 off / loose / strict（认不出就用最保守的 loose）。"""
    text = str(value or "").strip().lower()
    if text in MODES:
        return text
    # 兼容布尔写法：sandbox=true → strict，false → off
    if text in ("true", "1", "yes", "on"):
        return "strict"
    if text in ("false", "0", "no"):
        return "off"
    return "loose"


def host_allowed(host: str) -> bool:
    """strict 模式下是否允许连接该域名。"""
    name = (host or "").strip().lower()
    if not name:
        return False
    for suffix in ALLOWED_HOSTS:
        if name == suffix or name.endswith("." + suffix) or suffix in name:
            return True
    return False


class SandboxViolation(PermissionError):
    """插件触碰了被限制的能力。"""

    def __init__(self, what: str, detail: str = ""):
        self.what = what
        self.detail = detail
        super().__init__(f"插件沙箱拦截：{what}{('（' + detail + '）') if detail else ''}")


class PluginSandbox:
    """按插件目录生效的软限制（只影响该插件，不会污染其它插件的导入）。

    用法（`AstrBotHost.load()` 内）::

        with PluginSandbox(mode, plugin_dir, data_dir, plugin_name):
            spec.loader.exec_module(module)      # 安装期也受限制

        # 事件派发同样包一层（运行期调用也受限制）::
        with PluginSandbox(...):
            self._invoke(handler["func"], event, call_args)
    """

    def __init__(self, mode: str, plugin_dir: str, data_dir: str,
                 plugin_name: str = "", logger_obj: Optional[logging.Logger] = None):
        self.mode = normalize_mode(mode)
        self.plugin_dir = os.path.abspath(plugin_dir or ".")
        self.data_dir = os.path.abspath(data_dir or self.plugin_dir)
        self.plugin_name = plugin_name or os.path.basename(self.plugin_dir)
        self.log = logger_obj or logger
        self.violations: list = []
        self._saved: Dict[str, Any] = {}
        self._active = False

    # ------------------------------------------------------------------ 上下文管理
    def __enter__(self):
        if self.mode != "off":
            self._install()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._active:
            self._uninstall()
        return False    # 不吞异常

    # ------------------------------------------------------------------ 安装/卸载
    def _install(self):
        self._active = True
        try:
            if self.mode == "strict":
                self._save("__import__", builtins.__import__)
                builtins.__import__ = self._guarded_import
                self._patch_builtin_open()
                self._patch_network()
            # loose / strict 都拦：执行系统命令、起子进程
            self._patch_process()
        except Exception as exc:      # 打补丁失败不能把插件加载搞崩
            self._active = False
            self.log.warning("[沙箱] 安装失败（按不限制处理）：%s", exc)

    def _save(self, key: str, value: Any):
        self._saved.setdefault(key, value)

    def _restore(self, key: str, target: Any, attr: str):
        original = self._saved.get(key)
        if original is None:
            return
        try:
            setattr(target, attr, original)
        except Exception:
            pass

    def _uninstall(self):
        """把装补丁前的原始函数全部放回去。

        用「模块名.属性名」登记，恢复逻辑统一按 key 解析，避免每加一个补丁点
        就要记得在这里补一条 elif（漏一条就会把包装函数留在宿主里，污染其它插件）。
        """
        self._active = False
        import io as _io
        import socket as _socket

        targets = {
            "__import__": (builtins, "__import__"),
            "open": (builtins, "open"),
            "io.open": (_io, "open"),
            "socket.socket.connect": (_socket.socket, "connect"),
            "socket.socket.connect_ex": (_socket.socket, "connect_ex"),
            "socket.getaddrinfo": (_socket, "getaddrinfo"),
        }
        for key, value in list(self._saved.items()):
            if key in targets:
                target, attr = targets[key]
                try:
                    setattr(target, attr, value)
                except Exception:
                    pass
            elif key.startswith("os."):
                try:
                    setattr(os, key[3:], value)
                except Exception:
                    pass
        self._saved.clear()

    # ------------------------------------------------------------------ 记录
    def _violate(self, what: str, detail: str = ""):
        self.violations.append(f"{what}{(': ' + detail) if detail else ''}")
        self.log.warning("[沙箱][%s] 拦截 %s %s", self.plugin_name, what, detail)
        raise SandboxViolation(what, detail)

    # ------------------------------------------------------------------ 导入限制
    def _module_allowed(self, name: str) -> bool:
        if not name:
            return True
        head = name.split(".")[0]
        # 禁项按"按点分隔的完整段"比较：`name.startswith("code")` 会把 `codecs`
        # 误判成 `code`，`startswith("json")` 之类也会误伤同前缀的模块。
        segments = {head, name}
        for denied in DENIED_MODULES:
            if denied in segments:
                return False
            denied_head = denied.split(".")[0]
            if head == denied_head and name != denied_head:
                # 只禁该包下的具体子模块（例如 importlib.machinery），不禁整包
                if name == denied or name.startswith(denied + "."):
                    return False
        if name in ALLOWED_MODULES or head in ALLOWED_MODULES:
            return True
        # 插件自己的模块（同目录下的 xxx.py / 子包）必须放行，
        # 否则 `import utils` 这类内部导入会被当作越界，插件直接不可用。
        if head in self._local_modules():
            return True
        return False

    def _local_modules(self) -> Set[str]:
        """插件目录里可被 import 的顶层模块名（缓存一次）。"""
        cached = getattr(self, "_local_cache", None)
        if cached is not None:
            return cached
        names: Set[str] = set()
        try:
            for entry in os.listdir(self.plugin_dir):
                full = os.path.join(self.plugin_dir, entry)
                if entry.endswith(".py"):
                    names.add(entry[:-3])
                elif os.path.isdir(full) and os.path.isfile(os.path.join(full, "__init__.py")):
                    names.add(entry)
        except OSError:
            pass
        # 宿主给插件挂的模块名（`_qqbot_astrbot_xxx`）也要能用
        names.update({"_qqbot_astrbot_" + self.plugin_name, self.plugin_name})
        self._local_cache = names
        return names

    def _guarded_import(self, name, globals=None, locals=None, fromlist=(), level=0):
        # level > 0 是**相对导入**（如 json 包内部 `from . import decoder`），
        # 模块名不带包前缀，无法按白名单判断；这类导入来自已被允许的包内部，
        # 其顶层包已经过检查，因此直接放行（否则几乎所有标准库包都会被打断）。
        if level and level > 0:
            return self._saved["__import__"](name, globals, locals, fromlist, level)
        if not self._module_allowed(name):
            self._violate("导入模块", name)
        return self._saved["__import__"](name, globals, locals, fromlist, level)

    # ------------------------------------------------------------------ 文件限制
    def _path_allowed(self, path: Any) -> bool:
        try:
            text = os.path.abspath(os.fspath(path))
        except (TypeError, ValueError):
            return True          # 文件对象/描述符之类，交给原生逻辑
        text_parts = (text, os.path.realpath(text))
        for candidate in text_parts:
            if candidate.startswith(self.data_dir):
                return True
            if candidate.startswith(self.plugin_dir):
                return True      # 读自己插件目录（配置、资源）是正常需求
        return False

    def _patch_builtin_open(self):
        import io as _io

        original_open = builtins.open
        sandbox = self

        def guarded_open(file, mode="r", *args, **kwargs):
            writing = any(flag in str(mode) for flag in ("w", "a", "x", "+"))
            if not sandbox._path_allowed(file) and writing:
                sandbox._violate("写文件", str(file))
            if not sandbox._path_allowed(file) and not writing:
                # 读：只警告不拒绝（插件读系统文件很常见，拦了会大面积不兼容）
                sandbox.log.warning("[沙箱][%s] 读取插件目录外的文件：%s",
                                    sandbox.plugin_name, file)
            return original_open(file, mode, *args, **kwargs)

        self._save("open", original_open)
        builtins.open = guarded_open
        self._save("io.open", _io.open)
        _io.open = guarded_open

    # ------------------------------------------------------------------ 进程/命令
    def _patch_process(self):
        sandbox = self

        def blocked(name):
            def _blocked(*args, **kwargs):
                sandbox._violate("执行系统命令/启动进程", f"{name}{args[:1]}")
            return _blocked

        for attr in ("system", "popen", "execv", "execve", "execvp", "spawnv", "spawnl"):
            func = getattr(os, attr, None)
            if callable(func):
                self._save("os." + attr, func)
                setattr(os, attr, blocked("os." + attr))

    # ------------------------------------------------------------------ 网络限制
    def _patch_network(self):
        import socket as _socket

        sandbox = self

        def host_of(address) -> str:
            if isinstance(address, tuple) and address:
                return str(address[0])
            return ""

        # 1) 拦截 connect：挡住直连 IP 与被解析后的地址
        original_connect = _socket.socket.connect
        original_connect_ex = getattr(_socket.socket, "connect_ex", None)

        def guarded_connect(self_sock, address, *args, **kwargs):
            host = host_of(address)
            if host and not host_allowed(host):
                sandbox._violate("连接外部主机", host)
            return original_connect(self_sock, address, *args, **kwargs)

        self._save("socket.connect", original_connect)
        _socket.socket.connect = guarded_connect

        if original_connect_ex is not None:
            def guarded_connect_ex(self_sock, address, *args, **kwargs):
                host = host_of(address)
                if host and not host_allowed(host):
                    sandbox._violate("连接外部主机", host)
                return original_connect_ex(self_sock, address, *args, **kwargs)

            self._save("socket.connect_ex", original_connect_ex)
            _socket.socket.connect_ex = guarded_connect_ex

        # 2) 拦截 DNS 解析：`socket.create_connection()` 走的是 getaddrinfo，
        #    不经过 socket.connect，只包 connect 会漏掉它（实测发现）。
        original_getaddrinfo = _socket.getaddrinfo

        def guarded_getaddrinfo(host, port, *args, **kwargs):
            if host and not host_allowed(str(host)):
                sandbox._violate("解析外部域名", str(host))
            return original_getaddrinfo(host, port, *args, **kwargs)

        self._save("socket.getaddrinfo", original_getaddrinfo)
        _socket.getaddrinfo = guarded_getaddrinfo


# ---------------------------------------------------------------------------
# 静态预检：加载前先看源码有没有明显越界写法（strict 模式直接拒绝加载）
# ---------------------------------------------------------------------------
def preflight_warnings(scan_result: Dict[str, Any]) -> list:
    """从 `plugin_safety` 的扫描结果里挑出"strict 模式下不该加载"的高风险项。"""
    if not isinstance(scan_result, dict):
        return []
    risky = {"process", "system", "exec", "eval", "ctypes", "thread", "subprocess"}
    warnings = []
    for item in scan_result.get("findings") or []:
        rule_id = str(item.get("id") or "").lower()
        if item.get("level") == "high" or any(tag in rule_id for tag in risky):
            warnings.append(str(item.get("label") or rule_id))
    return warnings


def sandbox_config(config) -> str:
    """从配置读 `plugins.sandbox`（默认 loose）。"""
    try:
        return normalize_mode(config.str_of("plugins", "sandbox", default="loose"))
    except Exception:
        return "loose"
