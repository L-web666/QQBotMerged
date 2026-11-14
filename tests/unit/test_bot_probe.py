# -*- coding: utf-8 -*-
"""单元测试：云同步日志降级 + 退出汇总 + 机器人名称自动获取

三部分：

  A. **云同步日志降到 debug**：默认间隔下「云同步完成：上传 x…」会周期性刷屏，
     而它日常没有信息量（成功时通常零变化）。现在单次同步只写 debug，
     **退出前**打一行总账，保证用户仍看得到"这场运行同步了什么"。

  B. **`GET /users/@me` 客户端**：把官方返回整理成统一结构（不依赖具体字段位置）。

  C. **`/api/admin/config/probe_bot` 接口**：用 AppID + AppSecret 换取机器人名称；
     参数校验、平台报错透传、**绝不把 AppSecret 泄漏到消息/日志里**。

不联网：平台调用全部用桩替换（真实网络只在手工实测时跑过）。

运行：`python tests/unit/test_bot_probe.py`
"""

import json
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import cloud_sync as cloud_module      # noqa: E402
from core import config_schema as schema         # noqa: E402
from core.config_manager import load_config      # noqa: E402
from core.qq_api import QQApiClient              # noqa: E402
from web.server import create_app                # noqa: E402

PASSED = []
FAILED = []
TOKEN = "PROBE-TEST-TOKEN-0123456789"
FAKE_SECRET = "FAKE-SECRET-MUST-NOT-LEAK"


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


class CapturingLog:
    """记录 (级别, 文本) 的假 logger。"""

    def __init__(self):
        self.records = []

    def _add(self, level, msg, args):
        self.records.append((level, msg % args if args else msg))

    def debug(self, msg, *a, **k):
        self._add("DEBUG", msg, a)

    def info(self, msg, *a, **k):
        self._add("INFO", msg, a)

    def warning(self, msg, *a, **k):
        self._add("WARNING", msg, a)

    def error(self, msg, *a, **k):
        self._add("ERROR", msg, a)

    def count(self, level):
        return len([item for item in self.records if item[0] == level])


def make_client(root):
    cfg = schema.default_config()
    cfg["web"].update({"host": "127.0.0.1", "token": TOKEN})
    cfg["update"]["check_on_start"] = False
    path = os.path.join(root, "cfg_%d.json" % (int(time.time() * 1000) % 100000))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, ensure_ascii=False)
    return create_app(load_config(path)).test_client()


