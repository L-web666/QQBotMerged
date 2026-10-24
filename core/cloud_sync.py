# -*- coding: utf-8 -*-
"""云同步（Cloudflare D1）—— 安全重写版

原 `API_qqbot/core/cloud_sync.py` 依赖一个在 import 时**全局替换** `builtins.open` 与
`os.remove/rename/replace/stat` 的补丁模块（`deferred_writes.py`），用来实现"启动写缓存"。
那种做法副作用极大（整个进程的文件 IO 都被接管），排查问题时非常难受。

本实现改用**不污染全局**的方式达到同样目的：
- 同步的数据源集中在一个清单里（上下文、绑定、插件数据、统计、指令面板、群配置）；
- 每个文件记录 `(路径, 相对名, 修改时间, 大小, sha1)`，只有变化的才上传；
- 冲突规则：**谁的修改时间新谁赢**（云端更新则覆盖本地，本地更新则上传）；
- 删除用"墓碑"记录（`deleted=1` + 时间戳），不会把删掉的数据"复活"；
- 全部失败都会被记录并计数，连续失败会暂停同步一段时间，避免刷屏与无谓请求。

数据存在 D1 的一张表里：`bot_data(key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER, deleted INTEGER)`。
"""

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

D1_ENDPOINT = "https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{db}/query"

# 我们自己的 value 格式前缀。加上它以后，读到"没有前缀"的值就知道是别的程序写的。
VALUE_PREFIX = "b64:"
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")
# 文本类文件：解码后必须是合法 UTF-8（.json 还必须是合法 JSON）才允许覆盖本地
TEXT_SUFFIXES = (".json", ".txt", ".md", ".log", ".ini", ".cfg", ".csv", ".py", ".js",
                 ".html", ".css", ".xml", ".yaml", ".yml")

# 参与同步的文件/目录（相对程序根目录）
SYNC_PATHS: Tuple[str, ...] = (
    "data/user_context",
    # AstrBot 官方约定：插件把数据/大文件放在 data/plugin_data/<插件名>/ 下
    "data/plugin_data",
    # 注意：旧版用的 data/plugins_data/ 已废弃（启动时自动迁移到上面那个目录），
    # 不再参与同步，避免"云端恢复旧文件 → 又被迁移删掉"来回打架。
    # 也注意：`data/config/<插件>_config.json`（插件配置）**故意不同步** ——
    # 插件可以把 API Key 这类密钥写在那里，跟着上云风险太大。
    "data/plugins_disabled.json",
    "data/stats.json",
    "data/command_panel.json",
    "data/group_settings.json",
    "data/group_names.json",
)
# 永不上云（含密钥或纯运行时状态）
NEVER_SYNC = (
    "config.json",
    "config配置说明文件.txt",
    "data/logs",
    "data/merged.lock",
    "data/merged.pid",
    "data/messages.db",
    "data/media",
    ".cloud_sync_state.json",
)


def _sha1(path: str) -> str:
    digest = hashlib.sha1()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def encode_value(blob: bytes) -> str:
    """我们自己的云端格式：`b64:` + base64（二进制/文本都安全）。"""
    return VALUE_PREFIX + base64.b64encode(blob).decode("ascii")


def looks_like_base64(text: str) -> bool:
    """严格判断一段字符串像不像 base64（旧版合并程序写的就是裸 base64）。"""
    if not text or len(text) % 4 != 0:
        return False
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return False
    return bool(_BASE64_RE.match(text))


def decode_value(value: str) -> Tuple[bytes, str]:
    """把云端的 value 还原成字节，并告诉调用方它是什么格式。

    历史原因，同一张 `bot_data` 表里可能混着三种写法：
    - `b64:xxxx`       → 本程序现在的格式；
    - `xxxx`（裸 base64）→ 本程序早期版本写的；
    - 明文文本         → 原 `API_qqbot` 程序写的（它把文件内容直接当文本上传）。

    以前这里**无条件**按 base64 解码，于是旧程序的明文记录全部报错
    （"string argument should contain only ASCII characters" 等），
    个别"碰巧是合法 base64"的明文还会被解码成乱码覆盖掉本地文件。
    """
    text = value or ""
    if text.startswith(VALUE_PREFIX):
        body = text[len(VALUE_PREFIX):]
        body += "=" * (-len(body) % 4)
        return base64.b64decode(body), "b64"
    if looks_like_base64(text):
        try:
            return base64.b64decode(text, validate=True), "b64"
        except Exception:
            pass
    return text.encode("utf-8"), "text"


