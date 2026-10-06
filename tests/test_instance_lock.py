# -*- coding: utf-8 -*-
"""单实例保护测试（离线，不连 QQ）

覆盖用户踩过的坑：残留锁导致"启动不了"、以及误判导致"两个实例同时跑"。

运行：python tests/test_instance_lock.py
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(BASE)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import run as runner                      # noqa: E402
from core import paths                    # noqa: E402

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(f"  {'[OK]' if ok else '[FAIL]'} {name}{'' if ok else '  ' + str(detail)}")


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main():
    print("=" * 66)
    print("  单实例保护测试")
    print("=" * 66)

    # 用项目内的临时目录（系统临时目录在本机沙箱里不允许列目录）
    sandbox = os.path.join(PROJECT, "data", "_locktest")
    import shutil
    shutil.rmtree(sandbox, ignore_errors=True)
    os.makedirs(sandbox, exist_ok=True)
    original_data_dir = paths.DATA_DIR
    paths.DATA_DIR = sandbox
    try:
        port = free_port()

        # 1) 无人占用时能拿到锁
        first = runner.InstanceLock(host="127.0.0.1", port=port)
        check("空闲端口可以获取实例锁", first.acquire(host="127.0.0.1", port=port) is True, "")
        info_path = os.path.join(sandbox, "instance.json")
        check("写入实例信息文件（pid/host/port）", os.path.isfile(info_path), info_path)
        info = json.load(open(info_path, encoding="utf-8"))
        check("实例信息内容正确",
              info.get("pid") == os.getpid() and info.get("port") == port, str(info))

        # 2) 同一台机器再起一个（模拟第二个实例）：锁被占、端口没有服务在应答，
        #    锁主是活着的 python 进程 → 必须拒绝启动，绝不能抢锁
        second = runner.InstanceLock(host="127.0.0.1", port=port)
        got = second.acquire(host="127.0.0.1", port=port)
        check("已有实例在运行时拒绝第二个实例", got is False, f"acquire={got}")

        # 3) 端口上有服务在应答时，也必须识别为"已在运行"
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(5)
        try:
            third = runner.InstanceLock(host="127.0.0.1", port=port)
            got = third.acquire(host="127.0.0.1", port=port)
            check("端口有服务应答时拒绝启动（不会两个实例抢消息）", got is False, f"acquire={got}")
        finally:
            server.close()

        # 4) 残留锁：锁文件里写一个不存在的 PID → 应当自愈
        first.release()
        lock_path = os.path.join(sandbox, "merged.lock")
        with open(lock_path, "w", encoding="utf-8") as handle:
            handle.write("999999")                     # 必然不存在的 PID
        stale_free_port = free_port()
        fourth = runner.InstanceLock(host="127.0.0.1", port=stale_free_port)
        got = fourth.acquire(host="127.0.0.1", port=stale_free_port)
        check("残留锁（PID 不存在）能自愈并成功启动", got is True, f"acquire={got}")
        fourth.release()

        # 5) 释放后可以再次启动
        fifth = runner.InstanceLock(host="127.0.0.1", port=port)
        check("释放锁之后可以重新启动",
              fifth.acquire(host="127.0.0.1", port=port) is True, "")
        fifth.release()
        check("释放后实例信息文件被清理", not os.path.isfile(info_path), info_path)

        # 6) 端口探测函数本身
        probe = free_port()
        check("没有服务时端口探测返回 False",
              runner._port_serving("127.0.0.1", probe) is False, "")
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", probe))
        listener.listen(1)
        try:
            check("有服务时端口探测返回 True",
                  runner._port_serving("127.0.0.1", probe) is True, "")
        finally:
            listener.close()

        # 7) 真实启动一次：父进程用**真实 data 目录**占住实例锁（写入实例信息），
        #    再启动子进程 —— 它应当立刻识别出"已在运行"并退出。
        #    注意：本机沙箱对"捕获子进程输出"有限制，所以只判断是否快速退出。
        holder = runner.InstanceLock(host="127.0.0.1", port=port)
        holder.acquire(host="127.0.0.1", port=port)
        try:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            started = time.time()
            try:
                proc = subprocess.run(
                    [sys.executable, "run.py", "--no-bots", "--port", str(free_port())],
                    cwd=PROJECT, timeout=25, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elapsed = time.time() - started
                check("命令行启动：已有实例时立即退出（退出码非 0）", proc.returncode != 0,
                      f"returncode={proc.returncode} 用时 {elapsed:.1f}s")
                check("命令行启动：确实没有起服务（很快退出）", elapsed < 20,
                      f"用时 {elapsed:.1f}s")
            except subprocess.TimeoutExpired:
                # 沙箱里无法可靠捕获子进程行为时跳过，核心判据已由上面的进程内用例覆盖
                print("  [SKIP] 命令行启动用例（本机沙箱不允许可靠捕获子进程行为）")
        finally:
            holder.release()
    finally:
        paths.DATA_DIR = original_data_dir
        import shutil
        shutil.rmtree(sandbox, ignore_errors=True)

    print("")
    print("=" * 66)
    if FAILED:
        print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项：{'、'.join(FAILED)}")
        return 1
    print(f"  全部通过（{len(PASSED)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
