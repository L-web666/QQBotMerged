# -*- coding: utf-8 -*-
"""从 GitHub Release 取主程序文件。

**为什么单独复制一份下载逻辑，而不是 import 主程序的 `core.updater`**：
启动器的作用是在主程序还不存在的时候把它拉下来 —— 那时根本没有 `core` 可导入。
所以这里保留一份精简但同样严格的实现：主机白名单、大小上限、成员数上限、
拒绝绝对路径/`..`/链接文件。
"""

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from . import paths

logger = logging.getLogger("qqbot_cli")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

# 压缩包上限：程序本体约 1~2 MB，给足余量但不允许无限大
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_TOTAL_BYTES = 256 * 1024 * 1024

USER_AGENT = "qqbot-cli/1.0 (+https://github.com/L-web666/QQBotMerged)"


class FetchError(RuntimeError):
    """下载/解压过程中的可预期错误（会打印给用户看）。"""


# ---------------------------------------------------------------- HTTP
def _requests():
    try:
        import requests
    except ImportError:                                  # pragma: no cover
        raise FetchError("缺少依赖 requests，请先 pip install requests")
    return requests


# 内网/回环地址前缀：即使出现在白名单主机的 Location 里也要拒绝。
# （白名单主机不会重定向到这些地址；出现就是异常或攻击。）
_PRIVATE_PREFIXES = (
    "10.", "127.", "0.", "169.254.",          # 私有网段 / 回环 / 链路本地
    "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
    "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
)


def _host_looks_private(host: str) -> bool:
    host = (host or "").lower().strip("[]")
    if host in ("localhost", "::1", "0.0.0.0"):
        return True
    return any(host.startswith(prefix) for prefix in _PRIVATE_PREFIXES)


