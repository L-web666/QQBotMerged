# -*- coding: utf-8 -*-
"""单元测试：回复门卫（`core/ai_gate.py`）的判断与降级策略

**这个模块的风险点在哪**：
  · 解析模型输出 —— 模型不会老实只说 true/false，解析错了就会乱回或不回；
  · 失败时怎么办 —— 模型挂了如果按"不回"处理，机器人就等于哑了；
  · 三种模式的行为 —— observe 只记录，绝不能真的拦消息。

运行：`python tests/unit/test_ai_gate.py`
"""

import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from core.ai_gate import AIGate, parse_decision   # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


class FakeResponse:
    def __init__(self, content, status=200):
        self.status_code = status
        self.text = content if isinstance(content, str) else ""
        self._content = content

    def json(self):
        if isinstance(self._content, dict):
            return self._content
        return {"choices": [{"message": {"content": str(self._content)}}]}


class FakeSession:
    """桩：记录请求，返回预设回答。"""

    def __init__(self, reply="true", status=200, raises=None):
        self.reply = reply
        self.status = status
        self.raises = raises
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.raises:
            raise self.raises
        if isinstance(self.reply, dict):
            return FakeResponse(self.reply, self.status)
        return FakeResponse(
            {"choices": [{"message": {"content": self.reply}}]}, self.status)


