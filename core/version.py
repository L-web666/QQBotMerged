# -*- coding: utf-8 -*-
"""版本号与更新源（唯一事实来源）。

**为什么单独放一个模块**：版本号会被「关于页 / 运行状态页 / 侧栏 / 更新检查 /
启动日志」多处引用，散落在各处一定会漂移。这里集中定义，其它地方只 import 不写字面量。

**更新源固定写死**，不放进 `config.json`：配置里能改仓库地址 = 谁能写配置谁就能
让程序去拉任意仓库的代码（等于远程代码执行）。要换源只能改这个文件、走一次发版。
"""

import re
from typing import List, Optional, Tuple

# 当前程序版本（发版时只改这一处）
#
# 版本规则：严格遵循**语义化版本 2.0.0**（https://semver.org/lang/zh-CN/）
#   · 主版本号 X：**不兼容**的改动（配置结构变动、需要手动迁移、行为破坏性变更）
#       → Y 与 Z 同时归零
#   · 次版本号 Y：**向下兼容的功能新增**（新接口、新模块、新能力）
#       → Z 归零
#   · 修订号 Z：**向下兼容的问题修正**（修 bug、改文案、调日志格式）
#
# 怎么判定"算不算功能新增"：用户能不能用它做以前做不到的事？
#   能 → 次版本号；只是把原来的事做对了 / 做得更好看 → 修订号。
#
# **历史备注**：1.3.7 与 1.3.8 实际包含功能新增（`bot_info()`、`core/bot_names.py`
# 名称自动维护、每小时刷新），按本规则本应是 1.4.x。但语义化版本第 3 条规定
# **已发布的版本内容与号不得更改**，所以保持原样。**1.4.0 起完全合规**
# （该版本新增了出错告警功能，因此升次版本号并把修订号归零）。
#
# 注意：版本号必须**严格递增**，否则已装旧版的用户收不到更新提示（按版本号比较）。
__version__ = "1.4.1"

APP_NAME = "QQBotMerged"
APP_TITLE = "QQ 机器人合并版"

# 更新源（GitHub）
GITHUB_OWNER = "L-web666"
GITHUB_REPO = "QQBotMerged"
REPO_SLUG = f"{GITHUB_OWNER}/{GITHUB_REPO}"
REPO_URL = f"https://github.com/{REPO_SLUG}"
RELEASES_URL = f"{REPO_URL}/releases"
RELEASES_API = f"https://api.github.com/repos/{REPO_SLUG}/releases"
TAGS_API = f"https://api.github.com/repos/{REPO_SLUG}/tags"
# GitHub Git Data API（用来把 tag 解析成 commit，做双源校验）
API_BASE = f"https://api.github.com/repos/{REPO_SLUG}"

# 允许下载更新包的主机白名单（HTTPS only）。GitHub 的 tarball 会跳转到这些域名。
ALLOWED_UPDATE_HOSTS = (
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
)

_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
# 允许的版本前缀：`v1.2.7` / `ver1.2.7` / `version-1.2.7` / `release-1.2.7` / `tag_1.2.7`
_PREFIX_RE = re.compile(r"^(?:release|version|ver|tag)?[-_ ]?", re.IGNORECASE)
# 完整的 X.Y.Z（可带先行版本号 -alpha.1 与编译信息 +build.2），语义化版本第 2/9/10 条
_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$")
# 只有主次修订（允许段数不足，`1.2` 按 `1.2.0` 处理）
_CORE_RE = re.compile(r"^(?P<core>\d+(?:\.\d+)*)(?:-(?P<pre>[0-9A-Za-z.-]+))?$")


