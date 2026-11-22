# -*- coding: utf-8 -*-
"""单元测试：Werkzeug 设备名路径风险 + 静态路由白名单 + 管理员旧配置不再继承

对应审计报告 DEP-WERKZEUG-001 与 AUTH-SCOPE-001。

  A. **依赖约束**：requirements.txt 必须显式约束 Werkzeug>=3.1.9
     （GHSA-g6x2-hccm-hh4m / CVE-2026-102598：<3.1.9 在 Windows NTFS 上
     遇到 `NUL:` 这类空 ADS 标记设备名会让静态文件请求挂起）。

  B. **静态路由白名单**：Flask 自动注册的静态路由不经过令牌校验，
     这里改成自己实现的受限路由 —— 设备名 / ADS / 穿越 / 非白名单扩展名一律拒绝。
     **踩过的坑**：只设 `app.config["static_folder"] = None` 撤不掉 Flask 在构造时
     注册的路由，会出现两条同名路由，Flask 走先注册的那条（不受限）——
     必须构造时就不传 static_folder。

  C. **管理员配置不再继承全局**：`security` 自 v1.4.1 起按机器人隔离，
     但升级前的旧配置里可能还留着全局 `admin_openids`。若让没配过的机器人
     "继承全局"，旧名单会对所有机器人继续有效（跨机器人越权）。

运行：`python tests/unit/test_static_deps.py`
"""

import json
import os
import re
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import config_schema as schema                     # noqa: E402
from core.config_manager import load_config                  # noqa: E402
from web.server import create_app, _static_path_allowed      # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def make_client(root, token="STATIC-TOKEN-0123456789"):
    cfg = schema.default_config()
    cfg["web"].update({"host": "127.0.0.1", "token": token})
    cfg["update"]["check_on_start"] = False
    path = os.path.join(root, "cfg_%d.json" % len(os.listdir(root)))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, ensure_ascii=False)
    app = create_app(load_config(path))
    return app, app.test_client(), token


