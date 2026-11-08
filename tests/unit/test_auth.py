# -*- coding: utf-8 -*-
"""单元测试：后台访问令牌的校验强度（常数时间比较 + 暴力破解防护）

两层防护，分别钉住：

  1. **常数时间比较**：不能用 `==` 比令牌。字符串 `==` 会先比长度、再逐字节比，
     长度不同的输入耗时不同；攻击者能通过响应时间推断令牌长度甚至内容。
     现在的做法是两边各取 SHA-256 再 `hmac.compare_digest`（长度固定，抹平差异）。

  2. **失败次数限制**：同一来源连续输错 10 次后进入 5 分钟封锁（返回 429），
     避免"在局域网里慢慢试"。成功一次即清零，正常用户打错一两次不受影响。

不联网、不碰真实 data/。

运行：`python tests/unit/test_auth.py`
"""

import hashlib
import hmac
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

from core import config_schema as schema        # noqa: E402
from core.config_manager import load_config     # noqa: E402
from web.server import create_app               # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def make_client(root, token):
    cfg = schema.default_config()
    cfg["web"].update({"host": "127.0.0.1", "token": token})
    path = os.path.join(root, "cfg_%d.json" % abs(hash(token)))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, ensure_ascii=False)
    return create_app(load_config(path)).test_client()


