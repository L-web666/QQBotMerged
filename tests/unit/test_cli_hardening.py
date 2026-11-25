# -*- coding: utf-8 -*-
"""单元测试：按审计报告（2026-11-25）修复的启动器安全问题

对应报告的 9 项发现，逐条都有用例：

  · **CLI-ARCHIVE-001**（高危）解压可越出安装目录
    —— `_safe_member()` 逐组件校验；解压前再做 realpath 包含性检查
  · **CLI-STATE-001**（高危）不可信工作目录状态文件
    —— 不再读 cwd 下的状态文件；marker 的 install_dir 必须与实际路径一致；
       `launch()` 拒绝执行安装目录外的程序
  · **CLI-CONFIG-001**（中危）config.json 权限
    —— POSIX 下 0600
  · **CLI-EXPORT-001**（中危）导出跟随符号链接
    —— 导出时拒绝链接与越界文件
  · **CLI-REDIRECT-001**（中危）重定向未逐跳校验
    —— 只允许 HTTPS、逐跳 `_check_host()`、拒绝内网地址
  · **DEP-REQUESTS-001**（中危）requests 下限
    —— 所有清单统一 `>=2.32.4`
  · **CLI-UNINSTALL-001**（低危）卸载识别过宽
    —— 必须 marker 存在且路径一致
  · **CI-PUBLISH-001**（低危）Action 未固定 SHA
    —— workflow 里全部 `uses:` 都是 40 位 SHA
  · **DEP-PIP-001**（严重，条件性）pip 版本
    —— 装依赖前检查并提示

运行：`python tests/unit/test_cli_hardening.py`
"""

import builtins
import io
import json
import os
import re
import shutil
import sys
import tarfile
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from qqbot_cli import fetch, paths, runner, uninstall, wizard   # noqa: E402

PASSED = []
FAILED = []
WORK = os.path.join(PROJECT, "data", "_selftest", "unit_cli_hardening")


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def make_install(root, with_config=False):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "run.py"), "w", encoding="utf-8") as fh:
        fh.write("print('hi')\n")
    os.makedirs(os.path.join(root, ".venv", "Scripts"), exist_ok=True)
    with open(os.path.join(root, ".venv", "Scripts", "python.exe"), "w") as fh:
        fh.write("")
    if with_config:
        with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"web": {"port": 8666}}, fh)
    runner.write_marker(root, {"tag": "v1.5.0"})
    return root


