# -*- coding: utf-8 -*-
"""单元测试：CSP 与前端启动数据（NEW2-01 回归）

背景：为修 XSS 加的 CSP 用了 `script-src 'self'`（没有 'unsafe-inline'、没有 nonce），
而页面当时依赖一个**内联** `<script>` 注入 `window.__BOOT__` 与访问令牌 ——
结果内联脚本被浏览器拦掉：前端拿不到令牌（所有接口 401）、也拿不到启动数据
（主题/分页等设置全部回落默认值），后台变成"能加载 HTML 但不可用"的空壳。

旧的安全测试只断言"响应头里有 CSP"，**不会**在破坏页面时变红，所以没抓到。
本文件的断言目标是"**在严格 CSP 下页面仍然可用**"：

  · 响应头里没有 'unsafe-inline' / 'unsafe-eval'（安全底线不能为了可用性被放宽）
  · 模板里不再出现内联脚本赋值（会被 CSP 拦）
  · 启动数据通过 `application/json` 块下发（该类型不会被执行，不受 script-src 约束）
  · 前端 JS 能从 `#boot-data` 解析出 BOOT，并仍能从 URL 取到令牌

分层约定见 `PROGRESS.md`：不连网、不碰真实 `data/`，也不读取 `config.json`。

运行：`python tests/unit/test_csp_boot.py`
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

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def read(path):
    with open(os.path.join(PROJECT, path), "r", encoding="utf-8") as handle:
        return handle.read()


def csp_script_src(csp: str) -> str:
    for piece in (csp or "").split(";"):
        if "script-src" in piece:
            return piece.strip()
    return ""


def main():
    print("=" * 70)
    print("  CSP 与前端启动数据（后台在严格 CSP 下必须仍可用）")
    print("=" * 70)

    from core import config_schema as schema
    from core.config_manager import load_config
    from web.server import create_app

    sandbox = os.path.join(PROJECT, "data", "_selftest", "csp_boot_%d" % int(time.time() * 1000))
    shutil.rmtree(sandbox, ignore_errors=True)
    os.makedirs(sandbox, exist_ok=True)
    try:
        cfg_path = os.path.join(sandbox, "cfg.json")
        cfg = schema.default_config()
        cfg["web"].update({"host": "127.0.0.1", "token": "UNIT-TOKEN"})
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False)
        app = create_app(load_config(cfg_path))
        resp = app.test_client().get("/?token=UNIT-TOKEN")

        # ---------- 1. 安全底线：CSP 仍然严格 ----------
        print("\n[1] CSP 仍然严格（不能用放宽安全的方式换可用性）")
        csp = resp.headers.get("Content-Security-Policy", "")
        script_src = csp_script_src(csp)
        check("响应带 CSP", bool(csp), csp[:80])
        check("有 script-src 指令", bool(script_src), csp[:120])
        check("script-src 不允许 'unsafe-inline'（否则 XSS 防护失效）",
              "'unsafe-inline'" not in script_src, script_src)
        check("script-src 不允许 'unsafe-eval'",
              "'unsafe-eval'" not in script_src, script_src)
        check("仍然保留 frame-ancestors 'none'", "frame-ancestors 'none'" in csp, csp[:120])
        check("仍然保留 X-Frame-Options", bool(resp.headers.get("X-Frame-Options")), "")

        # ---------- 2. 模板不能再依赖内联脚本 ----------
        print("\n[2] 模板不再使用会被 CSP 拦截的内联脚本")
        html = resp.get_data(as_text=True)
        # 只找真正的内联 <script>（没有 src 属性）；<script type="application/json"> 不算脚本
        inline_scripts = []
        for match in re.finditer(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
                                 html, re.DOTALL | re.IGNORECASE):
            attrs = match.group("attrs") or ""
            if "src=" in attrs:
                continue
            if "application/json" in attrs.lower():
                continue          # 非执行块，不受 script-src 约束
            inline_scripts.append(match.group(0)[:80])
        check("页面里没有可执行的内联 <script>", not inline_scripts, str(inline_scripts[:2]))
        check("不再用内联脚本赋值 window.__BOOT__", "window.__BOOT__" not in html, "")
        check("不再用内联脚本赋值 window.__URL_TOKEN__",
              "window.__URL_TOKEN__" not in html, "")

        # ---------- 3. 启动数据走 JSON 块 ----------
        print("\n[3] 启动数据通过不执行的 JSON 块下发")
        boot_match = re.search(r'<script[^>]*id="boot-data"[^>]*>(.*?)</script>',
                               html, re.DOTALL)
        check("存在 #boot-data 块", bool(boot_match), "")
        if boot_match:
            raw = boot_match.group(1)
            check("该块的 type 是 application/json",
                  'type="application/json"' in boot_match.group(0), "")
            try:
                data = json.loads(raw)
                check("内容是合法 JSON", isinstance(data, dict), str(raw)[:80])
                check("含 ui 分组（主题/动画等设置要靠它）",
                      isinstance(data.get("ui"), dict), str(list(data))[:120])
                check("含 limits 分组（分页/大小限制要靠它）",
                      isinstance(data.get("limits"), dict), str(list(data))[:120])
            except ValueError as exc:
                check("内容是合法 JSON", False, repr(exc))
        else:
            check("内容是合法 JSON", False, "没有 #boot-data")

        # ---------- 4. 前端确实从 JSON 块读 ----------
        print("\n[4] 前端从 #boot-data 读取启动数据，并不依赖内联全局")
        util = read(os.path.join("web", "static", "js", "util.js"))
        check("util.js 会找 #boot-data", "boot-data" in util, "")
        check("util.js 用 JSON.parse 解析", "JSON.parse" in util, "")
        check("util.js 仍能从 URL 取令牌（媒体 <img> 依赖这条路径）",
              "token" in util and "location" in util, "")
        check("保留 window.__BOOT__ 兜底（不破坏离线单测与旧页面）",
              "__BOOT__" in util, "")

        # ---------- 5. 静态资源仍可正常引用 ----------
        print("\n[5] 页面引用的样式/脚本是可访问的自我资源")
        css_links = re.findall(r'href="([^"]*\.css)"', html)
        js_srcs = re.findall(r'<script[^>]*src="([^"]+)"', html)
        check("引用了 CSS", bool(css_links), str(css_links[:2]))
        check("引用了 JS", bool(js_srcs), str(js_srcs[:2]))
        check("全部是站内相对路径（CSP 只允许 'self'）",
              all(not link.startswith("http") for link in list(css_links) + list(js_srcs)),
              str([l for l in list(css_links) + list(js_srcs) if l.startswith("http")][:2]))
        css_resp = app.test_client().get(css_links[0]) if css_links else None
        check("样式表能正常取到", bool(css_resp and css_resp.status_code == 200),
              str(getattr(css_resp, "status_code", "n/a")))
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
