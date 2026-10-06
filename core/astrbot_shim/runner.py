# -*- coding: utf-8 -*-
"""常驻事件循环（AsyncRunner / RUNNER）。

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




# ======================================================================================
# 异步执行器：AstrBot 插件基本都是 async，我们统一在自己的一条事件循环线程里跑
# ======================================================================================
class AsyncRunner:
    """常驻事件循环，把协程提交进去并等待结果（避免每次 asyncio.run 重建事件循环）。"""

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()

            def run():
                asyncio.set_event_loop(loop)
                loop.run_forever()

            thread = threading.Thread(target=run, name="astrbot-loop", daemon=True)
            thread.start()
            self._loop = loop
            self._thread = thread
            return loop

    def run(self, coro, timeout: float = 60.0):
        """同步等待一个协程的结果（超时会抛 TimeoutError）。"""
        loop = self._ensure()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    def collect(self, agen, timeout: float = 60.0) -> List[Any]:
        """把异步生成器的结果全部收集起来。"""
        results: List[Any] = []

        async def drain():
            async for item in agen:
                results.append(item)

        self.run(drain(), timeout=timeout)
        return results

    def stop(self):
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=2.0)


RUNNER = AsyncRunner()
