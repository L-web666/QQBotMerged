# -*- coding: utf-8 -*-
"""单元测试：v1.3.5 安全修复（CSRF 同源判定 / 令牌失败拒绝启动 / 审计取证 / 更新回滚）

对应审计报告 SEC-02 / SEC-05 / SEC-06 / SEC-07 / SEC-08 / SEC-09 与 NEW5-01。

运行：`python tests/unit/test_security_v135.py`
"""

import json
import os
import shutil
import sys
import tarfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import audit_log, config_schema as schema, paths   # noqa: E402
from core.config_manager import load_config                  # noqa: E402
from core.updater import UpdateError, Updater                # noqa: E402
from web.server import create_app                            # noqa: E402

PASSED = []
FAILED = []
TOKEN = "Xk9mQ2pL7vR4nT8wZ1bYc3dE"


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def make_client(root, trust_proxy=False, token=TOKEN):
    cfg = schema.default_config()
    cfg["web"].update({"host": "127.0.0.1", "token": token, "port": 8666,
                       "trust_proxy": trust_proxy})
    cfg["update"]["check_on_start"] = False
    path = os.path.join(root, "cfg_%s_%d.json" % (trust_proxy, int(time.time() * 1000) % 100000))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, ensure_ascii=False)
    return create_app(load_config(path)).test_client()