def main():
    print("=" * 70)
    print("  后台访问令牌校验强度")
    print("=" * 70)

    root = os.path.join(PROJECT, "data", "_selftest",
                        "unit_auth_%d" % int(time.time() * 1000))
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)

    try:
        # ---------- 1. 静态检查：源码里不许用 == 比令牌 ----------
        print("\n[1] 源码不许用 `==` 直接比较令牌")
        source = open(os.path.join(PROJECT, "web", "server.py"),
                      encoding="utf-8").read()
        # 取出 check_token 的函数体
        match = re.search(r"def check_token\(\)(.*?)\n    def ", source, re.S)
        body = match.group(1) if match else ""
        check("找到了 check_token 函数体", bool(body), "正则没匹配到")
        check("check_token 里不再出现 `provided == expected`",
              "provided == expected" not in body, "")
        # 真正的比较在 _token_equal 里（check_token 通过它比对）
        check("check_token 调用了 _token_equal", "_token_equal(" in body, "")
        helper = re.search(r"def _token_equal\(.*?\n    def ", source, re.S)
        helper_body = helper.group(0) if helper else ""
        check("_token_equal 使用了 hmac.compare_digest",
              "compare_digest" in helper_body, "")
        check("_token_equal 先做 SHA-256（抹平长度差异）",
              "sha256" in helper_body, "")
        check("_token_equal 没有用 == 比原文",
              not re.search(r"provided\s*==\s*expected", helper_body), "")

        # ---------- 2. 常数时间比较的性质 ----------
        print("\n[2] 常数时间比较：正确才有 True，且不受输入长度影响")
        VERY_LONG = "x" * 1024

        def token_equal(provided, expected):
            left = hashlib.sha256(str(provided or "").encode("utf-8")).digest()
            right = hashlib.sha256(str(expected or "").encode("utf-8")).digest()
            return hmac.compare_digest(left, right)

        EXPECTED = "SECRET-TOKEN-ABCDEFGHIJ"
        check("正确令牌返回 True", token_equal(EXPECTED, EXPECTED) is True, "")
        check("错误令牌返回 False", token_equal("wrong", EXPECTED) is False, "")
        check("空令牌返回 False", token_equal("", EXPECTED) is False, "")
        for probe in ("a", VERY_LONG, EXPECTED[:-1], EXPECTED + "x"):
            check(f"错误输入 {len(probe)} 字符 → False",
                  token_equal(probe, EXPECTED) is False, "")
        try:
            ok = token_equal("中文令牌", "中文令牌") and not token_equal("中文令牌", "中文口令")
        except TypeError as exc:
            ok = False
            print("    抛了 TypeError：", exc)
        check("非 ASCII 令牌也能正确比较", ok, "")

        print("\n[2b] 耗时特征：长度差异不会造成明显差别（== 会）")
        def timed(func, rounds=20000):
            start = time.perf_counter()
            for _ in range(rounds):
                func()
            return time.perf_counter() - start

        short = timed(lambda: token_equal("x", EXPECTED))
        long = timed(lambda: token_equal(VERY_LONG, EXPECTED))
        ratio = max(short, long) / max(min(short, long), 1e-9)
        check("常数时间实现：长度差异耗时比值 < 3", ratio < 3.0, "比值 %.2f" % ratio)

        # ---------- 3. 接口行为：正确/错误/封锁 ----------
        print("\n[3] 接口行为：正确通过、错误 401、连续错误后封锁")
        client = make_client(root, "RIGHT-TOKEN-FOR-TEST")
        blocked = client.get("/api/admin/status?token=RIGHT-TOKEN-FOR-TEST")
        check("正确令牌不被鉴权拦下（返回码不是 401）",
              blocked.status_code != 401, str(blocked.status_code))
        wrong = client.get("/api/admin/status?token=WRONG")
        check("错误令牌返回 401", wrong.status_code == 401, str(wrong.status_code))
        # 请求头方式也要能用
        header_ok = client.get("/api/admin/status",
                               headers={"X-Auth-Token": "RIGHT-TOKEN-FOR-TEST"})
        check("请求头 X-Auth-Token 也能通过",
              header_ok.status_code != 401, str(header_ok.status_code))

        print("\n[4] 连续输错达到阈值后返回 429")
        codes = []
        for index in range(14):
            codes.append(client.get("/api/admin/status?token=BAD-%d" % index).status_code)
        check("前 9 次是 401", codes[:9] == [401] * 9, str(codes[:9]))
        check("第 10 次起变成 429（已封锁）", 429 in codes[9:], str(codes))
        check("封锁后即使令牌正确也暂时拒绝",
              client.get("/api/admin/status?token=RIGHT-TOKEN-FOR-TEST").status_code == 429,
              "")

        print("\n[5] 全新实例：错误次数从零开始（换机器/重启不受影响）")
        fresh = make_client(root, "ANOTHER-TOKEN")
        check("新实例第一次错误仍是 401",
              fresh.get("/api/admin/status?token=nope").status_code == 401, "")

        # ---------- 6. 未配置令牌时不拦截 ----------
        print("\n[6] 未配置令牌时不做校验（与原行为一致）")
        cfg = schema.default_config()
        cfg["web"].update({"host": "127.0.0.1", "token": ""})
        path = os.path.join(root, "cfg_notoken.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False)
        open_client = create_app(load_config(path)).test_client()
        check("空令牌配置下不返回 401",
              open_client.get("/api/admin/status").status_code != 401, "")

        # ---------- 7. 弱令牌检查（启动时告警，不拦截） ----------
        print("\n[7] 启动时的弱令牌检查：只提示、不拦截")
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "run_module", os.path.join(PROJECT, "run.py"))
        run_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_module)
        weakness = run_module._token_weakness

        for weak, expect_hint in [
                ("abc", "太短"),
                ("12345678", "太短"),
                ("1" * 20, "同一个字符"),
                ("aaaaaaaaaaaa", "同一个字符")]:
            reason = weakness(weak)
            check(f"弱令牌 {weak[:8]}… 会被标出", bool(reason), f"返回 {reason!r}")

        # 强令牌不应误报
        for strong in ("Xk9#mQ2pL7vR4nT8wZ1b",
                       "aB3dE6gH9jK2mN5pQ8sT1v",
                       "7f3c9d21-8b4e-4a6f-9c1d-2e5b8a7f3c91"):
            check(f"强令牌 {strong[:10]}… 不误报",
                  weakness(strong) == "", weakness(strong))
        check("空令牌不报（表示未启用鉴权）", weakness("") == "", "")
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