def main():
    print("=" * 70)
    print("  启动器安全加固（审计 2026-11-25）")
    print("=" * 70)
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    try:
        # ============ CLI-ARCHIVE-001 ============
        print("\n[A] CLI-ARCHIVE-001 解压不能越出安装目录（高危）")
        print("\n[A1] 逐组件校验：盘符 / ADS / 设备名 / 尾随点空格")
        must_reject = [
            "a/D:outside.txt",       # 报告点名的写法：中间组件是盘符相对路径
            "dir/C:/x.txt",
            "D:outside.txt",
            "a/b/c:stream",          # ADS
            "a/COM1.txt",            # 设备名（带目录）
            "a/nul",
            "a/AUX.css",
            "a/COM\u00b9.txt",       # 上标数字设备名
            "a/trailing.",           # 尾随点
            "a/trailing ",           # 尾随空格
            "../evil",
            "a/../../evil",
            "/abs/path",
            "~/home",
            "",
        ]
        for name in must_reject:
            check("拒绝 %r" % name, fetch._safe_member(name) == "",
                  repr(fetch._safe_member(name)))

        print("\n[A2] 正常路径仍然放行（不能误伤）")
        must_allow = [
            ("qqbot-1.0/run.py", "qqbot-1.0/run.py"),
            ("core/deep/x.py", "core/deep/x.py"),
            ("a\\b.py", "a/b.py"),
            ("a/./b.py", "a/b.py"),
            ("a_b-c.d.js", "a_b-c.d.js"),
            ("web/static/js/app.js", "web/static/js/app.js"),
        ]
        for name, expected in must_allow:
            check("放行 %r" % name, fetch._safe_member(name) == expected,
                  repr(fetch._safe_member(name)))

        print("\n[A3] _within 包含性检查")
        inside = os.path.join(WORK, "sub", "f.txt")
        outside = os.path.join(os.path.dirname(WORK), "evil.txt")
        check("内部判为在内", fetch._within(WORK, inside) is True, "")
        check("外部判为在外", fetch._within(WORK, outside) is False, "")
        check(".. 穿越判为在外",
              fetch._within(WORK, os.path.join(WORK, "..", "evil.txt")) is False, "")

        print("\n[A4] 真实 tar 包：危险成员不会落盘")
        evil_tar = os.path.join(WORK, "evil.tar.gz")
        with tarfile.open(evil_tar, "w:gz") as tar:
            for name, content in [
                ("shell/run.py", b"print(1)\n"),
                ("shell/a/D:outside.txt", b"PWNED\n"),
                ("shell/dir/C:/x.txt", b"PWNED\n"),
                ("shell/ok.txt", b"fine\n"),
            ]:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        target = os.path.join(WORK, "out_tar")
        fetch.extract(evil_tar, target)
        found = []
        for base, dirs, names in os.walk(target):
            for name in names:
                found.append(os.path.relpath(os.path.join(base, name), target)
                             .replace("\\", "/"))
        check("正常文件解出来", any(f.endswith("run.py") for f in found), str(found))
        check("危险的盘符成员被拒",
              not any("outside" in f for f in found), str(found))
        check("没有在目标外生成 PWNED",
              not os.path.exists(os.path.join(WORK, "outside.txt")), "")

        print("\n[A5] 真实 zip 包：同样的防护")
        evil_zip = os.path.join(WORK, "evil.zip")
        with zipfile.ZipFile(evil_zip, "w") as bundle:
            bundle.writestr("shell/run.py", "print(1)\n")
            bundle.writestr("shell/a/D:outside.txt", "PWNED\n")
            bundle.writestr("shell/ok.txt", "fine\n")
        target_zip = os.path.join(WORK, "out_zip")
        fetch.extract(evil_zip, target_zip)
        found_zip = []
        for base, dirs, names in os.walk(target_zip):
            for name in names:
                found_zip.append(os.path.relpath(os.path.join(base, name), target_zip)
                                 .replace("\\", "/"))
        check("zip 里正常文件解出来",
              any(f.endswith("run.py") for f in found_zip), str(found_zip))
        check("zip 里的危险成员被拒",
              not any("outside" in f for f in found_zip), str(found_zip))

        # ============ CLI-STATE-001 ============
        print("\n[B] CLI-STATE-001 不可信状态文件（高危）")
        print("\n[B1] 当前工作目录不再是状态文件候选")
        candidates = paths._state_files()
        cwd = os.getcwd()
        in_cwd = [c for c in candidates
                  if os.path.dirname(os.path.abspath(c)) == cwd]
        check("cwd 下的 .qqbot-cli.json 不在候选里", in_cwd == [], str(in_cwd))

        print("\n[B2] 伪造的 cwd 状态文件会被忽略")
        for one in paths._state_files():
            try:
                os.remove(one)
            except OSError:
                pass
        fake = make_install(os.path.join(WORK, "attacker"))
        forged = os.path.join(cwd, ".qqbot-cli.json")
        with open(forged, "w", encoding="utf-8") as fh:
            json.dump({"install_dir": fake}, fh)
        try:
            got = paths.remembered_install_dir()
            check("读到空（忽略伪造）", got != fake, "读到 %r" % got)
        finally:
            if os.path.exists(forged):
                os.remove(forged)

        print("\n[B3] marker 的 install_dir 必须与实际路径一致")
        good = make_install(os.path.join(WORK, "good"))
        check("正常安装判为已安装", runner.is_installed(good) is True, "")

        copied = make_install(os.path.join(WORK, "copied"))
        shutil.copy(os.path.join(good, paths.MARKER_FILE),
                    os.path.join(copied, paths.MARKER_FILE))
        check("marker 从别处抄来 → 不算已安装",
              runner.is_installed(copied) is False, "")

        pointing = make_install(os.path.join(WORK, "pointing"))
        with open(os.path.join(pointing, paths.MARKER_FILE), "w", encoding="utf-8") as fh:
            json.dump({"tag": "v1.5.0", "install_dir": good}, fh)
        check("marker 指向别的目录 → 不算已安装",
              runner.is_installed(pointing) is False, "")

        nofield = make_install(os.path.join(WORK, "nofield"))
        with open(os.path.join(nofield, paths.MARKER_FILE), "w", encoding="utf-8") as fh:
            json.dump({"tag": "v1.5.0"}, fh)
        check("marker 没记 install_dir → 不算已安装",
              runner.is_installed(nofield) is False, "")

        print("\n[B4] launch() 拒绝执行安装目录外的程序")
        outside_py = os.path.join(WORK, "outside.exe")
        with open(outside_py, "w") as fh:
            fh.write("")
        try:
            runner.launch(good, outside_py)
            check("拒绝目录外的解释器", False, "没有抛错")
        except runner.SetupError as exc:
            check("拒绝目录外的解释器", "不在安装目录里" in str(exc), str(exc)[:60])

        inside_py = os.path.join(good, ".venv", "Scripts", "python.exe")
        try:
            runner.launch(good, inside_py)
            check("目录内的解释器通过路径检查", True, "")
        except runner.SetupError as exc:
            check("目录内的解释器通过路径检查（只是跑不起来）",
                  "不在安装目录里" not in str(exc), str(exc)[:60])

        print("\n[B5] marker 存的是规范化绝对路径")
        recorded = runner.read_marker(good).get("install_dir")
        check("是绝对路径", os.path.isabs(recorded or ""), str(recorded))
        check("是 realpath 规范化后的",
              recorded == os.path.realpath(os.path.abspath(good)), str(recorded))

        # ============ CLI-EXPORT-001 ============
        print("\n[C] CLI-EXPORT-001 导出不跟随符号链接（中危）")
        export_root = make_install(os.path.join(WORK, "exportable"), with_config=True)
        data_dir = os.path.join(export_root, "data")
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "messages.db"), "wb") as fh:
            fh.write(b"DB" * 100)
        secrets = os.path.join(WORK, "secret.txt")
        with open(secrets, "w", encoding="utf-8") as fh:
            fh.write("TOP SECRET\n")
        link_path = os.path.join(data_dir, "leak.txt")
        try:
            os.symlink(secrets, link_path)
            has_symlink = True
        except (OSError, NotImplementedError, AttributeError):
            has_symlink = False
            print("       （当前环境不能建符号链接，跳过链接相关用例）")
        backup = os.path.join(WORK, "backup.zip")
        result = uninstall.export_data(export_root, backup)
        with zipfile.ZipFile(backup) as bundle:
            names = bundle.namelist()
            check("正常文件进了备份", "data/messages.db" in names, str(names))
            check("配置进了备份", "config.json" in names, str(names))
            if has_symlink:
                check("符号链接没进备份", "data/leak.txt" not in names, str(names))
                check("备份里读不到秘密内容",
                      not any(b"TOP SECRET" in bundle.read(n) for n in names
                              if not n.endswith("/")), "")
        if has_symlink:
            check("跳过的项被记录下来",
                  any("leak" in s for s in result["skipped"]), str(result["skipped"]))

        # ============ CLI-REDIRECT-001 ============
        print("\n[D] CLI-REDIRECT-001 重定向逐跳校验（中危）")
        print("\n[D1] _check_host 只放行 HTTPS + 白名单 + 非内网")
        reject_urls = [
            "http://api.github.com/x",           # 不是 HTTPS
            "https://evil.example.com/x",        # 非白名单
            "https://127.0.0.1/x",               # 回环
            "https://192.168.1.1/x",             # 内网
            "https://10.0.0.5/x",                # 内网
            "https://169.254.169.254/x",         # 链路本地（云元数据）
            "https://localhost/x",
        ]
        for url in reject_urls:
            try:
                fetch._check_host(url)
                check("拒绝 %s" % url, False, "没有报错")
            except fetch.FetchError:
                check("拒绝 %s" % url, True, "")
        allow_urls = [
            "https://api.github.com/repos/x/y",
            "https://codeload.github.com/x/y/tar.gz/refs/tags/v1.0.0",
            "https://objects.githubusercontent.com/x",
        ]
        for url in allow_urls:
            try:
                fetch._check_host(url)
                check("放行 %s" % url, True, "")
            except fetch.FetchError as exc:
                check("放行 %s" % url, False, str(exc))

        print("\n[D2] 源码里确实禁用了自动重定向")
        source = io.open(os.path.join(PROJECT, "qqbot_cli", "fetch.py"),
                         encoding="utf-8").read()
        check("用 allow_redirects=False",
              "allow_redirects=False" in source, "")
        check("有逐跳处理函数",
              "_get_following_redirects" in source, "")

        # ============ DEP-REQUESTS-001 ============
        print("\n[E] DEP-REQUESTS-001 requests 下限（中危）")
        req_files = [
            "requirements.txt",
            "plugins/astrbot_plugin_ollama/requirements.txt",
            "pyproject.toml",
            "qqbot_cli/paths.py",
        ]
        for relative in req_files:
            full = os.path.join(PROJECT, relative)
            if not os.path.exists(full):
                check("存在 %s" % relative, False, "文件不存在")
                continue
            text = io.open(full, encoding="utf-8").read()
            check("%s 里没有 requests>=2.28.0" % relative,
                  "requests>=2.28.0" not in text, "")
            check("%s 里是 requests>=2.32.4" % relative,
                  "requests>=2.32.4" in text, "")

        # ============ CLI-UNINSTALL-001 ============
        print("\n[F] CLI-UNINSTALL-001 卸载识别收紧（低危）")
        print("\n[F1] 只有 run.py 的目录拒绝删")
        stranger = os.path.join(WORK, "stranger")
        os.makedirs(stranger, exist_ok=True)
        with open(os.path.join(stranger, "run.py"), "w", encoding="utf-8") as fh:
            fh.write("print('other project')\n")
        with open(os.path.join(stranger, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"x": 1}, fh)
        try:
            uninstall.uninstall(stranger)
            check("拒绝删除没有 marker 的目录", False, "没有报错")
        except uninstall.UninstallError as exc:
            check("拒绝删除没有 marker 的目录", True, str(exc)[:50])
        check("陌生目录里的文件还在",
              os.path.exists(os.path.join(stranger, "run.py")), "")

        print("\n[F2] marker 指向别处的目录拒绝删")
        elsewhere = make_install(os.path.join(WORK, "elsewhere"))
        borrowed = make_install(os.path.join(WORK, "borrowed"))
        shutil.copy(os.path.join(elsewhere, paths.MARKER_FILE),
                    os.path.join(borrowed, paths.MARKER_FILE))
        try:
            uninstall.uninstall(borrowed)
            check("拒绝删除 marker 指向别处的目录", False, "没有报错")
        except uninstall.UninstallError as exc:
            check("拒绝删除 marker 指向别处的目录",
                  "指向的是别的目录" in str(exc), str(exc)[:60])
        check("该目录没被删", os.path.exists(borrowed), "")

        print("\n[F3] 正常的安装目录仍可卸载")
        ok_dir = make_install(os.path.join(WORK, "deletable"))
        result = uninstall.uninstall(ok_dir)
        check("正常目录被删除", result["removed"] is True, str(result))
        check("目录确实没了", not os.path.exists(ok_dir), "")

        # ============ CI-PUBLISH-001 ============
        print("\n[G] CI-PUBLISH-001 工作流加固（低危）")
        workflow = os.path.join(PROJECT, ".github", "workflows", "publish-cli.yml")
        text = io.open(workflow, encoding="utf-8").read()
        uses = re.findall(r"uses:\s*(\S+)", text)
        check("有 uses 语句", len(uses) > 0, str(len(uses)))
        bad = [u for u in uses if not re.fullmatch(r"[0-9a-f]{40}",
                                                   u.split("@")[-1])]
        check("全部 action 固定到 40 位 SHA", bad == [], str(bad))
        check("默认权限只有 contents: read",
              re.search(r"^permissions:\n\s+contents:\s*read", text, re.M) is not None, "")
        check("有独立测试 job（构建前先跑测试）",
              "test:" in text and "needs: test" in text, "")

        # ============ DEP-PIP-001 ============
        print("\n[H] DEP-PIP-001 pip 版本检查（条件性严重）")
        print("\n[H1] 版本解析与比较逻辑")
        check("解析 'pip 24.3.1 from ...'",
              runner._parse_version("pip 24.3.1 from /x") == (24, 3, 1), "")
        check("解析 'pip 26.2.2'",
              runner._parse_version("pip 26.2.2") == (26, 2, 2), "")
        check("解析失败返回空", runner._parse_version("nonsense") == (), "")
        check("安全下限是 26.2.2", runner.MIN_PIP_VERSION == (26, 2, 2),
              str(runner.MIN_PIP_VERSION))
        check("24.3.1 低于下限",
              (24, 3, 1) < runner.MIN_PIP_VERSION, "")
        check("26.2.2 达标", (26, 2, 2) >= runner.MIN_PIP_VERSION, "")

        print("\n[H2] 检查函数存在且不静默升级")
        source = io.open(os.path.join(PROJECT, "qqbot_cli", "runner.py"),
                         encoding="utf-8").read()
        check("有 check_pip", "def check_pip" in source, "")
        check("检查里不会执行 pip install --upgrade",
              "install\", \"--upgrade\", \"pip" not in source, "")

        # ============ CLI-CONFIG-001 ============
        print("\n[I] CLI-CONFIG-001 config.json 权限（中危）")
        config_root = os.path.join(WORK, "permdir")
        os.makedirs(config_root, exist_ok=True)
        target = wizard.write_config(config_root, {"web": {"token": "SECRET"}})
        check("config.json 写出来了", os.path.exists(target), target)
        if os.name == "nt":
            print("       （Windows 上不校验 POSIX 权限位，ACL 由目录决定）")
        else:
            mode = os.stat(target).st_mode & 0o777
            check("权限是 0600", mode == 0o600, oct(mode))
        source = io.open(os.path.join(PROJECT, "qqbot_cli", "wizard.py"),
                         encoding="utf-8").read()
        check("用 os.open 带 0o600 创建", "0o600" in source, "")
    finally:
        for one in paths._state_files():
            try:
                os.remove(one)
            except OSError:
                pass
        shutil.rmtree(WORK, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
