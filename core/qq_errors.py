# -*- coding: utf-8 -*-
"""QQ 接口的错误类型与错误分类（`APIError` / `TransientError` / 各类错误提示）。

从 `core/qq_api.py` 拆出：上传逻辑（`core/qq_upload.py`）与主客户端都要用，
单独放一个模块才不会互相循环导入；`core/qq_api.py` 继续再导出这些名字。
"""

import base64
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs
import requests



TRANSIENT_HTTP_STATUS = {429, 500, 502, 503, 504}
TOKEN_INVALID_CODES = {40001, 40014, 40002}
URL_UPLOAD_ERROR_HINTS = ("上传URL错误", "40093010")
PARAM_ERROR_HINTS = ("请求数据异常", "40011000")



class TransientError(Exception):
    """临时错误（网络抖动 / 5xx / 429 / token 失效），可重试。"""


class APIError(Exception):
    """永久错误（参数错误、业务错误码），重试无意义。"""


# ======================================================================================
# 判定工具
# ======================================================================================
def is_param_error(exc: Exception) -> bool:
    text = str(exc)
    return any(hint in text for hint in PARAM_ERROR_HINTS)


def is_url_upload_error(exc: Exception) -> bool:
    text = str(exc)
    return any(hint in text for hint in URL_UPLOAD_ERROR_HINTS)
