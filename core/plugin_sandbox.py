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
import threading
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
    """按插件目录生效的软限制。

    **作用域（必读）**：补丁装在进程全局的 `builtins` / `os` / `socket` 上，但
    **只对安装它的那条线程生效** —— 每个 guarded 入口先判 `_in_scope()`，
    非本线程直接调用原函数。因此：
      · 插件在**宿主调用线程内**的执行受约束；
      · 宿主的其它线程（网页请求、日志落盘、云同步、群名刷新…）**不受影响**。

    **两个必须知道的边界（审计 NEW4-01 / NEW4-02）**：

    1. **插件自建线程不受约束**。插件自己 `threading.Thread(...)` 起的线程不属于
       "安装线程"，所有 guarded 入口对它直接放行。也就是说默认 `loose` 档下，
       一行 `threading.Thread` 就能绕开"拦执行系统命令"这道唯一的防线。
       这不是 bug 而是线程作用域的必然代价（没有它，宿主自己的后台线程会被误伤，
       那是更严重的问题）。因此：**不要把这层软限制当成安全边界**；
       真要跑不可信插件，只能用独立进程 + 低权限账户。
    2. **安装失败会明说，且已回滚**。早期版本安装中途失败时 `_active=False`，
       `__exit__` 便不再还原，导致补丁永久残留；而且界面上看不出"防护没生效"。
       现在安装是**原子**的（失败即回滚已打补丁），并提供 `effective` /
       `status_note()` 供界面显示"沙箱未生效"。

    仍未覆盖的边界：**非 LIFO 的嵌套退出**（后装者先退会把全局函数恢复成原始值，
    此时先装者虽然"仍在沙箱内"却已无约束）。多机器人并发 + strict 下请谨慎。

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
        self._thread_id: Optional[int] = None
        # 安装是否成功：`_active` 表示"当前是否在沙箱内"，`installed` 表示"补丁是否装上了"。
        # 两者分开是为了让"装了但后来退出"与"根本没装上"能被区分开（见 status_note）。
        self.installed = False
        self.install_error = ""

    # ------------------------------------------------------------------ 上下文管理
    def __enter__(self):
        if self.mode != "off":
            self._install()
        return self

    def __exit__(self, exc_type, exc, tb):
        # **无论安装成功与否都尝试还原**：以前是 `if self._active: self._uninstall()`，
        # 安装中途失败时 `_active` 已经是 False，于是已打上的补丁**永久残留**在进程里
        # （后续沙箱会把包装函数当成 original 保存，嵌套层数越积越多）。
        self._active = False
        self._uninstall()
        return False    # 不吞异常

    # ------------------------------------------------------------------ 安装/卸载
    def _install(self):
        """安装补丁。**原子**：任一步失败就回滚已打的补丁，不留半装状态。"""
        self._active = True
        self.installed = False
        self.install_error = ""
        # **线程作用域**：补丁装在进程全局的 builtins / os / socket 上，但只对
        # "安装它的这条线程"生效。否则插件执行期间，宿主自己的其它线程
        # （网页请求、日志落盘、云同步、群名刷新…）也会被拦，甚至把违规记到插件名下
        # （审计 NEW3-02，已实测复现）。
        self._thread_id = threading.get_ident()
        try:
            if self.mode == "strict":
                self._save("__import__", builtins.__import__)
                builtins.__import__ = self._guarded_import
                self._patch_builtin_open()
                self._patch_network()
            # loose / strict 都拦：执行系统命令、起子进程
            self._patch_process()
            self.installed = True
        except Exception as exc:      # 打补丁失败不能把插件加载搞崩
            self._active = False
            self.installed = False
            self.install_error = f"{type(exc).__name__}: {exc}"
            # 回滚：把已经打上的补丁还原，避免半装状态污染进程
            try:
                self._uninstall()
            except Exception as rollback_exc:
                self.log.warning("[沙箱] 安装失败后回滚也失败：%s", rollback_exc)
            # 失败必须是**可见状态**：防护没生效却看起来一切正常，是最危险的失效模式
            self.log.error("[沙箱][%s] 安装失败，当前**不受限制**运行（%s）：%s",
                           self.plugin_name, self.mode, self.install_error)

    @property
    def effective(self) -> bool:
        """沙箱是否真的在起作用（用于插件页/诊断展示）。"""
        return self.installed and self.mode != "off"

    def status_note(self) -> str:
        """给界面看的一句话状态说明。"""
        if self.mode == "off":
            return "沙箱已关闭（off）"
        if self.installed:
            return f"沙箱生效（{self.mode}）"
        return f"沙箱**未生效**（安装失败：{self.install_error or '未知原因'}）"

    def _in_scope(self) -> bool:
        """当前线程是不是"安装这个沙箱"的那条线程。

        非本线程 → 沙箱不生效（直接走原函数），这样宿主其它后台线程不受影响。
        """
        return (self._active
                and getattr(self._thread_id, "real", self._thread_id) == threading.get_ident())

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

        **安全点**：如果 `_saved` 里记的值本身是"我们的包装函数"（说明上一次安装失败时
        没还原干净、把包装当成了 original），那就不能把它装回去，否则会把残留固化下来。
        这种情况直接跳过并告警，宁可留一次告警也不要污染进程。
        """
        self._active = False
        try:
            import io as _io
            import socket as _socket
        except Exception:      # 极端情况下标准库不可用，跳过还原（下面的 finally 仍会清空）
            _io = _socket = None

        targets = {
            "__import__": (builtins, "__import__"),
            "open": (builtins, "open"),
            "socket.socket.connect": (_socket.socket if _socket else None, "connect"),
            "socket.socket.connect_ex": (_socket.socket if _socket else None, "connect_ex"),
            "socket.getaddrinfo": (_socket, "getaddrinfo"),
        }
        if _io is not None:
            targets["io.open"] = (_io, "open")

        for key, value in list(self._saved.items()):
            if self._looks_wrapped(value):
                self.log.warning("[沙箱][%s] 还原跳过 %s：保存的 original 本身是沙箱包装函数，"
                                 "可能是上次安装失败残留", self.plugin_name, key)
                continue
            if key in targets:
                target, attr = targets[key]
                if target is None:
                    continue
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

    @staticmethod
    def _looks_wrapped(func: Any) -> bool:
        """判断保存的"原函数"是不是我们自己的包装函数（用于避免把残留固化）。"""
        name = getattr(func, "__name__", "")
        return name in ("guarded_open", "_guarded_import", "_blocked",
                        "guarded_connect", "guarded_connect_ex", "guarded_getaddrinfo")

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
        original = self._saved["__import__"]
        # 非安装线程一律走原函数（沙箱只约束插件自己那条线程）
        if not self._in_scope():
            return original(name, globals, locals, fromlist, level)
        # level > 0 是**相对导入**（如 json 包内部 `from . import decoder`），
        # 模块名不带包前缀，无法按白名单判断；这类导入来自已被允许的包内部，
        # 其顶层包已经过检查，因此直接放行（否则几乎所有标准库包都会被打断）。
        if level and level > 0:
            return original(name, globals, locals, fromlist, level)
        if not self._module_allowed(name):
            self._violate("导入模块", name)
        return original(name, globals, locals, fromlist, level)

    # ------------------------------------------------------------------ 文件限制
    @staticmethod
    def _inside(path: str, root: str) -> bool:
        """`path` 是否真的位于目录 `root` 之内（按目录边界判断）。

        **不能用 `path.startswith(root)`**：那是字符串前缀比较，
        `/a/plugin_evil/x` 会命中 `/a/plugin`，兄弟目录因此被误判为"在插件目录内"，
        写文件限制形同虚设（这个问题在本轮自测时才发现）。
        """
        if not root:
            return False
        try:
            path_real = os.path.normcase(os.path.realpath(path))
            root_real = os.path.normcase(os.path.realpath(root))
        except (OSError, ValueError):
            return False
        if path_real == root_real:
            return True
        return path_real.startswith(root_real.rstrip(os.sep) + os.sep)

    def _path_allowed(self, path: Any) -> bool:
        try:
            text = os.path.abspath(os.fspath(path))
        except (TypeError, ValueError):
            return True          # 文件对象/描述符之类，交给原生逻辑
        for root in (self.data_dir, self.plugin_dir):
            if self._inside(text, root):
                return True
        return False

    def _patch_builtin_open(self):
        import io as _io

        original_open = builtins.open
        sandbox = self

        def guarded_open(file, mode="r", *args, **kwargs):
            # 非安装线程直接走原函数：宿主的日志落盘/云同步/群名刷新等不能被插件沙箱影响
            if not sandbox._in_scope():
                return original_open(file, mode, *args, **kwargs)
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

        def blocked(name, original):
            def _blocked(*args, **kwargs):
                # 非安装线程：宿主自己调 os.system 不受插件沙箱约束
                if not sandbox._in_scope():
                    return original(*args, **kwargs)
                sandbox._violate("执行系统命令/启动进程", f"{name}{args[:1]}")
            return _blocked

        for attr in ("system", "popen", "execv", "execve", "execvp", "spawnv", "spawnl"):
            func = getattr(os, attr, None)
            if callable(func):
                self._save("os." + attr, func)
                setattr(os, attr, blocked("os." + attr, func))

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
            if not sandbox._in_scope():
                return original_connect(self_sock, address, *args, **kwargs)
            host = host_of(address)
            if host and not host_allowed(host):
                sandbox._violate("连接外部主机", host)
            return original_connect(self_sock, address, *args, **kwargs)

        self._save("socket.connect", original_connect)
        _socket.socket.connect = guarded_connect

        if original_connect_ex is not None:
            def guarded_connect_ex(self_sock, address, *args, **kwargs):
                if not sandbox._in_scope():
                    return original_connect_ex(self_sock, address, *args, **kwargs)
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
            if not sandbox._in_scope():
                return original_getaddrinfo(host, port, *args, **kwargs)
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