def main():
    print("=" * 70)
    print("  Werkzeug 设备名风险 + 静态路由白名单 + 管理员不继承全局")
    print("=" * 70)

    root = os.path.join(PROJECT, "data", "_selftest", "unit_static")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)

    try:
        # ================= A. 依赖约束 =================
        print("\n[A1] requirements.txt 显式约束 Werkzeug")
        req = open(os.path.join(PROJECT, "requirements.txt"), encoding="utf-8").read()
        m = re.search(r"^\s*Werkzeug\s*>=\s*([0-9.]+)", req, re.M | re.I)
        check("声明了 Werkzeug 下限", m is not None, req[:120])
        if m:
            version = tuple(int(x) for x in m.group(1).split("."))
            check("下限 >= 3.1.9", version >= (3, 1, 9), m.group(1))
        check("注释里说明了原因", "CVE-2026-102598" in req or "GHSA-g6x2" in req, "")

        print("\n[A2] 实际安装的版本（仅供参考，不强制）")
        try:
            from importlib.metadata import version as pkg_version
            installed = pkg_version("Werkzeug")
            print("      当前环境 Werkzeug:", installed)
        except Exception:
            print("      读不到安装版本")

        print("\n[A3] 最低 Python 版本已与 Werkzeug 3.1.9 的要求对齐")
        readme = open(os.path.join(PROJECT, "README.md"), encoding="utf-8").read()
        check("README 里不再声称支持 Python 3.8",
              "3.8+" not in readme, "")

        # ================= B. 静态路由白名单 =================
        print("\n[B1] 白名单函数：正常资源放行")
        for good in ("css/app.css", "js/util.js", "js/chat.js", "img/x.png",
                     "favicon.ico"):
            check(f"放行 {good}", _static_path_allowed(good) is True, "")

        print("\n[B2] 白名单函数：Windows 设备名一律拒绝")
        for bad in ("NUL", "NUL:", "nul:", "NUL.txt", "CON.js", "AUX.css",
                    "COM1.js", "LPT1.css", "js/CON.js"):
            check(f"拒绝 {bad}", _static_path_allowed(bad) is False, "")

        # 审计 SEC-STATIC-001：Windows 还认 **上标数字**写法（COM¹/LPT²…）。
        # 这些不是 ASCII，早期实现用字符串精确匹配会漏掉，`COM¹.js` 能通过白名单。
        print("\n[B2b] 白名单函数：上标数字设备名（SEC-STATIC-001）")
        for digit in ("\u00b9", "\u00b2", "\u00b3"):
            for base in ("COM", "LPT", "com", "lpt"):
                for suffix in (".js", ".css", ""):
                    bad = base + digit + suffix
                    check(f"拒绝 {bad!r}", _static_path_allowed(bad) is False, "")

        print("\n[B2c] 白名单函数：其它 Unicode 绕过写法")
        for bad in ("\uff23\uff2f\uff2e.js",      # 全角 CON
                    "COM\u2074.js",                # 上标 4
                    "NUL\u200b.js",                # 零宽空格
                    "NUL\u00a0.txt",               # 不换行空格
                    "con\u0301.js",                # 组合重音
                    "nul\u00ad.js"):               # 软连字符
            check(f"拒绝 {bad!r}", _static_path_allowed(bad) is False, "")

        print("\n[B2d] 这些加固不能误伤正常资源")
        for good in ("css/app.css", "css/anim.css", "js/util.js", "js/chat.js",
                     "js/settings.js", "js/admin.js", "js/groups.js",
                     "js/app.js", "js/effects.js", "favicon.ico",
                     "img/logo.png", "img/icon.svg", "a_b-c.d.js"):
            check(f"仍放行 {good}", _static_path_allowed(good) is True, "")

        print("\n[B3] 白名单函数：ADS / 穿越 / 其它异常一律拒绝")
        for bad in ("css/app.css:stream", "js/util.js::$DATA",
                    "../config.json", "css/../../config.json",
                    "..\\config.json", "css\\app.css", "", "css/",
                    "css//app.css", "css/./app.css", "app.css ", "app.css.",
                    "config.json", "../data/messages.db", "css/app.css\x00.png"):
            check(f"拒绝 {bad!r}", _static_path_allowed(bad) is False, "")

        print("\n[B4] 只有一条 static 路由（不能被 Flask 默认路由顶替）")
        app, client, token = make_client(root)
        static_rules = [r for r in app.url_map.iter_rules() if "static" in str(r)]
        check("static 路由恰好 1 条", len(static_rules) == 1,
              str([str(r) for r in static_rules]))
        if static_rules:
            check("走的是受限版本（endpoint=static_file）",
                  static_rules[0].endpoint == "static_file", static_rules[0].endpoint)

        print("\n[B5] 不带令牌也能取正常资源（前端 <link>/<script> 带不了头）")
        for url in ("/static/css/app.css", "/static/js/util.js"):
            response = client.get(url)
            check(f"{url} -> 200", response.status_code == 200,
                  str(response.status_code))

        print("\n[B6] 带令牌时可疑路径被白名单挡下（404）")
        for url in ("/static/NUL", "/static/NUL:", "/static/nul:",
                    "/static/NUL.txt", "/static/AUX.css", "/static/CON.js",
                    "/static/css/app.css:stream", "/static/js/util.js::$DATA",
                    "/static/config.json", "/static/no_such_file.css"):
            response = client.get(url + "?token=" + token)
            check(f"{url} -> 404", response.status_code == 404,
                  str(response.status_code))

        print("\n[B6b] 带令牌时上标数字设备名也 404（SEC-STATIC-001 的 HTTP 面）")
        for url in ("/static/COM\u00b9.js", "/static/COM\u00b2.js",
                    "/static/COM\u00b3.js", "/static/LPT\u00b9.css",
                    "/static/LPT\u00b2.css", "/static/LPT\u00b3.css",
                    "/static/com\u00b9.js", "/static/lpt\u00b3.css"):
            response = client.get(url + "?token=" + token)
            check(f"{url!r} -> 404", response.status_code == 404,
                  str(response.status_code))

        print("\n[B7] 不带令牌时可疑路径也不会拿到内容")
        for url in ("/static/NUL:", "/static/js/util.js::$DATA",
                    "/static/config.json"):
            response = client.get(url)
            check(f"{url} 被拒", response.status_code in (401, 404),
                  str(response.status_code))

        # ================= C. 管理员不继承全局 =================
        print("\n[C1] 旧配置的全局管理员名单不再作用于任何机器人")
        cfg = schema.default_config()
        cfg["web"].update({"host": "127.0.0.1", "token": "x"})
        cfg["security"]["admin_openids"] = ["LEGACY-GLOBAL-ADMIN"]
        cfg["security"]["alert_owner_openid"] = "LEGACY-OPENID"
        cfg["bots"] = [
            {"id": "bot1", "name": "一", "enabled": True, "app_id": "A1", "app_secret": "s1"},
            {"id": "bot2", "name": "二", "enabled": True, "app_id": "A2", "app_secret": "s2"},
        ]
        path = os.path.join(root, "legacy.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False)
        manager = load_config(path)
        for bid in ("bot1", "bot2"):
            sec = manager.resolve_for_bot(bid)
            admin = sec.list_of("security", "admin_openids", default=[])
            openid = sec.str_of("security", "alert_owner_openid", default="")
            check(f"{bid} 没有继承全局管理员", admin == [], str(admin))
            check(f"{bid} 没有继承全局告警 openid", openid == "", openid)

        print("\n[C2] 各机器人自己的配置仍然生效")
        manager.set_for_bot("bot1", "security.admin_openids", ["ADMIN-BOT1"])
        manager.set_for_bot("bot1", "security.alert_owner_openid", "OPENID-BOT1")
        manager.save()
        reloaded = load_config(path)
        sec1 = reloaded.resolve_for_bot("bot1")
        sec2 = reloaded.resolve_for_bot("bot2")
        check("bot1 用自己的管理员",
              sec1.list_of("security", "admin_openids", default=[]) == ["ADMIN-BOT1"],
              str(sec1.list_of("security", "admin_openids", default=[])))
        check("bot1 用自己的 openid",
              sec1.str_of("security", "alert_owner_openid", default="") == "OPENID-BOT1", "")
        check("bot2 仍然是空的（没捡到 bot1 的）",
              sec2.list_of("security", "admin_openids", default=[]) == [],
              str(sec2.list_of("security", "admin_openids", default=[])))

        print("\n[C3] 非 security 的按机器人设置照旧继承全局（没有误伤）")
        cfg2 = schema.default_config()
        cfg2["web"].update({"host": "127.0.0.1", "token": "x"})
        cfg2["reply"]["max_segment_length"] = 1234
        cfg2["bots"] = [{"id": "bot1", "name": "一", "enabled": True,
                         "app_id": "A1", "app_secret": "s1"}]
        path2 = os.path.join(root, "other.json")
        with open(path2, "w", encoding="utf-8") as handle:
            json.dump(cfg2, handle, ensure_ascii=False)
        got = load_config(path2).resolve_for_bot("bot1").int_of(
            "reply", "max_segment_length", default=0)
        check("reply 段仍继承全局", got == 1234, str(got))

        print("\n[C4] 全局配置本体仍保留原值（只是不再被继承）")
        cfg3 = schema.default_config()
        cfg3["web"].update({"host": "127.0.0.1", "token": "x"})
        cfg3["security"]["admin_openids"] = ["GLOBAL-ONLY"]
        cfg3["bots"] = []
        path3 = os.path.join(root, "empty.json")
        with open(path3, "w", encoding="utf-8") as handle:
            json.dump(cfg3, handle, ensure_ascii=False)
        kept = load_config(path3).config.list_of("security", "admin_openids", default=[])
        check("config 本体的全局值没被抹掉", kept == ["GLOBAL-ONLY"], str(kept))
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