def _check_host(url: str) -> None:
    """校验一个 URL 是否可以从这里发起请求。

    **只允许 HTTPS**，且主机必须在白名单里、不能是内网地址。
    重定向的每一跳都要单独过这个检查（见 `_get_following_redirects`）。
    """
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https":
        raise FetchError("只允许 https 地址：%s" % url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise FetchError("地址里没有主机名：%s" % url)
    if _host_looks_private(host):
        raise FetchError("拒绝访问内网地址：%s" % host)
    if host not in paths.ALLOWED_HOSTS:
        raise FetchError("拒绝从非白名单主机下载：%s" % host)


def _get_following_redirects(requests, url: str, timeout: int,
                             stream: bool = False, headers: Optional[Dict] = None,
                             max_hops: int = 5):
    """GET 一个 URL，**手动**跟随重定向，每一跳都重新校验。

    为什么不直接用 `requests` 的自动重定向：它只校验第一次请求的地址，
    白名单主机返回一个 `Location: http://内网/...` 时会被静默跟随 ——
    白名单形同虚设。这里关掉自动跟随，逐跳 `_check_host()`。
    """
    current = url
    for _hop in range(max_hops + 1):
        _check_host(current)
        response = requests.get(current, timeout=timeout, stream=stream,
                                headers=headers, allow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location") or ""
        try:
            response.close()
        except Exception:
            pass
        if not location:
            raise FetchError("重定向缺少 Location 头：%s" % current)
        current = urljoin(current, location)
    raise FetchError("重定向次数过多（超过 %d 次）" % max_hops)


def list_releases(timeout: int = 30) -> List[Dict]:
    """取发布列表（新的在前）。"""
    requests = _requests()
    try:
        response = _get_following_redirects(
            requests, paths.RELEASES_API, timeout,
            headers={"User-Agent": USER_AGENT,
                     "Accept": "application/vnd.github+json"})
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError("连不上 GitHub：%s" % exc)
    if response.status_code == 404:
        raise FetchError("仓库不存在或没有发布过版本")
    if response.status_code != 200:
        raise FetchError("GitHub 返回 %s（可能是网络受限或接口限流）"
                         % response.status_code)
    try:
        data = response.json()
    except Exception:
        raise FetchError("GitHub 返回的内容不是 JSON")
    if not isinstance(data, list):
        raise FetchError("GitHub 返回的数据格式不对")
    out = []
    for item in data:
        tag = str(item.get("tag_name") or "").strip()
        if not tag:
            continue
        out.append({
            "tag": tag,
            "name": item.get("name") or tag,
            "prerelease": bool(item.get("prerelease")),
            "draft": bool(item.get("draft")),
            "published": item.get("published_at") or "",
            "tarball": item.get("tarball_url") or "",
            "zipball": item.get("zipball_url") or "",
            "html": item.get("html_url") or "",
            "body": item.get("body") or "",
        })
    return out


def _stable(release: Dict) -> bool:
    """正式版：非草稿、非预发布，且版本号形如 1.2.3。"""
    if release.get("draft") or release.get("prerelease"):
        return False
    return bool(re.match(r"^v?\d+\.\d+\.\d+$", release.get("tag") or ""))


def pick_latest(releases: List[Dict]) -> Optional[Dict]:
    """挑最新的正式版；都没有就挑第一个非草稿的。"""
    for release in releases:
        if _stable(release):
            return release
    for release in releases:
        if not release.get("draft"):
            return release
    return None


# ---------------------------------------------------------------- 下载
def download(url: str, target: str, timeout: int = 60,
             progress: Optional[Callable[[int, int], None]] = None) -> str:
    """把 url 下载到 target，返回 sha256。超过上限立即中止。

    重定向**逐跳校验**（见 `_get_following_redirects`），不会跟着
    白名单主机跳到内网或别的主机去。
    """
    requests = _requests()
    try:
        response = _get_following_redirects(
            requests, url, timeout, stream=True,
            headers={"User-Agent": USER_AGENT})
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError("下载失败：%s" % exc)
    if response.status_code != 200:
        raise FetchError("下载失败：HTTP %s" % response.status_code)

    declared = response.headers.get("Content-Length")
    try:
        total = int(declared) if declared else 0
    except (TypeError, ValueError):
        total = 0
    if total > MAX_ARCHIVE_BYTES:
        raise FetchError("压缩包太大（%d 字节），已拒绝" % total)

    digest = hashlib.sha256()
    written = 0
    temp_path = target + ".part"
    try:
        with open(temp_path, "wb") as handle:
            for block in response.iter_content(256 * 1024):
                if not block:
                    continue
                written += len(block)
                if written > MAX_ARCHIVE_BYTES:
                    raise FetchError("压缩包超过 %d 字节，已中止" % MAX_ARCHIVE_BYTES)
                digest.update(block)
                handle.write(block)
                if progress:
                    progress(written, total)
        os.replace(temp_path, target)
    except FetchError:
        _quiet_remove(temp_path)
        raise
    except Exception as exc:
        _quiet_remove(temp_path)
        raise FetchError("写入文件失败：%s" % exc)
    return digest.hexdigest()


def _quiet_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- 解压
# Windows 保留设备名（组件名主干命中即拒绝，带扩展名也算：NUL.txt 同样是设备）
_WINDOWS_DEVICE_NAMES = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}
# 上标数字写法也算设备名（COM¹ / LPT² …）
_SUPERSCRIPT_DIGITS = {"\u00b9": "1", "\u00b2": "2", "\u00b3": "3"}


def _bad_component(part: str) -> bool:
    """单个路径组件是否危险（盘符、ADS、设备名、控制字符、尾随点空格等）。

    **必须逐组件检查，不能只看整个路径的开头**。反例：
    `a/D:outside.txt` 开头不是盘符，但 Windows 的 `ntpath.join('C:\\x', 'a',
    'D:outside.txt')` 会得到 `'D:outside.txt'` —— **直接跳出目标目录**。
    同类还有 `dir/C:/x.txt`、`a/b/c:stream`（ADS）。
    """
    if not part:
        return True
    if part in (".", ".."):
        return True
    # 盘符 / ADS / 任何位置的冒号
    if ":" in part:
        return True
    # 控制字符
    if any(ord(ch) < 32 for ch in part):
        return True
    # Windows 会静默去掉尾随的点与空格，导致"看起来不同其实是同一个文件"
    if part != part.rstrip(" ."):
        return True
    # 设备名：主干（第一个点之前）归一化后命中就是设备
    stem = part.split(".")[0]
    normalized = "".join(_SUPERSCRIPT_DIGITS.get(ch, ch) for ch in stem).lower()
    if normalized in _WINDOWS_DEVICE_NAMES:
        return True
    return False


def _safe_member(name: str) -> str:
    """把压缩包成员名规范化成相对路径；不安全的返回空串。

    **每个组件都要单独过一遍 `_bad_component()`** —— 只看开头会漏掉
    「中间组件是盘符」这种能把路径带出目标目录的写法。
    """
    raw = str(name or "").replace("\\", "/")
    if not raw or raw.startswith("/") or raw.startswith("~"):
        return ""
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts:
        return ""
    for part in parts:
        if _bad_component(part):
            return ""
    return "/".join(parts)


def _within(root: str, target: str) -> bool:
    """target 规范化后是否仍在 root 之内（防 `..`、盘符、符号链接绕过）。"""
    try:
        real_root = os.path.realpath(root)
        real_target = os.path.realpath(target)
        return os.path.commonpath([real_root, real_target]) == real_root
    except (ValueError, OSError):
        # 不同盘符时 commonpath 会抛 ValueError → 一定不在同一棵树里
        return False


def _has_symlink_parent(root: str, target: str) -> bool:
    """target 的任一父目录（相对 root）是否是符号链接。

    攻击者可以事先在安装目录里布置 `data -> /etc` 这样的链接，
    解压时文件就顺着链接写到了外面。
    """
    try:
        root_real = os.path.realpath(root)
    except OSError:
        return True
    current = os.path.dirname(os.path.abspath(target))
    while True:
        if os.path.islink(current):
            return True
        if os.path.abspath(current) == root_real or \
                os.path.dirname(current) == current:
            break
        current = os.path.dirname(current)
    return False


def _strip_root(names: List[str]) -> str:
    """识别 GitHub 压缩包的顶层壳目录（如 `L-web666-QQBotMerged-abc123/`）。

    判断依据是**目录条目**（`name` 以 `/` 结尾，且这条只表示那个顶层目录本身），
    而不是"所有路径的第一段" —— 后者在**没有壳目录**的包里会把 `core/` 之类
    的普通子目录误当成壳目录，结果 `run.py` 被留下来、`core/` 却被剥掉一层。

    返回空串表示"没有壳目录，原样解压"。
    """
    tops = set()
    for name in names:
        if not name or not name.endswith("/"):
            continue
        # 只接受"顶层那一层"的目录条目：去掉结尾 / 后不能再含 /
        if "/" in name[:-1]:
            continue
        tops.add(name[:-1])
        if len(tops) > 1:
            return ""
    if len(tops) != 1:
        return ""
    root = tops.pop()
    # 再确认一遍：除了这条目录条目，其它路径都应该在这个目录下
    for name in names:
        if not name or name == root + "/":
            continue
        if not name.startswith(root + "/"):
            return ""
    return root


def extract(archive: str, target_dir: str) -> None:
    """安全解压到 target_dir（会自动剥掉 GitHub 的顶层壳目录）。"""
    os.makedirs(target_dir, exist_ok=True)
    if zipfile.is_zipfile(archive):
        _extract_zip(archive, target_dir)
    else:
        _extract_tar(archive, target_dir)


def _extract_tar(archive: str, target_dir: str) -> None:
    total = 0
    seen = 0
    try:
        with tarfile.open(archive, "r:*") as tar:
            members = []
            for info in tar:
                seen += 1
                if seen > MAX_MEMBERS:
                    raise FetchError("压缩包内文件过多（超过 %d 个）" % MAX_MEMBERS)
                raw = str(info.name or "").replace("\\", "/")
                name = _safe_member(raw)
                if not name:
                    continue
                # `_safe_member` 会去掉末尾的 /，这里把它补回去 ——
                # 否则 `_strip_root` 认不出"顶层目录条目"（它靠结尾的 / 判断），
                # 壳目录就剥不掉，所有东西会被解到 `xxx-<sha>/` 下面。
                if raw.endswith("/") or info.isdir():
                    name = name + "/"
                members.append((name, info))
                total += max(0, int(info.size or 0))
                if total > MAX_TOTAL_BYTES:
                    raise FetchError("解压后体积过大，已拒绝")
            root = _strip_root([name for name, _ in members])
            for name, info in members:
                if root and name == root + "/":
                    relative = ""
                elif root and name.startswith(root + "/"):
                    relative = name[len(root) + 1:]
                else:
                    relative = name
                relative = relative.rstrip("/")
                if not relative:
                    continue
                destination = os.path.join(target_dir, *relative.split("/"))
                # **落盘前必须确认没跑出 target_dir**。`_safe_member()` 已经逐组件
                # 挡了盘符/ADS/设备名，但这里再做一次真实路径包含性检查 ——
                # 双保险，防止任何没想到的写法把文件写到安装目录外面。
                if not _within(target_dir, destination):
                    logger.debug("拒绝越界的解压目标：%r -> %r", name, destination)
                    continue
                if _has_symlink_parent(target_dir, destination):
                    logger.debug("拒绝经过符号链接的解压目标：%r", destination)
                    continue
                if info.isdir():
                    os.makedirs(destination, exist_ok=True)
                    continue
                if not info.isfile():
                    # 软链/硬链/设备文件一律跳过：发布包里不该有这些
                    continue
                os.makedirs(os.path.dirname(destination) or target_dir, exist_ok=True)
                source = tar.extractfile(info)
                if source is None:
                    continue
                with source, open(destination, "wb") as handle:
                    shutil.copyfileobj(source, handle, 1024 * 1024)
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError("解压失败：%s" % exc)


def _extract_zip(archive: str, target_dir: str) -> None:
    total = 0
    seen = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = []
            for info in bundle.infolist():
                seen += 1
                if seen > MAX_MEMBERS:
                    raise FetchError("压缩包内文件过多（超过 %d 个）" % MAX_MEMBERS)
                name = _safe_member(info.filename)
                if not name:
                    continue
                infos.append((name, info))
                total += max(0, int(info.file_size or 0))
                if total > MAX_TOTAL_BYTES:
                    raise FetchError("解压后体积过大，已拒绝")
            root = _strip_root([name for name, _ in infos])
            for name, info in infos:
                relative = name[len(root) + 1:] if root and name.startswith(root + "/") else name
                if not relative:
                    continue
                destination = os.path.join(target_dir, *relative.split("/"))
                if not _within(target_dir, destination):
                    logger.debug("拒绝越界的解压目标：%r -> %r", name, destination)
                    continue
                if _has_symlink_parent(target_dir, destination):
                    logger.debug("拒绝经过符号链接的解压目标：%r", destination)
                    continue
                if info.is_dir():
                    os.makedirs(destination, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(destination) or target_dir, exist_ok=True)
                with bundle.open(info) as source, open(destination, "wb") as handle:
                    shutil.copyfileobj(source, handle, 1024 * 1024)
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError("解压失败：%s" % exc)


# ---------------------------------------------------------------- 安装
def install_release(release: Dict, install_dir: str,
                    progress: Optional[Callable[[str, int, int], None]] = None) -> Dict:
    """下载并解压某个发布到 install_dir。返回 {tag, sha256, commit}。"""
    archive_url = release.get("tarball") or release.get("zipball") or ""
    if not archive_url:
        raise FetchError("这个发布没有提供源码包")
    os.makedirs(install_dir, exist_ok=True)
    # **下载缓冲放在安装目录里，而不是系统临时目录**：
    # 有些环境（企业机器、容器、受限的 %TEMP%）临时目录不可写，
    # 用临时目录会让安装直接失败；安装目录我们能确定是可写的。
    temp_dir = os.path.join(install_dir, ".qqbot-download")
    shutil.rmtree(temp_dir, ignore_errors=True)
    try:
        os.makedirs(temp_dir, exist_ok=True)
    except OSError:
        # 实在不行再退回系统临时目录
        temp_dir = tempfile.mkdtemp(prefix="qqbot-dl-")
    try:
        archive = os.path.join(temp_dir, "program.tar.gz")
        if progress:
            progress("downloading", 0, 0)

        def _tick(done: int, total: int) -> None:
            if progress:
                progress("downloading", done, total)

        digest = download(archive_url, archive, progress=_tick)
        if progress:
            progress("extracting", 0, 0)
        extract(archive, install_dir)
        return {"tag": release.get("tag") or "", "sha256": digest, "commit": ""}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def verify_sha256(path: str) -> str:
    """算文件 sha256（用于校验已下载的包）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
