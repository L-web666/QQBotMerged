# -*- coding: utf-8 -*-
"""单元测试：README 的目录锚点与标题一致

**为什么单独测这个**：README 的目录是手写的锚点（`[文字](#锚点)`），
标题一改锚点就失效 —— 点目录跳不过去，但文件本身看不出任何语法错误。
这类问题只能靠机械校验发现。

规则（GitHub / Gitee 的常见做法）：
  · 标题转小写；
  · 去掉 ` ` * _ ~ 等标记符号；
  · 空格转 `-`，连续 `-` 合并；
  · 其它标点（、，。：（）「」@ / 等）直接丢弃；
  · 中文与数字保留。

运行：`python tests/unit/test_readme_toc.py`
"""

import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
README = os.path.join(PROJECT, "README.md")

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def slug(text):
    text = text.strip().lower()
    text = re.sub(r"[`*_~]", "", text)
    out = []
    for ch in text:
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        elif ch in " \t":
            out.append("-")
    return re.sub(r"-+", "-", "".join(out)).strip("-")


def main():
    print("=" * 70)
    print("  README 目录锚点校验")
    print("=" * 70)

    lines = open(README, encoding="utf-8").read().splitlines()

    print("\n[1] 收集标题与目录")
    headings = {}
    for i, line in enumerate(lines, 1):
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            headings.setdefault(slug(m.group(2)), []).append((i, m.group(2)))
    toc = []
    for i, line in enumerate(lines, 1):
        for m in re.finditer(r"\[([^\]]+)\]\(#([^)]+)\)", line):
            toc.append((i, m.group(1), m.group(2)))
    check("有标题", len(headings) > 5, str(len(headings)))
    check("有目录条目", len(toc) >= 10, str(len(toc)))

    print("\n[2] 每个目录锚点都要能对上标题")
    bad = []
    for line_no, label, anchor in toc:
        if anchor.lower() not in headings:
            bad.append("L%d [%s] -> #%s" % (line_no, label, anchor))
    check("所有锚点都能解析", bad == [], "；".join(bad[:5]))

    print("\n[3] 三级标题编号统一为 X.Y 格式")
    sub_headings = [(i, ln) for i, ln in
                    ((i, l) for i, l in enumerate(lines, 1) if l.startswith("### "))
                    for ln in [ln]]
    sub_headings = [(i, l[4:].strip()) for i, l in sub_headings]
    check("有三级标题", len(sub_headings) > 0, str(len(sub_headings)))
    bad_num = []
    for line_no, text in sub_headings:
        # 允许 "3.1 xxx" 这种；不允许 "1. xxx"（那会和章号混）或 "3.1、xxx"
        if not re.match(r"^\d+\.\d+\s", text):
            bad_num.append("L%d %s" % (line_no, text[:24]))
    check("全部是「数字.数字 空格」开头", bad_num == [], "；".join(bad_num[:5]))
    check("没有「3.1、」这种带顿号的写法",
          not any(re.match(r"^\d+\.\d+、", t) for _, t in sub_headings), "")

    print("\n[4] 章号连续（一~十）")
    chapter_nums = "一二三四五六七八九十"
    chapters = []
    for i, line in enumerate(lines, 1):
        m = re.match(r"^## ([一二三四五六七八九十])、", line)
        if m:
            chapters.append((i, m.group(1)))
    check("十章齐全", len(chapters) == 10, str(len(chapters)))
    check("章号顺序正确",
          [c[1] for c in chapters] == list(chapter_nums),
          str([c[1] for c in chapters]))

    print("\n[5] 分隔线前后都有空行（渲染才稳）")
    bad_rule = []
    for i, line in enumerate(lines):
        if line.strip() == "---":
            before_ok = i == 0 or lines[i - 1].strip() == ""
            after_ok = i == len(lines) - 1 or lines[i + 1].strip() == ""
            if not (before_ok and after_ok):
                bad_rule.append("L%d" % (i + 1))
    check("分隔线两侧都空行", bad_rule == [], "；".join(bad_rule[:5]))

    print("\n[6] 代码块围栏成对且标注了语言")
    fences = [(i, l) for i, l in enumerate(lines, 1) if l.strip().startswith("```")]
    check("围栏数量为偶数（成对）", len(fences) % 2 == 0, str(len(fences)))
    # 起围栏（偶数序号）必须带语言，闭围栏必须是裸 ```
    bad_open = []
    bad_close = []
    for idx in range(0, len(fences) - 1, 2):
        opening = fences[idx][1].strip()
        closing = fences[idx + 1][1].strip() if idx + 1 < len(fences) else ""
        if opening == "```":
            bad_open.append("L%d" % fences[idx][0])
        if closing != "```":
            bad_close.append("L%d" % fences[idx + 1][0])
    check("起围栏都写了语言", bad_open == [], "；".join(bad_open[:6]))
    check("闭围栏都是裸 ```", bad_close == [], "；".join(bad_close[:6]))

    print("\n[7] 其它基础格式")
    trailing = [i for i, l in enumerate(lines, 1) if l != l.rstrip()]
    check("没有行尾空格", trailing == [], str(trailing[:6]))
    h1 = [l for l in lines if re.match(r"^# ", l)]
    check("只有 1 个一级标题", len(h1) == 1, str(len(h1)))
    # 标题层级不能跳级（H2 之后不能直接 H4）
    prev = 0
    jumps = []
    for i, l in enumerate(lines, 1):
        m = re.match(r"^(#{1,6})\s", l)
        if m:
            level = len(m.group(1))
            if prev and level > prev + 1:
                jumps.append("L%d" % i)
            prev = level
    check("标题不跳级", jumps == [], "；".join(jumps[:6]))

    print("\n[8] README 里的统计数字与实际一致")
    try:
        sys.path.insert(0, PROJECT)
        from core import config_schema as schema
        secs = schema.sections_payload()
        actual_groups = len(secs)
        actual_fields = sum(len(x["fields"]) for x in secs)
        server_src = open(os.path.join(PROJECT, "web", "server.py"),
                          encoding="utf-8").read()
        actual_routes = len(re.findall(r"@app\.route\(", server_src))
        html = open(os.path.join(PROJECT, "web", "templates", "index.html"),
                    encoding="utf-8").read()
        pages = []
        for p in re.findall(r'data-page="(\w+)"', html):
            if p not in pages:
                pages.append(p)
        actual_pages = len(pages)

        text = "\n".join(lines)
        stated = re.search(r"设置页 \*\*(\d+) 个分组、(\d+) 个配置项\*\*", text)
        check("开头那句写明了分组数与配置项数", stated is not None, "")
        if stated:
            check("分组数一致", int(stated.group(1)) == actual_groups,
                  "写了 %s，实际 %d" % (stated.group(1), actual_groups))
            check("配置项数一致", int(stated.group(2)) == actual_fields,
                  "写了 %s，实际 %d" % (stated.group(2), actual_fields))
        # 正文里每一处「N 个配置项」都要和实际一致
        mismatched = []
        for i, l in enumerate(lines, 1):
            for m in re.finditer(r"(\d+) 个配置项", l):
                if int(m.group(1)) != actual_fields:
                    mismatched.append("L%d 写了 %s" % (i, m.group(1)))
        check("所有「N 个配置项」都对得上", mismatched == [], "；".join(mismatched[:5]))
        routes_stated = re.search(r"\*\*(\d+) 个 HTTP 接口\*\*", text)
        if routes_stated:
            check("接口数一致", int(routes_stated.group(1)) == actual_routes,
                  "写了 %s，实际 %d" % (routes_stated.group(1), actual_routes))
        pages_stated = re.search(r"\*\*(\d+) 个页面\*\*", text)
        if pages_stated:
            check("页面数一致", int(pages_stated.group(1)) == actual_pages,
                  "写了 %s，实际 %d" % (pages_stated.group(1), actual_pages))
    except Exception as exc:
        check("统计数字校验没抛异常", False, str(exc))

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
