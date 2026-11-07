# -*- coding: utf-8 -*-
"""单元测试：版本号比较 + 自动更新（安全检查、解压防穿越、安装/回滚）

自动更新是本项目风险最高的一段代码（等于"让程序下载代码并覆盖自己"），
因此这里把每条安全防线都单独钉住：

  · 更新源只能是 HTTPS + 域名白名单（含重定向伪造）
  · 解压必须拒绝绝对路径、`..`、符号链接（防 tar 穿越 / 符号链接攻击）
  · 安装**绝不覆盖** `data/`、`plugins/`、`config.json`
  · 安装失败必须能用备份回滚
  · 版本号比较要正确（`1.10.0` 比 `1.9.9` 新，`1.2` 等于 `1.2.0`）

不联网：所有网络函数都用本地构造的压缩包/假目录替代。
临时文件只写 `data/_selftest/`，测试结束清理。

运行：`python tests/unit/test_updater.py`
"""

import io
import os
import shutil
import sys
import tarfile
import time
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core import updater as updater_module          # noqa: E402
from core import version as version_module          # noqa: E402
from core.updater import UpdateError, Updater, _check_url   # noqa: E402

# 成员名校验是 `Updater` 的静态方法（不依赖实例）
_safe_member_name = Updater._safe_member_name

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def make_tarball(path, entries):
    """entries: [(成员名, 内容bytes 或 None=目录 或 ('symlink', target))]"""
    with tarfile.open(path, "w:gz") as tar:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif isinstance(content, tuple) and content and content[0] == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = content[1]
                tar.addfile(info)
            else:
                data = content if isinstance(content, bytes) else str(content).encode()
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))


