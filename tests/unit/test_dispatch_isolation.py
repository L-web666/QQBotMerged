# -*- coding: utf-8 -*-
"""单元测试：插件分发状态的线程隔离（审计 NEW3-01）与按目标回查（NEW3-03）

背景：每个机器人一条工作线程（`message_processor._worker_loop`），插件 handler 又是异步的。
`dispatch()` 里的分发状态（`_emit_sink` / `_emit_used` / `stop`）原先是**实例属性**，
两个机器人并发分发时会互相覆盖 —— A 里的 `await event.send()` 可能按 B 的会话与账号发出。
`current_bot_id` 早就改成 `threading.local()` 了，但这几个兄弟状态漏了。

本文件的断言目标：
  · 分发状态（sink / emit_used / stop）**只存在于线程局部**，实例上不留同名可写字段；
  · 两条线程并发 dispatch 时，各自看到自己的 sink，互不覆盖；
  · 延迟发送时按 `(会话类型, openid)` 回查机器人（而不是"最近一次分发"）。

分层约定见 `PROGRESS.md`：不连网、不碰真实 `data/`。

运行：`python tests/unit/test_dispatch_isolation.py`
"""

import logging
import os
import sys
import threading
import types

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core.astrbot_shim.host import AstrBotHost        # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def make_host():
    """带一个"空插件"的 host：dispatch 不会提前返回，也没有 handler 会执行。"""
    host = AstrBotHost(logger_obj=logging.getLogger("dispatch-iso"))
    host.plugins = [types.SimpleNamespace(
        name="probe_plugin", path="plugins/probe", meta={}, handlers=[], instance=None)]
    return host


def main():
    print("=" * 70)
    print("  插件分发状态的线程隔离（NEW3-01 / NEW3-03）")
    print("=" * 70)

    host = make_host()

    # ---------- 1. 状态确实放进了线程局部 ----------
    print("\n[1] 分发状态在线程局部，而不是实例属性")
    check("_current 是 threading.local", isinstance(host._current, threading.local),
          type(host._current).__name__)
    for name in ("_emit_sink", "_emit_used", "stop"):
        check(f"存在 {name} 的线程内读写入口", hasattr(AstrBotHost, name), "")
    # 实例字典里不应再有这三个名字的"真值"
    check("实例 __dict__ 里没有 _emit_sink 副本", "_emit_sink" not in host.__dict__,
          str([k for k in host.__dict__ if "emit" in k]))
    check("实例 __dict__ 里没有 stop 副本", "stop" not in host.__dict__, "")

    # ---------- 2. 两条线程各自持有自己的 sink ----------
    print("\n[2] 两条线程并发 dispatch，各自看到自己的 sink")
    barrier = threading.Barrier(2)
    results = {}

    def worker(tag, bot_id, openid):
        def sink(components):
            results.setdefault("sink_calls", []).append(tag)
        barrier.wait()
        host.dispatch({"type": "private", "content": tag, "user_openid": openid,
                       "bot_id": bot_id, "msg_id": "M_" + tag}, sink=sink)
        # dispatch 结束后读本线程这份状态（各线程互不影响）
        results[tag] = {
            "bot_id": host.current_bot_id,
            "sink": getattr(host._current, "emit_sink", None),
        }

    t1 = threading.Thread(target=worker, args=("a", "botA", "U1"))
    t2 = threading.Thread(target=worker, args=("b", "botB", "U2"))
    t1.start(); t2.start(); t1.join(timeout=10); t2.join(timeout=10)

    check("线程 A 记录的机器人是 botA", results.get("a", {}).get("bot_id") == "botA",
          str(results.get("a")))
    check("线程 B 记录的机器人是 botB", results.get("b", {}).get("bot_id") == "botB",
          str(results.get("b")))
    sink_a = results.get("a", {}).get("sink")
    sink_b = results.get("b", {}).get("sink")
    check("两条线程的 sink 不是同一个对象", sink_a is not sink_b,
          f"{sink_a} vs {sink_b}")

    # ---------- 3. 按目标回查（NEW3-03） ----------
    print("\n[3] 延迟发送按目标回查机器人（不用「最近一次」）")
    host2 = make_host()
    host2.dispatch({"type": "private", "content": "x", "user_openid": "U1",
                    "bot_id": "botA", "msg_id": "M1"}, sink=lambda c: None)
    # 第二条消息换另一个机器人：此时"最近一次分发"已经是 botB
    host2.dispatch({"type": "private", "content": "x", "user_openid": "U2",
                    "bot_id": "botB", "msg_id": "M2"}, sink=lambda c: None)
    check("目标 U1 仍回查到 botA", host2.target_bot_id("private", "U1") == "botA",
          host2.target_bot_id("private", "U1"))
    check("目标 U2 回查到 botB", host2.target_bot_id("private", "U2") == "botB",
          host2.target_bot_id("private", "U2"))
    check("未知目标返回空（交给调用方决定是否拒绝）",
          host2.target_bot_id("private", "U_UNKNOWN") == "", "")

    # 群聊目标也要记账
    host2.dispatch({"type": "group", "content": "x", "group_openid": "G1",
                    "bot_id": "botA", "msg_id": "M3"}, sink=lambda c: None)
    check("群聊目标能回查到机器人",
          host2.target_bot_id("group", "G1") == "botA",
          host2.target_bot_id("group", "G1"))

    # ---------- 4. 目标表不会无限增长 ----------
    print("\n[4] 目标表有上限，不会随运行时间无限增长")
    host3 = make_host()
    for index in range(2600):
        host3.remember_target_bot("private", "U%d" % index, "botA")
    with host3._target_lock:
        size = len(host3._target_bots)
    check("目标表被裁剪到上限以内", size <= 2000, f"size={size}")

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
