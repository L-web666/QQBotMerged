# -*- coding: utf-8 -*-
"""单元测试：QQ 原生 markdown 发送 + 纯文本/markdown 切换（`core/qq_api.py` / `core/qq_errors.py`）

分层约定见 `PROGRESS.md`：`tests/unit/` 快、只依赖被测模块；**不联网、不碰真实 `data/`**。
做法：`QQApiClient` 的子类覆盖 `request()`，把每次请求的 payload 记下来，再照契约断言
（接口契约见 `.recon/md-plan.md` 第 1.2 / 1.6 节，官方字段见 `.recon/md-send-api.md` 第 1.2 节）。

运行：`python tests/unit/test_markdown_send.py`
"""

import copy
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core.qq_api import MARKDOWN_FALLBACK_NOTE, QQApiClient        # noqa: E402
from core.qq_errors import (APIError, MARKDOWN_ERROR_CODES, TransientError,   # noqa: E402
                            is_markdown_error, is_param_error)

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


class RecordingClient(QQApiClient):
    """只记录请求、不联网的客户端。

    `responses` 是"按顺序消费"的结果列表：字典 = 成功返回；异常实例 = 直接抛出。
    用完后一律返回 `{"id": "MSG_OK"}`。
    """

    def __init__(self, responses=None, seq=0):
        super().__init__("bot_md", "10000000", "fake-secret", sandbox=False, config=None)
        self.payloads = []
        self.urls = []
        self.responses = list(responses or [])
        self._seq = seq

    def request(self, method, url, json_data=None, params=None, retries=None):
        self.payloads.append(copy.deepcopy(json_data))
        self.urls.append(url)
        item = self.responses.pop(0) if self.responses else {"id": "MSG_OK",
                                                            "msg_idx": "IDX_OK"}
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def last(self):
        return self.payloads[-1] if self.payloads else {}


