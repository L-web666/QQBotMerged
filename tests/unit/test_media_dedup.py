# -*- coding: utf-8 -*-
"""单元测试：媒体留存的重复保存问题（`core/media_store.py` + `core/storage.py`）

背景（用户报的问题）：几个机器人同在一个群里时，群里发一张图会被**重复保存好几份**。
根因有两层：
  ① 每个机器人都会单独收到这条消息，各自去下载；而每份下载地址都带各自的 rkey 签名，
     **URL 完全不同** —— 只按 `url_hash` 去重拦不住；
  ② 两条线程同时发现"还没存过"，然后各写一份文件（查重与落盘之间没有锁）。
所以现在去重改成按**内容 sha256**，并且把"查重 → 落盘 → 登记"这一段串行起来。

分层约定见 `PROGRESS.md`：`tests/unit/` 快、只依赖被测模块；不连网、不碰真实 `data/`
（临时文件只写 `data/_selftest/`）。

运行：`python tests/unit/test_media_dedup.py`
"""

import os
import shutil
import sqlite3
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core.media_store import MediaStore, content_hash                # noqa: E402
from core.storage import SQLiteStore                                 # noqa: E402

PASSED = []
FAILED = []

# 一张最小的合法 PNG（和 test_offline 用的同一份，1x1）
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082")
OTHER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000200000002080600000072b60d240000001349444154789c6360"
    "6080a2ff0c0c0c0033000100fd5b7cef0000000049454e44ae426082")


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


class FakeConfig:
    """只提供 MediaStore 会读到的几个配置项。"""

    def __init__(self, media_dir):
        self.media_dir = media_dir

    def str_of(self, section, key, default=""):
        if (section, key) == ("storage", "media_dir"):
            return self.media_dir
        return default

    def abs_path(self, path):
        return path

    def bool_of(self, section, key, default=False):
        return default

    def int_of(self, section, key, default=0):
        return default

    def float_of(self, section, key, default=0.0):
        return default


class FakeResponse:
    def __init__(self, blob, content_type="image/png"):
        self.blob = blob
        self.status_code = 200
        self.headers = {"Content-Type": content_type}

    def iter_content(self, size=65536):
        yield self.blob

    def close(self):
        pass


class FakeSession:
    """按 URL 返回不同内容（默认都是 TINY_PNG，模拟"同一张图、不同签名地址"）。"""

    def __init__(self, blobs=None, default=None):
        self.blobs = blobs or {}
        self.default = default if default is not None else TINY_PNG
        self.calls = []
        self.lock = threading.Lock()

    def get(self, url, **kwargs):
        with self.lock:
            self.calls.append(url)
        return FakeResponse(self.blobs.get(url, self.default))


def make_store(root, name, opened):
    db_path = os.path.join(root, f"{name}.db")
    store = SQLiteStore(db_path)
    opened.append(store)
    return store


def media_files(media_dir):
    return sorted(name for name in os.listdir(media_dir) if not name.endswith(".json"))