def main():
    print("=" * 70)
    print("  v1.3.5 安全修复")
    print("=" * 70)

    root = os.path.join(PROJECT, "data", "_selftest",
                        "unit_v135_%d" % int(time.time() * 1000))
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    original_log = paths.LOG_DIR

    try:
        paths.LOG_DIR = os.path.join(root, "logs")
        os.makedirs(paths.LOG_DIR, exist_ok=True)
        client = make_client(root)

        # ================= SEC-05 / NEW5-01：同源判定 =================
        print("\n[1] CSRF 同源判定改为比较完整 origin（scheme+host+port）")
        # test_client 的 Host 是 localhost（无端口），同源对照必须与之一致
        def probe(headers):
            return client.post("/api/admin/maintenance?token=" + TOKEN,
                               headers=headers, data="{}").status_code

        check("同源 Origin 放行", probe({"Origin": "http://localhost"}) != 403, "")
        check("Sec-Fetch-Site: same-origin 放行",
              probe({"Sec-Fetch-Site": "same-origin"}) != 403, "")

        print("\n[2] same-site 不再被无条件信任")
        check("same-site + 外部 Origin 被拒",
              probe({"Sec-Fetch-Site": "same-site",
                     "Origin": "https://evil.example.com"}) == 403, "")
        check("same-site 无 Origin 被拒",
              probe({"Sec-Fetch-Site": "same-site"}) == 403, "")
        check("cross-site 被拒",
              probe({"Sec-Fetch-Site": "cross-site",
                     "Origin": "https://evil.example.com"}) == 403, "")

        print("\n[3] 端口与协议必须一致（旧实现是死代码）")
        check("同主机不同端口被拒",
              probe({"Origin": "http://localhost:9999"}) == 403, "")
        check("同主机不同协议被拒",
              probe({"Origin": "https://localhost"}) == 403, "")

        print("\n[4] 非浏览器客户端仍可调用（无来源头）")
        check("无 Origin / 无 Sec-Fetch 放行", probe({}) != 403, "")

        # ================= SEC-09：审计不信任 XFF =================
        print("\n[5] 审计日志不再无条件信任 X-Forwarded-For")
        client.post("/api/admin/maintenance",
                    headers={"X-Forwarded-For": "1.2.3.4",
                             "Origin": "https://evil.example.com",
                             "Sec-Fetch-Site": "cross-site"}, data="{}")
        entries = audit_log.read_tail(20)
        check("伪造的 IP 没有写进审计",
              not [e for e in entries if "1.2.3.4" in str(e.get("client") or "")], "")
        check("审计记录了真实来源",
              any("127.0.0.1" in str(e.get("client") or "") or "localhost"
                  in str(e.get("client") or "") for e in entries), "")

        class FakeReq:
            def __init__(self, remote, xff=None):
                self.remote_addr = remote
                self.headers = {"X-Forwarded-For": xff} if xff else {}

        check("client_of: 不信代理时忽略 XFF",
              audit_log.client_of(FakeReq("127.0.0.1", "9.9.9.9"),
                                  trust_proxy=False) == "127.0.0.1", "")
        check("client_of: 信任代理时采用 XFF",
              audit_log.client_of(FakeReq("127.0.0.1", "9.9.9.9"),
                                  trust_proxy=True) == "9.9.9.9", "")

        # ================= SEC-02：令牌失败拒绝启动 =================
        print("\n[6] 令牌生成失败必须能被调用方识别（不再是静默继续）")
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "run_module", os.path.join(PROJECT, "run.py"))
        run_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_module)

        blank = os.path.join(root, "blank.json")
        cfg = schema.default_config()
        cfg["web"]["token"] = ""
        with open(blank, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False)
        token_value, state = run_module.ensure_access_token(load_config(blank))
        check("空令牌 → 状态 created", state[0] == "created", str(state))
        check("生成了令牌", bool(token_value), "")

        reloaded = load_config(blank)
        again, state2 = run_module.ensure_access_token(reloaded)
        check("已有令牌 → 状态 existing", state2[0] == "existing", str(state2))

        class Broken:
            class config:
                @staticmethod
                def str_of(*a, **k):
                    return ""

                @staticmethod
                def int_of(*a, **k):
                    return 8666

            @staticmethod
            def set_path(*a, **k):
                pass

            @staticmethod
            def save():
                raise PermissionError("模拟只读目录")

        failed_value, failed_state = run_module.ensure_access_token(Broken())
        check("保存失败 → 状态 failed", failed_state[0] == "failed", str(failed_state))
        check("保存失败 → 令牌为空", failed_value == "", "")
        check("失败原因被带回", bool(failed_state[1]), str(failed_state))

        print("\n[7] 源码层面：启动处检查了返回值")
        source = open(os.path.join(PROJECT, "run.py"), encoding="utf-8").read()
        check("调用处会看状态", "token_state[0] == \"failed\"" in source, "")
        check("失败时返回非零退出码", "return 2" in source, "")
        check("提供 --allow-no-token 显式放行", "--allow-no-token" in source, "")

        # ================= SEC-07：回滚删除新增文件 =================
        print("\n[8] 更新回滚会删除本次新增的文件")
        live = os.path.join(root, "live")
        os.makedirs(os.path.join(live, "core"), exist_ok=True)
        os.makedirs(os.path.join(live, "web", "templates"), exist_ok=True)
        for relative, content in {
                "run.py": "# OLD\n",
                os.path.join("core", "__init__.py"): "# OLD\n",
                os.path.join("web", "templates", "index.html"): "OLD"} .items():
            full = os.path.join(live, relative)
            os.makedirs(os.path.dirname(full) or live, exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(content)
        new_tree = os.path.join(root, "new")
        os.makedirs(os.path.join(new_tree, "core"), exist_ok=True)
        os.makedirs(os.path.join(new_tree, "web", "templates"), exist_ok=True)
        for relative, content in {
                "run.py": "# NEW\n",
                os.path.join("core", "__init__.py"): "# NEW\n",
                os.path.join("web", "templates", "index.html"): "NEW",
                os.path.join("core", "added_by_update.py"): "# new file\n"}.items():
            full = os.path.join(new_tree, relative)
            os.makedirs(os.path.dirname(full) or new_tree, exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(content)

        upd = Updater(base_dir=live, data_dir=os.path.join(root, "udata"))
        result = upd.install(tag="v9.9.9", source_root=new_tree)
        check("安装成功", result.get("success") is True, str(result))
        added = os.path.join(live, "core", "added_by_update.py")
        check("新增文件已写入", os.path.isfile(added), "")
        upd.rollback(result.get("backup"))
        check("回滚后新增文件被删除", not os.path.isfile(added), "")
        check("回滚后原文件恢复",
              "OLD" in open(os.path.join(live, "run.py"), encoding="utf-8").read(), "")

        # ================= SEC-08：成员数检查提前 =================
        print("\n[9] 解压时成员数超限要尽早拒绝（不再先 getmembers 全读入）")
        upd_src = open(os.path.join(PROJECT, "core", "updater.py"), encoding="utf-8").read()
        check("extract 里不再直接 getmembers()",
              "entries = tar.getmembers()" not in upd_src, "")
        check("改成逐个迭代 tar", "for item in tar:" in upd_src, "")

        big = os.path.join(root, "many.tar.gz")
        with tarfile.open(big, "w:gz") as tar:
            for index in range(30):
                data = b"x"
                info = tarfile.TarInfo("pkg/f%d.txt" % index)
                info.size = len(data)
                import io as _io
                tar.addfile(info, _io.BytesIO(data))
        import core.updater as updater_module
        old_limit = updater_module.MAX_MEMBERS
        updater_module.MAX_MEMBERS = 10
        blocked_updater = Updater(base_dir=root, data_dir=os.path.join(root, "dl"))
        try:
            try:
                blocked_updater.extract(big, "v9.9.9")
                rejected = False
            except UpdateError as exc:
                rejected = "过多" in str(exc)
            check("超过成员上限时被拒绝", rejected, "")
        finally:
            updater_module.MAX_MEMBERS = old_limit
    finally:
        paths.LOG_DIR = original_log
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
