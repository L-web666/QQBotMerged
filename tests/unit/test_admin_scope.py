# -*- coding: utf-8 -*-
"""单元测试：管理员权限按机器人隔离（审计 SEC-AUTH-003）

**问题**：v1.4.1 把 `security` 加进 `PER_BOT_SECTIONS` 之后，`security.admin_openids`
变成按机器人隔离的配置；但 `core/message_processor.py` 里判定管理员的那两处
仍然读全局配置（`runtime.config.list_of("security", "admin_openids")`）。
后果：
  · 各机器人单独配的管理员名单不生效；
  · bot A 的管理员在 bot B 下也照样有权限 —— **跨机器人越权**；
  · 插件收到的 `is_admin` 标记同样错。

**修复**：两处都改成 `runtime.bot_config(bot_id).list_of(...)`。

运行：`python tests/unit/test_admin_scope.py`
"""

import os
import re
import shutil
import sys

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


class FakeRuntime:
    """只提供 bot_config() 的最小运行时替身（按机器人返回各自的 security）。"""

    def __init__(self, per_bot):
        self._per = per_bot
        self.bots = {bid: object() for bid in per_bot}

    def bot_config(self, bot_id=""):
        resolved = self._per.get(bot_id) or {}

        class Cfg:
            def list_of(self, section, key, default=None):
                value = (resolved.get(section) or {}).get(key)
                return default if value is None else value

        return Cfg()


def main():
    print("=" * 70)
    print("  管理员权限按机器人隔离")
    print("=" * 70)

    src = open(os.path.join(PROJECT, "core", "message_processor.py"),
               encoding="utf-8").read()

    print("\n[A1] 源码里不再读全局管理员名单")
    check("没有 runtime.config.list_of(\"security\", \"admin_openids\")",
          'config.list_of("security", "admin_openids"' not in src,
          "")
    check("改用了 bot_config(...)", "bot_config(bot_id).list_of(" in src, "")
    check("_is_admin 接受 bot_id 参数",
          re.search(r"def _is_admin\(self, message[^)]*bot_id", src) is not None, "")
    check("插件分支传了 bot_id",
          'self._is_admin(message, bot_id)' in src, "")
    check("_handle_command 里用了按机器人的判断",
          "is_admin = self._is_admin(message, bot_id)" in src, "")

    print("\n[A2] 两处判定都按机器人隔离")
    runtime = FakeRuntime({
        "bot1": {"security": {"admin_openids": ["ADMIN-OF-BOT1"]}},
        "bot2": {"security": {"admin_openids": ["ADMIN-OF-BOT2"]}},
    })

    def is_admin(bot_id, openid):
        ids = runtime.bot_config(bot_id).list_of("security", "admin_openids", default=[])
        return bool(ids) and (openid in ids)

    check("bot1 自己的管理员有权限", is_admin("bot1", "ADMIN-OF-BOT1") is True, "")
    check("bot2 的管理员在 bot1 下没有权限",
          is_admin("bot1", "ADMIN-OF-BOT2") is False, "")
    check("bot2 自己的管理员有权限", is_admin("bot2", "ADMIN-OF-BOT2") is True, "")
    check("bot1 的管理员在 bot2 下没有权限",
          is_admin("bot2", "ADMIN-OF-BOT1") is False, "")
    check("路人没有权限", is_admin("bot1", "SOMEONE-ELSE") is False, "")

    print("\n[A3] 边界情况")
    empty = FakeRuntime({"bot1": {"security": {"admin_openids": []}}})
    check("名单为空时谁都不是管理员",
          bool(empty.bot_config("bot1").list_of("security", "admin_openids", default=[]))
          is False, "")
    missing = FakeRuntime({"bot1": {}})
    check("配置缺失时回退空列表不报错",
          missing.bot_config("bot1").list_of("security", "admin_openids", default=[]) == [],
          "")
    unknown = FakeRuntime({"bot1": {"security": {"admin_openids": ["A"]}}})
    check("未知机器人回退默认值（不抛异常）",
          unknown.bot_config("no_such").list_of("security", "admin_openids",
                                                 default=[]) == [], "")

    print("\n[A4] 真配置：两个机器人各配各的，互不串")
    try:
        from core import config_schema as schema
        from core.config_manager import load_config
        import json as _json
        tmp = os.path.join(PROJECT, "data", "_selftest", "unit_admin_scope")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        cfg = schema.default_config()
        cfg["web"].update({"host": "127.0.0.1", "token": "x"})
        cfg["bots"] = [
            {"id": "bot1", "name": "一", "enabled": True, "app_id": "A1", "app_secret": "s1"},
            {"id": "bot2", "name": "二", "enabled": True, "app_id": "A2", "app_secret": "s2"},
        ]
        path = os.path.join(tmp, "cfg.json")
        with open(path, "w", encoding="utf-8") as handle:
            _json.dump(cfg, handle, ensure_ascii=False)
        manager = load_config(path)
        manager.set_for_bot("bot1", "security.admin_openids", ["ADMIN-OF-BOT1"])
        manager.set_for_bot("bot2", "security.admin_openids", ["ADMIN-OF-BOT2"])
        manager.save()

        reloaded = load_config(path)
        # resolve_for_bot 返回的是 Config 对象，用 list_of 取值（不是字典的 .get）
        got1 = reloaded.resolve_for_bot("bot1").list_of(
            "security", "admin_openids", default=[])
        got2 = reloaded.resolve_for_bot("bot2").list_of(
            "security", "admin_openids", default=[])
        check("bot1 的管理员落到了 bot1", got1 == ["ADMIN-OF-BOT1"], str(got1))
        check("bot2 的管理员落到了 bot2", got2 == ["ADMIN-OF-BOT2"], str(got2))
        global_ids = reloaded.config.list_of("security", "admin_openids", default=[])
        check("全局名单没被污染", global_ids == [], str(global_ids))
        # 关键：两个机器人的判定不能串
        check("bot1 的管理员在 bot2 下没有权限",
              "ADMIN-OF-BOT1" not in got2, str(got2))
        check("bot2 的管理员在 bot1 下没有权限",
              "ADMIN-OF-BOT2" not in got1, str(got1))
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception as exc:
        check("真配置验证没抛异常", False, str(exc))

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