def is_text_like(rel: str) -> bool:
    return str(rel or "").lower().endswith(TEXT_SUFFIXES)


def check_text_payload(rel: str, blob: bytes) -> str:
    """文本类文件的安全检查：返回空串表示可以写，否则返回拒绝原因。"""
    if not is_text_like(rel):
        return ""
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        return f"内容不是合法的 UTF-8 文本（{exc}）"
    if rel.lower().endswith(".json") and text.strip():
        try:
            json.loads(text)
        except ValueError as exc:
            return f"内容不是合法的 JSON（{exc}）"
    return ""


# 上传前的"敏感字段"扫描：字段名像密钥的一律不往云端传（PROGRESS.md P1）。
# 只认字段名，不猜值 —— 聊天记录里出现 "api_key" 这个词不会被误伤。
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|api[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|app[_-]?secret|secret[_-]?key|private[_-]?key|"
    r"password|passwd|credential|authorization|bearer[_-]?token|session[_-]?key)")
# 纯文本行："api_key = xxxxxxxx" / "\"token\": xxxxxxxx"（值要够长，避免把说明文字当成密钥）
SENSITIVE_LINE_RE = re.compile(
    r"(?im)^\s*[\"']?(?P<key>[A-Za-z_][A-Za-z0-9_\-]{0,40})[\"']?\s*[:=]\s*[\"']?(?P<value>\S{12,})")


def find_sensitive_keys(rel: str, blob: bytes) -> List[str]:
    """扫文本/JSON 里"像密钥的字段名"，返回命中的键（空列表 = 可以上传）。

    - `.json`：递归只看 dict 的 key（`{"role":"user","content":"... api_key ..."}` 不会命中）；
    - 其它文本：只认 `api_key = <够长的值>` 这类"键 + 分隔符 + 值"的行；
    - 二进制/解码失败：不算命中（图片、sqlite 之类本来就不该按文本判定）。
    """
    if not is_text_like(rel):
        return []
    text = None
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return []
    if rel.lower().endswith(".json") and text.strip():
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            hits: List[str] = []

            def walk(node):
                if isinstance(node, dict):
                    for key, value in node.items():
                        if SENSITIVE_KEY_RE.search(str(key)):
                            hits.append(str(key))
                        walk(value)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)

            walk(data)
            return sorted(set(hits))
    hits = [match.group("key") for match in SENSITIVE_LINE_RE.finditer(text)
            if SENSITIVE_KEY_RE.search(match.group("key"))]
    return sorted(set(hits))