def normalize_version(text: str) -> str:
    """把 tag 归一成版本号：`v1.2.7` / `release-1.2.7` / `1.2.7` → `1.2.7`。

    认不出数字段时返回空串（调用方据此跳过这个 tag，而不是猜一个版本号）。
    注意两个坑：
      · **不要**用"字符串里随便找个数字"的做法：`sha-abc123` 会被解析成 `123`，
        于是提交哈希被当成版本号；
      · 去掉前缀**不能**先做 `lstrip("vV")`：那会把 `version_2.0` 变成
        `ersion_2.0`（'v' 被吃掉），所以先试原文、再试剥掉前缀的版本。

    **先行版本号（`-alpha` / `-rc.1`）会被保留** —— 语义化版本第 9 条规定它的优先级
    低于同一主次修订的正式版（第 11.3 条：`1.0.0-alpha < 1.0.0`）。
    早期实现把它截掉了，导致 `1.4.0-beta` 被当成 `1.4.0`、装过正式版的用户收不到 beta。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    candidates = [raw]
    prefixed = _PREFIX_RE.match(raw)
    if prefixed and prefixed.end() > 0:
        candidates.append(raw[prefixed.end():])
    for candidate in candidates:
        # 只在"v 后面紧跟数字"时去掉 v，避免吃掉 version 的 v
        candidate = re.sub(r"^[vV](?=\d)", "", candidate.strip())
        # 编译信息（+build）不参与比较，直接丢掉（第 10 条）
        candidate = candidate.split("+", 1)[0]
        match = _CORE_RE.match(candidate)
        if not match:
            continue
        core = match.group("core").rstrip(".")
        if not _VERSION_RE.match(core):
            continue
        parts = [int(part) for part in core.split(".")]
        # 允许 `1.2` 这种少写一段的写法（按 `1.2.0` 处理）
        if len(parts) < 3:
            parts += [0] * (3 - len(parts))
        normalized = ".".join(str(part) for part in parts)
        prerelease = match.group("pre")
        if prerelease:
            normalized += "-" + prerelease
        return normalized
    return ""


def split_prerelease(text: str) -> Tuple[str, str]:
    """把版本号拆成 (主体版本, 先行版本)。没有先行版本时第二项为空串。"""
    normalized = normalize_version(text)
    if not normalized:
        return "", ""
    if "-" in normalized:
        core, pre = normalized.split("-", 1)
        return core, pre
    return normalized, ""


def _compare_prerelease(left: str, right: str) -> int:
    """比较先行版本号（语义化版本第 11.3 / 11.4 条）。

    规则：没有先行版本号的**优先级更高**（`1.0.0 > 1.0.0-rc.1`）；
    逐段比较：纯数字按数值比、含字母的按 ASCII 比、数字段低于字母段、
    前缀相同则段数多的更高。
    """
    if not left and not right:
        return 0
    if not left:
        return 1          # 正式版 > 先行版
    if not right:
        return -1
    left_parts = left.split(".")
    right_parts = right.split(".")
    for a, b in zip(left_parts, right_parts):
        a_num = a.isdigit()
        b_num = b.isdigit()
        if a_num and b_num:
            ia, ib = int(a), int(b)
            if ia != ib:
                return 1 if ia > ib else -1
        elif a_num != b_num:
            # 数字标识符比非数字的优先级低（第 11.4.3 条）
            return -1 if a_num else 1
        elif a != b:
            return 1 if a > b else -1
    if len(left_parts) != len(right_parts):
        # 前缀相同时，段数多的优先级更高（第 11.4.4 条）
        return 1 if len(left_parts) > len(right_parts) else -1
    return 0


def version_tuple(text: str) -> Tuple[int, ...]:
    """把版本号转成可比较的整数元组；坏输入返回空元组。

    只取主次修订三段（先行版本号见 `_compare_prerelease`）——
    这个函数用于排序键，先行版本号的字典序不能直接当排序依据。
    """
    core, _ = split_prerelease(text)
    if not core:
        return ()
    return tuple(int(part) for part in core.split("."))


def compare_versions(left: str, right: str) -> int:
    """比较两个版本号：`left > right` 返回 1，相等 0，小于 -1。

    按语义化版本第 11 条的优先层级比较：
    先比主次修订（补齐长度，`1.2` 与 `1.2.0` 相同），再比先行版本号
    （`1.0.0-alpha < 1.0.0-alpha.1 < 1.0.0-beta < 1.0.0`）。
    任一边解析失败时按"无法比较"返回 0 —— 调用方不应据此认为有新版本。
    """
    a = version_tuple(left)
    b = version_tuple(right)
    if not a or not b:
        return 0
    length = max(len(a), len(b))
    a = a + (0,) * (length - len(a))
    b = b + (0,) * (length - len(b))
    if a > b:
        return 1
    if a < b:
        return -1
    # 主次修订相同 → 比先行版本号（第 11.3 / 11.4 条）
    _, pre_a = split_prerelease(left)
    _, pre_b = split_prerelease(right)
    return _compare_prerelease(pre_a, pre_b)


def is_newer(candidate: str, current: Optional[str] = None) -> bool:
    """`candidate` 是否比当前版本新。"""
    return compare_versions(candidate, current or __version__) > 0


def describe() -> str:
    """给人看的一行版本说明。"""
    return f"v{__version__}"


def pick_latest(candidates: List[str]) -> str:
    """从一堆 tag 里挑出最高的合法版本号（忽略解析不出的 tag）。"""
    best = ""
    for item in candidates or []:
        normalized = normalize_version(item)
        if not normalized:
            continue
        if not best or compare_versions(normalized, best) > 0:
            best = normalized
    return best
