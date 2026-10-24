# -*- coding: utf-8 -*-
"""单元测试：发送侧 @ 某人（QQ 官方 `qqbot-at-user` 标签）

官方依据（`docs/QQ_MESSAGE_NOTES.md` 有来源链接）：
- 群聊 / 文字子频道支持 @某人；写法是把 `<qqbot-at-user id="对方openid" />` 嵌进正文，
  文本消息、markdown 消息都可以；旧协议 `<@userid>` 即将弃用；
- @全部成员 `<qqbot-at-everyone />` **仅文字子频道可用**（群聊里发真标签不生效），
  所以本程序把它退化成可读文字「@全体成员」。

运行：`python tests/unit/test_at_mentions.py`
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
    print("  单元测试：@ 某人（qqbot-at-user 标签）")
    print("=" * 70)

    # ---------- 1. 生成标签 ----------
    print("\n[1] 生成官方标签")
    check("build_at_user 生成官方格式（id 前后空白会去掉）",
          message_text.build_at_user("  ABC123  ") == '<qqbot-at-user id="ABC123" />',
          repr(message_text.build_at_user("  ABC123  ")))
    check("build_at_user 空值返回空串（不会生成半截标签）",
          message_text.build_at_user("") == "" and message_text.build_at_user(None) == "",
          repr(message_text.build_at_user("")))
    check("标签模板本身与官方一致（<qqbot-at-user id=\"…\" />）",
          message_text.AT_USER_TAG == '<qqbot-at-user id="{openid}" />',
          message_text.AT_USER_TAG)

    # ---------- 2. 旧写法 → 新写法 ----------
    print("\n[2] 旧协议自动换成官方新标签（官方：<@userid> 即将弃用）")
    cases = [
        ("hi <@ABC123>", 'hi <qqbot-at-user id="ABC123" />'),
        ("<@!XYZ789> 你好", '<qqbot-at-user id="XYZ789" /> 你好'),
        ("a<@U1234>b", 'a<qqbot-at-user id="U1234" />b'),
        ("多个：<@A1234> 和 <@B5678>",
         '多个：<qqbot-at-user id="A1234" /> 和 <qqbot-at-user id="B5678" />'),
        ('已经是新格式 <qqbot-at-user id="KEEP1" /> 不要动',
         '已经是新格式 <qqbot-at-user id="KEEP1" /> 不要动'),
        ("普通文本没有 @", "普通文本没有 @"),
        ("邮箱 a@everyone.com 不算标记", "邮箱 a@everyone.com 不算标记"),
        ("markdown：\n## 标题\n\n<@USER9> 你看",
         'markdown：\n## 标题\n\n<qqbot-at-user id="USER9" /> 你看'),
    ]
    for src, want in cases:
        got = message_text.normalize_outgoing_at(src)
        check(f"转换：{src[:22]!r} → 官方标签/原样", got == want,
              f"got={got!r} want={want!r}")

    # ---------- 3. @全体成员：群聊不支持真标签，退化成文字 ----------
    print("\n[3] @全体成员（官方仅文字子频道可用 → 群里退化成可读文字）")
    for src in ("<@all> 集合", "<@!all> 集合", "@everyone 集合"):
        got = message_text.normalize_outgoing_at(src)
        check(f"{src!r} 变成纯文本「@全体成员」（不发真标签）",
              got == "@全体成员 集合" and "qqbot-at-everyone" not in got, repr(got))
    check("用户手写的 <qqbot-at-everyone /> 保持原样（不擅自删）",
          message_text.normalize_outgoing_at("<qqbot-at-everyone /> 集合")
          == "<qqbot-at-everyone /> 集合", "")

    # ---------- 4. 幂等 / 不误伤 ----------
    print("\n[4] 幂等与边界")
    once = message_text.normalize_outgoing_at("hi <@ABC123>")
    twice = message_text.normalize_outgoing_at(once)
    check("重复调用结果不变（幂等）", once == twice, f"{once!r} vs {twice!r}")
    check("空串/None 不抛异常",
          message_text.normalize_outgoing_at("") == ""
          and message_text.normalize_outgoing_at(None) == "", "")
    check("不碰正文里其它尖括号（面/表情等）",
          message_text.normalize_outgoing_at("<faceType=4 faceId=\"0\">")
          == "<faceType=4 faceId=\"0\">", "")

    # ---------- 5. 取出正文里的 @ 名单（前端提示/测试用） ----------
    print("\n[5] 从正文里取 @ 名单")
    text = ('x <qqbot-at-user id="A" /> y <qqbot-at-user id="B"/> '
            'z <qqbot-at-user id="A" />')
    check("at_user_ids 去重并保持出现顺序", message_text.at_user_ids(text) == ["A", "B"],
          str(message_text.at_user_ids(text)))
    check("没有标签时返回空列表",
          message_text.at_user_ids("普通文本 <@A1234>") == [], "")
    check("对 None 宽容", message_text.at_user_ids(None) == [], "")

    # ---------- 6. 收到的消息展示不受影响 ----------
    print("\n[6] 收消息侧的展示（不能被这次改动弄坏）")
    shown = message_text.mention_markup_to_text(
        '你好 <qqbot-at-user id="U1" /> 和 <@U23456>',
        [{"openid": "U1", "username": "张三"}, {"openid": "U23456", "username": "李四"}])
    check("收到的两种 @ 写法都显示成 @昵称", shown == "你好 @张三 和 @李四", repr(shown))
    check("@全体成员 仍显示成可读标签",
          message_text.mention_markup_to_text("<qqbot-at-everyone /> 集合") == "@全体成员 集合", "")

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
