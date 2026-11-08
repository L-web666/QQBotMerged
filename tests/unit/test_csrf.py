# -*- coding: utf-8 -*-
"""单元测试：CSRF 防护（跨站写请求）+ 首次运行自动生成令牌

背景（已实测复现的真实漏洞）：
默认配置是「只监听本机 + 不设令牌」，`check_token()` 此时直接放行。
于是任意网页可以用**跨站简单请求**（`Content-Type: text/plain`，不触发预检）
POST 到 /api/admin/shutdown 或 /api/admin/update/install，
让本机浏览器替它发请求 —— 实测能真的关掉程序、并进入更新逻辑。

本文件钉住三件事：
  1. 跨站写请求被 403 拦下（shutdown / update/install / 改配置）；
  2. 同源请求、非浏览器客户端（curl）不受影响；
  3. 被拦下的请求在审计日志里留痕（result="denied"）。

不联网、不碰真实 data/。

运行：`python tests/unit/test_csrf.py`
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

from core import audit_log            # noqa: E402
from core import config_schema as schema   # noqa: E402
from core import paths                # noqa: E402
from core.config_manager import load_config   # noqa: E402
from web.server import create_app     # noqa: E402

PASSED = []
FAILED = []

CROSS = {"Origin": "https://evil.example.com", "Sec-Fetch-Site": "cross-site",
         "Content-Type": "text/plain"}
SAME = {"Origin": "http://127.0.0.1:8666", "Sec-Fetch-Site": "same-origin"}


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def make_client(root, token=""):
    cfg = schema.default_config()
    cfg["web"].update({"host": "127.0.0.1", "token": token, "port": 8666})
    cfg["update"]["check_on_start"] = False
    path = os.path.join(root, "cfg_%s_%d.json" % (bool(token), int(time.time() * 1000) % 100000))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, ensure_ascii=False)
    return create_app(load_config(path)).test_client()


def main():
    print("=" * 70)
    print("  CSRF 防护")
    print("=" * 70)

    root = os.path.join(PROJECT, "data", "_selftest",
                        "unit_csrf_%d" % int(time.time() * 1000))
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    original_log = paths.LOG_DIR

    try:
        paths.LOG_DIR = os.path.join(root, "logs")
        os.makedirs(paths.LOG_DIR, exist_ok=True)
        client = make_client(root)          # 无令牌（默认配置）

        # ---------- 1. 跨站写请求必须被拒 ----------
        print("\n[1] 跨站写请求被 403 拦下（无令牌时最危险）")
        for path in ("/api/admin/shutdown", "/api/admin/update/install",
                     "/api/admin/maintenance"):
            response = client.post(path, headers=CROSS, data="{}")
            check(f"跨站 POST {path} → 403", response.status_code == 403,
                  str(response.status_code))
            body = response.get_json() or {}
            check(f"{path} 响应标明 CSRF",
                  bool(body.get("cross_site_blocked")), str(body)[:80])

        print("\n[2] 跨站改配置同样被拒")
        response = client.post("/api/admin/config", headers=CROSS,
                               data=json.dumps({"web": {"page_size": 99}}))
        check("跨站 POST /api/admin/config → 403", response.status_code == 403,
              str(response.status_code))

        # ---------- 3. 同源请求不受影响 ----------
        print("\n[3] 同源请求正常放行（不被 CSRF 拦）")
        for path in ("/api/admin/shutdown", "/api/admin/maintenance"):
            response = client.post(path, headers=SAME, data="{}")
            check(f"同源 POST {path} 不是 403", response.status_code != 403,
                  str(response.status_code))

        print("\n[4] 读接口（GET）完全不受 CSRF 影响")
        response = client.get("/api/admin/update/status", headers=CROSS)
        check("跨站 GET 状态接口不是 403", response.status_code != 403,
              str(response.status_code))

        # ---------- 5. 非浏览器客户端不受影响 ----------
        print("\n[5] 没有 Origin/Sec-Fetch（curl、脚本）不被误伤")
        response = client.post("/api/admin/maintenance", data="{}")
        check("无来源头的 POST 不是 403", response.status_code != 403,
              str(response.status_code))

        # ---------- 6. 同站（same-site）视为安全 ----------
        print("\n[6] same-site / none 视为可接受")
        for site in ("same-site", "none"):
            response = client.post("/api/admin/maintenance",
                                   headers={"Sec-Fetch-Site": site}, data="{}")
            check(f"Sec-Fetch-Site={site} 不是 403", response.status_code != 403,
                  str(response.status_code))

        # ---------- 7. 拒绝必须留痕 ----------
        print("\n[7] 被拦下的请求在审计日志里留痕")
        entries = audit_log.read_tail(50)
        blocked = [item for item in entries
                   if item.get("action") == "security.csrf_blocked"]
        check("有 csrf_blocked 记录", len(blocked) >= 3, str(len(blocked)))
        if blocked:
            first = blocked[0]
            check("result 为 denied", first.get("result") == "denied",
                  str(first.get("result")))
            detail = first.get("detail") or {}
            check("记录了来源 Origin",
                  "evil.example.com" in str(detail.get("origin") or ""), str(detail))
            check("记录了目标路径", bool(first.get("target")), str(first.get("target")))

        print("\n[8] 有令牌时：跨站请求先被 CSRF 拦（不泄漏令牌是否有效）")
        authed = make_client(root, token="Xk9mQ2pL7vR4nT8wZ1bYc3dE")
        response = authed.post("/api/admin/shutdown?token=Xk9mQ2pL7vR4nT8wZ1bYc3dE",
                               headers=CROSS, data="{}")
        check("带正确令牌的跨站请求也被 403（CSRF 先于鉴权）",
              response.status_code == 403, str(response.status_code))

        print("\n[9] 失败鉴权也留痕")
        failed = authed.post("/api/admin/maintenance?token=WRONG", headers=SAME,
                             data="{}")
        check("错误令牌返回 401", failed.status_code == 401, str(failed.status_code))
        entries = audit_log.read_tail(50)
        auth_failed = [item for item in entries
                       if item.get("action") == "security.auth_failed"]
        check("有 auth_failed 记录", bool(auth_failed), str(len(auth_failed)))
        if auth_failed:
            text = json.dumps(auth_failed[-1], ensure_ascii=False)
            check("审计里不出现用户提供的令牌原文", "WRONG" not in text, text[:120])

        # ---------- 10. 首次运行自动生成令牌 ----------
        print("\n[10] 首次运行自动生成强令牌（不覆盖已有）")
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "run_module", os.path.join(PROJECT, "run.py"))
        run_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_module)

        blank = os.path.join(root, "blank.json")
        cfg = schema.default_config()
        cfg["web"]["token"] = ""
        with open(blank, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False)

        manager = load_config(blank)
        generated = run_module.ensure_access_token(manager)
        check("生成了令牌", bool(generated), repr(generated))
        check("长度 >= 32", len(generated) >= 32, str(len(generated)))
        check("不是弱令牌",
              __import__("core.security_rules", fromlist=["token_weakness"])
              .token_weakness(generated) == "", "")
        reloaded = load_config(blank)
        check("已写入配置文件",
              reloaded.config.str_of("web", "token", default="") == generated, "")
        check("已有令牌时不重新生成",
              run_module.ensure_access_token(reloaded) == "", "")

        keep = os.path.join(root, "keep.json")
        cfg2 = schema.default_config()
        cfg2["web"]["token"] = "USER-SET-TOKEN-0123456789abcdef"
        with open(keep, "w", encoding="utf-8") as handle:
            json.dump(cfg2, handle, ensure_ascii=False)
        check("用户自己设的令牌不会被改动",
              run_module.ensure_access_token(load_config(keep)) == "", "")
    finally:
        paths.LOG_DIR = original_log
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
