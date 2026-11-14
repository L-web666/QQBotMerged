# -*- coding: utf-8 -*-
"""单元测试：机器人名称自动维护 + 更新安装的日志与时序

  A. **名称不再由用户填**：保存时用凭据去开放平台取，之后每小时校正；
     查询失败时**保留原名**不清空；一小时内不重复查（避免打满接口）。

  B. **更新安装的两处体验问题**：
     · 「要点两次更新才提示重启」——根因是 install() 拿到锁后没立刻把状态置为
       进行中，前端轮询看到 idle 就把按钮解锁了。现在一开始就是 busy。
     · 安装完成不再打印 `====` 横线框，一行说清版本/文件数/备份位置。

运行：`python tests/unit/test_bot_names.py`
"""

import json
import os
import re
import shutil
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import bot_names                        # noqa: E402
from core import config_schema as schema          # noqa: E402
from core.config_manager import load_config       # noqa: E402
from core.updater import Updater                  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def main():
    print("=" * 70)
    print("  机器人名称自动维护 + 更新安装体验")
    print("=" * 70)

    root = os.path.join(PROJECT, "data", "_selftest",
                        "unit_names_%d" % int(time.time() * 1000))
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    original_fetch = bot_names.fetch_bot_name

    try:
        # ================= A. 名称自动维护 =================
        print("\n[A1] 保存/定时刷新：用凭据取名称并写回")
        calls = []

        def fake_fetch(app_id, app_secret, config=None, logger_obj=None):
            calls.append(app_id)
            return {"APP-1": "阳光小助手", "APP-2": "另一个机器人"}.get(app_id, "")

        bot_names.fetch_bot_name = fake_fetch
        cfg = schema.default_config()
        cfg["bots"] = [
            {"id": "bot1", "name": "", "enabled": True, "app_id": "APP-1",
             "app_secret": "s1", "intents": 100663296},
            {"id": "bot2", "name": "旧名字", "enabled": True, "app_id": "APP-2",
             "app_secret": "s2", "intents": 100663296},
            {"id": "bot3", "name": "没密钥", "enabled": False, "app_id": "APP-3",
             "app_secret": "", "intents": 100663296},
        ]
        path = os.path.join(root, "cfg.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False)
        manager = load_config(path)

        updated = bot_names.refresh_names(manager, force=True)
        check("空名字被填上", updated.get("bot1") == "阳光小助手", str(updated))
        check("旧名字被更新为平台名", updated.get("bot2") == "另一个机器人", str(updated))
        check("没有密钥的机器人不查询（不报错）",
              "APP-3" not in calls, str(calls))
        saved = {b["id"]: b for b in load_config(path).config.get("bots") or []}
        check("写回了配置文件", saved["bot1"].get("name") == "阳光小助手", "")
        check("记录了查询时间", bool(saved["bot1"].get("name_checked_at")), "")

        print("\n[A2] 一小时内不重复查询")
        calls.clear()
        again = bot_names.refresh_names(manager, force=False)
        check("不调用平台", calls == [], str(calls))
        check("返回空表示无变化", again == {}, str(again))

        print("\n[A3] 超过一小时后会重新查询")
        bots = manager.config.get("bots") or []
        for bot in bots:
            if bot.get("name_checked_at"):
                bot["name_checked_at"] = int(time.time()) - bot_names.REFRESH_SECONDS - 60
        calls.clear()
        bot_names.refresh_names(manager, force=False)
        check("重新查询了平台", len(calls) == 2, str(calls))

        print("\n[A4] 平台改名后能跟上")
        bot_names.fetch_bot_name = (
            lambda a, s, config=None, logger_obj=None: "改名后的机器人")
        changed = bot_names.refresh_names(manager, force=True)
        check("bot1 名称跟着变", changed.get("bot1") == "改名后的机器人", str(changed))

        print("\n[A5] 查询失败时保留原名（不清空）")
        bot_names.fetch_bot_name = (
            lambda a, s, config=None, logger_obj=None: "")
        before = {b["id"]: b.get("name")
                  for b in load_config(path).config.get("bots") or []}
        bot_names.refresh_names(manager, force=True)
        after = {b["id"]: b.get("name")
                 for b in load_config(path).config.get("bots") or []}
        check("名称未被清空", before == after, "%s → %s" % (before, after))

        print("\n[A6] 前端：名称只读、没有手填入口")
        js = open(os.path.join(PROJECT, "web", "static", "js", "settings.js"),
                  encoding="utf-8").read()
        check("名称输入框是 readonly", "readonly" in js, "")
        check("没有「获取名称」按钮", "data-probename" not in js, "")
        check("新机器人名称留空", "name: ''" in js, "")
        check("提示名称为自动获取", "名称自动获取" in js, "")

        print("\n[A7] 保存配置时会触发同步（源码层面）")
        server_src = open(os.path.join(PROJECT, "web", "server.py"),
                          encoding="utf-8").read()
        check("保存后调用 refresh_names",
              "bot_names.refresh_names(" in server_src, "")
        check("只对 bots 变更触发",
              '"bots" for item in changed' in server_src, "")
        lifecycle = open(os.path.join(PROJECT, "core", "runtime_pkg", "lifecycle.py"),
                         encoding="utf-8").read()
        check("运行时启动了定时刷新",
              "_start_bot_name_refresher" in lifecycle, "")
        check("停止时关闭刷新线程",
              "_bot_name_refresher" in lifecycle and "refresher.stop()" in lifecycle, "")

        # ================= B. 更新安装体验 =================
        print("\n[B1] 安装一开始就是 busy（修「要点两次」）")
        upd_src = open(os.path.join(PROJECT, "core", "updater.py"),
                       encoding="utf-8").read()
        install_body = upd_src.split("def install(")[1].split("def ")[0]
        check("拿到锁后立刻设置状态",
              'self.state.set("downloading", "正在准备更新' in install_body, "")
        # 顺序：先 set 状态，再做网络请求
        set_pos = install_body.find('self.state.set("downloading", "正在准备更新')
        net_pos = install_body.find("resolve_tag_commit")
        check("设置状态在解析 tag 之前", 0 <= set_pos < net_pos,
              "set=%d net=%d" % (set_pos, net_pos))

        engine = Updater(base_dir=root, data_dir=os.path.join(root, "u"))
        check("初始状态不是 busy", engine.state.snapshot().get("busy") is False, "")
        engine.state.set("downloading", "正在准备更新…", 1, "v9.9.9")
        check("置为 downloading 后 busy=True",
              engine.state.snapshot().get("busy") is True, "")
        engine.state.set("done", "完成", 100)
        check("完成后 busy=False", engine.state.snapshot().get("busy") is False, "")

        print("\n[B2] 安装完成不再打印横线框")
        check("没有 60 个等号的横线", re.search(r'"=" \* 60', upd_src) is None, "")
        check("版本/文件数/备份位置合并成一行",
              "更新已安装：%s → %s（写入 %d 个文件），" in upd_src, "")
        check("提示仍然说明要重启",
              "请重启程序让新版本生效" in upd_src, "")

        print("\n[B3] 前端有防抖锁定，避免空窗期误解锁")
        admin_js = open(os.path.join(PROJECT, "web", "static", "js", "admin.js"),
                        encoding="utf-8").read()
        check("有点击后的本地锁定", "updateLockUntil" in admin_js, "")
        check("轮询时尊重锁定",
              "Date.now() < Admin.updateLockUntil" in admin_js, "")
    finally:
        bot_names.fetch_bot_name = original_fetch
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