def main():
    print("=" * 70)
    print("  单元测试：markdown 发送与格式切换")
    print("=" * 70)

    content = "# 今日推荐\n\n**精选文章**\n> 知识就是力量"

    # ---------- 1. markdown 载荷结构 ----------
    print("\n[1] fmt=markdown → msg_type=2 + markdown.content（不带顶层 content）")
    client = RecordingClient()
    info = client.send_text("private", "USER_1", content, fmt="markdown")
    check("markdown 模式只发了一次请求", len(client.payloads) == 1, str(client.payloads))
    check("载荷是 {msg_type: 2, markdown: {content: 正文}}",
          client.last == {"msg_type": 2, "markdown": {"content": content}},
          str(client.last))
    check("带 markdown 时没有顶层 content（官方要求 content 为空）",
          "content" not in client.last, str(sorted(client.last.keys())))
    check("正文完整落在 markdown.content 里",
          client.last.get("markdown", {}).get("content") == content, str(client.last))
    check("请求打到单聊 messages 地址",
          client.urls and client.urls[0].endswith("/v2/users/USER_1/messages"),
          str(client.urls))
    check("返回值仍带消息 ID（可撤回）", info.get("id") == "MSG_OK", str(info))

    # 群聊同理
    group_client = RecordingClient()
    group_client.send_text("group", "GROUP_1", content, fmt="markdown")
    check("群聊 markdown 载荷同样是 msg_type=2 + markdown.content",
          group_client.last == {"msg_type": 2, "markdown": {"content": content}}
          and group_client.urls[0].endswith("/v2/groups/GROUP_1/messages"),
          str(group_client.last))

    # 大小写/空白不敏感
    case_client = RecordingClient()
    case_client.send_text("private", "USER_1", content, fmt="  MarkDown  ")
    check("fmt 大小写与首尾空白不敏感（仍走 markdown）",
          case_client.last.get("markdown", {}).get("content") == content
          and case_client.last.get("msg_type") == 2, str(case_client.last))

    # ---------- 2. text / 默认仍是老行为 ----------
    print("\n[2] fmt=text 与不传 fmt → 仍是 {content, msg_type: 0}")
    text_client = RecordingClient()
    text_client.send_text("private", "USER_2", "普通文本", fmt="text")
    check("fmt=text 载荷是 {content, msg_type: 0}",
          text_client.last == {"content": "普通文本", "msg_type": 0}, str(text_client.last))
    check("text 模式不带 markdown 字段", "markdown" not in text_client.last, "")

    default_client = RecordingClient()
    default_client.send_text("private", "USER_2", "普通文本")
    check("不传 fmt 时与原来完全一致（纯文本，msg_type=0）",
          default_client.last == {"content": "普通文本", "msg_type": 0},
          str(default_client.last))

    empty_fmt_client = RecordingClient()
    empty_fmt_client.send_text("private", "USER_2", "普通文本", fmt="")
    check("fmt=\"\" 时按 msg_type 老行为（纯文本）",
          empty_fmt_client.last == {"content": "普通文本", "msg_type": 0},
          str(empty_fmt_client.last))

    unknown_fmt_client = RecordingClient()
    unknown_fmt_client.send_text("private", "USER_2", "普通文本", fmt="xml")
    check("无法识别的 fmt 退回纯文本（不会误当 markdown）",
          unknown_fmt_client.last == {"content": "普通文本", "msg_type": 0},
          str(unknown_fmt_client.last))

    # ---------- 3. 被动回复字段在 markdown 模式下保留 ----------
    print("\n[3] 被动回复：markdown 模式下 msg_id / msg_seq 仍在")
    reply_client = RecordingClient(seq=41)
    info3 = reply_client.send_text("private", "USER_3", content, reply_msg_id="IN_MSG_9",
                                   fmt="markdown")
    payload = reply_client.last
    check("markdown 被动回复仍带 msg_id", payload.get("msg_id") == "IN_MSG_9", str(payload))
    check("markdown 被动回复仍带 msg_seq", payload.get("msg_seq") == 42, str(payload))
    check("markdown 被动回复正文仍在 markdown.content",
          payload.get("markdown", {}).get("content") == content, str(payload))
    check("markdown 被动回复没有顶层 content", "content" not in payload, str(payload))
    check("markdown + 引用（both）时 message_reference 也在",
          (payload.get("message_reference") or {}).get("message_id") == "IN_MSG_9",
          str(payload.get("message_reference")))
    check("被动回复成功时没有降级说明（_note 不该凭空出现）",
          "_note" not in info3, str(info3))

    # ---------- 4. 引用降级链没被改坏 ----------
    print("\n[4] 引用降级链：第一次「请求数据异常」→ 第二次保留引用、去掉被动回复字段")
    fallback_client = RecordingClient(responses=[
        APIError("API 错误 40011000: 请求数据异常"), {"id": "MSG_OK"}])
    info4 = fallback_client.send_text("private", "USER_4", content, reply_msg_id="IN_MSG_OLD",
                                      fmt="markdown")
    check("引用被拒时会重发一次（共 2 次请求）", len(fallback_client.payloads) == 2,
          str(fallback_client.payloads))
    second = fallback_client.payloads[1]
    check("第二次不再带 msg_id / msg_seq", "msg_id" not in second and "msg_seq" not in second,
          str(sorted(second.keys())))
    check("第二次仍保留 message_reference（引用没丢）",
          (second.get("message_reference") or {}).get("message_id") == "IN_MSG_OLD",
          str(second.get("message_reference")))
    check("降级重发时 markdown 正文没被改坏",
          second.get("markdown", {}).get("content") == content and "content" not in second,
          str(second))
    check("降级原因写进了返回值的 _note（说明改成了「引用」方式）",
          bool(info4.get("_note")) and "引用" in str(info4.get("_note")),
          str(info4.get("_note")))

    # 纯文本路径的降级链同样没坏（老行为）
    text_fallback = RecordingClient(responses=[
        TransientError("API 错误 40011000: 请求数据异常"), {"id": "MSG_OK"}])
    text_fallback.send_text("private", "USER_4", "普通文本", reply_msg_id="IN_MSG_OLD",
                            fmt="text")
    check("纯文本路径的引用降级链也没被改坏（第二次不带 msg_id、保留引用）",
          len(text_fallback.payloads) == 2
          and "msg_id" not in text_fallback.payloads[1]
          and (text_fallback.payloads[1].get("message_reference") or {}).get("message_id")
          == "IN_MSG_OLD",
          str(text_fallback.payloads))

    # ---------- 5. markdown 被拒 → 自动降级为纯文本 ----------
    print("\n[5] markdown 被平台拒绝（40034124）→ 自动用纯文本重发")
    degrade = RecordingClient(responses=[
        APIError("API 错误 40034124: markdown消息参数错误"), {"id": "MSG_TEXT_OK"}])
    info5 = degrade.send_text("private", "USER_5", content, fmt="markdown")
    check("第一次是 markdown，第二次是纯文本（共 2 次请求）",
          degrade.payloads[0].get("markdown", {}).get("content") == content
          and len(degrade.payloads) == 2, str(degrade.payloads))
    check("降级后的载荷是纯文本：{content, msg_type: 0}",
          degrade.payloads[1] == {"content": content, "msg_type": 0},
          str(degrade.payloads[1]))
    check("降级后的载荷里没有 markdown 字段",
          "markdown" not in degrade.payloads[1] and "content" in degrade.payloads[1],
          str(sorted(degrade.payloads[1].keys())))
    check("降级说明写在返回值 _note 里（非空）", bool(info5.get("_note")), str(info5))
    check("_note 里写明「已回退为纯文本」并带上原因",
          MARKDOWN_FALLBACK_NOTE in str(info5.get("_note"))
          and "40034124" in str(info5.get("_note")), str(info5.get("_note")))
    check("降级后仍返回消息 ID（这条消息还能撤回）", info5.get("id") == "MSG_TEXT_OK",
          str(info5))

    # 被动回复 + markdown 降级：第二次必须又是纯文本（不能退回 markdown）
    degrade_reply = RecordingClient(responses=[
        APIError("API 错误 50037: 带有 markdown 消息只支持 markdown 或者 keyboard 组合"),
        {"id": "MSG_OK"}])
    info5b = degrade_reply.send_text("private", "USER_5", content, reply_msg_id="IN_5",
                                     fmt="markdown")
    check("被动回复下 markdown 被拒也会降级成纯文本",
          degrade_reply.payloads[-1].get("markdown") is None
          and degrade_reply.payloads[-1].get("content") == content
          and degrade_reply.payloads[-1].get("msg_type") == 0,
          str(degrade_reply.payloads[-1]))
    check("被动回复降级后 msg_id 仍在（这条仍是被动回复）",
          degrade_reply.payloads[-1].get("msg_id") == "IN_5",
          str(degrade_reply.payloads[-1]))
    check("被动回复降级的 _note 也非空", bool(info5b.get("_note")), str(info5b))

    # 非 markdown 错误必须原样抛出（不能把网络/权限错误吞成"已降级"）
    hard_fail = RecordingClient(responses=[RuntimeError("HTTP 500: 服务器内部错误")])
    raised = None
    try:
        hard_fail.send_text("private", "USER_5", content, fmt="markdown")
    except Exception as exc:                       # noqa: BLE001
        raised = exc
    check("与 markdown 无关的失败照样抛出（不会静默降级）",
          isinstance(raised, RuntimeError) and len(hard_fail.payloads) == 1, repr(raised))

    # ---------- 6. is_markdown_error ----------
    print("\n[6] is_markdown_error 错误码判定")
    for code in ("40034124", "40054007", "50037", "304036"):
        check(f"{code} 判定为 markdown 错误",
              is_markdown_error(APIError(f"API 错误 {code}: 平台拒绝")) is True, code)
    codes_bad = [code for code in MARKDOWN_ERROR_CODES
                 if not is_markdown_error(APIError(f"API 错误 {code}: x"))]
    check("契约里的全部 markdown 错误码都能识别", not codes_bad, str(codes_bad))
    check("笼统的「请求数据异常」也算（is_param_error 一路）",
          is_markdown_error(APIError("API 错误 40011000: 请求数据异常")) is True
          and is_param_error(APIError("请求数据异常")) is True, "")
    check("普通网络异常不算 markdown 错误",
          is_markdown_error(ConnectionError("connection reset by peer")) is False
          and is_markdown_error(TimeoutError("请求超时")) is False
          and is_markdown_error(RuntimeError("HTTP 502 网关错误")) is False, "")
    check("消息里只是恰好出现数字也不算（比如错误里带 5003）",
          is_markdown_error(APIError("API 错误 5003: 无关错误")) is False, "")
    check("返回的是真正的 bool（而不是 None/真值对象）",
          is_markdown_error(APIError("50037")) is True
          and is_markdown_error(RuntimeError("x")) is False, "")

    # ---------- 7. normalize_format ----------
    print("\n[7] normalize_format 规范化")
    check('"Markdown" → "markdown"', QQApiClient.normalize_format("Markdown") == "markdown",
          QQApiClient.normalize_format("Markdown"))
    check('" text " → "text"', QQApiClient.normalize_format(" text ") == "text",
          QQApiClient.normalize_format(" text "))
    check('"" → ""', QQApiClient.normalize_format("") == "",
          repr(QQApiClient.normalize_format("")))
    check('"xml" → ""（不认识的格式不猜）', QQApiClient.normalize_format("xml") == "",
          repr(QQApiClient.normalize_format("xml")))
    check("None → \"\"（容错，不抛异常）", QQApiClient.normalize_format(None) == "",
          repr(QQApiClient.normalize_format(None)))
    check("MARKDOWN → markdown（全大写）",
          QQApiClient.normalize_format("MARKDOWN") == "markdown", "")

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