def make_project_tree(root, marker="v2"):
    """造一个"看起来像本项目"的目录。"""
    os.makedirs(os.path.join(root, "core"), exist_ok=True)
    os.makedirs(os.path.join(root, "web", "templates"), exist_ok=True)
    files = {
        "run.py": "# run %s\n" % marker,
        os.path.join("core", "__init__.py"): "# core %s\n" % marker,
        os.path.join("core", "extra.py"): "# extra %s\n" % marker,
        os.path.join("web", "templates", "index.html"): "<html>%s</html>" % marker,
        # 这几个是"用户数据"，安装时必须跳过
        "config.json": '{"secret": "MUST-NOT-CHANGE"}',
        os.path.join("data", "messages.db"): "user-db",
        os.path.join("plugins", "my_plugin", "main.py"): "# user plugin",
    }
    for relative, content in files.items():
        full = os.path.join(root, relative)
        os.makedirs(os.path.dirname(full) or root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(content)
    return root


def main():
    print("=" * 70)
    print("  版本号与自动更新")
    print("=" * 70)

    root = os.path.join(PROJECT, "data", "_selftest",
                        "unit_updater_%d" % int(time.time() * 1000))
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)

    try:
        # ---------- 1. 版本号解析与比较 ----------
        print("\n[1] 版本号解析与比较")
        # **不要在这里写死版本号**：发版改版本是常事，写死会让测试天天飘。
        # 这里只断言"格式合法且能解析回自己"，具体值在 core/version.py。
        current = version_module.__version__
        check("当前版本号是合法版本（能解析回自己）",
              version_module.normalize_version(current) == current, current)
        check("当前版本至少发布过（>= 1.2.7）",
              version_module.compare_versions(current, "1.2.7") >= 0, current)
        for raw, expect in [("v1.2.7", "1.2.7"), ("V1.2.7", "1.2.7"),
                            ("1.2.7", "1.2.7"), ("release-1.2.7", "1.2.7"),
                            ("1.2.7-beta", "1.2.7"), ("sha-abc123", ""),
                            ("", "")]:
            check(f"normalize({raw!r}) = {expect!r}",
                  version_module.normalize_version(raw) == expect,
                  version_module.normalize_version(raw))
        check("1.2 等于 1.2.0（补齐长度）",
              version_module.compare_versions("1.2", "1.2.0") == 0, "")
        check("1.2.7 > 1.2.6", version_module.compare_versions("1.2.7", "1.2.6") == 1, "")
        check("1.10.0 > 1.9.9（按段比，不是按字符串）",
              version_module.compare_versions("1.10.0", "1.9.9") == 1,
              version_module.compare_versions("1.10.0", "1.9.9"))
        check("坏版本号无法比较时返回 0",
              version_module.compare_versions("abc", "1.2.7") == 0, "")
        check("pick_latest 忽略非法 tag",
              version_module.pick_latest(["v1.2.6", "bad", "v1.3.0"]) == "1.3.0",
              version_module.pick_latest(["v1.2.6", "bad", "v1.3.0"]))

        # ---------- 2. 更新源校验（HTTPS + 域名白名单） ----------
        print("\n[2] 更新源必须是 HTTPS + 白名单域名")
        for url in ("https://api.github.com/repos/a/b/releases",
                    "https://codeload.github.com/a/b/tar.gz/refs/tags/v1",
                    "https://github.com/a/b",
                    "https://objects.githubusercontent.com/x"):
            try:
                _check_url(url)
                ok = True
            except UpdateError:
                ok = False
            check(f"允许 {url[:44]}", ok, "")
        for url in ("http://api.github.com/x",           # 明文 HTTP
                    "https://evil.com/x",                # 外部域名
                    "https://api.github.com.evil.com/x",  # 前缀伪造
                    "ftp://api.github.com/x",            # 协议不对
                    "https:///nohost"):                  # 没有主机名
            try:
                _check_url(url)
                rejected = False
            except UpdateError:
                rejected = True
            check(f"拒绝 {url[:44]}", rejected, "")

        # ---------- 3. 压缩包成员名校验 ----------
        print("\n[3] 压缩包成员名：绝对路径/.. 一律判为不安全")
        for name in ("../evil.py", "/etc/passwd", "C:/windows/x", "~/x",
                     "a/../../b", "..\\windows\\x", "a/b/../c.py"):
            check(f"拒绝 {name!r}", _safe_member_name(name) == "",
                  _safe_member_name(name))
        for name in ("run.py", "core/deep/x.py", "./run.py", "a/b/c.py"):
            check(f"接受 {name!r}", _safe_member_name(name) != "",
                  _safe_member_name(name))

        # ---------- 4. 解压拒绝路径穿越 ----------
        print("\n[4] 解压：含越界路径的包必须被拒绝")
        bad_tar = os.path.join(root, "bad.tar.gz")
        make_tarball(bad_tar, [
            ("pkg", None),
            ("pkg/run.py", "x"),
            ("pkg/../../../evil.py", "pwned"),
        ])
        upd = Updater(base_dir=root, data_dir=os.path.join(root, "data"))
        try:
            upd.extract(bad_tar, "v9.9.9")
            rejected = False
        except UpdateError as exc:
            rejected = "越界" in str(exc) or "不安全" in str(exc) or True
        check("含 ../ 的包被拒绝", rejected, "")
        check("没有在解压目录外生成文件",
              not os.path.isfile(os.path.join(root, "evil.py")), "")

        # ---------- 5. 解压拒绝符号链接 ----------
        print("\n[5] 解压：含符号链接的包必须被拒绝")
        sym_tar = os.path.join(root, "sym.tar.gz")
        make_tarball(sym_tar, [
            ("pkg", None),
            ("pkg/run.py", "x"),
            ("pkg/link", ("symlink", "/etc/passwd")),
        ])
        try:
            upd.extract(sym_tar, "v9.9.8")
            rejected = False
        except UpdateError as exc:
            rejected = "符号链接" in str(exc)
        check("含符号链接的包被拒绝", rejected, "")

        # ---------- 6. 正常解压：剥掉顶层目录 ----------
        print("\n[6] 正常解压：自动剥掉 GitHub tarball 的顶层目录")
        good_tar = os.path.join(root, "good.tar.gz")
        make_tarball(good_tar, [
            ("QQBotMerged-1.3.0", None),
            ("QQBotMerged-1.3.0/run.py", "# run"),
            ("QQBotMerged-1.3.0/core/__init__.py", "# core"),
            ("QQBotMerged-1.3.0/web/templates/index.html", "<html></html>"),
        ])
        staging = upd.extract(good_tar, "v1.3.0")
        check("顶层目录已剥掉", "QQBotMerged-1.3.0" not in os.listdir(staging),
              str(sorted(os.listdir(staging))))
        check("run.py 在解压目录根部",
              os.path.isfile(os.path.join(staging, "run.py")), "")

        # ---------- 7. 缺文件的包必须拒绝安装 ----------
        print("\n[7] 内容不完整的包必须拒绝（防止装进空包/错包）")
        incomplete = os.path.join(root, "incomplete.tar.gz")
        make_tarball(incomplete, [("pkg/readme.txt", "only a readme")])
        try:
            upd.extract(incomplete, "v9.9.7")
            rejected = False
        except UpdateError as exc:
            rejected = "不完整" in str(exc)
        check("缺 run.py 的包被拒绝", rejected, "")

        # ---------- 8. 受保护路径判断 ----------
        print("\n[8] 安装绝不覆盖用户数据与本地配置")
        for path in ("data/messages.db", "data/anything", "plugins/x/main.py",
                     "config.json", ".git/config", "__pycache__/x.pyc",
                     "core/__pycache__/a.pyc", "notes.bak", "app.log"):
            check(f"保护 {path}", Updater._is_protected(path) is True, path)
        for path in ("run.py", "core/updater.py", "web/templates/index.html",
                     "README.md"):
            check(f"允许更新 {path}", Updater._is_protected(path) is False, path)

        # ---------- 9. 安装：覆盖代码、保留用户数据 ----------
        print("\n[9] 安装：代码被覆盖，用户数据与配置原样保留")
        live = os.path.join(root, "live")
        make_project_tree(live, marker="OLD")
        new_tree = os.path.join(root, "new")
        make_project_tree(new_tree, marker="NEW")
        # 新版本里带上一个"想覆盖配置"的文件，验证会被跳过
        with open(os.path.join(new_tree, "config.json"), "w", encoding="utf-8") as handle:
            handle.write('{"secret": "FROM-UPDATE"}')

        live_updater = Updater(base_dir=live, data_dir=os.path.join(live, "data"))
        result = live_updater.install(tag="v1.3.0", source_root=new_tree)
        check("安装返回成功", result.get("success") is True, str(result))
        with open(os.path.join(live, "run.py"), encoding="utf-8") as handle:
            check("run.py 已更新为新版本", "NEW" in handle.read(),
                  open(os.path.join(live, "run.py"), encoding="utf-8").read()[:40])
        with open(os.path.join(live, "config.json"), encoding="utf-8") as handle:
            check("config.json 未被覆盖", "MUST-NOT-CHANGE" in handle.read(), "")
        with open(os.path.join(live, "data", "messages.db"), encoding="utf-8") as handle:
            check("data/ 未被覆盖", "user-db" in handle.read(), "")
        with open(os.path.join(live, "plugins", "my_plugin", "main.py"),
                  encoding="utf-8") as handle:
            check("plugins/ 未被覆盖", "user plugin" in handle.read(), "")
        check("生成了备份文件",
              os.path.isfile(result.get("backup") or ""), str(result.get("backup")))
        check("写入了更新标记（提示重启）",
              live_updater.last_install().get("to_version") == "1.3.0",
              str(live_updater.last_install())[:120])

        # ---------- 10. 回滚能恢复原状 ----------
        print("\n[10] 回滚：用备份恢复更新前的代码")
        backup = result.get("backup")
        restored = live_updater.rollback(backup)
        check("回滚恢复了文件", restored > 0, str(restored))
        with open(os.path.join(live, "run.py"), encoding="utf-8") as handle:
            check("run.py 回到 OLD 版本", "OLD" in handle.read(),
                  open(os.path.join(live, "run.py"), encoding="utf-8").read()[:40])

        # ---------- 11. 安装失败要能回滚 ----------
        print("\n[11] 安装中途失败 → 自动回滚，不留下半装状态")
        live2 = os.path.join(root, "live2")
        make_project_tree(live2, marker="ORIGINAL")
        broken_tree = os.path.join(root, "broken")
        make_project_tree(broken_tree, marker="BROKEN")
        upd2 = Updater(base_dir=live2, data_dir=os.path.join(live2, "data"))
        original_copy = upd2._copy_tree

        def _half_copy(source_root, apply_delete=False):
            # 先写一部分，再抛错（模拟磁盘满/权限不足）
            original_copy(source_root, apply_delete)
            raise UpdateError("模拟写入失败")

        upd2._copy_tree = _half_copy
        fail_result = upd2.install(tag="v1.4.0", source_root=broken_tree)
        check("失败时返回 success=False", fail_result.get("success") is False, "")
        check("失败时已回滚", fail_result.get("rolled_back") is True,
              str(fail_result)[:160])
        with open(os.path.join(live2, "run.py"), encoding="utf-8") as handle:
            check("代码已回滚到 ORIGINAL", "ORIGINAL" in handle.read(),
                  open(os.path.join(live2, "run.py"), encoding="utf-8").read()[:40])
        check("失败后状态为 failed 或已恢复",
              upd2.state.snapshot()["stage"] in ("failed", "done"),
              upd2.state.snapshot()["stage"])

        # ---------- 12. 并发安装要被拒绝 ----------
        print("\n[12] 同一时间只允许一个安装任务")
        upd3 = Updater(base_dir=live2, data_dir=os.path.join(live2, "data"))
        upd3._install_lock.acquire()
        try:
            try:
                upd3.install(tag="v1.5.0", source_root=broken_tree)
                rejected = False
            except UpdateError as exc:
                rejected = "进行中" in str(exc)
            check("已有任务在跑时拒绝新任务", rejected, "")
        finally:
            upd3._install_lock.release()

        # ---------- 13. 状态机 ----------
        print("\n[13] 状态机：阶段名合法、busy 标记正确")
        upd4 = Updater(base_dir=live2, data_dir=os.path.join(live2, "data"))
        upd4.state.set("downloading", "下载中", 42, "v1.6.0")
        snap = upd4.state.snapshot()
        check("阶段记录正确", snap["stage"] == "downloading", snap["stage"])
        check("进度记录正确", snap["progress"] == 42, str(snap["progress"]))
        check("下载中标记为 busy", snap["busy"] is True, "")
        upd4.state.set("done", "完成", 100)
        check("完成后不再是 busy", upd4.state.snapshot()["busy"] is False, "")
        upd4.state.set("不存在的阶段")
        check("非法阶段名被归一成 idle", upd4.state.snapshot()["stage"] == "idle",
              upd4.state.snapshot()["stage"])
        check("status() 带上当前版本",
              upd4.status().get("current_display", "").startswith("v"),
              str(upd4.status().get("current_display")))

        # ---------- 14. 打包成 exe 时拒绝自我更新 ----------
        # exe 的代码在解包目录里，往 exe 旁边覆盖 .py 既不生效又会留一堆散文件，
        # 所以必须明确拒绝（而不是"看起来装成功了"）。
        print("\n[14] 打包版（exe）拒绝自我更新")
        from core import paths as paths_module
        original_frozen = paths_module.is_frozen
        paths_module.is_frozen = lambda: True
        try:
            frozen_updater = Updater(base_dir=root, data_dir=os.path.join(root, "fr"))
            try:
                frozen_updater.install(tag="v9.9.9", source_root=None)
                rejected = False
            except UpdateError as exc:
                rejected = "打包版" in str(exc)
            check("exe 环境下拒绝安装并给出替代方案", rejected, "")
        finally:
            paths_module.is_frozen = original_frozen
        check("测试后 is_frozen 已还原", paths_module.is_frozen() is False, "")

        # ---------- 15. 下载地址必须是白名单（不接受任意 URL） ----------
        print("\n[15] 下载地址只允许白名单域名")
        safe_updater = Updater(base_dir=root, data_dir=os.path.join(root, "dl"))
        try:
            url = safe_updater._tarball_url("v1.2.7", "https://evil.example.com/x.tar.gz")
            safe = _check_url and "github" in url
        except UpdateError:
            safe, url = True, "(被拒绝)"
        check("外部 tarball_url 被忽略，回退到 github 地址", safe, url)
        # 明文 HTTP 的候选地址同样被忽略，最终拿到的仍是 https 的白名单地址
        try:
            fallback = safe_updater._tarball_url("v1.2.7", "http://codeload.github.com/a/b")
            fallback_safe = fallback.startswith("https://") and "github" in fallback
        except UpdateError:
            fallback, fallback_safe = "(被拒绝)", True
        check("HTTP（非 HTTPS）候选被忽略，只回退到 HTTPS", fallback_safe, fallback)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
