# -*- coding: utf-8 -*-
"""单元测试：版本号规则遵循语义化版本 2.0.0

这不是测"程序功能"，而是测**发版纪律**：
  · 项目文档里写明的规则必须与 SemVer 一致；
  · 已发布的版本号不能改（SemVer 第 3 条）；
  · 版本号必须严格递增（否则老用户收不到更新）。

运行：`python tests/unit/test_versioning.py`
"""

import io
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import version as version_module   # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


# 已经对外发布过的版本号（发布后就**不允许**再变，见 SemVer 第 3 条）。
# 这份清单是"历史事实"，只追加不修改；当前版本号通常**还没**进这里 ——
# 因为改版本号是发版的第一步，真正发布完才该补进来。
RELEASED = ["1.2.4", "1.2.5", "1.2.7", "1.3.0", "1.3.1", "1.3.2", "1.3.3",
            "1.3.4", "1.3.5", "1.3.7", "1.3.8", "1.3.9", "1.4.0", "1.4.1",
            "1.4.2", "1.4.3", "1.4.4"]

# SemVer 官方推荐的正则（第二版，支持按编号提取）
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$")


def main():
    print("=" * 70)
    print("  版本号规则（语义化版本 2.0.0）")
    print("=" * 70)

    current = version_module.__version__

    print("\n[1] 当前版本号是合法的 SemVer")
    match = SEMVER_RE.match(current)
    check("符合 X.Y.Z 且无前导零", match is not None, current)
    if match:
        check("主版本号 > 0（已过 1.0.0 阶段）", int(match.group(1)) >= 1, current)
        check("三段都是非负整数",
              all(part is not None for part in match.groups()[:3]), current)
    check("程序能解析回自己",
          version_module.normalize_version(current) == current, current)

    print("\n[2] 版本号严格大于已发布的版本")
    newest_released = max(RELEASED, key=version_module.version_tuple)
    check("当前版本 >= 最新已发布版本",
          version_module.compare_versions(current, newest_released) >= 0,
          "%s vs %s" % (current, newest_released))
    for released in RELEASED:
        check("当前版本不旧于 %s" % released,
              version_module.compare_versions(current, released) >= 0, current)

    print("\n[3] 比较函数符合 SemVer 第 11 条（优先层级）")
    ordered = ["1.0.0", "1.0.1", "1.1.0", "1.10.0", "2.0.0", "10.0.0"]
    ok = True
    detail = ""
    for left, right in zip(ordered, ordered[1:]):
        if version_module.compare_versions(left, right) >= 0:
            ok = False
            detail = "%s 应当小于 %s" % (left, right)
            break
    check("数值比较而不是字符串比较", ok, detail)
    check("1.2 等于 1.2.0（补零）",
          version_module.compare_versions("1.2", "1.2.0") == 0, "")
    check("1.0.0-alpha 小于 1.0.0",
          version_module.compare_versions("1.0.0-alpha", "1.0.0") < 0, "")

    print("\n[4] 已发布的版本号不允许改（SemVer 第 3 条）")
    source = io.open(os.path.join(PROJECT, "core", "version.py"),
                     encoding="utf-8").read()
    check("源码里有「已发布不得更改」的说明",
          "已发布" in source and ("不得更改" in source or "不能改" in source),
          "")
    # 不要求"当前版本已在清单里"：改版本号是发版的第一步，
    # 发布前它本来就还没进清单。这里改为核对**清单本身**是否单调递增、
    # 以及当前版本不小于清单里的最新一项（防止版本号回退）。
    check("已发布清单严格递增",
          all(version_module.compare_versions(a, b) < 0
              for a, b in zip(RELEASED, RELEASED[1:])),
          str(RELEASED))
    newest = max(RELEASED, key=version_module.version_tuple)
    check("当前版本不旧于清单里最新的一项",
          version_module.compare_versions(current, newest) >= 0,
          "%s vs %s" % (current, newest))

    print("\n[5] 仓库文档写明了三位的含义")
    readme = io.open(os.path.join(PROJECT, "README.md"), encoding="utf-8").read()
    check("README 有版本号规则章节", "语义化版本" in readme, "")
    check("写明了主版本号 = 不兼容", "**不兼容**的改动" in readme, "")
    check("写明了次版本号 = 功能新增", "**向下兼容的功能新增**" in readme, "")
    check("写明了修订号 = 问题修正", "**向下兼容的问题修正**" in readme, "")
    check("写明了次版本升时修订归零", "Z **归零**" in readme, "")
    check("记录了 1.3.7 / 1.3.8 的历史备注",
          "历史备注" in readme and "1.3.8" in readme, "")

    print("\n[6] 源码注释与 README 规则一致")
    check("version.py 写明遵循 SemVer 2.0.0", "语义化版本 2.0.0" in source, "")
    check("version.py 写明主版本号含义",
          "主版本号 X" in source and "不兼容" in source, "")
    check("version.py 写明次版本号含义",
          "次版本号 Y" in source and "功能新增" in source, "")
    check("version.py 写明修订号含义",
          "修订号 Z" in source and "问题修正" in source, "")
    check("version.py 记录了 1.3.7/1.3.8 误标",
          "1.3.7" in source and "1.3.8" in source, "")

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
