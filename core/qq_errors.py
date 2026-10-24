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

# 官方文档里"与 markdown 有关"的错误码（来源：.recon/md-send-api.md 第 4.2 节）：
# 22006/340069 消息类型与内容不匹配、40034124/40034011 markdown 参数错误、
# 40034008/40034009/40034010 markdown 参数有空值或换行、304036/40034127 无 markdown 权限、
# 40054007/40054018 消息长度超限、40054010 不允许发送 URL、
# 50037/50041/50055/50056/50057 公共错误码（markdown 组合/空值/content 不合法）。
MARKDOWN_ERROR_CODES = ("40034124", "40034011", "40034008", "40034009", "40034010",
                        "40054007", "40054018", "40054010", "50037", "50041", "50055",
                        "50056", "50057", "304036", "40034127", "22006", "340069")



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


def is_markdown_error(exc: Exception) -> bool:
    """该异常是否"很可能是 markdown 被平台拒绝"（用于自动降级为纯文本）。

    命中 `MARKDOWN_ERROR_CODES` 任一错误码即算；平台笼统的"请求数据异常"
    （`is_param_error`）也算，因为 markdown 参数不合法时常被归到这一类。
    """
    text = str(exc)
    if any(code in text for code in MARKDOWN_ERROR_CODES):
        return True
    return is_param_error(exc)
