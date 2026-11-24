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
import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from . import paths

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


def _check_host(url: str) -> None:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in ("https", "http"):
        raise FetchError("只允许 http/https 地址：%s" % url)
    host = (parsed.hostname or "").lower()
    if host not in paths.ALLOWED_HOSTS:
        raise FetchError("拒绝从非白名单主机下载：%s" % host)


def list_releases(timeout: int = 30) -> List[Dict]:
    """取发布列表（新的在前）。"""
    requests = _requests()
    _check_host(paths.RELEASES_API)
    try:
        response = requests.get(paths.RELEASES_API, timeout=timeout,
                                headers={"User-Agent": USER_AGENT,
                                         "Accept": "application/vnd.github+json"})
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
    """把 url 下载到 target，返回 sha256。超过上限立即中止。"""
    requests = _requests()
    _check_host(url)
    try:
        response = requests.get(url, timeout=timeout, stream=True,
                                headers={"User-Agent": USER_AGENT})
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
def _safe_member(name: str) -> str:
    """把压缩包成员名规范化成相对路径；不安全的返回空串。"""
    raw = str(name or "").replace("\\", "/")
    if not raw or raw.startswith("/") or raw.startswith("~"):
        return ""
    if re.match(r"^[A-Za-z]:", raw):
        return ""
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


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
