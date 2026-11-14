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
        self.config_manager = type("CM", (), {"active_bot_id": lambda self_: ""})()


class PerBotManager:
    """按机器人隔离的配置替身：`resolve_for_bot()` 返回该机器人自己的 security。"""

    def __init__(self, per_bot=None, default=None):
        self._per_bot = per_bot or {}
        self.config = FakeConfig({"security": default or {}})

    def resolve_for_bot(self, bot_id):
        sec = self._per_bot.get(bot_id)
        if sec is None:
            return {"security": dict(schema.default_config().get("security") or {})}
        base = dict(schema.default_config().get("security") or {})
        base.update(sec)
        return {"security": base}


def make_per_bot(bot_id, **security):
    return PerBotManager({bot_id: security})


def main():
    print("=" * 70)
    print("  出错告警（按机器人隔离）")
    print("=" * 70)

    print("\n[A1] 配置项齐全，且整组按机器人隔离")
    data = schema.default_config()
    sec = data.get("security") or {}
    check("有管理员 openid", "admin_openids" in sec, str(list(sec)))
    check("有告警开关", "alert_enabled" in sec, "")
    check("有告警接收人 openid", "alert_owner_openid" in sec, "")
    check("有冷却时间", "alert_cooldown_seconds" in sec, "")
    check("不再有「用哪个机器人发告警」（改为按机器人）",
          "alert_bot_id" not in sec, str(list(sec)))
    check("security 是 per_bot 分组",
          "security" in schema.PER_BOT_SECTIONS, "")
    for path in ("security.admin_openids", "security.alert_enabled",
                 "security.alert_owner_openid", "security.alert_cooldown_seconds"):
        check("%s 按机器人隔离" % path, schema.is_per_bot(path), "")
    hint = str((schema.FIELDS.get("security.alert_owner_openid") or {}).get("hint") or "")
    check("openid 提示写明不是 QQ 号", "不是 QQ 号" in hint, hint[:60])
    check("openid 提示写明各机器人不能混用", "混用" in hint, hint[:80])

    print("\n[A2] 没配置时不会发送（也不报错）")
    alerter = alerts.Alerter(make_per_bot("bot1"), runtime_getter=lambda: None)
    check("available() 为假", alerter.available("bot1") is False, "")
    check("notify 返回 False", alerter.notify("bot1", "k", "测试") is False, "")

    print("\n[A3] 只有 openid、但告警关掉了 → 不发")
    alerter = alerts.Alerter(
        make_per_bot("bot1", alert_enabled=False, alert_owner_openid="X"),
        runtime_getter=lambda: None)
    check("available() 为假", alerter.available("bot1") is False, "")

    print("\n[A4] 没填 openid → 不发")
    alerter = alerts.Alerter(
        make_per_bot("bot1", alert_enabled=True, alert_owner_openid=""),
        runtime_getter=lambda: None)
    check("available() 为假", alerter.available("bot1") is False, "")

    print("\n[A5] 两个机器人各配各的，互不串")
    two = PerBotManager({
        "bot1": {"alert_enabled": True, "alert_owner_openid": "OPENID-BOT1"},
        "bot2": {"alert_enabled": False, "alert_owner_openid": "OPENID-BOT2"},
    })
    alerter = alerts.Alerter(two, runtime_getter=lambda: None)
    check("bot1 可用", alerter.available("bot1") is True, "")
    check("bot2 关掉了 → 不可用", alerter.available("bot2") is False, "")
    check("bot1 的 openid 是自己的",
          alerter.target("bot1")["openid"] == "OPENID-BOT1", "")
    check("bot2 的 openid 是自己的",
          alerter.target("bot2")["openid"] == "OPENID-BOT2", "")
    check("bot2 关着告警时 notify 返回 False",
          alerter.notify("bot2", "k", "测试") is False, "")

    print("\n[A6] openid + 开关都配好 → 用该机器人自己的凭据发")
    client = FakeClient()
    rt = FakeRuntime({"bot1": FakeBot("bot1", client)})
    alerter = alerts.Alerter(
        make_per_bot("bot1", alert_enabled=True, alert_owner_openid="OPENID-BOT1"),
        runtime_getter=lambda: rt)
    check("available() 为真", alerter.available("bot1") is True, "")
    sent = alerter.notify("bot1", "test_key", "机器人掉线了", "详情若干")
    check("发送成功", sent is True, "")
    check("用的是单聊（c2c）", client.sent and client.sent[0][0] == "c2c",
          str(client.sent[:1]))
    check("发给了该机器人自己的 openid",
          client.sent and client.sent[0][1] == "OPENID-BOT1", "")
    check("正文里带告警标记", client.sent and "告警" in client.sent[0][2], "")
    check("正文里带详情", client.sent and "详情若干" in client.sent[0][2], "")

    print("\n[A7] 冷却按机器人分别计算")
    client.sent.clear()
    again = alerter.notify("bot1", "test_key", "机器人掉线了")
    check("冷却期内不再发送", again is False and client.sent == [], str(client.sent))
    other = alerter.notify("bot1", "another_key", "另一个问题")
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
        make_per_bot("bot1", alert_enabled=True, alert_owner_openid="WRONG-OPENID"),
        runtime_getter=lambda: rt, logger_obj=Log())
    result = alerter.notify("bot1", "fail_key", "测试失败")
    check("返回 False", result is False, "")
    text = " ".join(str(item[1]) for item in logged)
    check("日志里有真实原因", "openid 不属于该机器人" in text, text[:120])
    check("日志里提示了 openid 按机器人隔离", "按机器人隔离" in text, text[:120])

    print("\n[A9] 机器人没连上时明确说明，不假装发出")
    logged.clear()
    client = FakeClient(configured=False)
    rt = FakeRuntime({"bot1": FakeBot("bot1", client)})
    alerter = alerts.Alerter(
        make_per_bot("bot1", alert_enabled=True, alert_owner_openid="X"),
        runtime_getter=lambda: rt, logger_obj=Log())
    check("返回 False", alerter.notify("bot1", "dead", "测试") is False, "")
    text = " ".join(str(item[1]) for item in logged)
    check("日志说明机器人不可用", "不可用" in text, text[:100])

    print("\n[A10] 机器人不存在 → 不发、不崩")
    rt = FakeRuntime({"bot1": FakeBot("bot1", FakeClient())})
    alerter = alerts.Alerter(
        make_per_bot("no_such_bot", alert_enabled=True, alert_owner_openid="X"),
        runtime_getter=lambda: rt, logger_obj=Log())
    check("返回 False 且不抛异常",
          alerter.notify("no_such_bot", "x", "测试") is False, "")

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
          any(item["key"] == "bot_offline" for item in problems), str(problems))
    check("掉线告警带上是哪个机器人",
          any(item.get("bot_id") == "bot1" for item in problems), str(problems))

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

    print("\n[C2] 前端按机器人渲染（security 是 per_bot 分组）")
    server_src = open(os.path.join(PROJECT, "web", "server.py"),
                      encoding="utf-8").read()
    check("已不再有「用哪个机器人发告警」的动态选项逻辑",
          "_fill_alert_bot_options" not in server_src, "")
    settings_js = open(os.path.join(PROJECT, "web", "static", "js", "settings.js"),
                       encoding="utf-8").read()
    check("前端按 per_bot 分组读 bot_config",
          "section.per_bot" in settings_js and "spec.bot_config" in settings_js, "")

    print("\n[C3] 真接口：两个机器人的告警配置互不影响")
    try:
        import json as _json
        from core.config_manager import load_config
        from web.server import create_app
        tmp = os.path.join(PROJECT, "data", "_selftest", "unit_alert_iso")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        token = "ISO-TOKEN-0123456789"
        raw = schema.default_config()
        raw["web"].update({"host": "127.0.0.1", "token": token})
        raw["update"]["check_on_start"] = False
        raw["bots"] = [
            {"id": "bot1", "name": "机器人一", "enabled": True, "app_id": "A1", "app_secret": "s1"},
            {"id": "bot2", "name": "机器人二", "enabled": True, "app_id": "A2", "app_secret": "s2"},
        ]
        cfg_path = os.path.join(tmp, "cfg.json")
        with open(cfg_path, "w", encoding="utf-8") as handle:
            _json.dump(raw, handle, ensure_ascii=False)
        web = create_app(load_config(cfg_path)).test_client()

        # 给 bot1 单独配一套
        two_bots = [
            {"id": "bot1", "name": "机器人一", "enabled": True, "app_id": "A1", "app_secret": ""},
            {"id": "bot2", "name": "机器人二", "enabled": True, "app_id": "A2", "app_secret": ""},
        ]
        body1 = _json.loads(_json.dumps(schema.default_config()))
        body1["security"]["alert_owner_openid"] = "OPENID-BOT1"
        body1["security"]["admin_openids"] = ["ADMIN-BOT1"]
        body1["bots"] = two_bots          # 必须带上完整机器人列表，否则 bot2 会被当成未知
        r1 = web.post("/api/admin/config?token=" + token,
                      json={"config": body1, "bot_id": "bot1"}).get_json() or {}
        check("给 bot1 保存成功", r1.get("success") is True, str(r1.get("message"))[:80])

        # 给 bot2 配另一套
        body2 = _json.loads(_json.dumps(schema.default_config()))
        body2["security"]["alert_owner_openid"] = "OPENID-BOT2"
        body2["security"]["admin_openids"] = ["ADMIN-BOT2"]
        body2["bots"] = two_bots
        r2 = web.post("/api/admin/config?token=" + token,
                      json={"config": body2, "bot_id": "bot2"}).get_json() or {}
        check("给 bot2 保存成功", r2.get("success") is True, str(r2.get("message"))[:80])

        saved = load_config(cfg_path)
        for bid, openid, admin in (("bot1", "OPENID-BOT1", ["ADMIN-BOT1"]),
                                   ("bot2", "OPENID-BOT2", ["ADMIN-BOT2"])):
            sec = ((saved.resolve_for_bot(bid) or {}).get("security") or {})
            check("%s 的 openid 是它自己的" % bid,
                  sec.get("alert_owner_openid") == openid, str(sec.get("alert_owner_openid")))
            check("%s 的管理员是它自己的" % bid,
                  sec.get("admin_openids") == admin, str(sec.get("admin_openids")))

        global_sec = saved.config.data.get("security") or {}
        check("全局兜底值没被污染（openid 仍为空）",
              global_sec.get("alert_owner_openid") == "",
              repr(global_sec.get("alert_owner_openid")))
        check("全局兜底值没被污染（管理员仍为空）",
              global_sec.get("admin_openids") == [],
              str(global_sec.get("admin_openids")))
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception as exc:
        check("真接口验证没抛异常", False, str(exc))

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
