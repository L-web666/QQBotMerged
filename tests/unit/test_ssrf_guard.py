# -*- coding: utf-8 -*-
"""单元测试：媒体下载的 SSRF 防护（DNS 失败必须拒绝 / 内网地址必须拒绝）

背景（审计 v5 SEC-03）：`resolve_public()` 早期在 DNS 解析失败时返回 `True`（放行），
属于 fail-open —— 解析一旦被干扰/被投毒，内网地址校验就形同虚设。
现在改为 **fail-closed**：解析失败、解析结果为空、任一条落到内网地址，一律拒绝。

不连网：DNS 查询全部用猴子补丁替换（`socket.getaddrinfo`），不产生真实请求。

运行：`python tests/unit/test_ssrf_guard.py`
"""

import ipaddress
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import media_store        # noqa: E402

PASSED = []
FAILED = []

_REAL_GETADDRINFO = media_store.socket.getaddrinfo


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def fake_dns(mapping):
    """替换 getaddrinfo：域名 → 地址列表；不在 mapping 里的抛 gaierror（模拟解析失败）。"""
    def _resolver(host, port, *args, **kwargs):
        host = str(host)
        if host not in mapping:
            raise media_store.socket.gaierror("模拟解析失败: %s" % host)
        return [(media_store.socket.AF_INET, media_store.socket.SOCK_STREAM, 6, "",
                 (addr, 0)) for addr in mapping[host]]
    return _resolver


def main():
    print("=" * 70)
    print("  媒体下载 SSRF 防护（DNS fail-closed）")
    print("=" * 70)

    original = media_store.socket.getaddrinfo
    try:
        # ---------- 1. 解析失败必须拒绝 ----------
        print("\n[1] DNS 解析失败 → 拒绝（fail-closed，不是放行）")
        media_store.socket.getaddrinfo = fake_dns({})
        check("完全无法解析的域名被拒",
              media_store.resolve_public("https://no-such-host.example/x") is False, "")

        # ---------- 2. 解析到内网必须拒绝 ----------
        print("\n[2] 解析到内网/回环 → 拒绝")
        media_store.socket.getaddrinfo = fake_dns({
            "to-internal.example": ["192.168.1.10"],
            "to-loopback.example": ["127.0.0.1"],
            "to-linklocal.example": ["169.254.169.254"],
            "to-ipv6-local.example": ["::1"],
        })
        for host, label in [("to-internal.example", "内网 192.168"),
                            ("to-loopback.example", "回环 127.0.0.1"),
                            ("to-linklocal.example", "链路本地 169.254"),
                            ("to-ipv6-local.example", "IPv6 回环")]:
            check(f"拒绝 {label}",
                  media_store.resolve_public("https://%s/x" % host) is False, host)

        # ---------- 3. 多地址中只要有一个是内网就拒绝 ----------
        print("\n[3] 多条解析结果里只要有一条是内网 → 拒绝")
        media_store.socket.getaddrinfo = fake_dns({
            "mixed.example": ["8.8.8.8", "10.0.0.5"],
        })
        check("混合结果（公网+内网）被拒",
              media_store.resolve_public("https://mixed.example/x") is False, "")

        # ---------- 4. 正常公网域名放行 ----------
        print("\n[4] 正常公网域名 → 放行")
        media_store.socket.getaddrinfo = fake_dns({
            "public.example": ["8.8.8.8", "1.1.1.1"],
        })
        check("纯公网解析结果放行",
              media_store.resolve_public("https://public.example/x") is True, "")

        # ---------- 5. IP 字面量直接判断，不走 DNS ----------
        print("\n[5] IP 字面量：内网拒绝、公网放行（不触发 DNS）")
        def _boom(host, port, *args, **kwargs):
            raise AssertionError("IP 字面量不应触发 DNS 查询")
        media_store.socket.getaddrinfo = _boom
        check("字面量 127.0.0.1 被拒",
              media_store.resolve_public("http://127.0.0.1/x") is False, "")
        check("字面量 192.168.1.1 被拒",
              media_store.resolve_public("http://192.168.1.1/x") is False, "")
        check("字面量 8.8.8.8 放行",
              media_store.resolve_public("https://8.8.8.8/x") is True, "")
        check("字面量 [::1] 被拒",
              media_store.resolve_public("http://[::1]/x") is False, "")

        # ---------- 6. 空结果也拒绝 ----------
        print("\n[6] 解析结果为空 → 拒绝")
        media_store.socket.getaddrinfo = lambda *a, **k: []
        check("空解析结果被拒",
              media_store.resolve_public("https://empty.example/x") is False, "")

        # ---------- 7. 非法 URL 不抛异常 ----------
        print("\n[7] 非法输入不抛异常、一律拒绝")
        media_store.socket.getaddrinfo = fake_dns({})
        for bad in ("", "not-a-url", "http://", "://nohost"):
            check(f"拒绝非法输入 {bad or '(空)'}",
                  media_store.resolve_public(bad) is False, bad)
    finally:
        media_store.socket.getaddrinfo = original

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
