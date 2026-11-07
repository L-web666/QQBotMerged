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
# 版本规则：`主.次.修订`
#   · 主版本：不兼容的大改（配置结构变动、需要手动迁移）
#   · 次版本：新增功能（如本次的自动更新体系）
#   · 修订号：只修 bug
# 注意：必须**严格递增**，否则已装旧版的用户收不到更新提示（按版本号比较）。
__version__ = "1.3.0"

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


def normalize_version(text: str) -> str:
    """把 `v1.2.7` / `V1.2.7` / `release-1.2.7` / `version_2.0` / `1.2.7` 归一成数字版本。

    认不出数字段时返回空串（调用方据此跳过这个 tag，而不是猜一个版本号）。
    注意两个坑：
      · **不要**用"字符串里随便找个数字"的做法：`sha-abc123` 会被解析成 `123`，
        于是提交哈希被当成版本号；
      · 去掉前缀**不能**先做 `lstrip("vV")`：那会把 `version_2.0` 变成
        `ersion_2.0`（'v' 被吃掉），所以先试原文、再试剥掉前缀的版本。
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
        # 必须**以数字开头**，且只取开头的数字与点（`1.2.7-beta` → `1.2.7`）
        match = re.match(r"(\d+(?:\.\d+)*)", candidate)
        if not match:
            continue
        value = match.group(1).rstrip(".")
        if _VERSION_RE.match(value):
            return value
    return ""


def version_tuple(text: str) -> Tuple[int, ...]:
    """把版本号转成可比较的整数元组；坏输入返回空元组。"""
    normalized = normalize_version(text)
    if not normalized:
        return ()
    return tuple(int(part) for part in normalized.split("."))


def compare_versions(left: str, right: str) -> int:
    """比较两个版本号：`left > right` 返回 1，相等 0，小于 -1。

    按段比较并**补齐长度**（`1.2` 与 `1.2.0` 视为同一版本，不是"更小"）。
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
    return 0


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
