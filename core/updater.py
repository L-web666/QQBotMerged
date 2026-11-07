# -*- coding: utf-8 -*-
"""从 GitHub 检查 / 下载 / 安装程序更新。

## 安全边界（这是本项目风险最高的一段代码，必须读完）

自动更新等于"让程序从网络下载代码并覆盖自己"，做错一点就是远程代码执行。
这里的防线（每一条都有对应的测试）：

1. **更新源写死**：仓库地址在 `core/version.py`，不进 `config.json`。
   能改配置的人不能改更新源。
2. **只允许 HTTPS + 域名白名单**：`_open_url()` 逐跳校验协议与主机，
   拒绝重定向到白名单外的地址（与 `media_store.fetch` 同一套思路）。
3. **体积上限**：单次下载 ≤ `MAX_DOWNLOAD_BYTES`，边下边数，超了立刻中断。
4. **压缩包解压防穿越**：拒绝绝对路径、`..`、符号链接/硬链接/设备文件，
   并限制成员数量与解压后总大小（防 zip bomb / tar bomb）。
5. **安装前先备份**：把当前代码打成 zip 放进 `data/update/backup/`，
   任何一步失败都用它回滚。
6. **绝不覆盖用户数据**：`data/`、`plugins/`、`config.json`、`.git/`、
   `__pycache__/`、`*.bak` 一律跳过（用 `_is_protected()` 判断）。
7. **安装完不自动执行**：只写标记文件 + 提示重启。真正的"生效"由用户重启决定，
   避免"更新过程中把自己写坏还停不下来"。

## 不做的事

- 不做增量/删除同步：新版本删掉的文件不会从本地删掉（宁可留垃圾也不误删用户文件）。
- 不校验签名：GitHub 的 tarball 走 TLS，但仓库若被攻破这里挡不住。
  真要更强保证，得做发布签名（离线私钥签 manifest），不在本次范围。
"""

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from core import paths
from core import version as version_module
from core.version import ALLOWED_UPDATE_HOSTS

logger = logging.getLogger(__name__)

# ---- 硬上限（防止被喂超大包 / zip bomb）------------------------------------
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024      # 更新包最大 64MB
MAX_MEMBERS = 20000                        # 压缩包内文件数上限
MAX_EXTRACT_BYTES = 256 * 1024 * 1024      # 解压后总大小上限
DOWNLOAD_TIMEOUT = 30                      # 单个请求超时（秒）
CACHE_MINUTES = 30                         # 检查结果缓存时间（避开 GitHub 限流）

# 安装时必须存在、且必须"看起来像本项目"的文件（防止装进一个空包/错包）
REQUIRED_FILES = ("run.py", os.path.join("core", "__init__.py"),
                  os.path.join("web", "templates", "index.html"))

# 安装时**永不覆盖**的路径（用户数据与本地配置）
PROTECTED_DIRS = ("data", "plugins", ".git", "__pycache__", "dist", "build",
                  ".venv", "venv", ".idea", ".vscode")
# 注意：`README.md` / `PROGRESS.md` 是正常仓库文件，**允许**随版本更新；
# 这里保护的是"用户本地才有、覆盖了会丢东西"的文件。
PROTECTED_FILES = ("config.json", "config.example.json", "config配置说明文件.txt")
PROTECTED_SUFFIXES = (".bak", ".pyc", ".log", ".db")

_USER_AGENT = "QQBotMerged-Updater/1.0 (+%s)" % version_module.REPO_URL


class UpdateError(RuntimeError):
    """更新过程中的可预期失败（网络/校验/安装），消息可直接给用户看。"""