def main():
    print("=" * 70)
    print("  云同步日志降级 + 机器人名称自动获取")
    print("=" * 70)

    root = os.path.join(PROJECT, "data", "_selftest",
                        "unit_probe_%d" % int(time.time() * 1000))
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)

    try:
        # ================= A. 云同步日志 =================
        print("\n[A1] 源码里单次同步的完成日志是 debug")
        source = open(os.path.join(PROJECT, "core", "cloud_sync.py"),
                      encoding="utf-8").read()
        check("用 log.debug 输出同步完成",
              'self.log.debug("云同步完成：%s"' in source, "")
        check("不再用 log.info 输出同步完成",
              'self.log.info("云同步完成：%s"' not in source, "")
        check("首次同步也降到 debug",
              'self.log.debug("首次云同步完成' in source, "")
        check("有 summary() 供退出时汇总", "def summary(self)" in source, "")

        print("\n[A2] summary() 的内容与边界")
        probe = cloud_module.CloudSync.__new__(cloud_module.CloudSync)
        probe.log = CapturingLog()
        probe.totals = {"uploaded": 0, "downloaded": 0, "deleted": 0,
                        "skipped": 0, "errors": 0}
        probe._sync_runs = 0
        check("一次都没同步过 → 空串（不打无意义的 0）", probe.summary() == "",
              repr(probe.summary()))

        probe.totals.update({"uploaded": 5, "downloaded": 2, "deleted": 1,
                             "skipped": 30, "errors": 0})
        probe._sync_runs = 3
        text = probe.summary()
        check("汇总里带次数", "共 3 次" in text, text)
        check("汇总里带上传数", "上传 5" in text, text)
        check("汇总里带恢复数", "恢复 2" in text, text)
        check("汇总里带删除数", "删除 1" in text, text)
        check("汇总里带跳过数", "跳过 30" in text, text)
        check("汇总里带失败数", "失败 0" in text, text)

        print("\n[A3] 运行时在退出前调用 summary()")
        lifecycle = open(os.path.join(PROJECT, "core", "runtime_pkg", "lifecycle.py"),
                         encoding="utf-8").read()
        check("stop() 里调用了 cloud_sync.summary()",
              "self.cloud_sync.summary()" in lifecycle, "")
        check("汇总以 info 级别输出（默认可见）",
              "self.log.info(\"%s\", line)" in lifecycle, "")

        # ================= B. bot_info 客户端 =================
        print("\n[B1] bot_info() 从官方返回里取字段")
        client = QQApiClient(bot_id="probe", app_id="102000001",
                             app_secret=FAKE_SECRET, config=None)
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            return {"id": "5777414462219517083", "username": "阳光小助手",
                    "avatar": "https://thirdqq.qlogo.cn/x", "bot": True}

        client.request = fake_request
        info = client.bot_info()
        check("用的是 GET", captured.get("method") == "GET", str(captured.get("method")))
        check("打的是 /users/@me",
              str(captured.get("url", "")).endswith("/users/@me"), str(captured.get("url")))
        check("取到 username", info.get("username") == "阳光小助手", str(info))
        check("取到 id", info.get("id") == "5777414462219517083", "")
        check("取到头像", info.get("avatar", "").startswith("https://"), "")

        print("\n[B2] 返回结构异常时不抛异常")
        client.request = lambda *a, **k: {}
        check("空返回 → 空字典结构", client.bot_info().get("username") == "",
              str(client.bot_info()))
        client.request = lambda *a, **k: {"data": {"username": "嵌在 data 里"}}
        check("支持 data 包一层", client.bot_info().get("username") == "嵌在 data 里",
              str(client.bot_info()))

        # ================= C. probe_bot 接口 =================
        print("\n[C1] 参数校验")
        web = make_client(root)
        response = web.post("/api/admin/config/probe_bot", json={})
        check("没有令牌 → 401", response.status_code == 401, str(response.status_code))
        response = web.post("/api/admin/config/probe_bot?token=" + TOKEN,
                            json={"app_secret": "x"})
        check("缺 AppID → 400", response.status_code == 400, str(response.status_code))
        response = web.post("/api/admin/config/probe_bot?token=" + TOKEN,
                            json={"app_id": "102000001"})
        check("缺 AppSecret → 400", response.status_code == 400, str(response.status_code))

        print("\n[C2] 成功路径（桩替换平台调用）")
        import core.qq_api as qq_module
        original = qq_module.QQApiClient.bot_info

        def fake_info(self):
            return {"id": "123", "username": "测试机器人", "avatar": "https://a", "bot": True}

        qq_module.QQApiClient.bot_info = fake_info
        try:
            response = web.post("/api/admin/config/probe_bot?token=" + TOKEN,
                                json={"app_id": "102000001", "app_secret": FAKE_SECRET})
            body = response.get_json() or {}
            check("成功 → 200", response.status_code == 200, str(response.status_code))
            check("带回机器人名称", body.get("name") == "测试机器人", str(body))
            check("带回头像", bool(body.get("avatar")), str(body))

            print("\n[C3] 平台没给名称时算失败")
            qq_module.QQApiClient.bot_info = lambda self: {"username": ""}
            response = web.post("/api/admin/config/probe_bot?token=" + TOKEN,
                                json={"app_id": "102000001", "app_secret": FAKE_SECRET})
            check("名称为空 → 502", response.status_code == 502, str(response.status_code))

            print("\n[C4] 平台报错时**不泄漏 AppSecret**")
            def boom(self):
                raise RuntimeError("凭据错误 secret=%s" % self.app_secret)

            qq_module.QQApiClient.bot_info = boom
            response = web.post("/api/admin/config/probe_bot?token=" + TOKEN,
                                json={"app_id": "102000001", "app_secret": FAKE_SECRET})
            body = response.get_json() or {}
            message = str(body.get("message") or "")
            check("失败 → 502", response.status_code == 502, str(response.status_code))
            check("错误信息里有原因", "检测失败" in message, message[:80])
            check("错误信息里没有 AppSecret", FAKE_SECRET not in message, message[:120])
        finally:
            qq_module.QQApiClient.bot_info = original

        print("\n[C5] 前端入口齐全")
        js = open(os.path.join(PROJECT, "web", "static", "js", "settings.js"),
                  encoding="utf-8").read()
        check("编辑器里有「获取名称」按钮", "data-probename" in js, "")
        check("点击事件里接了处理", "probeBotName(target)" in js, "")
        check("调用了 probe_bot 接口", "/api/admin/config/probe_bot" in js, "")
        check("会写回「显示名称」", "nameInput.value = data.name" in js, "")
        check("漏填密钥时给出提示",
              "请重新填一次 AppSecret" in js, "")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
