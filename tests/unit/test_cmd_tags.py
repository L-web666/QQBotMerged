# -*- coding: utf-8 -*-
"""单元测试：指令标签（`qqbot-cmd-enter` / `qqbot-cmd-input`）与「只在 Markdown 生效」判定

官方依据（`docs/QQ_MESSAGE_NOTES.md` 有来源链接）：
- 目前仅在 markdown 消息支持；
- `<qqbot-cmd-enter text="xxx" />`：用户点击后**直接发送** text，群聊/文字子频道不支持；
- `<qqbot-cmd-input text="xxx" show="xxx" reference="false" />`：用户点击后把 text
  **插入输入框**（reference=true 时带原文引用）；
- text 必填、show/reference 选填，最长 100 字符，**传值前必须 urlencode**。

另外验证 `markdown_only_tags()`：正文里有 @某人 / 指令标签时要能识别出来，
`Runtime.send_text()` 靠它把「纯文本」请求自动升级成 Markdown（实测纯文本渲染不出这两类标签）。

运行：`python tests/unit/test_cmd_tags.py`
"""

import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import message_text                                          # noqa: E402

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
    print("  单元测试：指令标签（qqbot-cmd-enter / qqbot-cmd-input）")
    print("=" * 70)

    # ---------- 1. 生成标签 ----------
    print("\n[1] 生成官方指令标签（值必须 urlencode）")
    check("回车指令标签格式与官方一致",
          message_text.build_cmd_enter("/help") == '<qqbot-cmd-enter text="%2Fhelp" />',
          message_text.build_cmd_enter("/help"))
    check("中文会被 urlencode（官方要求）",
          message_text.build_cmd_enter("签到")
          == '<qqbot-cmd-enter text="%E7%AD%BE%E5%88%B0" />',
          message_text.build_cmd_enter("签到"))
    check("参数指令：text + show",
          message_text.build_cmd_input("/ban", "封禁")
          == '<qqbot-cmd-input text="%2Fban" show="%E5%B0%81%E7%A6%81" />',
          message_text.build_cmd_input("/ban", "封禁"))
    check("参数指令：reference=true 才写 reference（官方默认 false）",
          message_text.build_cmd_input("/ban", "", True)
          == '<qqbot-cmd-input text="%2Fban" reference="true" />'
          and message_text.build_cmd_input("/ban", "", False)
          == '<qqbot-cmd-input text="%2Fban" />',
          message_text.build_cmd_input("/ban", "", True))
    check("show 与 text 相同时不重复写 show（省长度）",
          message_text.build_cmd_input("/help", "/help")
          == '<qqbot-cmd-input text="%2Fhelp" />',
          message_text.build_cmd_input("/help", "/help"))
    check("text 为空时返回空串（不生成半截标签）",
          message_text.build_cmd_enter("") == ""
          and message_text.build_cmd_enter("   ") == ""
          and message_text.build_cmd_input(None) == "",
          repr(message_text.build_cmd_enter("")))
    check("text 超过 100 字符按官方上限截断",
          len(message_text.read_cmd_tag(message_text.build_cmd_enter("x" * 300))["text"]) == 100,
          str(len(message_text.read_cmd_tag(message_text.build_cmd_enter("x" * 300))["text"])))
    check("上限常量就是官方的 100",
          message_text.CMD_TEXT_MAX == 100, str(message_text.CMD_TEXT_MAX))
    check("含 & 和空格的文本编码后不会破坏标签结构",
          message_text.build_cmd_enter("a&b c") == '<qqbot-cmd-enter text="a%26b%20c" />',
          message_text.build_cmd_enter("a&b c"))
    check("编码规则与前端 chat.js 的 encodeCmdValue 逐字符一致（!*'() 也要编码）",
          message_text.build_cmd_enter("a!b*c'd(e)f g&h")
          == '<qqbot-cmd-enter text="a%21b%2Ac%27d%28e%29f%20g%26h" />',
          message_text.build_cmd_enter("a!b*c'd(e)f g&h"))

    # ---------- 2. 解析标签 ----------
    print("\n[2] 解析标签（值已解码，给提示/展示用）")
    info = message_text.read_cmd_tag(
        '<qqbot-cmd-input text="%2Fban%20%E5%BC%A0%E4%B8%89" '
        'show="%E5%B0%81%E7%A6%81" reference="true" />')
    check("kind / text / show / reference 都解析正确",
          info.get("kind") == "input" and info.get("text") == "/ban 张三"
          and info.get("show") == "封禁" and info.get("reference") is True,
          str(info))
    check("没写 show 时 show 跟着 text（官方默认行为）",
          message_text.read_cmd_tag('<qqbot-cmd-enter text="%2Fhelp" />').get("show") == "/help",
          str(message_text.read_cmd_tag('<qqbot-cmd-enter text="%2Fhelp" />')))
    check("不是指令标签时返回空字典",
          message_text.read_cmd_tag("普通文本 <@U1234>") == {}, "")
    check("cmd_tags 按出现顺序列出类型（含重复）",
          message_text.cmd_tags('a <qqbot-cmd-enter text="x" /> b '
                                '<qqbot-cmd-input text="y" /> c <qqbot-cmd-enter text="z" />')
          == ["enter", "input", "enter"],
          str(message_text.cmd_tags('<qqbot-cmd-enter text="x" />')))
    check("没有指令标签时 cmd_tags 返回空列表",
          message_text.cmd_tags("普通消息") == [] and message_text.cmd_tags(None) == [], "")

    # ---------- 3. 展示 / 给 AI 看 ----------
    print("\n[3] 收到的指令标签换成可读文字")
    shown = message_text.cmd_markup_to_text(
        '点 <qqbot-cmd-enter text="%2Fhelp" /> 或 '
        '<qqbot-cmd-input text="%2Fban" show="%E5%B0%81%E7%A6%81" />')
    check("回车/参数指令都换成 /回车指令、/参数指令 + 文本",
          shown == "点 /回车指令 /help 或 /参数指令 封禁", repr(shown))
    check("format_for_ai 里也会转换（AI 不该看到裸标签）",
          message_text.format_for_ai('<qqbot-cmd-enter text="%2Fhelp" />') == "/回车指令 /help",
          repr(message_text.format_for_ai('<qqbot-cmd-enter text="%2Fhelp" />')))
    check("会话列表预览也会转换",
          message_text.preview('<qqbot-cmd-enter text="%2Fhelp" />') == "/回车指令 /help",
          repr(message_text.preview('<qqbot-cmd-enter text="%2Fhelp" />')))
    check("普通文本一个字都不动",
          message_text.cmd_markup_to_text("普通文本") == "普通文本"
          and message_text.cmd_markup_to_text("") == "", "")

    # ---------- 4. 「只在 markdown 生效」判定 ----------
    print("\n[4] markdown_only_tags：哪些正文必须改用 Markdown 发")
    check("@某人（新标签）能识别",
          message_text.markdown_only_tags('<qqbot-at-user id="U1" /> 你好') == ["@某人"],
          str(message_text.markdown_only_tags('<qqbot-at-user id="U1" /> 你好')))
    check("旧写法 <@openid> 也能识别（发送口转换前）",
          message_text.markdown_only_tags("你好 <@U1234>") == ["@某人"],
          str(message_text.markdown_only_tags("你好 <@U1234>")))
    check("指令标签能识别",
          message_text.markdown_only_tags('<qqbot-cmd-enter text="x" />') == ["指令标签"],
          str(message_text.markdown_only_tags('<qqbot-cmd-enter text="x" />')))
    check("两类同时出现时都列出来",
          message_text.markdown_only_tags(
              '<qqbot-at-user id="U1" /> <qqbot-cmd-input text="x" />') == ["@某人", "指令标签"],
          str(message_text.markdown_only_tags('<qqbot-at-user id="U1" /> <qqbot-cmd-input text="x" />')))
    check("普通文本 / @全体成员不算（@全体成员是纯文本兜底）",
          message_text.markdown_only_tags("普通文本 @全体成员") == []
          and message_text.markdown_only_tags("<qqbot-at-everyone /> 集合") == [],
          str(message_text.markdown_only_tags("<qqbot-at-everyone /> 集合")))
    check("空值/None 不抛异常",
          message_text.markdown_only_tags("") == [] and message_text.markdown_only_tags(None) == [], "")

    # ---------- 5. 发送说明文案 ----------
    print("\n[5] 自动改用 Markdown 的说明文案")
    note = message_text.AUTO_MARKDOWN_NOTE.format(what="、".join(
        message_text.markdown_only_tags('<qqbot-at-user id="U1" />')))
    check("文案里带上是哪个标签触发的（用户看得懂为什么格式变了）",
          "@某人" in note and "Markdown" in note, note)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