def main():
    print("=" * 70)
    print("  单元测试：同一张图片只保存一次（多机器人同群）")
    print("=" * 70)

    test_root = os.path.join(PROJECT, "data", "_selftest")
    os.makedirs(test_root, exist_ok=True)
    run_id = str(int(time.time() * 1000) % 100000000)
    sandbox = os.path.join(test_root, f"unit_media_{run_id}")
    shutil.rmtree(sandbox, ignore_errors=True)
    os.makedirs(sandbox, exist_ok=True)
    opened = []

    try:
        media_dir = os.path.join(sandbox, "media")
        os.makedirs(media_dir, exist_ok=True)
        store = make_store(sandbox, "dedup", opened)
        session = FakeSession()
        media = MediaStore(store, FakeConfig(media_dir), session=session)

        # ---------- 1. 老问题复现：两个机器人、不同 URL、同一张图 ----------
        print("\n[1] 两个机器人同一张图（URL 带各自 rkey，内容相同）")
        url_a = "https://multimedia.nt.qq.com.cn/download?appid=1&fileid=abc&rkey=AAA"
        url_b = "https://multimedia.nt.qq.com.cn/download?appid=1&fileid=abc&rkey=BBB"
        group_key = "BOT_A:group:GROUP_1"
        rec_a = media.download(url_a, file_name="pic.png", source="received",
                               bot_id="BOT_A", conv_key=group_key, msg_id="M1")
        rec_b = media.download(url_b, file_name="pic.png", source="received",
                               bot_id="BOT_B", conv_key="BOT_B:group:GROUP_1", msg_id="M1")
        check("两个机器人都拿到了可用的本地地址",
              bool((rec_a or {}).get("local_url")) and bool((rec_b or {}).get("local_url")),
              f"{rec_a and rec_a.get('local_url')} / {rec_b and rec_b.get('local_url')}")
        check("第二个机器人复用的是同一份文件（没有 _1 副本）",
              rec_a["local_name"] == rec_b["local_name"],
              f"{rec_a['local_name']} vs {rec_b['local_name']}")
        check("磁盘上只有 1 个文件", len(media_files(media_dir)) == 1, str(media_files(media_dir)))
        check("媒体表里只有 1 条记录（媒体页不会重复）",
              len(store.list_media(limit=50, state="done")) == 1,
              str([item["local_name"] for item in store.list_media(limit=50)]))
        check("命中的是复用标记", rec_b.get("reused") is True, str(rec_b.get("reused")))

        # ---------- 2. 三个机器人 + 并发 ----------
        print("\n[2] 三个机器人几乎同时收到（并发也不能各存一份）")
        media2_dir = os.path.join(sandbox, "media2")
        os.makedirs(media2_dir, exist_ok=True)
        store2 = make_store(sandbox, "concurrent", opened)
        media2 = MediaStore(store2, FakeConfig(media2_dir), session=FakeSession())
        errors = []
        results = []

        def worker(index):
            try:
                results.append(media2.download(
                    f"https://multimedia.nt.qq.com.cn/download?fileid=same&rkey=R{index}",
                    file_name="group_pic.jpg", source="received",
                    bot_id=f"BOT_{index}", conv_key=f"BOT_{index}:group:G", msg_id="M9"))
            except Exception as exc:  # pragma: no cover - 出错也要让用例给出原因
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        check("并发下载没有抛异常", not errors, str(errors))
        check("并发下磁盘上也只有 1 个文件",
              len(media_files(media2_dir)) == 1, str(media_files(media2_dir)))
        check("并发下媒体表也只有 1 条记录",
              len(store2.list_media(limit=50, state="done")) == 1,
              str(len(store2.list_media(limit=50, state="done"))))
        check("4 个机器人拿到的都是同一个文件名",
              len({item["local_name"] for item in results if item}) == 1,
              str([item and item.get("local_name") for item in results]))

        # ---------- 3. 不同内容不能被误判成重复 ----------
        print("\n[3] 内容不同 → 该存两份就存两份")
        other_url = "https://multimedia.nt.qq.com.cn/download?fileid=other&rkey=CCC"
        session.blobs[other_url] = OTHER_PNG
        rec_c = media.download(other_url,
                               file_name="pic.png", source="received", bot_id="BOT_A",
                               conv_key=group_key, msg_id="M2")
        check("不同内容换了一份文件", rec_c["local_name"] != rec_a["local_name"],
              f"{rec_a['local_name']} vs {rec_c['local_name']}")
        check("同名不同内容 → 后一份加 _1 后缀（老行为不变）",
              rec_c["local_name"] == "pic_1.png", rec_c["local_name"])
        check("磁盘上现在是 2 个文件", len(media_files(media_dir)) == 2, str(media_files(media_dir)))

        # ---------- 4. 同一个 URL 重复下载 → 走 URL 层去重，不再发起请求 ----------
        print("\n[4] 同一个 URL 再来一次：直接复用，不重复下载")
        before = len(session.calls)
        rec_d = media.download(url_a, file_name="pic.png", source="received", bot_id="BOT_A",
                               conv_key=group_key, msg_id="M3")
        check("同 URL 不重复请求远端", len(session.calls) == before, f"{before} -> {len(session.calls)}")
        check("同 URL 复用同一个文件", rec_d["local_name"] == rec_a["local_name"],
              f"{rec_d['local_name']} vs {rec_a['local_name']}")

        # ---------- 5. 文件被手工删掉后，不能复用一条空记录 ----------
        print("\n[5] 文件被删掉之后不能「假复用」")
        os.remove(os.path.join(media_dir, rec_a["local_name"]))
        rec_e = media.download(url_a, file_name="pic.png", source="received", bot_id="BOT_A",
                               conv_key=group_key, msg_id="M4")
        check("文件不在了就重新下载一份", rec_e and os.path.isfile(
            os.path.join(media_dir, rec_e["local_name"])), str(rec_e and rec_e["local_name"]))

        # ---------- 6. 老库自动补 sha256 列 ----------
        print("\n[6] 老数据库（没有 sha256 列）能自动升级")
        old_db = os.path.join(sandbox, "old.db")
        legacy = sqlite3.connect(old_db)
        legacy.execute("""CREATE TABLE media (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT DEFAULT '', conv_key TEXT DEFAULT '',
            msg_id TEXT DEFAULT '', source TEXT DEFAULT 'received', url TEXT DEFAULT '',
            url_hash TEXT DEFAULT '', local_name TEXT DEFAULT '', file_name TEXT DEFAULT '',
            content_type TEXT DEFAULT '', size INTEGER DEFAULT 0, state TEXT DEFAULT 'done',
            error TEXT DEFAULT '', created_at REAL DEFAULT 0)""")
        legacy.execute("INSERT INTO media (url_hash, local_name, state) VALUES ('h1','old.png','done')")
        legacy.commit()
        legacy.close()
        upgraded = SQLiteStore(old_db)
        opened.append(upgraded)
        columns = {row["name"] for row in upgraded._conn.execute("PRAGMA table_info(media)")}
        check("老库补上了 sha256 列", "sha256" in columns, str(sorted(columns)))
        check("老记录还在", bool(upgraded.list_media(limit=5)), "")
        check("按 sha256 查询可用（老记录指纹为空，不该被误匹配）",
              upgraded.find_media_by_sha256("") is None
              and upgraded.find_media_by_sha256(content_hash(TINY_PNG)) is None, "")
        upgraded.add_media({"sha256": content_hash(TINY_PNG), "local_name": "new.png",
                            "state": "done", "created_at": time.time()})
        check("新记录能按内容指纹查到",
              (upgraded.find_media_by_sha256(content_hash(TINY_PNG)) or {}).get("local_name") == "new.png",
              "")

        # ---------- 7. 下载失败仍然登记 failed（老行为不变） ----------
        print("\n[7] 下载失败：登记 failed 记录，不生成文件")
        store3 = make_store(sandbox, "failed", opened)
        media3_dir = os.path.join(sandbox, "media3")
        os.makedirs(media3_dir, exist_ok=True)

        class BrokenSession:
            def get(self, url, **kwargs):
                raise RuntimeError("假装网络断了")

        media3 = MediaStore(store3, FakeConfig(media3_dir), session=BrokenSession())
        check("失败返回 None",
              media3.download("https://multimedia.nt.qq.com.cn/download?fileid=x&rkey=y",
                              file_name="x.png", bot_id="BOT_A") is None, "")
        failed = store3.list_media(limit=10, state="failed")
        check("登记了 failed 记录", len(failed) == 1, str(len(failed)))
        check("失败时没有留下文件", not media_files(media3_dir), str(media_files(media3_dir)))
    finally:
        # Windows 上文件被 sqlite 连接占着会删不掉，先关连接再清目录
        for store in opened:
            try:
                store.close()
            except Exception:
                pass
        shutil.rmtree(sandbox, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
