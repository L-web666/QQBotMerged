#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 机器人合并版 · 启动入口

一个进程同时运行：
1. 多个 QQ 机器人的 WebSocket 网关（各自独立连接，互不影响）；
2. 统一的 Web 后台（左侧导航：聊天窗口 / 状态 / 统计 / 设置 / 插件 / 群管理 / 日志 / 上下文 / 指令面板 / 媒体留存）。

用法：
    python run.py                # 正常启动
    python run.py --check        # 只做自检（导入、配置、目录），不连接 QQ 也不开网页
    python run.py --port 9000     # 临时覆盖端口
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time


def _bootstrap_paths():
    """把程序根目录加入搜索路径，并切换工作目录（保证 config.json / data/ 定位正确）。

    打包成 exe 后：代码与内置资源在 PyInstaller 的解包目录（`sys._MEIPASS`），
    而 `config.json` / `data/` / `plugins/` 要写在 **exe 旁边**，所以工作目录切到 exe 所在目录。
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
        for item in (getattr(sys, "_MEIPASS", ""), base):
            if item and item not in sys.path:
                sys.path.insert(0, item)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
        if base not in sys.path:
            sys.path.insert(0, base)
    try:
        os.chdir(base)
    except OSError:
        pass
    return base


BASE_DIR = _bootstrap_paths()

from core import paths                      # noqa: E402
from core.config_manager import load_config  # noqa: E402
from core.logger import Logger              # noqa: E402
from core.runtime import Runtime            # noqa: E402


# ======================================================================================
# 单实例保护（同一份程序不要重复启动，否则会重复回复）
# ======================================================================================
def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _looks_like_our_process(pid: int) -> bool:
    """占用者是不是"我们这类程序"（python 进程）。

    用于区分两种"锁拿不到"的情况：
      · 旧实例还在跑（python 进程，可能配置了别的端口）→ 绝不能抢锁；
      · 被强杀留下的残留锁（PID 已经被系统回收给别的无关进程）→ 可以安全清理。
    """
    if not pid or not _pid_alive(pid):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return True                       # 查不到就保守认为是我们的人
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                name = os.path.basename(buffer.value).lower()
                return "python" in name or "qqai" in name
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True                           # 查询失败时保守处理
    return True


def _port_serving(host: str, port: int, timeout: float = 1.2) -> bool:
    """配置端口上是否已有服务在应答（用来区分"残留锁"与"真的在运行"）。"""
    if not port:
        return False
    import socket
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((probe_host, int(port))) == 0
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


class InstanceLock:
    """单实例保护（三重保险，防止"两个进程各自回一条"）

    1. 文件锁（fcntl / msvcrt）：常规环境最可靠；
    2. 目录锁（原子 mkdir + PID 存活校验）：文件系统不支持文件锁时降级；
    3. **独占端口探测**：即使锁被绕过，第二个进程也会在绑定端口时被拒绝，
       从而在"连上 QQ 之前"就退出——不会有第二个进程去收同一条消息。

    第 3 条是关键：Windows 上默认允许端口复用（SO_REUSEADDR），
    两个进程可以同时 bind 同一个端口，那样就会各收一条、各回一条。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.handle = None
        self.dir_path = None
        self.pid_path = os.path.join(paths.DATA_DIR, "merged.pid")
        self.lock_path = os.path.join(paths.DATA_DIR, "merged.lock")
        # 实例信息写在**独立**小文件里：锁文件被占用时读不到内容，这个文件随时可读
        self.info_path = os.path.join(paths.DATA_DIR, "instance.json")
        self.host = host
        self.port = int(port or 0)
        self.sock = None

    # ---------------------------------------------------------------- 实例信息
    def _write_info(self):
        try:
            os.makedirs(paths.DATA_DIR, exist_ok=True)
            payload = {
                "pid": os.getpid(),
                "host": self.host,
                "port": self.port,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "base_dir": paths.BASE_DIR,
            }
            tmp = self.info_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle2:
                json.dump(payload, handle2, ensure_ascii=False, indent=1)
            os.replace(tmp, self.info_path)
        except OSError:
            pass

    def _read_info(self) -> dict:
        """读取当前实例信息（pid / host / port）。读不到返回空 dict。"""
        try:
            with open(self.info_path, "r", encoding="utf-8") as handle2:
                data = json.load(handle2) or {}
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def describe_running(self) -> str:
        """描述"正在运行的那个实例"（用于提示语）。"""
        info = self._read_info()
        pid = info.get("pid") or self._read_pid()
        if not info:
            return f"PID={pid or '未知'}"
        where = f"{info.get('host')}:{info.get('port')}"
        return f"PID={pid}，监听 {where}（启动于 {info.get('started_at', '未知')}）"

    # ---------------------------------------------------------------- 端口
    def claim_port(self) -> bool:
        """以独占方式占住网页端口；失败说明已有实例在跑。"""
        import socket
        if not self.port:
            return True
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            return True
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):      # Windows：禁止他人复用
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(64)
            self.sock = sock
            return True
        except OSError as exc:
            try:
                sock.close()
            except OSError:
                pass
            print("=" * 62)
            print(f"⚠️  端口 {self.host}:{self.port} 已被占用（{exc}）")
            print("    说明已经有一个实例在运行。为了避免「同一条消息被回复两次」，")
            print("    本次启动已取消。请先关闭旧实例：")
            old_pid = self._read_pid()
            if old_pid:
                print(f"    Windows: taskkill /F /PID {old_pid}    （当前占用者 PID={old_pid}）")
                print(f"    Linux/macOS: kill {old_pid}")
            print("    如果确认没有实例在运行，可改端口启动：python run.py --port 8667")
            print("=" * 62)
            return False

    def release_port(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # ---------------------------------------------------------------- 锁
    def _read_pid(self) -> int:
        for path in (self.pid_path, os.path.join(self.dir_path or "", "pid")):
            try:
                if path and os.path.isfile(path):
                    with open(path, "r", encoding="utf-8") as handle:
                        return int((handle.read() or "0").strip() or 0)
            except (OSError, ValueError):
                continue
        return 0

    def acquire(self, host: str = "", port: int = 0) -> bool:
        """获取实例锁。返回 False 表示确实有另一个实例在跑。

        程序被强杀（taskkill /F）时，Windows 的文件字节锁有时会残留，
        导致"明明没在跑却启动不了"，而锁文件里的 PID 也可能已被清空。

        这里用两级判据，既不会误杀正在运行的实例，也能自愈残留锁：
          1. 锁文件里的 PID 还活着 → 确实在跑；
          2. PID 读不到 → 看**配置端口上有没有服务在应答**：
             有应答说明另一个实例正健康运行（跳过启动）；
             没应答说明是残留锁（清理后重试）。
        """
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        locked = self._acquire_file_lock()
        if locked is None:
            locked = self._acquire_dir_lock()
        if locked is False:
            # 判断"锁被占"是另一个实例在跑，还是残留锁。判据按可靠性排序：
            #   1. 独立实例信息文件里的 PID 还活着，或它记录的端口有服务应答 → 在跑；
            #   2. **配置端口本身有服务应答** → 在跑（哪怕锁主信息读不到）；
            #   3. 锁里的 PID 明显不是本程序（已被系统回收给别的进程）→ 残留锁；
            #   4. 其余（读不到任何信息）→ 认定残留锁，清理后重试。
            info = self._read_info()
            holder = int(info.get("pid") or 0) or self._lock_holder_pid()
            running = False
            if holder and _pid_alive(holder) and _looks_like_our_process(holder):
                running = True
            elif info and info.get("port") and _port_serving(
                    info.get("host") or "127.0.0.1", int(info["port"])):
                running = True
            elif _port_serving(host or self.host, port or self.port):
                # 配置端口上已经有人在服务 → 另一个实例正健康运行（最常见的情况）
                running = True
            stale = False
            if not running:
                if holder and not _pid_alive(holder):
                    print(f"⚠️  发现残留实例锁（记录里的 PID={holder} 已不存在），清理后重试…")
                    stale = True
                elif holder and not _looks_like_our_process(holder):
                    print(f"⚠️  发现残留实例锁（PID={holder} 已不是本程序的进程），清理后重试…")
                    stale = True
                elif not holder:
                    # 拿不到锁主信息、端口也没应答：可能是残留，也可能是"旧实例跑在别的端口上"。
                    # 这种情况不猜，明确告知用户怎么处理（绝不冒险并发运行）。
                    print("⚠️  实例锁被占用，但读不到锁主信息（可能是被强杀后留下的残留锁，"
                          "也可能是旧实例换了端口在运行）。")
                    print("    处理办法：先结束旧的 python 进程；"
                          "确认已退出后删除 data/merged.lock 与 data/merged.lockdir 再启动。")
                    print("    如果旧实例还在用别的端口服务，直接访问它即可，不必再启动新的。")
                    return False
            if stale:
                self._clear_stale()
                locked = self._acquire_file_lock()
                if locked is None:
                    locked = self._acquire_dir_lock()
            if locked is False:
                print("=" * 62)
                print(f"⚠️  检测到程序已在运行：{self.describe_running()}")
                print("    为避免重复回复与上下文冲突，同一份程序只应运行一个实例。")
                info = self._read_info()
                pid = info.get("pid")
                if pid:
                    print(f"    结束旧实例：Windows 用 taskkill /F /PID {pid}，"
                          f"Linux/macOS 用 kill {pid}")
                print("    也可以直接用别的端口启动：python run.py --port 8667")
                print("    若确认已退出，可删除残留锁：data/merged.lock 与 data/merged.lockdir")
                print("=" * 62)
                return False
        self._write_pid()
        self._write_info()
        return True

    def _lock_holder_pid(self) -> int:
        """读锁文件（或目录锁）里记录的持有者 PID。"""
        for path in (self.lock_path, os.path.join(self.dir_path or "", "pid"), self.pid_path):
            try:
                if path and os.path.isfile(path):
                    with open(path, "r", encoding="utf-8") as handle:
                        value = int((handle.read() or "0").strip() or 0)
                    if value:
                        return value
            except (OSError, ValueError):
                continue
        return 0

    def _clear_stale(self):
        """清理残留锁（仅当占用进程已不存在时调用）。"""
        if self.handle is not None:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None
        for path in (self.lock_path, self.pid_path):
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        if self.dir_path and os.path.isdir(self.dir_path):
            try:
                import shutil
                shutil.rmtree(self.dir_path, ignore_errors=True)
            except Exception:
                pass

    def _acquire_file_lock(self):
        """返回 True=拿到 / False=被占用 / None=文件系统不支持。

        拿锁成功后把 PID 写进锁文件，供下次启动判断"占用者是否还活着"。
        """
        try:
            handle = open(self.lock_path, "a+", encoding="utf-8")
        except OSError:
            return None
        try:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            self.handle = handle
            try:
                handle.seek(0)
                handle.truncate()
                handle.write(str(os.getpid()))
                handle.flush()
            except OSError:
                pass
            return True
        except OSError:
            handle.close()
            return False
        except Exception:
            handle.close()
            return None

    def _acquire_dir_lock(self):
        """目录锁（原子 mkdir）；已有残留时按 PID 存活情况决定是否清理。"""
        lock_dir = os.path.join(paths.DATA_DIR, "merged.lockdir")
        self.dir_path = lock_dir
        try:
            os.mkdir(lock_dir)
        except FileExistsError:
            pid = 0
            try:
                with open(os.path.join(lock_dir, "pid"), "r", encoding="utf-8") as handle:
                    pid = int((handle.read() or "0").strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if not pid or _pid_alive(pid):
                return False                      # 无法确认已死 → 视为被占用
            try:
                import shutil
                shutil.rmtree(lock_dir, ignore_errors=True)
                os.mkdir(lock_dir)
            except OSError:
                return False
        except OSError:
            return None                            # 文件系统不支持 → 交给端口兜底
        try:
            with open(os.path.join(lock_dir, "pid"), "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
        except OSError:
            pass
        return True

    def _write_pid(self):
        try:
            with open(self.pid_path, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
        except OSError:
            pass

    def release(self):
        if self.handle is not None:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None
        if self.dir_path:
            try:
                import shutil
                shutil.rmtree(self.dir_path, ignore_errors=True)
            except Exception:
                pass
            self.dir_path = None
        try:
            if os.path.isfile(self.pid_path):
                os.remove(self.pid_path)
        except OSError:
            pass
        # 只有"这份实例信息确实是我们写的"才删除，避免把别人的记录删掉
        try:
            info = self._read_info()
            if int(info.get("pid") or 0) == os.getpid() and os.path.isfile(self.info_path):
                os.remove(self.info_path)
        except (OSError, ValueError):
            pass
        self.release_port()



# ======================================================================================
# 启动
# ======================================================================================
def build_logger(config, config_manager=None) -> Logger:
    """构建日志器。

    `config_source` 用于让「HTTP 访问日志」开关实时生效：
    这里读取的是配置管理器里**当前**那份配置，热更新后也能跟着变。
    """
    source = (lambda: config_manager.config) if config_manager is not None else (lambda: config)
    return Logger(
        max_size_mb=config.float_of("logging", "max_size_mb", default=10),
        console_color=config.bool_of("logging", "console_color", default=True),
        level=config.str_of("logging", "level", default="INFO"),
        log_dir=paths.LOG_DIR,
        keep_days=config.int_of("logging", "keep_days", default=30),
        config_source=source,
    )


def self_check(config_path: str = "") -> int:
    """离线自检：配置读写、存储、媒体留存、文本处理、Web 应用能否构建。"""
    print("=" * 66)
    print("  QQBotMerged 自检（不会连接 QQ，也不会监听端口）")
    print("=" * 66)
    paths.ensure_dirs()
    print(f"程序目录：{paths.BASE_DIR}")
    print(f"数据目录：{paths.DATA_DIR}")

    config_manager = load_config(config_path or None)
    config = config_manager.config
    print(f"配置文件：{config_manager.config_path}")
    print(f"配置分组：{len(config_manager.payload()['sections'])} 个 · "
          f"可配置项 {len(config_manager.payload()['editable'])} 个")

    logger = build_logger(config, config_manager)
    logger.log("INFO", "自检开始")

    runtime = Runtime(config_manager, logger.get_logger())
    runtime.media  # noqa: B018  触发属性访问，确认依赖就绪
    print(f"存储后端：{runtime.store.__class__.__name__}")
    print(f"机器人：{[bot.id for bot in runtime.bots.values()]}")

    from web.server import create_app
    app = create_app(config_manager, logger.get_logger())
    app.attach_state(runtime, logger)
    routes = sorted({str(rule.rule) for rule in app.url_map.iter_rules()})
    print(f"Web 路由：{len(routes)} 个")
    for rule in routes:
        print("   ", rule)

    # 文本与媒体处理
    from core import message_text
    sample = '<faceType=4 faceId="0" ext="eyJ0ZXh0Ijoi5b6X5oSPIn0=">你好 <@!ABC123>'
    text, images = message_text.extract_face_info(sample)
    print(f"表情解析：{text!r} 图片 {len(images)} 张")
    print(f"无意义判定：'哈哈哈' -> {message_text.looks_meaningless('哈哈哈')}，"
          f"'你好' -> {message_text.looks_meaningless('你好')}")

    runtime.store.close()
    print("-" * 66)
    print("自检完成：所有模块加载正常。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ 机器人合并版")
    parser.add_argument("--check", action="store_true", help="只做自检，不启动服务")
    parser.add_argument("--port", type=int, default=0, help="覆盖网页后台端口")
    parser.add_argument("--host", default="", help="覆盖网页后台监听地址")
    parser.add_argument("--no-bots", action="store_true", help="只启动网页后台，不连接 QQ（调试用）")
    parser.add_argument("--config", default="", help="指定配置文件路径（默认 config.json）")
    args = parser.parse_args()

    if args.check:
        return self_check(args.config)

    paths.ensure_dirs()
    config_manager = load_config(args.config or None)
    config = config_manager.config
    logger = build_logger(config, config_manager)
    log = logger.get_logger()

    log.info("=" * 62)
    log.info("QQ 机器人合并版 启动")
    log.info("=" * 62)

    host = args.host or config.str_of("web", "host", default="127.0.0.1")
    port = args.port or config.int_of("web", "port", default=8666)
    token = config.str_of("web", "token", default="")

    # 单实例保护：必须在"连接 QQ 之前"完成，
    # 否则两个进程会各收一条消息、各回一条（用户看到的就是"回复了两条"）
    lock = InstanceLock(host=host, port=port)
    if not lock.acquire(host=host, port=port):
        return 1
    if not lock.claim_port():
        lock.release()
        return 1

    runtime = None
    try:
        runtime = Runtime(config_manager, log)
        # 网页「关闭程序」按钮：先停机器人/同步，再结束进程
        def _on_shutdown(reason: str = ""):
            def worker():
                time.sleep(0.8)          # 留一点时间让 HTTP 响应先回到浏览器
                try:
                    log.warning("正在关闭程序（%s）…", reason or "网页按钮")
                    runtime.stop()
                except Exception as exc:
                    log.error("关闭时出错：%s", exc)
                finally:
                    lock.release()
                    os._exit(0)          # 立即退出（Flask 开发服务器没有优雅关停手段）
            threading.Thread(target=worker, name="shutdown", daemon=True).start()
        runtime.set_shutdown_callback(_on_shutdown)

        # 记录实际监听地址，供后台提示"配置改了端口但没重启"这类问题
        runtime.set_bind(host, port)
        if not args.no_bots:
            runtime.start()
        else:
            # 调试模式：标记为"不连 QQ"，这样后面保存设置也不会偷偷把机器人连上
            runtime.set_bots_disabled(True)
            runtime.processor.start()
            log.warning("已按 --no-bots 启动：不会连接 QQ，仅网页后台可用")

        from web.server import create_app
        app = create_app(config_manager, logger.get_logger())
        app.attach_state(runtime, logger)

        if host not in ("127.0.0.1", "localhost", "::1") and not token:
            log.warning("⚠️  当前监听 %s 且未设置访问令牌，任何能访问该端口的人都能操作后台！"
                        "请在「设置 → 网页后台」里设置 token。", host)
        if host in ("0.0.0.0", "::"):
            shown_host = "127.0.0.1"
        else:
            shown_host = host

        log.info("网页后台已就绪：http://%s:%s/%s", shown_host, port,
                 f"?token={token}" if token else "")
        if not token and host in ("127.0.0.1", "localhost", "::1"):
            log.info("（仅本机可访问，未设置令牌）")

        def shutdown(*_args):
            log.info("收到退出信号，正在停止…")
            try:
                if runtime is not None:
                    runtime.stop()
            finally:
                lock.release()
            os._exit(0)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, shutdown)
            except (ValueError, OSError):
                pass

        # 探测用的 socket 用完就放掉，让 Flask 自己绑定端口
        # （文件锁已经保证只有一个实例；万一锁被绕过，端口占用会让后启动的实例启动失败）
        lock.release_port()

        try:
            app.run(host=host, port=port, debug=False, threaded=True,
                    use_reloader=False, load_dotenv=False)
        except OSError as exc:
            log.error("网页服务启动失败：%s", exc)
            log.error("常见原因：端口 %s 已被其他程序占用，"
                      "可用 python run.py --port 8667 换个端口。", port)
            return 1
        return 0
    except KeyboardInterrupt:
        log.info("已中断")
        return 0
    except Exception as exc:
        log.error("启动失败：%s", exc, exc_info=True)
        return 1
    finally:
        try:
            if runtime is not None:
                runtime.stop()
        except Exception:
            pass
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