# ---------------------------------------------------------------------------
# 状态机（前端轮询它显示进度）
# ---------------------------------------------------------------------------
class UpdateState:
    """线程安全的更新进度状态。"""

    STAGES = ("idle", "checking", "downloading", "extracting", "backup",
              "installing", "done", "failed")

    def __init__(self):
        self._lock = threading.Lock()
        self.stage = "idle"
        self.message = ""
        self.progress = 0          # 0..100
        self.tag = ""
        self.error = ""
        self.updated_at = 0.0

    def set(self, stage: str, message: str = "", progress: int = 0, tag: str = ""):
        with self._lock:
            self.stage = stage if stage in self.STAGES else "idle"
            self.message = message
            self.progress = max(0, min(100, int(progress)))
            if tag:
                self.tag = tag
            self.error = message if stage == "failed" else ""
            self.updated_at = time.time()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"stage": self.stage, "message": self.message,
                    "progress": self.progress, "tag": self.tag,
                    "error": self.error, "updated_at": self.updated_at,
                    "busy": self.stage in ("checking", "downloading", "extracting",
                                           "backup", "installing")}


# ---------------------------------------------------------------------------
# 网络：受控的 HTTPS 请求（逐跳校验协议与主机）
# ---------------------------------------------------------------------------
def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").strip().lower()
    except ValueError:
        return ""


def _check_url(url: str) -> None:
    """校验一个 URL 是否允许访问（HTTPS + 域名白名单）。"""
    try:
        parsed = urlparse(url or "")
    except ValueError as exc:
        raise UpdateError("更新地址格式不对：%s" % exc)
    if parsed.scheme != "https":
        raise UpdateError("更新只允许 HTTPS 地址（收到 %r）" % (parsed.scheme or "空"))
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise UpdateError("更新地址缺少主机名")
    for allowed in ALLOWED_UPDATE_HOSTS:
        if host == allowed or host.endswith("." + allowed):
            return
    raise UpdateError("更新地址不在允许范围内（%s），已拒绝" % host)


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    """跟随重定向，但每一跳都重新校验地址（防止 302 到内网/别的主机）。"""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener():
    opener = urllib.request.build_opener(_SafeRedirects())
    return opener


