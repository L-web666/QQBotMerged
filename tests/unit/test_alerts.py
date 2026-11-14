# -*- coding: utf-8 -*-
"""单元测试：出错告警（openid 按机器人隔离这件事怎么处理）

**要解决的问题**：QQ 的 openid 是**按机器人隔离**的 —— 同一个 QQ 号在
机器人 A 下和在机器人 B 下拿到的 openid 完全不同。所以"发给谁"必须与
"谁来发"配对，不能只存一个 openid 就指望所有机器人都能发。

处理方式：
  · 全局选一个「发送告警的机器人」（`security.alert_bot_id`）；
  · 再填**这个机器人下**看到的 openid（`security.alert_owner_openid`）；
  · 两者缺一就不发，并说明原因；发送失败时把真实原因写进日志。

运行：`python tests/unit/test_alerts.py`
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

from core import alert_checks                       # noqa: E402
from core import alerts                             # noqa: E402
from core import config_schema as schema            # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


class FakeConfig:
    def __init__(self, data):
        self._d = data

    def str_of(self, a, b, default=""):
        value = (self._d.get(a) or {}).get(b)
        return default if value is None else str(value)

    def bool_of(self, a, b, default=False):
        value = (self._d.get(a) or {}).get(b)
        return default if value is None else bool(value)

    def int_of(self, a, b, default=0):
        value = (self._d.get(a) or {}).get(b)
        return int(value) if isinstance(value, (int, float)) else default


class FakeManager:
    def __init__(self, data):
        self.config = FakeConfig(data)


class FakeClient:
    def __init__(self, configured=True, boom=None):
        self.configured = configured
        self.boom = boom
        self.sent = []

    def send_text(self, target_type, openid, content, **kwargs):
        if self.boom:
            raise RuntimeError(self.boom)
        self.sent.append((target_type, openid, content))
        return {"ok": True}


class FakeBot:
    def __init__(self, bid, client, enabled=True, online=False, name=""):
        self.id = bid
        self.name = name or bid
        self.enabled = enabled
        self.client = client
        self._online = online

    def status(self):
        return {"id": self.id, "name": self.name, "online": self._online,
                "state": "已连接" if self._online else "未连接", "configured": True}


class SQLiteStore:
    """正常存储的替身。

    检查逻辑按**类名**认 SQLiteStore（与状态接口同一套判定），
    所以这里必须真的叫这个名字。
    """


SQLiteStoreStub = SQLiteStore


class FakeRuntime:
    def __init__(self, bots, store="default", plugins=None):
        self.bots = bots
        # 默认给一个"正常"的存储，避免每个用例都被存储降级告警干扰；
        # 要测降级时显式传 store=None。
        self.store = SQLiteStoreStub() if store == "default" else store
        self.plugin_manager = type("PM", (), {"list_plugins": lambda self_: plugins or []})()


def make_cfg(**security):
    data = schema.default_config()
    data["security"].update(security)
    return FakeManager(data)


def main():
    print("=" * 70)
    print("  出错告警（openid 按机器人隔离）")
    print("=" * 70)

    print("\n[A1] 配置项齐全")
    data = schema.default_config()
    sec = data.get("security") or {}
    check("有「用哪个机器人发告警」", "alert_bot_id" in sec, str(list(sec)))
    check("有「告警接收人 openid」", "alert_owner_openid" in sec, "")
    check("有冷却时间", "alert_cooldown_seconds" in sec, "")
    meta = schema.FIELDS.get("security.alert_bot_id") or {}
    check("是下拉选择（enum）", meta.get("type") == "enum", str(meta.get("type")))
    hint = str(meta.get("hint") or "")
    check("提示里说明了 openid 按机器人隔离", "openid" in hint and "机器人" in hint, hint[:60])
    check("openid 字段的提示写明不是 QQ 号",
          "不是 QQ 号" in str((schema.FIELDS.get("security.alert_owner_openid")
                              or {}).get("hint") or ""), "")

    print("\n[A2] 没配置时不会发送（也不报错）")
    alerter = alerts.Alerter(make_cfg(), runtime_getter=lambda: None)
    check("available() 为假", alerter.available() is False, "")
    check("notify 返回 False", alerter.notify("k", "测试") is False, "")

    print("\n[A3] 只选了机器人、没填 openid → 不发")
    alerter = alerts.Alerter(make_cfg(alert_bot_id="bot1"), runtime_getter=lambda: None)
    check("available() 仍为假", alerter.available() is False, "")

    print("\n[A4] 只填了 openid、没选机器人 → 不发")
    alerter = alerts.Alerter(make_cfg(alert_owner_openid="OPENID-X"), runtime_getter=lambda: None)
    check("available() 仍为假", alerter.available() is False, "")

    print("\n[A5] 关掉总开关 → 不发")
    alerter = alerts.Alerter(
        make_cfg(alert_enabled=False, alert_bot_id="bot1", alert_owner_openid="OPENID-X"),
        runtime_getter=lambda: None)
    check("available() 为假", alerter.available() is False, "")

    print("\n[A6] 机器人 + openid 都配对 → 用该机器人自己的凭据发")
    client = FakeClient()
    rt = FakeRuntime({"bot1": FakeBot("bot1", client)})
    alerter = alerts.Alerter(
        make_cfg(alert_bot_id="bot1", alert_owner_openid="OPENID-BOT1"),
        runtime_getter=lambda: rt)
    check("available() 为真", alerter.available() is True, "")
    sent = alerter.notify("test_key", "机器人掉线了", "详情若干")
    check("发送成功", sent is True, "")
    check("用的是单聊（c2c）", client.sent and client.sent[0][0] == "c2c",
          str(client.sent[:1]))
    check("发给了配置里的 openid", client.sent and client.sent[0][1] == "OPENID-BOT1", "")
    check("正文里带告警标记", client.sent and "告警" in client.sent[0][2], "")
    check("正文里带详情", client.sent and "详情若干" in client.sent[0][2], "")

    print("\n[A7] 冷却：同类问题在冷却期内只发一次")
    client.sent.clear()
    again = alerter.notify("test_key", "机器人掉线了")
    check("冷却期内不再发送", again is False and client.sent == [], str(client.sent))
    other = alerter.notify("another_key", "另一个问题")
    check("不同 key 不受影响", other is True and len(client.sent) == 1, str(client.sent))

    print("\n[A8] 发送失败时把真实原因写进日志（不静默）")
    logged = []

    class Log:
        def __getattr__(self, name):
            def _rec(msg, *a, **k):
                logged.append((name, msg % a if a else msg))
            return _rec

    client = FakeClient(boom="openid 不属于该机器人")
    rt = FakeRuntime({"bot1": FakeBot("bot1", client)})
    alerter = alerts.Alerter(
        make_cfg(alert_bot_id="bot1", alert_owner_openid="WRONG-OPENID"),
        runtime_getter=lambda: rt, logger_obj=Log())
    result = alerter.notify("fail_key", "测试失败")
    check("返回 False", result is False, "")
    text = " ".join(str(item[1]) for item in logged)
    check("日志里有真实原因", "openid 不属于该机器人" in text, text[:120])
    check("日志里提示了 openid 按机器人隔离", "按机器人隔离" in text, text[:120])

    print("\n[A9] 机器人没连上时明确说明，不假装发出")
    logged.clear()
    client = FakeClient(configured=False)
    rt = FakeRuntime({"bot1": FakeBot("bot1", client)})
    alerter = alerts.Alerter(
        make_cfg(alert_bot_id="bot1", alert_owner_openid="X"),
        runtime_getter=lambda: rt, logger_obj=Log())
    check("返回 False", alerter.notify("dead", "测试") is False, "")
    text = " ".join(str(item[1]) for item in logged)
    check("日志说明机器人不可用", "不可用" in text, text[:100])

    print("\n[A10] 选了一个不存在的机器人 → 不发、不崩")
    rt = FakeRuntime({"bot1": FakeClient() and FakeBot("bot1", FakeClient())})
    alerter = alerts.Alerter(
        make_cfg(alert_bot_id="no_such_bot", alert_owner_openid="X"),
        runtime_getter=lambda: rt, logger_obj=Log())
    check("返回 False 且不抛异常", alerter.notify("x", "测试") is False, "")

    # ================= B. 检查项 =================
    print("\n[B1] 只报告严重问题，不报正常状态")
    online = FakeRuntime({"bot1": FakeBot("bot1", FakeClient(), online=True)})
    check("全部在线 → 没有告警", alert_checks.collect_problems(online) == [], "")

    print("\n[B2] 机器人离线要等够时间才报（避免刚启动误报）")
    bot = FakeBot("bot1", FakeClient(), online=False)
    rt = FakeRuntime({"bot1": bot})
    first = alert_checks.collect_problems(rt)
    check("第一次检查只开始计时、不告警", first == [], str(first))
    bot._offline_since = time.time() - alert_checks.OFFLINE_ALERT_AFTER - 5
    problems = alert_checks.collect_problems(rt)
    check("超时后报出机器人掉线",
          any(item["key"].startswith("bot_offline") for item in problems), str(problems))

    print("\n[B3] 没配凭据的机器人不算故障")
    empty = FakeBot("bot2", FakeClient(configured=False), online=False)
    empty._offline_since = time.time() - 9999
    rt = FakeRuntime({"bot2": empty})
    check("没凭据不报", alert_checks.collect_problems(rt) == [], "")

    print("\n[B4] 停用的机器人不算故障")
    off = FakeBot("bot3", FakeClient(), enabled=False, online=False)
    off._offline_since = time.time() - 9999
    check("停用不报", alert_checks.collect_problems(FakeRuntime({"bot3": off})) == [], "")

    print("\n[B5] 插件加载失败 / 存储降级也要报")
    rt = FakeRuntime({}, store=None,
                     plugins=[{"name": "坏插件", "error": "语法错误"}])
    keys = [item["key"] for item in alert_checks.collect_problems(rt)]
    check("报出插件加载失败", "plugin_load_failed" in keys, str(keys))
    check("报出存储降级", "storage_degraded" in keys, str(keys))

    print("\n[B6] 存储正常时不报降级")
    rt = FakeRuntime({}, store=SQLiteStoreStub())
    keys = [item["key"] for item in alert_checks.collect_problems(rt)]
    check("SQLite 不报降级", "storage_degraded" not in keys, str(keys))

    print("\n[B7] 检查过程不会被单点异常打断")
    broken = FakeRuntime({"bot1": type("B", (), {"enabled": True})()})
    try:
        alert_checks.collect_problems(broken)
        ok = True
    except Exception:
        ok = False
    check("结构异常也不抛异常", ok, "")

    # ================= C. 接线 =================
    print("\n[C1] 运行时启动了告警检查")
    lifecycle = open(os.path.join(PROJECT, "core", "runtime_pkg", "lifecycle.py"),
                     encoding="utf-8").read()
    check("start() 里调用", "_start_alert_watcher()" in lifecycle, "")
    check("用了 alert_checks", "alert_checks.check_and_notify" in lifecycle, "")

    print("\n[C2] 设置页能动态拿到机器人列表")
    server_src = open(os.path.join(PROJECT, "web", "server.py"),
                      encoding="utf-8").read()
    check("填充 alert_bot_id 的选项", "security.alert_bot_id" in server_src, "")
    check("从 config.bots 取机器人", 'payload.get("config") or {}' in server_src, "")

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
