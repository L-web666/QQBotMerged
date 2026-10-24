# -*- coding: utf-8 -*-
"""公共常量与 logger。

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



# 名字保持 "core.astrbot_compat"：拆分前日志里显示的就是它，改了会让日志对不上
logger = logging.getLogger("core.astrbot_compat")

# 我们模拟的 AstrBot 版本。插件用 metadata.yaml 里的 astrbot_version 做兼容判断时，
# 低于这个要求会给出明确警告（而不是静默跑挂）。
ASTRBOT_VERSION = "4.9.2"
SHIM_MARK = "_qqbot_astrbot_shim"
