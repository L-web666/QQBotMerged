# -*- coding: utf-8 -*-
"""单元测试：安全审计修复回归（对应《QQBotMerged 安全审计报告》）

每一节对应报告里的一个编号，断言"修好的行为"，防止后续改动把它改回去。
分层约定见 `PROGRESS.md`：`tests/unit/` 快、只依赖被测模块；不连网、不碰真实 `data/`
（临时文件只写 `data/_selftest/`）。**本文件不读取 config.json。**

运行：`python tests/unit/test_security_fixes.py`
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
    print("  安全审计修复回归")
    print("=" * 70)

    # ============================================================
    # SEC-03 / NEW-01：密钥脱敏必须覆盖 bots[].overrides
    # ============================================================
    print("\n[SEC-03/NEW-01] 密钥脱敏递归覆盖 bots[].overrides")
    from core.config_manager import ConfigManager
    from core.config_schema import SECRET_MASK

    cm = ConfigManager.__new__(ConfigManager)   # 只调纯函数，不读任何配置
    sample = {
        "ai": {"api_key": "GLOBAL-PLAIN-KEY"},
        "web": {"token": "WEB-PLAIN-TOKEN"},
        "cloud_sync": {"api_token": "CF-PLAIN-TOKEN"},
        "bots": [{
            "id": "bot1", "app_secret": "APP-PLAIN-SECRET",
            "overrides": {
                "ai": {"api_key": "OVERRIDE-PLAIN-KEY", "model": "deepseek-chat"},
                "send": {"max_file_mb": 8},
                "nested": {"deeper": {"secret": "DEEP-PLAIN-SECRET"}},
            },
        }],
    }
    masked = cm.mask_secrets(sample)
    blob = json.dumps(masked, ensure_ascii=False)
    for leak in ("GLOBAL-PLAIN-KEY", "WEB-PLAIN-TOKEN", "CF-PLAIN-TOKEN",
                 "APP-PLAIN-SECRET", "OVERRIDE-PLAIN-KEY", "DEEP-PLAIN-SECRET"):
        check(f"明文已打码：{leak}", leak not in blob, blob[:200])
    check("打码用的是统一掩码", masked["bots"][0]["overrides"]["ai"]["api_key"] == SECRET_MASK,
          masked["bots"][0]["overrides"]["ai"])
    check("非密钥字段不受影响（模型名保留）",
          masked["bots"][0]["overrides"]["ai"]["model"] == "deepseek-chat", "")
    check("非密钥字段不受影响（数字配置保留）",
          masked["bots"][0]["overrides"]["send"]["max_file_mb"] == 8, "")
    check("空值不会被打成掩码（表示「未配置」）",
          cm.mask_secrets({"bots": [{"overrides": {"ai": {"api_key": ""}}}]})
          ["bots"][0]["overrides"]["ai"]["api_key"] == "", "")

    # ============================================================
    # SEC-06 / NEW-07：云同步敏感字段
    # ============================================================
    print("\n[SEC-06/NEW-07] 云同步敏感字段扫描（裸 token/secret、短值）")
    from core.cloud_sync import find_sensitive_keys as find_keys

    for name, payload in [("裸 token", {"token": "example-token-value"}),
                          ("裸 secret", {"secret": "example-secret-value"}),
                          ("短值", {"token": "abc12345"}),
                          ("嵌套裸 secret", {"a": {"secret": "zzzzzzzzz"}})]:
        check(f"能检出 {name}", find_keys("x.json", json.dumps(payload).encode()) != [],
              str(find_keys("x.json", json.dumps(payload).encode())))
    for name, payload in [("max_tokens", {"ai": {"max_tokens": 1024}}),
                          ("token_refresh_buffer_seconds",
                           {"send": {"token_refresh_buffer_seconds": 60}}),
                          ("skip_secret_files", {"cloud_sync": {"skip_secret_files": True}})]:
        check(f"不误伤 {name}", find_keys("x.json", json.dumps(payload).encode()) == [],
              str(find_keys("x.json", json.dumps(payload).encode())))
    check("纯文本行能检出 token",
          find_keys("x.txt", b"token = example-token-value") == ["token"],
          str(find_keys("x.txt", b"token = example-token-value")))

    # ============================================================
    # SEC-07 / SEC-16 / NEW-04：插件扫描
    # ============================================================
    print("\n[SEC-07/SEC-16/NEW-04] 插件扫描：失败不报 ok、递归子目录")
    from core import plugin_safety

    broken = plugin_safety.scan_source("def broken(:\n  pass", "main.py")
    check("解析失败会带 parse_error 标记", any(i.get("parse_error") for i in broken),
          str(broken)[:120])
    check("level 枚举里有 unknown（不再只有 ok）",
          "unknown" in plugin_safety.LEVEL_ORDER, str(plugin_safety.LEVEL_ORDER))
    check("unknown 排在 ok 之前（更严重）",
          plugin_safety.LEVEL_ORDER.get("unknown", 99) < plugin_safety.LEVEL_ORDER["ok"], "")

    sandbox = os.path.join(PROJECT, "data", "_selftest",
                           "sec_scan_%d" % int(time.time() * 1000))
    shutil.rmtree(sandbox, ignore_errors=True)
    os.makedirs(os.path.join(sandbox, "helpers"), exist_ok=True)
    try:
        with open(os.path.join(sandbox, "main.py"), "w", encoding="utf-8") as f:
            f.write("print('ok')\n")
        with open(os.path.join(sandbox, "helpers", "evil.py"), "w", encoding="utf-8") as f:
            f.write("import subprocess\nsubprocess.run(['cmd'])\n")
        result = plugin_safety.scan_plugin_dir(sandbox)
        check("子目录里的 .py 也被扫描（NEW-04）",
              any("helpers/evil.py" in name for name in result["files"]), str(result["files"]))
        check("子目录里的危险调用被识别", result["level"] == "high", result["level"])
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    # ============================================================
    # SEC-09 / NEW-03：SSRF（IP/DNS 校验 + 白名单下沉）
    # ============================================================
    print("\n[SEC-09/NEW-03] SSRF：内网/回环/非白名单域名一律拒绝")
    from core import media_store

    for url, label in [("http://127.0.0.1:8080/x", "本机回环"),
                       ("http://192.168.1.1/x", "内网 192.168"),
                       ("http://10.0.0.5/x", "内网 10."),
                       ("http://172.16.0.1/x", "内网 172.16"),
                       ("http://169.254.169.254/latest", "云元数据"),
                       ("http://[::1]/x", "IPv6 回环"),
                       ("file:///etc/passwd", "非 http 协议")]:
        check(f"拒绝 {label}", media_store.host_allowed(url) is False, url)
    check("白名单外域名被拒（fetch 内部就拦）",
          media_store.host_allowed("https://evil.example.com/a.png", allowlist_only=True) is False, "")
    check("腾讯媒体域名放行",
          media_store.host_allowed("https://multimedia.nt.qq.com.cn/download?x=1",
                                   allowlist_only=True) is True, "")
    check("IP 私有判定覆盖 IPv6 ULA", media_store._is_private_ip("fd00::1") is True, "")
    check("公网 IP 不算私有", media_store._is_private_ip("1.1.1.1") is False, "")
    src = open(os.path.join(PROJECT, "core", "media_store.py"), encoding="utf-8").read()
    check("fetch 使用 allow_redirects=False（逐跳校验重定向）",
          "allow_redirects=False" in src, "")
    check("fetch 里调用了 host_allowed(allowlist_only=True)",
          "host_allowed(current, allowlist_only=True)" in src, "")

    # ============================================================
    # SEC-10：启动日志不能输出令牌
    # ============================================================
    print("\n[SEC-10] 启动日志不再输出带令牌的 URL")
    run_src = open(os.path.join(PROJECT, "run.py"), encoding="utf-8").read()
    check("日志里没有 ?token= 拼接", 'f"?token={token}" if token else ""' not in run_src, "")
    check("保留了「已设置令牌」的提示", "已设置访问令牌" in run_src, "")

    # ============================================================
    # SEC-11 / NEW-06：错误信息脱敏
    # ============================================================
    print("\n[SEC-11/NEW-06] AI 错误与日志脱敏")
    from core.ai_client import redact_text

    leaky = ("HTTP 401: {\"error\":\"invalid api_key sk-abcdef123456789\","
             "\"Authorization\":\"Bearer eyJhbGciOiJIUzI1NiJ9.signature\"}")
    cleaned = redact_text(leaky, "sk-abcdef123456789")
    check("明文密钥被抹掉", "sk-abcdef123456789" not in cleaned, cleaned)
    check("Bearer 令牌被抹掉", "eyJhbGciOiJIUzI1NiJ9.signature" not in cleaned, cleaned)
    check("错误码等有用信息保留", "HTTP 401" in cleaned and "invalid" in cleaned, cleaned)
    check("已知密钥值直接替换", "MY-SECRET-VALUE" not in redact_text(
        "boom MY-SECRET-VALUE boom", "MY-SECRET-VALUE"), "")

    # ============================================================
    # SEC-13/14：安全响应头与 413 文案
    # ============================================================
    print("\n[SEC-13/SEC-14] 安全响应头与上传上限文案")
    server_src = open(os.path.join(PROJECT, "web", "server.py"), encoding="utf-8").read()
    check("补了 Content-Security-Policy", "Content-Security-Policy" in server_src, "")
    check("CSP 含 frame-ancestors 'none'", "frame-ancestors 'none'" in server_src, "")
    check("补了 X-Frame-Options", "X-Frame-Options" in server_src, "")
    check("413 文案不再写死 64MB", "服务端上限 64MB" not in server_src, "")
    check("413 文案跟着真实上限走", "limit // 1048576" in server_src, "")
    check("/media 路由有令牌校验",
          "def media_file" in server_src and
          server_src.split("def media_file")[1].split("def ")[0].count("check_token") > 0, "")
    check("SVG 强制下载（as_attachment）", "as_attachment=True" in server_src, "")
    check("上传改为分块读取（不再一次 read）",
          "_read_upload_capped" in server_src and "blob = upload.read()" not in server_src, "")

    # ============================================================
    # SEC-12：日志按尾部读取
    # ============================================================
    print("\n[SEC-12] 日志改从尾部按块读取")
    logger_src = open(os.path.join(PROJECT, "core", "logger.py"), encoding="utf-8").read()
    # 只剔除"以 # 开头"的注释行不够：三引号 docstring 里也写着被修掉的写法。
    # 这里直接检查"有没有真正调用过 readlines"（出现 .readlines() 且前面不是文档说明）。
    logger_code = "\n".join(line for line in logger_src.splitlines()
                            if not line.strip().startswith("#"))
    calls = [line.strip() for line in logger_code.splitlines()
             if ".readlines()" in line and "以前用" not in line and "审计" not in line]
    check("代码里不再 readlines() 整文件读入", not calls, str(calls[:2]))
    check("新增 _tail_lines 倒读实现", "_tail_lines" in logger_code, "")
    # 日志下载也改成流式（send_file），不再 read_file 整文件进内存
    check("日志下载改流式发送", "download_name=name, conditional=True" in server_src, "")
    check("日志下载不再整文件读入", "content = holder.read_file(name)" not in server_src, "")

    # ============================================================
    # SEC-05：插件发送带上 bot_id
    # ============================================================
    print("\n[SEC-05] 插件主动发消息带上当前机器人")
    host_src = open(os.path.join(PROJECT, "core", "astrbot_shim", "host.py"),
                    encoding="utf-8").read()
    send_block = host_src.split("def send_to_umo")[1].split("def emit")[0]
    check("send_to_umo 不再硬编码 bot_id 为空", '"bot_id": ""}' not in send_block,
          send_block[:200])
    check("send_to_umo 使用 current_bot_id", "current_bot_id" in send_block, "")

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
