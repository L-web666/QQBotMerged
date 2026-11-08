# -*- coding: utf-8 -*-
"""配置安全检查的公共判定（启动时与体检共用）。

**为什么单独一个模块**：`_token_weakness` 既要在启动时用来告警（`run.py`），
又要在安全体检里复用（`core/security_audit.py`）。如果从 `run.py` 反向导入，
会形成 `run → core → run` 的循环导入，程序直接起不来。
"""

# 常见弱口令片段（小写比较）
WEAK_FRAGMENTS = ("123456", "password", "qwerty", "admin", "abc123",
                  "111111", "000000", "iloveyou", "token", "secret", "letmein",
                  "welcome", "monkey", "dragon")

MIN_TOKEN_LENGTH = 12          # 低于这个长度直接判弱
RECOMMENDED_TOKEN_LENGTH = 20  # 建议长度


def token_weakness(token: str) -> str:
    """检查访问令牌强度，返回问题描述（空串 = 看起来够强）。

    **只做提示不做拦截**：令牌是用户自己配的，突然拒绝启动会让正在用的程序起不来。
    检查项都是"一眼就知道弱"的：
      · 太短（< 12 位）——暴力枚举现实可行；
      · 纯数字 —— 10 个字符的手工令牌通常就是生日/手机号；
      · 全同一个字符、或是明显的顺序串（123456/abcdef/qwerty…）；
      · 出现在内置的常见弱口令表里。
    """
    value = str(token or "")
    if not value:
        return ""
    if len(value) < MIN_TOKEN_LENGTH:
        return "长度只有 %d 位，建议至少 %d 位" % (len(value), RECOMMENDED_TOKEN_LENGTH)
    if value.isdigit():
        return "是纯数字，容易被猜中"
    if len(set(value)) <= 2:
        return "几乎都是同一个字符"
    lowered = value.lower()
    for weak in WEAK_FRAGMENTS:
        if weak in lowered:
            return "包含常见弱口令片段「%s」" % weak
    # 连续递增/递减（如 abcdef、987654）
    if len(value) >= 6:
        codes = [ord(ch) for ch in lowered]
        if all(codes[i + 1] - codes[i] == 1 for i in range(len(codes) - 1)):
            return "是连续递增字符"
        if all(codes[i + 1] - codes[i] == -1 for i in range(len(codes) - 1)):
            return "是连续递减字符"
    return ""


def token_generator_hint() -> str:
    """给用户一条"生成强令牌"的现成命令。"""
    return 'python -c "import secrets; print(secrets.token_urlsafe(24))"'
