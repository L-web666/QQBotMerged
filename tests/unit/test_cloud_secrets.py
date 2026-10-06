# -*- coding: utf-8 -*-
"""单元测试：云同步上传前的"敏感字段"扫描（`core/cloud_sync.py`）

分层约定见 `PROGRESS.md`：`tests/unit/` 快、只依赖被测模块；不连网、不碰真实 `data/`
（临时文件只写 `data/_selftest/`，且只测 `_upload` / 纯函数，不跑整轮同步）。

运行：`python tests/unit/test_cloud_secrets.py`
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

from core.cloud_sync import CloudSync, find_sensitive_keys            # noqa: E402
from core.config_manager import load_config                          # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


class FakeBackend:
    """只记录上传调用，不联网。"""

    def __init__(self):
        self.calls = []
        self.fail_next = False

    def put(self, key, value, updated_at, deleted=0):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("假装网络断了")
        self.calls.append({"key": key, "value": value, "updated_at": updated_at})
        return True


def main():
    print("=" * 70)
    print("  单元测试：云同步敏感字段扫描")
    print("=" * 70)

    # ---------- 1. 纯函数：JSON 按字段名判断 ----------
    print("\n[1] JSON 只看字段名，不猜值")
    secret_json = json.dumps({"ai": {"api_key": "sk-abcdefghijklmnop"},
                              "note": "正常内容"}, ensure_ascii=False).encode("utf-8")
    hits = find_sensitive_keys("data/plugin_data/x/config.json", secret_json)
    check("JSON 里的 api_key 字段会被扫出来", hits == ["api_key"], str(hits))

    nested = json.dumps({"a": {"b": [{"client_secret": "x"}]}}).encode("utf-8")
    check("嵌套 dict / list 里的密钥字段也能扫出来",
          find_sensitive_keys("data/plugin_data/x/a.json", nested) == ["client_secret"], "")

    chat_log = json.dumps({"role": "user",
                           "content": "帮我看看 api_key = sk-abcdefghijklmnop 对不对",
                           "extra": {"token_hint": "这不是字段名"}},
                          ensure_ascii=False).encode("utf-8")
    check("聊天记录里『提到』密钥不算命中（只看字段名）",
          find_sensitive_keys("data/user_context/private/bot1__U.json", chat_log) == [],
          str(find_sensitive_keys("data/user_context/private/bot1__U.json", chat_log)))

    clean = json.dumps({"user_openids": ["A"], "max_history": 20}).encode("utf-8")
    check("普通业务 JSON 不命中",
          find_sensitive_keys("data/group_settings.json", clean) == [], "")

    # ---------- 2. 纯函数：纯文本按"键 = 长值"判断 ----------
    print("\n[2] 纯文本只认『键 + 分隔符 + 够长的值』")
    text_hit = "api_key = sk-abcdefghijklmnop\ntoken: abcdefghijklmnopqrst\n".encode("utf-8")
    got = find_sensitive_keys("data/plugin_data/x/notes.txt", text_hit)
    check("文本里的 api_key = 长值 会命中", "api_key" in got, str(got))
    short = "token = short\n".encode("utf-8")
    check("明显太短的值不算（避免把说明文字当密钥）",
          find_sensitive_keys("data/plugin_data/x/notes.txt", short) == [], "")
    check("二进制内容直接跳过（图片/sqlite 不按文本判定）",
          find_sensitive_keys("data/plugin_data/x/blob.png", b"\x89PNG\r\n\x1a\n\x00\x01") == [], "")
    check("非文本后缀一律不扫",
          find_sensitive_keys("data/plugin_data/x/data.db", text_hit) == [], "")

    # ---------- 3. 上传：命中就跳过，且绝不动本地文件 ----------
    print("\n[3] 上传行为：命中 → 不上传；本地文件一个字节都不动")
    test_root = os.path.join(PROJECT, "data", "_selftest")
    os.makedirs(test_root, exist_ok=True)
    run_id = str(int(time.time() * 1000) % 100000000)
    sandbox = os.path.join(test_root, f"unit_cloud_{run_id}")
    shutil.rmtree(sandbox, ignore_errors=True)
    os.makedirs(sandbox, exist_ok=True)
    try:
        config_path = os.path.join(sandbox, "config_unit.json")
        config_manager = load_config(config_path)
        sync = CloudSync(config_manager)
        backend = FakeBackend()

        secret_path = os.path.join(sandbox, "secret.json")
        with open(secret_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"api_key": "sk-abcdefghijklmnop"}, ensure_ascii=False))
        with open(secret_path, "rb") as handle:
            before = handle.read()

        outcome = sync._upload(backend, secret_path, "data/plugin_data/x/secret.json", 123456)
        check("含密钥的文件返回 sensitive（既不算成功也不算失败）", outcome == "sensitive", outcome)
        check("没有真的上传（后端一次都没被调用）", backend.calls == [], str(backend.calls))
        with open(secret_path, "rb") as handle:
            check("本地文件没被改动/删除", handle.read() == before, "")
        check("跳过的文件被记下来（给网页/日志展示）",
              sync._sensitive_rels and "secret.json" in sync._sensitive_rels[0],
              str(sync._sensitive_rels))

        normal_path = os.path.join(sandbox, "normal.json")
        with open(normal_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"a": 1}, ensure_ascii=False))
        check("正常文件照旧上传（返回 ok 且后端被调用一次）",
              sync._upload(backend, normal_path, "data/plugin_data/x/normal.json", 1) == "ok"
              and len(backend.calls) == 1, str(len(backend.calls)))

        backend.fail_next = True
        check("后端报错时返回 fail（不会被误记成『含密钥跳过』）",
              sync._upload(backend, normal_path, "data/plugin_data/x/normal.json", 1) == "fail", "")

        # 关掉开关：确实需要备份插件密钥的人可以放行
        config_manager.set_path("cloud_sync.skip_secret_files", False)
        backend.calls = []
        check("关掉「含密钥的文件不上传」后，同一个文件可以正常上传",
              sync._upload(backend, secret_path, "data/plugin_data/x/secret.json", 2) == "ok"
              and len(backend.calls) == 1, str(len(backend.calls)))
        config_manager.set_path("cloud_sync.skip_secret_files", True)
        check("开关默认值是 true（配置里没写也是开启）",
              CloudSync(load_config(os.path.join(sandbox, "config_default.json"))).skip_secret_files
              is True, "")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    # ---------- 4. 与配置/设置页接线 ----------
    print("\n[4] 与配置 Schema 接线")
    from core import config_schema as schema
    check("Schema 里有 cloud_sync.skip_secret_files 且类型是 bool",
          (schema.FIELDS.get("cloud_sync.skip_secret_files") or {}).get("type") == "bool",
          str(schema.FIELDS.get("cloud_sync.skip_secret_files")))
    check("默认配置里是 True",
          schema.DEFAULT_CONFIG["cloud_sync"]["skip_secret_files"] is True, "")
    cloud_section = next((item for item in schema.sections_payload() if item["key"] == "cloud"), {})
    check("设置页的「云同步」分组会渲染这个开关",
          "cloud_sync.skip_secret_files" in (cloud_section.get("fields") or []),
          str(cloud_section.get("fields"))[:200])

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
