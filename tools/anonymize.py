# -*- coding: utf-8 -*-
"""脱敏器：把聊天样本里的隐私信息换成占位符。

**为什么需要独立成工具**：
  训练「这条消息该不该回」的模型，需要真实的群聊语料；
  但语料里带着昵称、路径、数字、机构名 —— 一旦模型或数据传出去，
  就等于把群友的聊天公开了。这个工具负责在**训练之前**把这些换掉。

**设计原则**：
  1. **稳定映射**：同一个人在整批文件里始终是同一个编号（`USER_1`），
     这样模型还能学到"不同人在对话"这个特征；
  2. **不需要可逆**：训练数据不需要还原成原文，所以映射表不落盘
     （落盘反而成了泄露源）；
  3. **保守优先**：拿不准的宁可多替换 —— 多换个词只是损失一点语义，
     漏换一个名字就是真的泄露；
  4. **可复核**：脱敏完会再跑一遍扫描，把"可能还有残留"的地方报出来，
     交给人判断（机器做不了"这句话会不会指向某个人"）。

用法：

    # 单个文件，输出到新文件
    python tools/anonymize.py samples.jsonl -o samples.clean.jsonl

    # 额外补充昵称（或从文件读）
    python tools/anonymize.py samples.jsonl --names "锋跃,小熊猫"
    python tools/anonymize.py samples.jsonl --names-file names.txt

    # 只检查不输出（看当前文件还剩多少隐私）
    python tools/anonymize.py samples.jsonl --check

支持的格式：JSONL，每行一个对象，替换其中所有字符串字段。
"""

import argparse
import json
import os
import re
import sys

# ---------------------------------------------------------------- 占位符
PLACEHOLDER_USER = "USER_%d"
PLACEHOLDER_PATH = "<PATH>"
PLACEHOLDER_DRIVE = "<DRIVE>"
PLACEHOLDER_NUM = "<NUM>"
PLACEHOLDER_EMAIL = "<EMAIL>"
PLACEHOLDER_PHONE = "<PHONE>"
PLACEHOLDER_URL = "<URL>"
PLACEHOLDER_QQ = "<QQ>"
PLACEHOLDER_ID = "<ID>"

# ---------------------------------------------------------------- 通用规则
# 顺序很重要：先替换"更具体、更长"的模式，避免被后面的规则吃掉一部分

# 邮箱（要在 URL 之前，否则 @ 可能被别的规则处理）
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# URL
RE_URL = re.compile(r"https?://[^\s<>\"']+", re.I)

# Windows 路径（C:\xxx、D:/xxx）
RE_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>]*")

# 单独的盘符（"C盘"、"D盘"）。
# **不能用 \b**：中文和 ASCII 之间没有词边界，"往C盘里" 匹配不到。
RE_DRIVE_CN = re.compile(r"([A-Za-z])\s*盘(?![子面])")

# 手机号（中国大陆 11 位）
RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# 长数字（>=5 位连续数字，可能是 QQ 号 / 学号 / 身份证片段）
RE_LONG_NUM = re.compile(r"(?<!\d)\d{5,}(?!\d)")

# QQ 号常见写法
RE_QQ = re.compile(r"(?i)\bqq\s*[:：]?\s*\d{5,12}\b")

# openid 形态的长十六进制串
RE_HEXID = re.compile(r"\b[0-9A-Fa-f]{16,}\b")


def _drive_repl(match):
    return "%s:\x00DRIVE\x00" % match.group(1)


