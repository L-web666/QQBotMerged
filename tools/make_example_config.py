# -*- coding: utf-8 -*-
"""生成一份示例配置 `config.example.json`（**仅本地参考，不入库**）。

为什么这个文件不该提交进仓库：
- 它是纯生成物 —— 程序首次运行时会自己写出 `config.json`，默认值来自
  `core/config_schema.py` 的 `DEFAULT_CONFIG`，模板随时能从代码重新生成；
- 放一份静态副本在仓库里会**和代码默认值漂移**：安全审计 SEC-01 / NEW-02 就是
  示例文件里写着 `web.host = 0.0.0.0`、而代码默认是 `127.0.0.1` 引起的。

用法：
    python tools/make_example_config.py

**不读取你的 `config.json`**：内容直接来自 `config_schema.default_config()`，
所有密钥字段天然为空。
"""

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
os.chdir(BASE)

from core import config_schema as schema          # noqa: E402

TARGET = os.path.join(BASE, "config.example.json")

# 兜底：万一默认配置里将来塞了带默认值的密钥，也要清空
SENSITIVE_KEYS = ("app_secret", "api_key", "secret", "token", "password",
                  "api_token", "access_token", "account_id", "webhook")


def scrub(node):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            low = str(key).lower()
            if isinstance(value, (dict, list)):
                out[key] = scrub(value)
            elif any(word in low for word in SENSITIVE_KEYS) and isinstance(value, str):
                out[key] = ""
            else:
                out[key] = value
        return out
    if isinstance(node, list):
        return [scrub(item) for item in node]
    return node


def main():
    data = scrub(schema.default_config())
    with open(TARGET, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("已生成：", TARGET)
    print("提示：这只是本地参考文件，.gitignore 已忽略它，不会被提交。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
