# -*- coding: utf-8 -*-
"""单元测试：群成员身份（`author.member_role`）从群消息里取得

来源：用户实测的官方报文 —— 群里每条消息的 `author` 都带 `member_role`：
    普通成员 `member` / 管理员 `admin` / 群主 `owner`
而且**不需要机器人是管理员**，比群成员接口（要白名单）可靠得多。

背景（之前的问题）：群管理页「身份」列**每个人都显示「成员」**。根因是 `role` 到处
兜底成 `"member"`，而大部分成员是从历史消息认出来的、平台没给身份。
现在：能从消息里拿到真实身份就存下来；真拿不到才显示「未知」。

链路：gateway 解析 → runtime 落库（group_members）→ 群管理页读出来显示。
分层约定见 `PROGRESS.md`：`tests/unit/` 快、只依赖被测模块；不连网、不碰真实 `data/`。

运行：`python tests/unit/test_member_role.py`
"""

import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core.gateway import QQGateway                              # noqa: E402
from core.group_manager import (ROLE_UNKNOWN, _normalize_role,    # noqa: E402
                                member_role)
from core.runtime_pkg.messaging import RuntimeMessagingMixin     # noqa: E402
from core.storage import SQLiteStore                             # noqa: E402

PASSED = []
FAILED = []

GROUP = "099E843F61EFAD94175DA39544936472"
OWNER_ID = "7B7D8D4A38188B367D42FFAC71A7C99D"
BOT_ID = "7C8CC5AB126D58717A7C8D145AD167CB"

# 用户提供的真实群消息报文（群主发的 /签到）
REAL_PAYLOAD = {
    "author": {
        "bot": False,
        "id": OWNER_ID,
        "member_openid": OWNER_ID,
        "member_role": "owner",
        "union_openid": "",
        "username": "锋跃",
    },
    "content": "<@%s> /签到 " % BOT_ID,
    "group_id": GROUP,
    "group_openid": GROUP,
    "id": "ROBOT1.0_s8V722VSTD",
    "mentions": [{
        "bot": True, "id": BOT_ID, "is_you": True,
        "member_openid": BOT_ID, "member_role": "admin",
        "scope": "single", "username": "锋跃_AI",
    }],
    "message_type": 0,
    "timestamp": "2026-10-25T00:02:03+08:00",
}


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


class FakeRuntime(RuntimeMessagingMixin):
    """只提供 _remember_member_role 需要的 store / log（复用真实实现，不自造一份）。"""

    def __init__(self, store):
        import logging
        self.store = store
        self.log = logging.getLogger("test-member-role")


def parse_message(payload, event="GROUP_AT_MESSAGE_CREATE"):
    """走真实的 gateway 解析（不连网：只喂一条 dispatch 报文给 _handle_dispatch）。"""
    gateway = QQGateway("bot1", api_client=None, on_message=None)
    captured = {}
    gateway.on_message = lambda msg: captured.update(msg)
    gateway._handle_dispatch({"t": event, "d": payload, "id": "EVT_1", "s": 1})
    return captured


def roles_of(store):
    return {row["member_openid"]: row for row in store.list_members(GROUP, "bot1")}