def main():
    print("=" * 70)
    print("  回复门卫")
    print("=" * 70)

    # ============ A. 解析模型输出
    print("\n[A] 解析模型输出（模型不会老实只说一个词）")
    cases = [
        ("true", True), ("True", True), ("TRUE.", True),
        ("false", False), ("False", False), ("false.", False),
        ("应该回复", True), ("不需要回复", False),
        ("是", True), ("否", False),
        ("可以回复，因为对方在提问。", True),
        ("不用回，这只是附和。", False),
        ('{"reply": true}', True),
        ('{"reply": false}', False),
        ('{"should_reply": true}', True),
        ("  true  ", True),
        ("1", True), ("0", False),
        ("yes", True), ("no", False),
    ]
    for text, expect in cases:
        got = parse_decision(text)
        check("解析 %r → %s" % (text, expect), got is expect, f"得到 {got}")

    print("\n[A2] 认不出来时返回 None（交给失败策略处理，不瞎猜）")
    for text in ("", None, "嗯……", "我不知道", "～～～"):
        check("认不出 %r → None" % (text,), parse_decision(text) is None,
              f"得到 {parse_decision(text)}")

    # ============ B. 配置解析
    print("\n[B] 配置与可用性")
    gate = AIGate({"mode": "off"})
    check("默认模式是 off", gate.mode == "off", gate.mode)
    check("off 时 enabled 为假", gate.enabled is False, "")
    check("off 时可用性为假", gate.usable() is False, "")
    check("off 时 judge 一律返回 True（老行为：都当该回）",
          gate.judge("随便一句话") is True, "")

    gate = AIGate({"mode": "observe", "base_url": "https://x/y", "model": "m"})
    check("observe 模式生效", gate.mode == "observe", gate.mode)
    check("配好地址和模型后可用", gate.usable() is True, "")

    gate = AIGate({"mode": "enforce", "base_url": "", "model": ""})
    check("没配地址/模型时不可用", gate.usable() is False, "")

    gate = AIGate({"mode": "乱七八糟"})
    check("非法模式回落到 off", gate.mode == "off", gate.mode)

    gate = AIGate({"mode": "observe", "on_error": "skip"})
    check("on_error=skip 生效", gate.on_error == "skip", gate.on_error)
    gate = AIGate({"mode": "observe", "on_error": "xxx"})
    check("非法 on_error 回落到 reply", gate.on_error == "reply", gate.on_error)

    # ============ C. LLM 后端调用
    print("\n[C] LLM 后端")
    session = FakeSession("true")
    gate = AIGate({"mode": "enforce", "backend": "llm",
                   "base_url": "https://api.example.com/v1",
                   "api_key": "sk-test", "model": "small"},
                  session=session)
    result = gate.judge("这个怎么弄")
    check("判为该回", result is True, str(result))
    check("发起了 1 次请求", len(session.calls) == 1, str(len(session.calls)))
    call = session.calls[0]
    check("接口地址补上了 /chat/completions",
          call["url"].endswith("/chat/completions"), call["url"])
    check("带上了 Authorization", call["headers"].get("Authorization") == "Bearer sk-test",
          str(call["headers"].get("Authorization")))
    check("温度是 0（判断任务要稳定）", call["json"]["temperature"] == 0.0,
          str(call["json"]["temperature"]))
    check("max_tokens 很小（只要一个词）", call["json"]["max_tokens"] <= 16,
          str(call["json"]["max_tokens"]))
    check("提示词里带了判断规则",
          any("true" in str(m.get("content", "")).lower()
              for m in call["json"]["messages"]), "")

    print("\n[C2] 已有 /chat/completions 时不再重复拼")
    session = FakeSession("true")
    gate = AIGate({"mode": "enforce",
                   "base_url": "https://api.example.com/v1/chat/completions",
                   "model": "m"}, session=session)
    gate.judge("x")
    check("地址没有重复拼接",
          session.calls[0]["url"] == "https://api.example.com/v1/chat/completions",
          session.calls[0]["url"])

    print("\n[C3] 带上下文时把上下文一起给模型")
    session = FakeSession("true")
    gate = AIGate({"mode": "enforce", "base_url": "https://x/v1", "model": "m"},
                  session=session)
    gate.judge("怎么办", ["我在装软件", "装不上"])
    user_msg = session.calls[0]["json"]["messages"][-1]["content"]
    check("上下文出现在发给模型的内容里",
          "我在装软件" in user_msg and "装不上" in user_msg, user_msg[:120])
    check("当前消息也在", "怎么办" in user_msg, "")

    # ============ D. 失败处理
    print("\n[D] 判断失败时（这是最容易出事的地方）")
    session = FakeSession(raises=RuntimeError("网络不通"))
    gate = AIGate({"mode": "enforce", "base_url": "https://x/v1", "model": "m"},
                  session=session)
    result = gate.judge("你好")
    check("请求异常时返回 None（不是 False）", result is None, str(result))
    check("记下了失败原因", bool(gate.last_error), gate.last_error)
    check("失败计数 +1", gate.stats["failed"] == 1, str(gate.stats))

    session = FakeSession("garbage", status=500)
    gate = AIGate({"mode": "enforce", "base_url": "https://x/v1", "model": "m"},
                  session=session)
    result = gate.judge("你好")
    check("HTTP 500 时返回 None", result is None, str(result))
    check("错误里带上了状态码", "500" in gate.last_error, gate.last_error)

    session = FakeSession("我觉得还行")   # 认不出来的回答
    gate = AIGate({"mode": "enforce", "base_url": "https://x/v1", "model": "m"},
                  session=session)
    result = gate.judge("你好")
    check("看不懂的回答 → None（不当成不回）", result is None, str(result))

    print("\n[D2] 统计能反映判断结果")
    session = FakeSession("true")
    gate = AIGate({"mode": "enforce", "base_url": "https://x/v1", "model": "m"},
                  session=session)
    gate.judge("a")
    session.reply = "false"
    gate.judge("b")
    check("问了 2 次", gate.stats["asked"] == 2, str(gate.stats))
    check("该回 1 次", gate.stats["yes"] == 1, str(gate.stats))
    check("不回 1 次", gate.stats["no"] == 1, str(gate.stats))

    # ============ E. 边界
    print("\n[E] 边界情况")
    session = FakeSession("true")
    gate = AIGate({"mode": "enforce", "base_url": "https://x/v1", "model": "m"},
                  session=session)
    check("空消息直接判不回（不浪费时间请求）",
          gate.judge("") is False, "")
    check("空消息没有发起请求", len(session.calls) == 0, str(len(session.calls)))
    check("纯空白也算空消息", gate.judge("   ") is False, "")

    print("\n[E2] 状态接口")
    status = gate.status()
    for key in ("mode", "backend", "enabled", "usable", "on_error", "stats"):
        check("状态里有 %s" % key, key in status, str(sorted(status)))

    print("\n[E3] 热更新配置")
    gate = AIGate({"mode": "off"})
    check("初始没开", gate.enabled is False, "")
    gate.update_config({"mode": "observe", "base_url": "https://x/v1", "model": "m"})
    check("更新后打开了", gate.enabled is True, "")
    check("更新后可用", gate.usable() is True, "")

    # ============ F. 本地后端（不加载模型，只测分支）
    print("\n[F] 本地后端")
    gate = AIGate({"mode": "observe", "backend": "local",
                   "local_model": "/不存在的/model.pkl",
                   "local_config": "/不存在的/model.json"})
    check("本地后端模式识别正确", gate.backend == "local", gate.backend)
    check("模型文件不存在时不可用", gate.usable() is False, "")
    result = gate.judge("测试")
    check("模型加载不了时返回 None（按失败处理）", result is None, str(result))

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
