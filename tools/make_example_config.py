# -*- coding: utf-8 -*-
"""生成脱敏的 config.example.json（把 config.json 里所有密钥/身份清空）。

用法：python tools/make_example_config.py
"""
import copy
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(BASE, "config.json")
TARGET = os.path.join(BASE, "config.example.json")

# 需要清空的字段（按字段名匹配，大小写不敏感）
SENSITIVE_KEYS = ("app_secret", "api_key", "secret", "token", "password", "api_token",
                  "access_token", "account_id", "webhook", "app_id", "key")


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
    if not os.path.isfile(SOURCE):
        print("找不到 config.json，无法生成示例配置", file=sys.stderr)
        return 1
    with open(SOURCE, "r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    example = copy.deepcopy(scrub(data))
    with open(TARGET, "w", encoding="utf-8") as handle:
        json.dump(example, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("已生成：", TARGET)
    return 0


if __name__ == "__main__":
    sys.exit(main())