class Anonymizer:
    """把文本里的隐私替换掉；同一个人始终换成同一个编号。

    **关键做法：先从整批数据里收集"谁说过话"（所有 sender 字段），
    再用这份名单去替换正文中出现的名字。**
    只替换 sender 字段是不够的 —— 群聊里经常有人喊别人的名字
    （"锋跃人呢"），那些名字同样要换掉。
    """

    def __init__(self, extra_names=None, keep_digits=False, known_names=None):
        self._user_ids = {}          # 原文 -> USER_n
        self._next_user = 1
        self._extra_names = list(extra_names or [])
        self.keep_digits = keep_digits
        # 额外昵称按长度倒序，避免"小明"先把"小明明"吃掉一半
        self._extra_names.sort(key=len, reverse=True)
        self.stats = {
            "user": 0, "email": 0, "url": 0, "path": 0, "drive": 0,
            "phone": 0, "num": 0, "qq": 0, "hexid": 0, "name_in_text": 0,
        }

    def learn_names(self, items):
        """从整批记录里收集所有"说过话的人"的名字。

        除了 sender 字段，也收 `<@xxx>` 提及里能找到的昵称线索
        （openid 本身拿不到名字，所以主要还是靠 sender）。
        """
        found = []
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("sender", "username", "nick", "nickname", "from",
                        "from_name", "user_name", "speaker"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    found.append(value.strip())
        # 去重 + 按长度倒序（长的优先替换）
        seen = set()
        names = []
        for n in sorted(found, key=len, reverse=True):
            if n in seen:
                continue
            seen.add(n)
            names.append(n)
        # 合并额外提供的名字
        for n in self._extra_names:
            if n and n not in seen:
                seen.add(n)
                names.append(n)
        names.sort(key=len, reverse=True)
        self._extra_names = names
        return names

    # ------------------------------------------------------------ 人名
    def user_token(self, name):
        """把昵称换成稳定编号。空名字返回空串。"""
        name = (name or "").strip()
        if not name:
            return ""
        if name not in self._user_ids:
            self._user_ids[name] = PLACEHOLDER_USER % self._next_user
            self._next_user += 1
        self.stats["user"] += 1
        return self._user_ids[name]

    def _replace_extra_names(self, text):
        """替换已知人名（出现在正文里的情况）。

        **要点：把"名字 + 常见后缀"当成一个整体替换。**
        群里常有人这样喊机器人：「@锋跃_AI」。
        如果只替换「锋跃」，剩下「_AI」就成了新线索（能拼回原发用户名），
        所以要连 `_AI` / `的机器人` / `bot` 这类后缀一起吃掉。

        跳过长度 < 2 的名字：一个字的昵称替换起来误伤太大。
        """
        for name in self._extra_names:
            if not name or len(name) < 2 or name not in text:
                continue
            token = self.user_token(name)
            # 先替换"名字+后缀"的完整形式，再替换裸名字
            for suffix in ("_AI", "_ai", "的机器人", "机器人", "bot", "Bot", "BOT"):
                combo = name + suffix
                if combo in text:
                    text = text.replace(combo, token)
                    self.stats["name_in_text"] += 1
            if name in text:
                count = text.count(name)
                text = text.replace(name, token)
                self.stats["name_in_text"] += count
        return text

    # ------------------------------------------------------------ 正文
    def text(self, value):
        """脱敏一段文本。"""
        if not isinstance(value, str) or not value:
            return value

        out = value

        out = RE_EMAIL.sub(PLACEHOLDER_EMAIL, out)
        out = RE_URL.sub(PLACEHOLDER_URL, out)
        out = RE_QQ.sub(PLACEHOLDER_QQ, out)
        # 先处理盘符（"C盘" -> "<DRIVE>盘"），再处理完整路径
        out = RE_DRIVE_CN.sub(_drive_repl, out)
        out = RE_WIN_PATH.sub(PLACEHOLDER_PATH, out)
        out = out.replace("C:\x00DRIVE\x00", PLACEHOLDER_DRIVE)
        out = RE_PHONE.sub(PLACEHOLDER_PHONE, out)
        out = RE_HEXID.sub(PLACEHOLDER_ID, out)
        if not self.keep_digits:
            out = RE_LONG_NUM.sub(PLACEHOLDER_NUM, out)

        out = self._replace_extra_names(out)
        return out

    # ------------------------------------------------------------ 名称字段
    def name(self, value):
        """专门处理"这是谁"这类字段（sender / username / nick 等）。"""
        return self.user_token(value)

    # ------------------------------------------------------------ 整条记录
    def record(self, obj):
        """递归脱敏一个 JSON 对象。"""
        if isinstance(obj, dict):
            out = {}
            for key, val in obj.items():
                low = str(key).lower()
                if low in ("sender", "username", "nick", "nickname", "from",
                           "from_name", "user_name", "speaker"):
                    out[key] = self.name(val)
                elif low in ("openid", "group_openid", "user_openid",
                             "member_openid", "union_openid", "id", "msg_id"):
                    # 标识类字段直接清空 —— 训练用不到，留着就是风险
                    out[key] = ""
                elif low in ("group", "group_id", "chat_id", "conv_key"):
                    out[key] = ""
                else:
                    out[key] = self.record(val)
            return out
        if isinstance(obj, list):
            return [self.record(x) for x in obj]
        if isinstance(obj, str):
            return self.text(obj)
        return obj


# ---------------------------------------------------------------- 残留扫描
# 下面这些模式用来**事后检查**，不是用来替换的：
# 它们提示"这里可能还需要人工看一眼"

SUSPICIOUS = [
    # 只认「@了某人」或「某人叫什么」这种明确指向人的写法。
    # 以前用「2-3 个汉字 + 语气词」去猜，误报率极高
    # （"这么长吗""有积分啊"全被判成姓名），把真信号淹没了。
    ("可能的称呼", re.compile(
        r"@[\u4e00-\u9fa5A-Za-z0-9_]{2,16}|"
        r"[\u4e00-\u9fa5]{2,4}(?:老师|同学|先生|女士|总|哥|姐)")),
    ("可能的学校/机构", re.compile(
        r"[\u4e00-\u9fa5]{2,8}(中学|小学|大学|学院|公司|集团|银行|医院)")),
    ("可能的地址", re.compile(
        r"[\u4e00-\u9fa5]{2,6}(?:省|市|区|县)(?![\u4e00-\u9fa5])|"
        r"[\u4e00-\u9fa5]{2,8}(?:路|街|小区|号楼)\d*号?")),
    ("年龄/年级线索", re.compile(
        r"(高[一二三]|初[一二三]|[一二三四五六七八九]|十[一二])年级|中考|高考|复读")),
    ("工作/收入线索", re.compile(
        r"(工资|月薪|年薪|奖金)\s*\d+|离职|入职|裁员|试用期")),
    ("健康信息", re.compile(
        r"(确诊|抑郁症?|焦虑症|住院|手术|病历|吃药)")),
    ("家庭信息", re.compile(
        r"(我爸|我妈|老公|老婆|儿子|女儿|孩子|未婚|离婚)")),
    ("证件号", re.compile(r"\b\d{15}\b|\b\d{17}[\dXx]\b")),
]


def scan_residual(text, where=""):
    """扫描一段文本，返回可疑项列表。"""
    found = []
    if not isinstance(text, str) or not text:
        return found
    for label, pattern in SUSPICIOUS:
        for match in pattern.finditer(text):
            found.append((label, match.group(0), where))
    return found


# ---------------------------------------------------------------- 文件名脱敏
def anonymize_path(path):
    return path


# ---------------------------------------------------------------- CLI
def load_jsonl(path):
    items = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print("  [跳过] 第 %d 行不是合法 JSON：%s" % (lineno, exc),
                      file=sys.stderr)
    return items


def collect_strings(obj, acc):
    if isinstance(obj, dict):
        for val in obj.values():
            collect_strings(val, acc)
    elif isinstance(obj, list):
        for val in obj:
            collect_strings(val, acc)
    elif isinstance(obj, str):
        acc.append(obj)
    return acc


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="anonymize",
        description="把聊天样本里的隐私（昵称/路径/数字/机构）换成占位符",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python tools/anonymize.py samples.jsonl -o clean.jsonl\n"
            "  python tools/anonymize.py samples.jsonl --check\n"
            "  python tools/anonymize.py samples.jsonl --names-file names.txt "
            "-o clean.jsonl\n"
        ))
    parser.add_argument("input", help="输入 JSONL 文件")
    parser.add_argument("-o", "--output", help="输出 JSONL 文件（不填则只打印统计）")
    parser.add_argument("--names", default="",
                        help="额外要替换的昵称，逗号分隔")
    parser.add_argument("--names-file", default="",
                        help="额外昵称文件（一行一个）")
    parser.add_argument("--check", action="store_true",
                        help="只扫描残留，不输出（用来复查）")
    parser.add_argument("--keep-digits", action="store_true",
                        help="保留长数字（默认替换成 <NUM>）")
    parser.add_argument("--quiet", action="store_true", help="少打印")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.input):
        print("找不到输入文件：%s" % args.input, file=sys.stderr)
        return 2

    # 额外昵称
    extra = [n.strip() for n in args.names.split(",") if n.strip()]
    if args.names_file and os.path.isfile(args.names_file):
        with open(args.names_file, "r", encoding="utf-8") as fh:
            extra += [ln.strip() for ln in fh if ln.strip()]

    items = load_jsonl(args.input)
    if not items:
        print("输入文件里没有有效记录", file=sys.stderr)
        return 2

    # ---- 只检查模式
    if args.check:
        total = 0
        by_label = {}
        for idx, item in enumerate(items, 1):
            for text in collect_strings(item, []):
                for label, hit, _ in scan_residual(text):
                    total += 1
                    by_label.setdefault(label, []).append(text[:50])
        print("=" * 66)
        print("  残留扫描：%d 条记录" % len(items))
        print("=" * 66)
        if not total:
            print("  没有发现明显残留")
        else:
            for label, hits in sorted(by_label.items(), key=lambda x: -len(x[1])):
                print("\n  [%s] %d 处" % (label, len(hits)))
                for sample in hits[:3]:
                    print("     %s" % sample)
        print()
        print("  注意：机器扫不出「这句话会不会指向某个人」，最后仍需人工过一遍。")
        return 0

    # ---- 脱敏
    anon = Anonymizer(extra_names=extra, keep_digits=args.keep_digits)
    # **关键步骤**：先从整批数据里学出"谁说过话"，再用这份名单替换正文。
    # 只替换 sender 字段不够 —— 群里会有人喊别人的名字。
    learned = anon.learn_names(items)
    cleaned = [anon.record(item) for item in items]

    # ---- 事后扫描
    residual = []
    for idx, item in enumerate(cleaned, 1):
        for text in collect_strings(item, []):
            for label, hit, _ in scan_residual(text):
                residual.append((idx, label, hit, text[:60]))

    if not args.quiet:
        print("=" * 66)
        print("  脱敏完成")
        print("=" * 66)
        print("  记录数：%d" % len(cleaned))
        print()
        print("  替换统计：")
        labels = {
            "user": "昵称 → USER_n",
            "email": "邮箱 → <EMAIL>",
            "url": "链接 → <URL>",
            "path": "路径 → <PATH>",
            "drive": "盘符 → <DRIVE>",
            "phone": "手机号 → <PHONE>",
            "num": "长数字 → <NUM>",
            "qq": "QQ 号 → <QQ>",
            "hexid": "长 ID → <ID>",
            "name_in_text": "正文里的人名 → USER_n",
        }
        for key, label in labels.items():
            count = anon.stats.get(key, 0)
            if count:
                print("    %-20s %d 次" % (label, count))
        print()
        print("  涉及的不同昵称：%d 个 → %s"
              % (len(anon._user_ids),
                 ", ".join(sorted(anon._user_ids.values(),
                                  key=lambda s: int(s.split("_")[1])))))

        if residual:
            print()
            print("  ⚠️ 还有 %d 处可能需要人工确认：" % len(residual))
            shown = {}
            for idx, label, hit, text in residual:
                shown.setdefault(label, []).append((idx, hit, text))
            for label, hits in shown.items():
                print("    [%s] %d 处" % (label, len(hits)))
                for idx, hit, text in hits[:2]:
                    print("       第 %d 条：%s" % (idx, hit))
        else:
            print()
            print("  事后扫描：没有发现明显残留")

    # ---- 写文件
    if args.output:
        out_dir = os.path.dirname(os.path.abspath(args.output))
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            for item in cleaned:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        if not args.quiet:
            print()
            print("  已写入：%s" % os.path.abspath(args.output))
            print()
            print("  **提醒**：这个文件仍然包含群友的真实聊天内容（只是去掉了身份），")
            print("  不要提交到公开仓库。要公开请只发布**训练好的模型**，不要发数据。")
    elif not args.quiet:
        print()
        print("  未指定 -o，只做了统计，没有写文件。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