def _open_url(url: str, timeout: int = DOWNLOAD_TIMEOUT,
              headers: Optional[Dict[str, str]] = None):
    """打开一个已校验的 URL，返回 (响应对象, 最终 URL)。"""
    _check_url(url)
    request = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json, application/octet-stream, */*",
        **(headers or {}),
    })
    try:
        response = _build_opener().open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise UpdateError("请求 GitHub 失败：HTTP %s（%s）" % (exc.code, url))
    except urllib.error.URLError as exc:
        raise UpdateError("连不上 GitHub：%s（检查网络/代理）" % (exc.reason,))
    except (TimeoutError, OSError) as exc:
        raise UpdateError("请求 GitHub 超时或失败：%s" % (exc,))
    # 重定向后最终地址也要合法
    _check_url(getattr(response, "url", url) or url)
    return response


def _read_json(url: str, timeout: int = DOWNLOAD_TIMEOUT) -> Any:
    response = _open_url(url, timeout=timeout)
    try:
        raw = response.read(4 * 1024 * 1024)      # API 响应不会超过 4MB
    finally:
        response.close()
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise UpdateError("GitHub 返回的内容不是合法 JSON")


# ---------------------------------------------------------------------------
# 检查更新
# ---------------------------------------------------------------------------
class Updater:
    """更新检查与安装。

    `base_dir` 默认是程序根目录（`core/paths.BASE_DIR`）；
    `data_dir` 默认 `data/`，更新相关的临时文件、备份、标记都放这里，不污染代码目录。
    """

    def __init__(self, base_dir: str = "", data_dir: str = "",
                 logger_obj: Optional[logging.Logger] = None):
        self.base_dir = os.path.abspath(base_dir or paths.BASE_DIR)
        self.data_dir = os.path.abspath(data_dir or paths.DATA_DIR)
        self.log = logger_obj or logger
        self.update_dir = os.path.join(self.data_dir, "update")
        self.download_dir = os.path.join(self.update_dir, "downloads")
        self.staging_dir = os.path.join(self.update_dir, "staging")
        self.backup_dir = os.path.join(self.update_dir, "backup")
        self.cache_file = os.path.join(self.update_dir, "check_cache.json")
        self.marker_file = os.path.join(self.update_dir, "last_install.json")
        self.state = UpdateState()
        self._install_lock = threading.Lock()
        self.current_version = version_module.__version__

    # ------------------------------------------------------------ 工具
    def _ensure_dirs(self):
        for path in (self.update_dir, self.download_dir,
                     self.staging_dir, self.backup_dir):
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as exc:
                raise UpdateError("无法创建更新目录 %s：%s" % (path, exc))

    def _read_cache(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.cache_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _write_cache(self, payload: Dict[str, Any]):
        try:
            self._ensure_dirs()
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.cache_file)
        except OSError as exc:
            self.log.debug("写入更新缓存失败：%s", exc)

    def _cache_fresh(self, cached: Dict[str, Any]) -> bool:
        try:
            age = time.time() - float(cached.get("checked_at") or 0)
        except (TypeError, ValueError):
            return False
        return 0 <= age < CACHE_MINUTES * 60

    # ------------------------------------------------------------ 查询
    def _fetch_releases(self) -> List[Dict[str, Any]]:
        """优先用 releases，没有 release 就退回 tags。返回统一结构的候选列表。"""
        candidates: List[Dict[str, Any]] = []
        try:
            payload = _read_json(version_module.RELEASES_API + "?per_page=30")
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    if item.get("draft") or item.get("prerelease"):
                        continue
                    tag = str(item.get("tag_name") or "")
                    normalized = version_module.normalize_version(tag)
                    if not normalized:
                        continue
                    candidates.append({
                        "tag": tag,
                        "version": normalized,
                        "name": str(item.get("name") or tag),
                        "notes": str(item.get("body") or "")[:4000],
                        "published_at": str(item.get("published_at") or ""),
                        "url": str(item.get("html_url") or version_module.RELEASES_URL),
                        "tarball_url": str(item.get("tarball_url") or ""),
                        "source": "release",
                    })
        except UpdateError as exc:
            self.log.debug("读取 releases 失败（可能还没发过 release）：%s", exc)

        if not candidates:
            # 退回 tags：仓库只打 tag 没建 release 时用这条
            payload = _read_json(version_module.TAGS_API + "?per_page=50")
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    tag = str(item.get("name") or "")
                    normalized = version_module.normalize_version(tag)
                    if not normalized:
                        continue
                    candidates.append({
                        "tag": tag,
                        "version": normalized,
                        "name": tag,
                        "notes": "（该版本没有发布说明，只打了 tag）",
                        "published_at": "",
                        "url": version_module.RELEASES_URL,
                        "tarball_url": "",
                        "source": "tag",
                    })
        if not candidates:
            raise UpdateError("GitHub 上没有找到任何版本标签（tag）")
        candidates.sort(key=lambda item: version_module.version_tuple(item["version"]),
                        reverse=True)
        return candidates

    def check(self, force: bool = False) -> Dict[str, Any]:
        """检查是否有新版本。网络失败不抛异常，而是把 error 放进返回值。"""
        current = self.current_version
        result: Dict[str, Any] = {
            "current": current, "current_display": "v" + current,
            "latest": "", "latest_display": "", "has_update": False,
            "url": version_module.RELEASES_URL, "notes": "", "name": "",
            "published_at": "", "checked_at": time.time(),
            "cached": False, "error": "",
            "repo": version_module.REPO_SLUG,
        }
        if not force:
            cached = self._read_cache()
            if cached and self._cache_fresh(cached):
                merged = dict(cached)
                merged["current"] = current
                merged["current_display"] = "v" + current
                merged["cached"] = True
                merged["has_update"] = version_module.is_newer(
                    merged.get("latest") or "", current)
                if merged.get("latest"):
                    merged["latest_display"] = "v" + merged["latest"]
                return merged

        self.state.set("checking", "正在查询 GitHub…")
        try:
            candidates = self._fetch_releases()
            latest = candidates[0]
            result.update({
                "latest": latest["version"],
                "latest_display": "v" + latest["version"],
                "name": latest["name"],
                "notes": latest["notes"],
                "url": latest["url"],
                "published_at": latest["published_at"],
                "has_update": version_module.is_newer(latest["version"], current),
                "tag": latest["tag"],
                "source": latest["source"],
                "all_versions": [item["version"] for item in candidates[:10]],
            })
            self.state.set("idle", "检查完成", 100)
        except UpdateError as exc:
            result["error"] = str(exc)
            self.state.set("failed", str(exc))
            self.log.warning("检查更新失败：%s", exc)
            return result
        except Exception as exc:            # 兜底：绝不让检查更新把进程带崩
            result["error"] = "%s: %s" % (type(exc).__name__, exc)
            self.state.set("failed", result["error"])
            self.log.warning("检查更新异常：%s", exc)
            return result

        self._write_cache(result)
        return result

    # ------------------------------------------------------------ 下载
    def _tarball_url(self, tag: str, explicit: str = "") -> str:
        """决定下载地址：优先用 API 给的 tarball_url，否则按 codeload 规则拼。"""
        candidates = [explicit,
                      "https://codeload.github.com/%s/tar.gz/refs/tags/%s"
                      % (version_module.REPO_SLUG, tag),
                      version_module.REPO_URL + "/archive/refs/tags/" + tag + ".tar.gz"]
        for url in candidates:
            if not url:
                continue
            try:
                _check_url(url)
            except UpdateError:
                continue
            return url
        raise UpdateError("找不到合法的更新包下载地址（tag=%s）" % tag)

    def download(self, tag: str, tarball_url: str = "") -> Tuple[str, str]:
        """下载更新包，返回 (本地路径, sha256)。已下载过则直接复用。"""
        self._ensure_dirs()
        safe_tag = re.sub(r"[^A-Za-z0-9._-]", "_", tag or "")
        if not safe_tag:
            raise UpdateError("版本标签不合法")
        target = os.path.join(self.download_dir, "%s.tar.gz" % safe_tag)

        if os.path.isfile(target) and os.path.getsize(target) > 0:
            digest = self._sha256_of(target)
            self.log.info("复用已下载的更新包：%s", os.path.basename(target))
            return target, digest

        url = self._tarball_url(tag, tarball_url)
        self.state.set("downloading", "正在下载 %s…" % tag, 5, tag)
        response = _open_url(url)
        total = 0
        try:
            try:
                length = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length > MAX_DOWNLOAD_BYTES:
                raise UpdateError("更新包过大（%.1fMB > %.0fMB），已拒绝"
                                  % (length / 1048576, MAX_DOWNLOAD_BYTES / 1048576))
            digest = hashlib.sha256()
            tmp = target + ".part"
            with open(tmp, "wb") as handle:
                while True:
                    block = response.read(256 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise UpdateError("更新包超过上限（>%.0fMB），已中断"
                                          % (MAX_DOWNLOAD_BYTES / 1048576))
                    digest.update(block)
                    handle.write(block)
                    if length:
                        pct = 5 + int(total * 85 / max(length, 1))
                        self.state.set("downloading", "正在下载 %.1f/%.1fMB…"
                                       % (total / 1048576, length / 1048576),
                                       min(pct, 90), tag)
            if total == 0:
                raise UpdateError("下载到的更新包是空的")
            os.replace(tmp, target)
        finally:
            try:
                response.close()
            except Exception:
                pass
        sha = digest.hexdigest()
        self.log.info("更新包已下载：%s（%.1fMB, sha256=%s…）",
                      os.path.basename(target), total / 1048576, sha[:12])
        self.state.set("downloading", "下载完成", 90, tag)
        return target, sha

    @staticmethod
    def _sha256_of(path: str) -> str:
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            return ""
        return digest.hexdigest()

    # ------------------------------------------------------------ 解压
    @staticmethod
    def _safe_member_name(name: str) -> str:
        """把 tar 成员名规范化成相对路径；不安全的返回空串。"""
        raw = str(name or "").replace("\\", "/")
        if not raw or raw.startswith("/") or raw.startswith("~"):
            return ""
        if re.match(r"^[A-Za-z]:", raw):       # Windows 盘符
            return ""
        parts = [part for part in raw.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            return ""
        return "/".join(parts)

    def extract(self, archive: str, tag: str) -> str:
        """把 tar.gz 安全解压到 `staging/<tag>__<时间戳>/`，返回该目录。

        安全点：拒绝绝对路径/`..`/符号链接/硬链接/设备文件，限制成员数与总大小。
        GitHub 的 tarball 顶层会有一层 `QQBotMerged-<version>/`，这里自动剥掉。
        """
        self._ensure_dirs()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe_tag = re.sub(r"[^A-Za-z0-9._-]", "_", tag or "unknown")
        root = os.path.join(self.staging_dir, "%s__%s" % (safe_tag, stamp))
        os.makedirs(root, exist_ok=True)

        self.state.set("extracting", "正在解压…", 91, tag)
        total_bytes = 0
        members_seen = 0
        try:
            with tarfile.open(archive, "r:gz") as tar:
                entries = tar.getmembers()
                if len(entries) > MAX_MEMBERS:
                    raise UpdateError("压缩包内文件过多（%d > %d），已拒绝"
                                      % (len(entries), MAX_MEMBERS))
                # 先算一遍顶层目录，用来剥壳
                tops = {self._safe_member_name(item.name).split("/")[0]
                        for item in entries if self._safe_member_name(item.name)}
                strip_top = ""
                if len(tops) == 1:
                    only = next(iter(tops))
                    if all(self._safe_member_name(item.name).startswith(only + "/")
                           or self._safe_member_name(item.name) == only
                           for item in entries if self._safe_member_name(item.name)):
                        strip_top = only + "/"

                for item in entries:
                    members_seen += 1
                    relative = self._safe_member_name(item.name)
                    if not relative:
                        continue
                    if strip_top:
                        # 顶层目录自己的条目（`Pkg-1.2.7`，没有结尾斜杠）要整条丢掉，
                        # 否则会在解压目录里留下一个空的 `Pkg-1.2.7/`（实测踩到过）。
                        if relative == strip_top.rstrip("/"):
                            continue
                        if relative.startswith(strip_top):
                            relative = relative[len(strip_top):]
                    if not relative:
                        continue
                    # 只接受普通文件与目录：符号链接/硬链接/设备文件一律拒绝
                    if item.issym() or item.islnk():
                        raise UpdateError("更新包里有符号链接/硬链接（%s），已拒绝"
                                          % item.name)
                    if not (item.isfile() or item.isdir()):
                        continue
                    destination = os.path.join(root, *relative.split("/"))
                    # 双保险：规范化后必须仍在 root 里
                    if not os.path.abspath(destination).startswith(root + os.sep) \
                            and os.path.abspath(destination) != root:
                        raise UpdateError("更新包里有越界路径（%s），已拒绝" % item.name)
                    if item.isdir():
                        os.makedirs(destination, exist_ok=True)
                        continue
                    total_bytes += max(0, int(item.size or 0))
                    if total_bytes > MAX_EXTRACT_BYTES:
                        raise UpdateError("解压后体积超过上限（>%.0fMB），已中断"
                                          % (MAX_EXTRACT_BYTES / 1048576))
                    os.makedirs(os.path.dirname(destination) or root, exist_ok=True)
                    source = tar.extractfile(item)
                    if source is None:
                        continue
                    with source, open(destination, "wb") as handle:
                        shutil.copyfileobj(source, handle, 256 * 1024)
                    if members_seen % 50 == 0:
                        self.state.set("extracting", "正在解压（%d 个文件）…" % members_seen,
                                       91, tag)
        except UpdateError:
            shutil.rmtree(root, ignore_errors=True)
            raise
        except (tarfile.TarError, OSError) as exc:
            shutil.rmtree(root, ignore_errors=True)
            raise UpdateError("解压更新包失败：%s" % exc)

        self._validate_tree(root)
        self.state.set("extracting", "解压完成（%d 个文件）" % members_seen, 93, tag)
        return root

    def _validate_tree(self, root: str):
        """确认解压出来的东西真的像本项目（防止装进错包/空包）。"""
        missing = [name for name in REQUIRED_FILES
                   if not os.path.isfile(os.path.join(root, *name.split(os.sep)))]
        if missing:
            raise UpdateError("更新包内容不完整，缺少：%s（已拒绝安装）"
                              % "、".join(missing))

    # ------------------------------------------------------------ 备份 / 安装
    @staticmethod
    def _is_protected(relative: str) -> bool:
        """安装时是否要跳过这个相对路径（用户数据/本地配置绝不覆盖）。"""
        parts = [part for part in relative.replace("\\", "/").split("/") if part]
        if not parts:
            return True
        if parts[0] in PROTECTED_DIRS:
            return True
        name = parts[-1]
        if name in PROTECTED_FILES:
            return True
        if name.endswith(PROTECTED_SUFFIXES):
            return True
        return False

    def backup(self) -> str:
        """把当前代码打成一个 zip 备份（不含 data/、plugins/、.git/、配置）。"""
        self._ensure_dirs()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = os.path.join(self.backup_dir, "v%s_%s.zip"
                              % (self.current_version, stamp))
        self.state.set("backup", "正在备份当前版本…", 94)
        count = 0
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                for current_dir, dirnames, filenames in os.walk(self.base_dir):
                    relative_dir = os.path.relpath(current_dir, self.base_dir)
                    if relative_dir == ".":
                        relative_dir = ""
                    # 剪掉受保护目录，避免把 data/ 里的聊天记录打进备份
                    dirnames[:] = [name for name in dirnames
                                   if not self._is_protected(
                                       os.path.join(relative_dir, name))]
                    for name in filenames:
                        relative = os.path.join(relative_dir, name)
                        if self._is_protected(relative):
                            continue
                        full = os.path.join(current_dir, name)
                        try:
                            if os.path.islink(full) or os.path.getsize(full) > 8 * 1024 * 1024:
                                continue
                            archive.write(full, relative.replace("\\", "/"))
                            count += 1
                        except OSError:
                            continue
        except (OSError, zipfile.BadZipFile) as exc:
            raise UpdateError("备份当前版本失败：%s（已中止更新）" % exc)
        if count == 0:
            raise UpdateError("备份时没找到任何代码文件（程序目录不对？），已中止更新")
        self.log.info("已备份当前版本：%s（%d 个文件）", os.path.basename(target), count)
        self.state.set("backup", "备份完成（%d 个文件）" % count, 95)
        return target

    def _copy_tree(self, source_root: str, apply_delete: bool = False) -> int:
        """把 staging 里的文件覆盖到程序目录（跳过受保护路径）。返回写入文件数。"""
        written = 0
        for current_dir, dirnames, filenames in os.walk(source_root):
            relative_dir = os.path.relpath(current_dir, source_root)
            if relative_dir == ".":
                relative_dir = ""
            dirnames[:] = [name for name in dirnames
                           if not self._is_protected(os.path.join(relative_dir, name))]
            for name in filenames:
                relative = os.path.join(relative_dir, name)
                if self._is_protected(relative):
                    continue
                source = os.path.join(current_dir, name)
                destination = os.path.join(self.base_dir, relative)
                try:
                    os.makedirs(os.path.dirname(destination) or self.base_dir,
                                exist_ok=True)
                    # 先写临时文件再替换：避免"写一半断电"留下半个文件
                    temporary = destination + ".new"
                    shutil.copy2(source, temporary)
                    os.replace(temporary, destination)
                    written += 1
                except OSError as exc:
                    raise UpdateError("写入文件失败（%s）：%s" % (relative, exc))
                if written % 20 == 0:
                    self.state.set("installing", "正在写入（%d 个文件）…" % written, 96)
        return written

    def rollback(self, backup_zip: str) -> int:
        """用备份 zip 恢复代码（只覆盖备份里有的文件）。"""
        if not backup_zip or not os.path.isfile(backup_zip):
            raise UpdateError("找不到备份文件，无法回滚")
        restored = 0
        try:
            with zipfile.ZipFile(backup_zip) as archive:
                for member in archive.namelist():
                    relative = self._safe_member_name(member)
                    if not relative or self._is_protected(relative):
                        continue
                    destination = os.path.join(self.base_dir, *relative.split("/"))
                    if not os.path.abspath(destination).startswith(
                            os.path.abspath(self.base_dir) + os.sep):
                        continue
                    os.makedirs(os.path.dirname(destination) or self.base_dir,
                                exist_ok=True)
                    with archive.open(member) as source:
                        with open(destination, "wb") as handle:
                            shutil.copyfileobj(source, handle, 256 * 1024)
                    restored += 1
        except (OSError, zipfile.BadZipFile) as exc:
            raise UpdateError("回滚失败：%s（备份文件：%s）" % (exc, backup_zip))
        return restored

    def install(self, tag: str = "", tarball_url: str = "",
                source_root: str = "") -> Dict[str, Any]:
        """下载 → 解压 → 备份 → 覆盖。失败自动回滚。

        `source_root` 非空时跳过下载/解压（测试用，直接给一个准备好的目录）。
        """
        # 打包成 exe 时，代码在 exe 内部（`sys._MEIPASS` 是临时解包目录），
        # 把 .py 覆盖到 exe 旁边既不生效、又会在用户目录里留一堆散文件。
        # exe 版请手动下载新版本替换 exe；这里明确拒绝而不是装作成功。
        if paths.is_frozen():
            raise UpdateError("当前是打包版（exe），不能这样自我更新："
                              "请到 GitHub 下载新的 exe 替换旧文件")
        if not self._install_lock.acquire(blocking=False):
            raise UpdateError("已经有一个更新在进行中，请稍候")
        backup_zip = ""
        try:
            self._ensure_dirs()
            if source_root:
                staging = source_root
                self._validate_tree(staging)
                tag = tag or "local-test"
            else:
                if not tag:
                    raise UpdateError("没有指定要安装的版本")
                archive, sha = self.download(tag, tarball_url)
                self.log.info("开始安装 %s（sha256=%s…）", tag, (sha or "")[:12])
                staging = self.extract(archive, tag)

            backup_zip = self.backup()
            self.state.set("installing", "正在写入新文件…", 96, tag)
            written = self._copy_tree(staging)
            if written == 0:
                raise UpdateError("新版本里没有可写入的文件，已中止")

            marker = {
                "from_version": self.current_version,
                "to_version": version_module.normalize_version(tag) or tag,
                "tag": tag,
                "installed_at": time.time(),
                "backup": backup_zip,
                "written": written,
                "restart_required": True,
            }
            try:
                with open(self.marker_file, "w", encoding="utf-8") as handle:
                    json.dump(marker, handle, ensure_ascii=False, indent=2)
            except OSError as exc:
                self.log.debug("写入更新标记失败：%s", exc)

            self.state.set("done", "已更新到 %s，请重启程序" % tag, 100, tag)
            self.log.warning("=" * 60)
            self.log.warning("更新已安装：%s → %s（写入 %d 个文件）",
                             self.current_version, marker["to_version"], written)
            self.log.warning("**请重启程序让新版本生效**（备份：%s）", backup_zip)
            self.log.warning("=" * 60)
            self.cleanup(keep_backups=3)
            return {"success": True, "message": "已更新到 %s，请重启程序" % tag,
                    "from_version": self.current_version,
                    "to_version": marker["to_version"],
                    "written": written, "backup": backup_zip,
                    "restart_required": True}
        except UpdateError as exc:
            self.state.set("failed", str(exc))
            self.log.error("更新失败：%s", exc)
            if backup_zip:
                try:
                    restored = self.rollback(backup_zip)
                    self.log.warning("已回滚 %d 个文件到更新前状态", restored)
                    return {"success": False, "message": "%s（已回滚到更新前状态）" % exc,
                            "rolled_back": True, "backup": backup_zip}
                except UpdateError as rollback_exc:
                    self.log.error("回滚也失败：%s", rollback_exc)
                    return {"success": False,
                            "message": "%s；且回滚失败（%s），请手动用备份恢复：%s"
                                       % (exc, rollback_exc, backup_zip),
                            "rolled_back": False, "backup": backup_zip}
            return {"success": False, "message": str(exc), "rolled_back": False}
        finally:
            self._install_lock.release()

    # ------------------------------------------------------------ 杂项
    def last_install(self) -> Dict[str, Any]:
        """上一次安装的记录（用于提示"已更新，请重启"）。"""
        try:
            with open(self.marker_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def pending_restart(self) -> bool:
        """上次更新装的是不是"比当前运行的版本新"（= 需要重启才生效）。"""
        marker = self.last_install()
        target = str(marker.get("to_version") or "")
        if not target:
            return False
        return version_module.compare_versions(target, self.current_version) > 0

    def cleanup(self, keep_backups: int = 3):
        """清理旧的 staging 与备份（只留最近几个）。"""
        try:
            entries = sorted(
                (os.path.join(self.staging_dir, name)
                 for name in os.listdir(self.staging_dir)),
                key=lambda path: os.path.getmtime(path), reverse=True)
            for old in entries[1:]:       # 只留最新一个解压目录
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
        try:
            backups = sorted(
                (os.path.join(self.backup_dir, name)
                 for name in os.listdir(self.backup_dir)),
                key=lambda path: os.path.getmtime(path), reverse=True)
            for old in backups[max(1, keep_backups):]:
                try:
                    os.remove(old)
                except OSError:
                    pass
        except OSError:
            pass

    def status(self) -> Dict[str, Any]:
        """给前端的一份完整状态（**不联网**，只读内存状态 + 本地缓存）。"""
        snap = self.state.snapshot()
        # 把"上次检查的结果"一起带上：否则刷新页面后「最新版本」会退回"未检查"
        # （结果原本只存在前端内存里，一刷新就没了）。
        cached = self._read_cache() or {}
        last_check: Dict[str, Any] = {}
        if cached.get("latest"):
            last_check = {
                "latest": cached.get("latest") or "",
                "latest_display": cached.get("latest_display")
                                   or ("v" + str(cached.get("latest") or "")),
                "has_update": version_module.is_newer(
                    str(cached.get("latest") or ""), self.current_version),
                "name": cached.get("name") or "",
                "notes": cached.get("notes") or "",
                "url": cached.get("url") or version_module.RELEASES_URL,
                "published_at": cached.get("published_at") or "",
                "checked_at": cached.get("checked_at") or 0,
                "tag": cached.get("tag") or "",
                "cached": True,
            }
        snap.update({
            "current": self.current_version,
            "current_display": "v" + self.current_version,
            "repo": version_module.REPO_SLUG,
            "repo_url": version_module.REPO_URL,
            "pending_restart": self.pending_restart(),
            "last_install": self.last_install(),
            "last_check": last_check,
        })
        return snap
