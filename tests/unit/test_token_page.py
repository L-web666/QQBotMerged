# -*- coding: utf-8 -*-
"""单元测试：令牌相关的两条界面行为

  A. **不带令牌不加载页面**：以前是先渲染整个后台、再由页面脚本发现 401 弹提示，
     等于"先把页面给你看，再告诉你没权限"。现在服务端直接判断，令牌不对只返回
     一页极简提示，**绝不下发后台页面**（含前端路由回落 404 那条路）。

  B. **控制台带令牌、日志不带**：每次启动在控制台打印带令牌的完整地址（方便复制），
     但写进日志文件的只有打码形式 —— 日志会轮转、会被云同步上传、会被截图。

不联网、不碰真实 data/。

运行：`python tests/unit/test_token_page.py`
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import config_schema as schema        # noqa: E402
from core.config_manager import load_config     # noqa: E402
from web.server import create_app               # noqa: E402

PASSED = []
FAILED = []
TOKEN = "ABCDEFGH12345678abcdefgh"


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def make_client(root, token):
    cfg = schema.default_config()
    cfg["web"].update({"host": "127.0.0.1", "token": token, "port": 8666})
    path = os.path.join(root, "cfg_%d.json" % abs(hash(token)))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, ensure_ascii=False)
    return create_app(load_config(path)).test_client()


def main():
    print("=" * 70)
    print("  令牌页面门禁 + 控制台/日志分离")
    print("=" * 70)

    root = os.path.join(PROJECT, "data", "_selftest",
                        "unit_tokenpage_%d" % int(time.time() * 1000))
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)

    try:
        client = make_client(root, TOKEN)

        # ---------- A. 不带令牌不加载页面 ----------
        print("\n[A1] 不带令牌访问首页：只返回提示页")
        response = client.get("/")
        body = response.get_data(as_text=True)
        check("返回 401", response.status_code == 401, str(response.status_code))
        check("是提示页", "需要访问令牌" in body, body[:60])
        check("没有下发后台页面（无 boot-data）", "boot-data" not in body, "")
        check("没有下发后台页面（无导航栏标记）", "app-nav" not in body, "")
        check("体积很小（不是整份前端）", len(body) < 4000, str(len(body)))
        check("提示里说明了去哪里看令牌",
              "控制台" in body or "config.json" in body, "")

        print("\n[A2] 错误令牌同样拒绝")
        response = client.get("/?token=WRONG")
        body = response.get_data(as_text=True)
        check("错误令牌返回 401", response.status_code == 401, str(response.status_code))
        check("错误令牌也只给提示页", "boot-data" not in body, "")

        print("\n[A3] 正确令牌正常加载")
        response = client.get("/?token=" + TOKEN)
        body = response.get_data(as_text=True)
        check("返回 200", response.status_code == 200, str(response.status_code))
        check("是完整的后台页面", "boot-data" in body, "")
        check("带上版本号", "1.3." in body, "")

        print("\n[A4] 前端路由回落（404 那条路）不能绕过")
        response = client.get("/some/route")
        body = response.get_data(as_text=True)
        check("回落也返回 401", response.status_code == 401, str(response.status_code))
        check("回落也只看得到提示页", "boot-data" not in body, "")

        print("\n[A5] 用请求头带令牌也能进")
        response = client.get("/", headers={"X-Auth-Token": TOKEN})
        check("请求头令牌 → 200", response.status_code == 200, str(response.status_code))

        print("\n[A6] 未配置令牌时不加门禁（但启动流程会拒绝无令牌运行）")
        # 说明：`check_token()` 本身在"令牌为空"时仍然放行 —— 这是历史行为，
        # 真正的把关在启动阶段（run.py 的 ensure_access_token + 拒绝启动）。
        # 这条用例只描述**服务层**语义，不再把"无鉴权可用"当成推荐配置：
        # 启动侧由 test_auth.py / 本轮新增用例保证"生成失败即拒绝启动"。
        open_client = make_client(root, "")
        response = open_client.get("/")
        check("服务层：无令牌配置不做门禁", response.status_code == 200,
              str(response.status_code))

        print("\n[A7] 令牌错误次数过多时首页也是提示页")
        hammer = make_client(root, TOKEN)
        for index in range(12):
            hammer.get("/?token=BAD%d" % index)
        response = hammer.get("/")
        check("被锁定后首页不是后台页面",
              "boot-data" not in response.get_data(as_text=True),
              str(response.status_code))

        # ---------- B. 控制台带令牌、日志打码 ----------
        print("\n[B1] 打码函数")
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import run\n"
                "print('M1:', run._mask_token(%r))\n"
                "print('M2:', run._mask_token('short'))\n"
                "print('M3:', repr(run._mask_token('')))\n"
                % (PROJECT, TOKEN))
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        out = proc.stdout or ""
        check("长令牌打码成 头…尾", "ABCD…efgh" in out, out.strip())
        check("短令牌整体打码", "M2: ***" in out, "")
        check("空令牌返回空串", "M3: ''" in out, "")
        check("打码结果不含完整令牌", TOKEN not in out, "")

        print("\n[B2] 源码层面：控制台用 print，日志用打码")
        source = open(os.path.join(PROJECT, "run.py"), encoding="utf-8").read()
        check("有控制台打印带令牌地址",
              re.search(r'print\([^)]*\?token=', source) is not None, "")
        # 日志里必须出现打码函数，不能直接把 token 传给 log
        check("日志里用的是打码形式", "_mask_token(token)" in source, "")
        bad = re.search(r'log\.\w+\([^)]*token=%s', source)
        check("没有把 token 直接拼进日志", bad is None, str(bad) if bad else "")
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
