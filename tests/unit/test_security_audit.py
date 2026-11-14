# -*- coding: utf-8 -*-
"""单元测试：配置安全体检 + 审计日志

两部分：

  A. **配置安全体检**（`core/security_audit.py`）：单项配置看着都正常，
     危险往往出在组合上。这里钉住"该报的必须报、不该报的不能误报"。

  B. **审计日志**（`core/audit_log.py`）：关闭程序 / 安装更新 / 改配置 / 清数据
     这些操作要留痕；且**绝不把令牌密钥写进日志**。

不联网、不碰真实 data/（审计日志写到临时目录再还原）。

运行：`python tests/unit/test_security_audit.py`
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
from core import security_audit       # noqa: E402
from core.security_rules import token_weakness   # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


class Cfg:
    """把 dict 包成 config 对象（体检只需要 str_of/bool_of/int_of）。"""

    def __init__(self, data):
        self._d = data

    def _get(self, section, key):
        return (self._d.get(section) or {}).get(key)

    def str_of(self, section, key, default=""):
        value = self._get(section, key)
        return default if value is None else str(value)

    def bool_of(self, section, key, default=False):
        value = self._get(section, key)
        return default if value is None else bool(value)

    def int_of(self, section, key, default=0):
        value = self._get(section, key)
        return int(value) if isinstance(value, (int, float)) else default


def audit_with(**changes):
    """按默认配置 + 覆盖项做一次体检，返回 (标题列表, 原始问题列表)。"""
    data = schema.default_config()
    for dotted, value in changes.items():
        section, key = dotted.split("__", 1)
        data.setdefault(section, {})[key] = value
    issues = security_audit.check_config(Cfg(data))
    return [item["title"] for item in issues], issues


def main():
    print("=" * 70)
    print("  配置安全体检 + 审计日志")
    print("=" * 70)

    root = os.path.join(PROJECT, "data", "_selftest",
                        "unit_sec_%d" % int(time.time() * 1000))
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    original_log_dir = paths.LOG_DIR

    try:
        # ================= A. 配置安全体检 =================
        print("\n[A1] 默认配置（本机 + 无令牌）应报出「无令牌」提示")
        # 这条规则是补上的：本机 + 空令牌曾被当成安全，但实测跨站请求能借本机
        # 浏览器关掉程序、驱动更新端点。现在会以 warning 级报出。
        titles, issues = audit_with()
        levels = {item["title"]: item["level"] for item in issues}
        check("默认配置报出「仅本机监听但没有设置访问令牌」",
              any("没有设置访问令牌" in t for t in titles), str(titles))
        check("该项不低于 warning",
              levels.get("仅本机监听但没有设置访问令牌") in ("warning", "critical"),
              str(levels))

        print("\n[A1b] 设置了强令牌后不再报告该问题")
        titles, issues = audit_with(
            web__token="Xk9mQ2pL7vR4nT8wZ1bYc3dE")
        check("有令牌时不再报无令牌",
              not any("没有设置访问令牌" in t for t in titles), str(titles))
        check("本机 + 强令牌没有 critical/warning",
              not [i for i in issues if i["level"] in ("critical", "warning")],
              str([i["title"] for i in issues]))

        print("\n[A2] 弱令牌 + 对局域网开放 → 严重")
        titles, issues = audit_with(web__host="0.0.0.0", web__token="12345678")
        levels = {item["title"]: item["level"] for item in issues}
        check("报出令牌强度不足",
              any("令牌强度" in t for t in titles), str(titles))
        check("该项定为 critical",
              levels.get("访问令牌强度不足") == "critical", str(levels))

        print("\n[A3] 强令牌 + 局域网 + 明文 HTTP → 提示明文传输")
        strong = "Xk9mQ2pL7vR4nT8wZ1bYc3dE"
        titles, issues = audit_with(web__host="0.0.0.0", web__token=strong)
        check("报出明文传输风险",
              any("明文传输" in t for t in titles), str(titles))
        check("不再是令牌强度问题",
              not any("令牌强度" in t for t in titles), str(titles))

        print("\n[A4] 有 HTTPS 对外地址时不报明文传输")
        titles, _ = audit_with(web__host="0.0.0.0", web__token=strong,
                               web__public_base_url="https://bot.example.com")
        check("HTTPS 场景不报明文传输",
              not any("明文传输" in t for t in titles), str(titles))

        print("\n[A5] 反向代理相关的两个方向都要提示")
        titles, _ = audit_with(web__host="0.0.0.0", web__token=strong,
                               web__public_base_url="https://bot.example.com",
                               web__trust_proxy=False)
        check("反代但没开 trust_proxy 会提示",
              any("没开 trust_proxy" in t for t in titles), str(titles))
        titles, _ = audit_with(web__trust_proxy=True)
        check("开了 trust_proxy 但没对外地址会提示",
              any("trust_proxy" in t and "没设置对外地址" in t for t in titles),
              str(titles))

        print("\n[A6] AI 明文地址 / 插件沙箱关闭 / 自动安装 各自会提示")
        titles, _ = audit_with(ai__enabled=True,
                               ai__base_url="http://api.example.com/v1")
        check("AI 走 HTTP 会提示", any("AI 接口地址是明文" in t for t in titles),
              str(titles))
        titles, _ = audit_with(plugins__sandbox="off")
        check("沙箱关闭会提示", any("插件沙箱已关闭" in t for t in titles), str(titles))
        titles, _ = audit_with(update__auto_install=True)
        check("自动安装会提示", any("自动安装" in t for t in titles), str(titles))

        print("\n[A7] 体检不会因为配置缺失而抛异常")
        try:
            broken = security_audit.check_config(Cfg({}))
            ok = isinstance(broken, list)
        except Exception as exc:
            ok = False
            print("    抛异常:", exc)
        check("空配置返回空列表而不是崩", ok, "")

        print("\n[A8] 报告文本包含图标/说明/建议")
        issues = security_audit.check_config(
            Cfg({**schema.default_config(),
                 "web": {**schema.default_config()["web"],
                         "host": "0.0.0.0", "token": "12345678"}}))
        lines = security_audit.format_report(issues)
        text = "\n".join(lines)
        check("含严重图标", "🔴" in text, text[:80])
        check("含「说明」", "说明：" in text, "")
        check("含「建议」", "建议：" in text, "")

        # 排版：标题行不能有前导空格（会与日志前缀 `01:56:55 [INFO] ` 错位），
        # 续行（说明/建议）统一缩进，且缩进量一致。
        print("\n[A9] 日志排版：标题顶格、续行统一缩进")
        title_lines = [ln for ln in lines if ln and not ln.startswith(" ")]
        cont_lines = [ln for ln in lines if ln.startswith(" ")]
        check("标题行都顶格（无前导空格）",
              all(not ln.startswith(" ") for ln in title_lines), str(title_lines[:2]))
        check("续行都以空格缩进", len(cont_lines) > 0, str(len(cont_lines)))
        indents = {len(ln) - len(ln.lstrip(" ")) for ln in cont_lines}
        check("续行缩进量一致", len(indents) == 1, str(indents))
        check("缩进是 4 个空格", indents == {4}, str(indents))
        check("续行不含多余的图标前缀",
              all("🔴" not in ln and "🟠" not in ln and "🔵" not in ln
                  for ln in cont_lines), "")

        # ================= B. 审计日志 =================
        print("\n[B1] 审计日志写入格式")
        paths.LOG_DIR = os.path.join(root, "logs")     # 重定向到临时目录
        os.makedirs(paths.LOG_DIR, exist_ok=True)
        line = audit_log.record("config.update", target="bot1",
                                detail={"changed": ["ai.model"]}, client="1.2.3.4")
        parsed = json.loads(line)
        check("是合法 JSON", isinstance(parsed, dict), "")
        check("含时间字段", "time" in parsed and "ts" in parsed, str(parsed.keys()))
        check("含 action/target/client/result",
              parsed.get("action") == "config.update" and parsed.get("target") == "bot1"
              and parsed.get("client") == "1.2.3.4", str(parsed))
        check("落盘到了 audit.log",
              os.path.isfile(os.path.join(paths.LOG_DIR, "audit.log")), "")

        print("\n[B2] 敏感字段必须打码")
        audit_log.record("config.update", target="global",
                         detail={"web": {"token": "SUPER-SECRET-TOKEN"},
                                 "ai": {"api_key": "sk-should-not-appear"},
                                 "password": "hunter2"})
        content = open(os.path.join(paths.LOG_DIR, "audit.log"), encoding="utf-8").read()
        check("令牌原文没出现", "SUPER-SECRET-TOKEN" not in content, "")
        check("API Key 原文没出现", "sk-should-not-appear" not in content, "")
        check("password 原文没出现", "hunter2" not in content, "")
        check("出现掩码 ***", "***" in content, "")

        print("\n[B3] 超长内容会被截断")
        audit_log.record("test.long", detail={"text": "x" * 5000})
        tail = audit_log.read_tail(1)
        saved = str(tail[-1].get("detail", ""))
        check("长文本被截断（< 300 字符）", len(saved) < 300, str(len(saved)))

        print("\n[B4] read_tail 能按条数与顺序读取")
        audit_log.record("test.a")
        audit_log.record("test.b")
        entries = audit_log.read_tail(2)
        check("取到 2 条", len(entries) == 2, str(len(entries)))
        check("顺序是最近的在后",
              entries[-1].get("action") == "test.b", str(entries[-1].get("action")))

        print("\n[B5] 每种敏感操作都有对应动作名")
        for action in ("config.update", "program.shutdown", "update.install",
                       "maintenance.cleanup_media", "maintenance.trim_db"):
            audit_log.record(action)
        actions = {item.get("action") for item in audit_log.read_tail(50)}
        for action in ("config.update", "program.shutdown", "update.install",
                       "maintenance.cleanup_media", "maintenance.trim_db"):
            check(f"记录了 {action}", action in actions, str(sorted(actions)))

        print("\n[B6] 写日志失败不影响业务")
        paths.LOG_DIR = os.path.join(root, "no_such_dir", "\0bad")
        try:
            audit_log.record("test.fail")
            survived = True
        except Exception as exc:
            survived = False
            print("    抛异常:", exc)
        check("写入失败也不抛异常", survived, "")

        print("\n[B7] 令牌强度判定（启动与体检共用）")
        for weak in ("abc", "12345678", "1" * 20, "password12345"):
            check(f"弱令牌 {weak[:10]} 被判定为弱", bool(token_weakness(weak)), "")
        for strong in ("Xk9mQ2pL7vR4nT8wZ1bYc3dE",
                       "7f3c9d21-8b4e-4a6f-9c1d-2e5b8a7f3c91"):
            check(f"强令牌 {strong[:10]} 不误判", token_weakness(strong) == "",
                  token_weakness(strong))
    finally:
        paths.LOG_DIR = original_log_dir
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