class D1Backend:
    """Cloudflare D1 的极简客户端（REST API）。"""

    def __init__(self, account_id: str, database_id: str, api_token: str, timeout: float = 30):
        self.account_id = (account_id or "").strip()
        self.database_id = (database_id or "").strip()
        self.api_token = (api_token or "").strip()
        self.timeout = timeout
        self.session = requests.Session()

    def configured(self) -> bool:
        return bool(self.account_id and self.database_id and self.api_token)

    def _url(self) -> str:
        return D1_ENDPOINT.format(account=self.account_id, db=self.database_id)

    def query(self, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        if not self.configured():
            raise RuntimeError("云同步未配置完整（account_id / database_id / api_token）")
        payload = {"sql": sql, "params": params or []}
        headers = {"Authorization": f"Bearer {self.api_token}",
                   "Content-Type": "application/json"}
        response = self.session.post(self._url(), json=payload, headers=headers,
                                     timeout=self.timeout)
        try:
            result = response.json()
        except ValueError:
            raise RuntimeError(f"云端返回非 JSON（HTTP {response.status_code}）")
        if response.status_code >= 400 or not result.get("success", True):
            errors = result.get("errors") or result.get("messages") or result
            raise RuntimeError(f"云端错误：{str(errors)[:300]}")
        payloads = result.get("result") or []
        if isinstance(payloads, list) and payloads:
            first = payloads[0]
            if isinstance(first, dict) and first.get("results") is not None:
                return first["results"] or []
        return payloads if isinstance(payloads, list) else []

    def ensure_table(self):
        self.query("""CREATE TABLE IF NOT EXISTS bot_data (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at INTEGER NOT NULL,
                        deleted INTEGER DEFAULT 0)""")

    def fetch_all(self) -> Dict[str, Dict[str, Any]]:
        rows = self.query("SELECT key, value, updated_at, deleted FROM bot_data")
        out = {}
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("key"):
                continue
            out[row["key"]] = {
                "value": row.get("value") or "",
                "updated_at": int(row.get("updated_at") or 0),
                "deleted": int(row.get("deleted") or 0),
            }
        return out

    def put(self, key: str, value: str, updated_at: int, deleted: int = 0):
        self.query(
            """INSERT INTO bot_data (key, value, updated_at, deleted) VALUES (?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at, deleted=excluded.deleted""",
            [key, value, int(updated_at), int(deleted)])


class CloudSync:
    """按间隔把本地数据同步到 D1，并把云端更新的数据拉回本地。"""

    def __init__(self, config_manager, logger_obj: logging.Logger = None):
        self.config_manager = config_manager
        self.log = logger_obj or logger
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._paused_until = 0.0
        self._fail_counts: Dict[str, int] = {}
        self.last_result: Dict[str, Any] = {}
        self.first_sync_done = False
        self.totals = {"uploaded": 0, "downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0}
        self._skipped_rels: List[str] = []
        self._sensitive_rels: List[str] = []

    # ------------------------------------------------------------------ 配置
    @property
    def config(self):
        return self.config_manager.config

    @property
    def skip_secret_files(self) -> bool:
        """含"密钥类字段"的文本/JSON 是否跳过上传（默认是：宁可少备份，也别把密钥传上云）。"""
        return self.config.bool_of("cloud_sync", "skip_secret_files", default=True)

    @property
    def enabled(self) -> bool:
        return self.config.bool_of("cloud_sync", "enabled", default=False)

    @property
    def interval(self) -> int:
        return max(30, self.config.int_of("cloud_sync", "interval_seconds", default=300))

    @property
    def max_file_bytes(self) -> int:
        return int(float(self.config.float_of("cloud_sync", "max_file_mb", default=10) or 10) * 1024 * 1024)

    @property
    def pause_minutes(self) -> int:
        return int(self.config.int_of("cloud_sync", "error_pause_minutes", default=30) or 0)

    @property
    def apply_remote_deletes(self) -> bool:
        """云端的删除标记要不要真的删本地文件（默认否，备份场景更安全）。"""
        return self.config.bool_of("cloud_sync", "apply_remote_deletes", default=False)

    def backend(self) -> Optional[D1Backend]:
        return D1Backend(
            self.config.str_of("cloud_sync", "account_id", default=""),
            self.config.str_of("cloud_sync", "database_id", default=""),
            self.config.str_of("cloud_sync", "api_token", default=""),
        )

    # ------------------------------------------------------------------ 文件清单
    def _iter_files(self) -> List[Tuple[str, str]]:
        """返回 [(绝对路径, 相对名)]。"""
        from core import paths
        out: List[Tuple[str, str]] = []
        for entry in SYNC_PATHS:
            full = os.path.join(paths.BASE_DIR, entry)
            if not os.path.exists(full):
                continue
            if os.path.isfile(full):
                out.append((full, entry.replace("\\", "/")))
                continue
            for root, _dirs, files in os.walk(full):
                for name in files:
                    if name.endswith((".tmp", ".lock")):
                        continue
                    file_path = os.path.join(root, name)
                    rel = os.path.relpath(file_path, paths.BASE_DIR).replace("\\", "/")
                    if self.is_blocked(rel):
                        continue
                    out.append((file_path, rel))
        return out

    @staticmethod
    def _key(rel: str) -> str:
        return rel

    @staticmethod
    def _norm(rel: str) -> str:
        return str(rel or "").replace("\\", "/").strip()

    def is_blocked(self, rel: str) -> bool:
        """这个相对路径是不是"永不同步"的（日志、锁文件、消息库、配置……）。"""
        rel = self._norm(rel)
        for blocked in NEVER_SYNC:
            blocked = self._norm(blocked)
            if rel == blocked or rel.startswith(blocked.rstrip("/") + "/"):
                return True
        return False

    def in_scope(self, rel: str) -> bool:
        """云端这条记录是不是本程序该管的文件。

        同一张 D1 表可能被别的程序（原 API_qqbot）共用，它会往里写日志、
        `system/instances/...` 之类的键；这些**绝不能拉回本地**，也不该算失败。
        """
        rel = self._norm(rel)
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            return False
        if self.is_blocked(rel):
            return False
        for entry in SYNC_PATHS:
            entry = self._norm(entry)
            if rel == entry or rel.startswith(entry.rstrip("/") + "/"):
                return True
        return False

    # ------------------------------------------------------------------ 同步
    def sync_once(self, force: bool = False) -> Dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "message": "云同步未启用"}
        if self.is_paused() and not force:
            info = self.pause_info()
            return {"ok": False, "message": info.get("resume_text") or "云同步已暂停"}
        backend = self.backend()
        if backend is None or not backend.configured():
            return {"ok": False, "message": "云同步凭据不完整（account_id / database_id / api_token）"}

        started = time.time()
        result = {"uploaded": 0, "downloaded": 0, "deleted": 0, "skipped": 0, "errors": 0,
                  "cloud_newer": 0, "sensitive": 0}
        try:
            backend.ensure_table()
            remote = backend.fetch_all()
        except Exception as exc:
            self.totals["errors"] += 1
            self.log.error("云同步失败（无法连接 D1）：%s", exc)
            self._note_failure("__connection__")
            return {"ok": False, "message": f"无法连接云端：{exc}"}

        local_files = self._iter_files()
        local_map = {rel: path for path, rel in local_files}

        with self._lock:
            self._skipped_rels = []
            self._sensitive_rels = []
            result["out_of_scope"] = 0
            # 1) 云端有、本地没有 → 下载（或按墓碑删除）
            for rel, item in remote.items():
                if not self.in_scope(rel):
                    # 别的程序（原 API_qqbot）写进来的日志、system/instances 等，一律不碰
                    result["out_of_scope"] += 1
                    result["skipped"] += 1
                    continue
                if rel not in local_map:
                    if item["deleted"]:
                        result["skipped"] += 1
                        continue
                    if len(item.get("value") or "") > self.max_file_bytes * 2:
                        # base64 后大约是原大小的 4/3，这里够用了；太大的先不拉
                        result["skipped"] += 1
                        self._note_skip(rel)
                        continue
                    outcome = self._download(backend, rel, item, only_if_newer=False)
                    if outcome == "ok":
                        result["downloaded"] += 1
                    elif outcome == "skip":
                        result["skipped"] += 1
                    else:
                        result["errors"] += 1

            # 2) 本地有的 → 比较后上传，或按云端更新覆盖本地
            for path, rel in local_files:
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if stat.st_size > self.max_file_bytes:
                    result["skipped"] += 1
                    continue
                local_mtime = int(stat.st_mtime * 1000)
                remote_item = remote.get(rel)
                if remote_item is None:
                    outcome = self._upload(backend, path, rel, local_mtime)
                    if outcome == "ok":
                        result["uploaded"] += 1
                    elif outcome == "sensitive":
                        result["sensitive"] += 1
                    else:
                        result["errors"] += 1
                    continue
                if remote_item["deleted"]:
                    # 云端墓碑比本地新 → 删除本地；否则本地复活并覆盖标记。
                    # `cloud_sync.apply_remote_deletes=false`（默认）时**不删本地**，
                    # 避免"备份场景下文件被云端删除牵连消失"（以前这个开关根本没被读）。
                    if not self.apply_remote_deletes:
                        if remote_item["updated_at"] > local_mtime:
                            result["skipped"] += 1
                            self._note_skip(rel)
                        else:
                            outcome = self._upload(backend, path, rel, local_mtime)
                            if outcome == "ok":
                                result["uploaded"] += 1
                            elif outcome == "sensitive":
                                result["sensitive"] += 1
                        continue
                    if remote_item["updated_at"] > local_mtime:
                        try:
                            os.remove(path)
                            result["deleted"] += 1
                        except OSError:
                            result["errors"] += 1
                    else:
                        outcome = self._upload(backend, path, rel, local_mtime)
                        if outcome == "ok":
                            result["uploaded"] += 1
                        elif outcome == "sensitive":
                            result["sensitive"] += 1
                    continue
                if remote_item["updated_at"] > local_mtime + 1000:
                    outcome = self._download(backend, rel, remote_item, only_if_newer=True)
                    if outcome == "ok":
                        result["downloaded"] += 1
                        result["cloud_newer"] += 1
                    elif outcome == "skip":
                        result["skipped"] += 1
                    else:
                        result["errors"] += 1
                elif local_mtime > remote_item["updated_at"] + 1000:
                    outcome = self._upload(backend, path, rel, local_mtime)
                    if outcome == "ok":
                        result["uploaded"] += 1
                    elif outcome == "sensitive":
                        result["sensitive"] += 1
                    else:
                        result["errors"] += 1
                else:
                    result["skipped"] += 1

        self.first_sync_done = True
        for key in ("uploaded", "downloaded", "deleted", "skipped", "errors"):
            self.totals[key] += result.get(key, 0)
        result["ok"] = result["errors"] == 0
        result["seconds"] = round(time.time() - started, 2)
        result["sensitive_files"] = list(self._sensitive_rels)
        result["message"] = (f"上传 {result['uploaded']}，恢复 {result['downloaded']}，"
                             f"删除 {result['deleted']}，跳过 {result['skipped']}，"
                             f"失败 {result['errors']}，用时 {result['seconds']} 秒")
        if result["sensitive"]:
            result["message"] += (f"（另有 {result['sensitive']} 个文件含密钥字段，"
                                  f"按设置没有上传）")
        result["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.last_result = result
        if result.get("out_of_scope"):
            self.log.info("云端另有 %d 条记录不属于本程序（其它程序写的日志/锁文件等），已跳过不算失败",
                          result["out_of_scope"])
        if result["sensitive"]:
            self.log.warning("有 %d 个文件含密钥字段、按设置没有上传：%s",
                             result["sensitive"], "；".join(result["sensitive_files"][:6]))
        if result["errors"]:
            self.log.warning("云同步完成但有失败：%s", result["message"])
        else:
            self.log.info("云同步完成：%s", result["message"])
        return result

    def _upload(self, backend: D1Backend, path: str, rel: str, mtime: int) -> str:
        """上传一个文件。返回 `ok` / `sensitive`（含密钥字段，故意没传）/ `fail`。"""
        try:
            with open(path, "rb") as handle:
                blob = handle.read()
            if self.skip_secret_files:
                hits = find_sensitive_keys(rel, blob)
                if hits:
                    # 只跳过上传，本地文件一个字节都不动；说清原因，别让人以为同步坏了
                    self.log.warning(
                        "跳过上传 %s：内容里有像密钥的字段（%s）。"
                        "本程序的 config.json 本来就不同步；插件数据里的密钥也不该上云。"
                        "如果这个文件确实需要备份，请在「设置 → 云同步」关掉「含密钥的文件不上传」",
                        rel, "、".join(hits[:6]))
                    self._note_skip(rel)
                    if len(self._sensitive_rels) < 20:
                        self._sensitive_rels.append(f"{rel}（{'、'.join(hits[:4])}）")
                    return "sensitive"
            backend.put(self._key(rel), encode_value(blob), mtime, 0)
            self._note_success(rel)
            self.log.debug("已上传 %s（%d 字节）", rel, len(blob))
            return "ok"
        except Exception as exc:
            self.log.warning("上传 %s 失败：%s", rel, exc)
            self._note_failure(rel)
            return "fail"

    def _download(self, backend: D1Backend, rel: str, item: Dict[str, Any],
                  only_if_newer: bool = False) -> str:
        """把云端内容写到本地。返回 `ok` / `skip`（内容不可信，故意不写）/ `fail`。"""
        from core import paths
        full = os.path.join(paths.BASE_DIR, rel)
        try:
            if only_if_newer and item["updated_at"] <= int(
                    os.stat(full).st_mtime * 1000 if os.path.exists(full) else 0):
                return "ok"
            blob, fmt = decode_value(item["value"] or "")
            # 文本/JSON 文件的安全闸：内容不合法就**绝不覆盖**本地，
            # 免得把"碰巧是合法 base64 的明文"（例如旧程序写的状态串）解码成乱码写进去
            problem = check_text_payload(rel, blob)
            if problem:
                self.log.warning("跳过恢复 %s：%s（云端这条记录不是本程序的文件内容）", rel, problem)
                self._note_skip(rel)
                return "skip"
            os.makedirs(os.path.dirname(full) or paths.BASE_DIR, exist_ok=True)
            tmp = full + ".tmp"
            with open(tmp, "wb") as handle:
                handle.write(blob)
            os.replace(tmp, full)
            self._note_success(rel)
            if fmt == "text":
                self.log.info("已从云端恢复 %s（%d 字节，旧程序的明文格式）", rel, len(blob))
            else:
                self.log.info("已从云端恢复 %s（%d 字节）", rel, len(blob))
            return "ok"
        except Exception as exc:
            self.log.warning("恢复 %s 失败：%s", rel, exc)
            self._note_failure(rel)
            return "fail"

    def mark_deleted(self, path: str):
        """本地删除文件后调用：写入云端墓碑，避免其它机器"复活"它。"""
        if not self.enabled:
            return
        from core import paths
        rel = os.path.relpath(path, paths.BASE_DIR).replace("\\", "/")
        backend = self.backend()
        if backend is None or not backend.configured():
            return
        try:
            backend.put(self._key(rel), "", int(time.time() * 1000), 1)
            self.totals["deleted"] += 1
        except Exception as exc:
            self.log.debug("写入删除标记失败：%s", exc)

    # ------------------------------------------------------------------ 暂停
    def _note_failure(self, rel: str):
        self._fail_counts[rel] = self._fail_counts.get(rel, 0) + 1
        if self.pause_minutes and self._fail_counts[rel] >= 3:
            self._paused_until = time.time() + self.pause_minutes * 60
            self.log.error("云同步已暂停 %d 分钟（%s 连续失败 %d 次）",
                           self.pause_minutes, rel, self._fail_counts[rel])

    def _note_success(self, rel: str):
        self._fail_counts.pop(rel, None)

    def _note_skip(self, rel: str):
        """跳过（不是失败）：记个名字，最后汇总成一行日志，避免刷屏。"""
        if len(self._skipped_rels) < 20:
            self._skipped_rels.append(rel)

    def is_paused(self) -> bool:
        return self._paused_until > time.time()

    def resume(self, why: str = "") -> Dict[str, Any]:
        self._paused_until = 0.0
        self._fail_counts.clear()
        self.log.info("云同步已恢复%s", f"（{why}）" if why else "")
        return {"ok": True, "message": "云同步已恢复"}

    def pause_info(self) -> Dict[str, Any]:
        remaining = max(0, int(self._paused_until - time.time()))
        return {
            "paused": self.is_paused(),
            "pause_reason": (f"连续失败，已暂停 {remaining // 60} 分 {remaining % 60} 秒"
                             if self.is_paused() else ""),
            "resume_text": ("可在网页「状态」页点『立即同步』强制重试" if self.is_paused() else ""),
            "fail_files": sorted(self._fail_counts.keys()),
            "interval": self.interval,
            "interval_text": f"每 {self.interval} 秒",
            "uploaded": self.totals["uploaded"],
            "downloaded": self.totals["downloaded"],
            "deleted": self.totals["deleted"],
            "errors": self.totals["errors"],
            "skipped": self.totals["skipped"],
            "first_sync_done": self.first_sync_done,
            "upload_logs": self.config.bool_of("cloud_sync", "upload_logs", default=False),
            "last_result": self.last_result,
        }

    # ------------------------------------------------------------------ 后台线程
    def start(self, immediate: bool = False):
        if not self.enabled:
            return
        if self._running:
            return
        self._running = True

        def loop():
            if immediate:
                first = self.sync_once()
                if first.get("ok"):
                    self.log.info("首次云同步完成：%s", first.get("message"))
            while self._running:
                time.sleep(max(5, min(self.interval, 30)))
                # 按配置间隔判断是否到点（用整数对齐，避免漂移）
                elapsed = time.time() - getattr(self, "_last_run", 0)
                if elapsed < self.interval:
                    continue
                self._last_run = time.time()
                if self.is_paused():
                    continue
                try:
                    self.sync_once()
                except Exception as exc:
                    self.log.warning("云同步异常：%s", exc)

        self._last_run = time.time()
        self._thread = threading.Thread(target=loop, name="cloud-sync", daemon=True)
        self._thread.start()
        self.log.info("云同步已启动（每 %d 秒，%s）", self.interval,
                      "启动时先拉取一次" if self.config.bool_of("cloud_sync", "pull_on_start",
                                                              default=True) else "不立即拉取")

    def stop(self, final_sync: bool = True):
        if not self._running:
            return
        self._running = False
        if final_sync and self.enabled:
            try:
                self.sync_once(force=True)
            except Exception as exc:
                self.log.debug("退出前同步失败：%s", exc)
        self.log.info("云同步已停止")

    # ------------------------------------------------------------------ 测试
    def test_connection(self) -> Dict[str, Any]:
        backend = self.backend()
        if backend is None or not backend.configured():
            return {"ok": False, "message": "请先填写 account_id / database_id / api_token"}
        try:
            backend.ensure_table()
            rows = backend.query("SELECT COUNT(*) AS n FROM bot_data")
            count = (rows[0].get("n") if rows and isinstance(rows[0], dict) else 0)
            return {"ok": True,
                    "message": f"连接成功，云端已有 {count} 条记录"}
        except Exception as exc:
            return {"ok": False, "message": f"连接失败：{exc}"}
