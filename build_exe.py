# -*- coding: utf-8 -*-
"""把 QQBotMerged 打包成 Windows exe（PyInstaller）

用法（在项目根目录执行）：

    python build_exe.py                # 单文件 exe → dist/QQBotMerged.exe
    python build_exe.py --onedir       # 文件夹版（启动更快）→ dist/QQBotMerged/QQBotMerged.exe
    python build_exe.py --console      # 默认就是控制台版；加这个也一样
    python build_exe.py --clean-only   # 只清理 build/ 与 dist/，不打包

打包后的结构（以单文件为例）：

    dist/
    ├── QQBotMerged.exe     ← 双击即用（网页后台在 exe 旁边生成 config.json / data/ / plugins/）
    └── README.md           ← 说明文件（脚本自动复制）

- 只把前端（`web/` 模板、CSS、JS）打进 exe；
- **插件不随 exe 附带**：首次运行只会在 exe 旁边创建**空**的 `plugins/` 目录，
  插件由用户自己放进去（`plugins/<插件名>/metadata.yaml + main.py`）；
- `config.json`、`data/`、`plugins/` 都在 exe 旁边，方便修改与备份；
- 想改前端就不用重新打包：在 exe 旁边放一个 `web/` 目录即可（优先使用外部的）。
"""

import argparse
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "QQBotMerged"


def run_pyinstaller(args):
    """优先用 PyInstaller 的 Python API，失败再退回命令行。"""
    try:
        import PyInstaller.__main__ as pyi
    except ImportError:
        print("！没有安装 PyInstaller，先执行： python -m pip install pyinstaller")
        return 1
    return pyi.run(args) or 0


def main() -> int:
    parser = argparse.ArgumentParser(description="打包成 exe")
    parser.add_argument("--onedir", action="store_true",
                        help="打包成文件夹版（启动更快，推荐给长期挂机的用户）")
    parser.add_argument("--onefile", action="store_true",
                        help="打包成单文件 exe（默认）")
    parser.add_argument("--icon", default="", help="可选：指定 .ico 图标")
    parser.add_argument("--noconsole", action="store_true",
                        help="隐藏控制台窗口（不推荐：看不到日志与报错）")
    parser.add_argument("--clean-only", action="store_true", help="只清理 build/ 与 dist/")
    args = parser.parse_args()

    os.chdir(BASE)

    if args.clean_only:
        for name in ("build", "dist", f"{APP_NAME}.spec"):
            target = os.path.join(BASE, name)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
                print("已删除目录：", name)
            elif os.path.isfile(target):
                os.remove(target)
                print("已删除文件：", name)
        return 0

    # ---------------- 检查必要文件 ----------------
    for needed in ("run.py", os.path.join("web", "templates", "index.html"),
                   os.path.join("web", "static", "js", "chat.js")):
        if not os.path.exists(os.path.join(BASE, needed)):
            print(f"！缺少文件：{needed}（请在项目根目录执行本脚本）")
            return 1

    onefile = not args.onedir
    sep = os.pathsep          # Windows 上是 ";"
    py_args = [
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--onefile" if onefile else "--onedir",
        "--console",                      # 机器人后台要看日志，保留控制台
        "--noupx",                        # 不用 UPX，避免杀软误报
        # 打进 exe 的资源：只有前端（插件由用户自己放进 plugins/，不打进 exe）
        "--add-data", f"{os.path.join(BASE, 'web')}{sep}web",
        # 这两个库的导入方式比较动态，显式声明更稳
        "--collect-submodules", "websocket",
        "--collect-submodules", "flask",
        "--hidden-import", "jinja2.ext",
        "--hidden-import", "werkzeug.middleware.proxy_fix",
        # 明显的无关大件，排除掉能显著减小体积
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "PyQt5",
        "--exclude-module", "test",
    ]
    if args.icon:
        icon = args.icon if os.path.isabs(args.icon) else os.path.join(BASE, args.icon)
        if os.path.isfile(icon):
            py_args += ["--icon", icon]
        else:
            print("！找不到图标文件，已忽略：", icon)
    if args.noconsole:
        py_args[py_args.index("--console")] = "--noconsole"

    print("=" * 66)
    print(f"  开始打包：{'单文件 exe' if onefile else '文件夹版'}")
    print("=" * 66)
    code = run_pyinstaller(py_args + ["run.py"])
    if code:
        print("！打包失败，退出码：", code)
        return code

    # ---------------- 收尾：说明文件 + 提示 ----------------
    dist = os.path.join(BASE, "dist")
    readme = os.path.join(BASE, "README.md")
    if os.path.isfile(readme) and os.path.isdir(dist):
        try:
            shutil.copy2(readme, os.path.join(dist, "README.md"))
        except OSError:
            pass

    target = (os.path.join(dist, f"{APP_NAME}.exe") if onefile
              else os.path.join(dist, APP_NAME, f"{APP_NAME}.exe"))
    size_mb = 0.0
    if os.path.isfile(target):
        size_mb = os.path.getsize(target) / 1024 / 1024
    print("=" * 66)
    print("  ✅ 打包完成")
    print(f"  程序：{target}")
    if size_mb:
        print(f"  大小：{size_mb:.1f} MB")
    if onefile:
        print("  用法：把 exe 放到一个**可写目录**（如新建的文件夹）里双击运行，")
        print("        首次运行会在它旁边生成 config.json / data/ 和空的 plugins/。")
        print("        插件请自行放进 plugins/<插件名>/（metadata.yaml + main.py）。")
    else:
        print("  用法：整个 dist/%s 文件夹一起拷走，双击里面的 %s.exe 运行。" % (APP_NAME, APP_NAME))
        print("        插件请自行放进 exe 旁边的 plugins/<插件名>/。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