def main():
    print("=" * 70)
    print("  单元测试：群成员身份（author.member_role）")
    print("=" * 70)

    # ---------- 1. 身份值解析 ----------
    print("\n[1] 身份值解析（实测取值 owner / admin / member）")
    check("owner → owner", member_role("owner") == "owner", member_role("owner"))
    check("admin → admin", member_role("admin") == "admin", member_role("admin"))
    check("member → member", member_role("member") == "member", member_role("member"))
    check("大小写容错", member_role("Owner") == "owner", member_role("Owner"))
    check("空串 → 空（代表「没带身份」，不是「普通成员」）",
          member_role("") == "", repr(member_role("")))
    check("None → 空", member_role(None) == "", repr(member_role(None)))
    check("没见过的值 → 空（不瞎猜成 member）", member_role("vip") == "", repr(member_role("vip")))
    check("中文写法仍兼容（群主）", member_role("群主") == "owner", member_role("群主"))
    check("_normalize_role 对成员 dict 仍然可用",
          _normalize_role({"member_role": "admin"}) == "admin", "")
    check("认不出来的 dict → unknown", _normalize_role({}) == ROLE_UNKNOWN, "")
    check("非 dict 不抛异常", _normalize_role(None) == ROLE_UNKNOWN, "")

    # ---------- 2. gateway 要把两个身份都解析出来 ----------
    print("\n[2] gateway 解析真实报文：author / mentions 的 member_role 都要带出来")
    message = parse_message(REAL_PAYLOAD)
    check("消息被解析出来了（回调收到）", bool(message), str(message)[:120])
    check("发消息的人身份 = owner", message.get("member_role") == "owner",
          repr(message.get("member_role")))
    mentions = message.get("mentions") or []
    check("被 @ 的机器人身份 = admin",
          bool(mentions) and mentions[0].get("member_role") == "admin", str(mentions[:1]))
    check("被 @ 的人仍是 is_you（原有行为不变）",
          bool(mentions) and mentions[0].get("is_you") is True, "")
    check("正文与昵称照旧", message.get("username") == "锋跃"
          and "/签到" in (message.get("content") or ""), str(message.get("username")))

    # ---------- 3. 私聊没有这个字段 ----------
    print("\n[3] 私聊报文（author 里没有 member_role）→ 解析成空串，不凭空造身份")
    private_msg = parse_message({
        "author": {"user_openid": "USER_X", "username": "小明"},
        "content": "你好", "id": "MSG_P1", "timestamp": "2026-10-25T00:00:00+08:00",
    }, event="C2C_MESSAGE_CREATE")
    check("私聊消息解析成功", bool(private_msg), "")
    check("私聊身份是空串（不是 member）", private_msg.get("member_role") == "",
          repr(private_msg.get("member_role")))

    # ---------- 4~7. 落库 ----------
    sandbox = os.path.join(PROJECT, "data", "_selftest", "unit_role_%d" % int(time.time() * 1000))
    shutil.rmtree(sandbox, ignore_errors=True)
    os.makedirs(sandbox, exist_ok=True)
    store = None
    try:
        store = SQLiteStore(os.path.join(sandbox, "role.db"))
        runtime = FakeRuntime(store)

        print("\n[4] 落库：群消息里的身份进 group_members 表")
        runtime._remember_member_role(message, "bot1", "group", GROUP, OWNER_ID)
        rows = roles_of(store)
        check("发消息的群主入库且身份 = owner",
              rows.get(OWNER_ID, {}).get("role") == "owner", str(rows.get(OWNER_ID)))
        check("昵称一起存下来了", rows.get(OWNER_ID, {}).get("username") == "锋跃", "")
        check("被 @ 的机器人也入库且身份 = admin",
              rows.get(BOT_ID, {}).get("role") == "admin", str(rows.get(BOT_ID)))

        print("\n[5] 没带身份的更新不能把已知身份降级")
        runtime._remember_member_role(
            {"member_role": "", "mentions": [], "username": "锋跃"},
            "bot1", "group", GROUP, OWNER_ID)
        check("空身份不会覆盖已知的 owner",
              roles_of(store).get(OWNER_ID, {}).get("role") == "owner",
              str(roles_of(store).get(OWNER_ID)))

        print("\n[6] 身份变了要跟着更新")
        runtime._remember_member_role(
            {"member_role": "admin", "mentions": [], "username": "锋跃"},
            "bot1", "group", GROUP, OWNER_ID)
        check("重新发消息后身份更新为 admin",
              roles_of(store).get(OWNER_ID, {}).get("role") == "admin",
              str(roles_of(store).get(OWNER_ID)))

        print("\n[7] 私聊消息不往群成员表里写东西")
        before = len(store.list_members(GROUP, "bot1"))
        runtime._remember_member_role(
            {"member_role": "owner", "mentions": []}, "bot1", "private", "", "USER_X")
        check("私聊没有改写群成员表", before == len(store.list_members(GROUP, "bot1")),
              f"{before} -> {len(store.list_members(GROUP, 'bot1'))}")
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        shutil.rmtree(sandbox, ignore_errors=True)

    # ---------- 8. 从历史报文补齐（老版本丢掉的身份能补回来） ----------
    print("\n[8] 从历史消息的 raw_event 补齐身份（不联网）")
    from core.member_roles import backfill_from_history           # noqa: E402
    sandbox2 = os.path.join(PROJECT, "data", "_selftest",
                            "unit_backfill_%d" % int(time.time() * 1000))
    shutil.rmtree(sandbox2, ignore_errors=True)
    os.makedirs(sandbox2, exist_ok=True)
    store2 = None
    try:
        store2 = SQLiteStore(os.path.join(sandbox2, "bf.db"))
        import json                                                 # noqa: E402
        # 两条历史消息：群主 + 一个普通成员（都是老版本丢掉身份的那种）
        for idx, (user, role, at) in enumerate([
                (OWNER_ID, "owner", "锋跃"),
                ("MEMBER_1", "member", "小明")]):
            store2.add({
                "bot_id": "bot1", "direction": "in", "type": "group",
                "group_openid": GROUP, "openid": user, "username": at,
                "content": "hi", "msg_id": "H_%d" % idx, "attachments": [],
                "raw_event": json.dumps({
                    "author": {"member_openid": user, "member_role": role, "username": at},
                    "content": "hi", "group_openid": GROUP,
                }, ensure_ascii=False),
            })
        stats = backfill_from_history(store2)
        rows = {row["member_openid"]: row for row in store2.list_members(GROUP, "bot1")}
        check("补齐函数报告写入了成员", stats.get("members") == 2, str(stats))
        check("群主身份补回来了", rows.get(OWNER_ID, {}).get("role") == "owner",
              str(rows.get(OWNER_ID)))
        check("普通成员身份补回来了", rows.get("MEMBER_1", {}).get("role") == "member",
              str(rows.get("MEMBER_1")))
        check("昵称也一起补上了", rows.get(OWNER_ID, {}).get("username") == "锋跃",
              str(rows.get(OWNER_ID)))
        # 再跑一次不应该有新增（幂等）
        again = backfill_from_history(store2)
        check("重复补齐是幂等的", again.get("members") == 0, str(again))
    finally:
        if store2 is not None:
            try:
                store2.close()
            except Exception:
                pass
        shutil.rmtree(sandbox2, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
