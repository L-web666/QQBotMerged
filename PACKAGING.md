# QQBotMerged 启动器（`qqbot-cli`）

一条命令装好并运行 [QQBotMerged](https://github.com/L-web666/QQBotMerged) —— 一个可多开的
QQ 官方机器人后台。

```bash
pipx install qqbot-cli     # 或： pip install --user qqbot-cli
qqbot
```

第一次运行会依次完成：**选安装目录 → 下载最新版 → 建虚拟环境 → 装依赖 → 几个必填项 → 启动**。
之后再运行 `qqbot` 就是直接启动。

**安装目录由你自己选**：向导第一步就问（直接回车用默认的 `~/QQBotMerged`），
也可以一开始就用 `--dir` 指定。配置、数据、日志都在这个目录里，
想查看或改东西直接打开它即可 —— 装完向导会把 `cd` 命令打印出来。

## 命令

| 命令 | 作用 |
|---|---|
| `qqbot` | 没装过就走向导，装过了就直接启动 |
| `qqbot init` | 重新走向导（不会删 `data/` 里的数据） |
| `qqbot run` | 只启动，不问任何问题 |
| `qqbot export` | 把 `config.json` 和 `data/` 导出成 zip 备份 |
| `qqbot uninstall` | 卸载（会问要不要先导出数据） |
| `qqbot --dir D:\bots\qq` | 直接指定安装目录（不追问；可以在一台机器上装多份） |
| `qqbot --upgrade` | 重新下载最新版，保留 `config.json` 和 `data/` |
| `qqbot --version` | 显示启动器版本 |

安装目录会被**记住**，所以之后直接 `qqbot` 就能找到它；要换一份就用 `--dir`。

### 卸载

```bash
qqbot uninstall
```

会先列出装了什么、占多大，然后问：

1. **要不要先导出 `config.json` 和 `data/`？**（默认要）
   导出成一个 zip，里面**只有**这两样 —— 不含程序代码、不含 `.venv`、不含 `__pycache__`。
2. **确认删除吗？**（默认否）

只想备份不卸载就 `qqbot export`：

```bash
qqbot export                      # 导到安装目录旁边的 xxx-backup-时间.zip
qqbot export --output D:\bak.zip  # 指定位置
```

脚本里用可以跳过询问：`qqbot uninstall --no-export -y`。

**恢复**：把备份解压回一个目录，`qqbot --dir <那个目录> run` 就能继续用。

> 装了启动器不等于装了程序：`pipx uninstall qqbot-cli` 只移除启动器本身，
> 各安装目录里的程序和数据都不动。

## 支持的系统

| 系统 | 支持 | 说明 |
|---|---|---|
| Windows 10/11 | ✅ | `cd /d`、`Scripts\python.exe`、`activate.bat` |
| Linux | ✅ | `cd`、`bin/python`、`source activate` |
| macOS | ✅ | 同 Linux |

要求 Python **3.9+**（Werkzeug 3.1.9 的要求）。
Linux 上如果建不了虚拟环境，装一下系统包：`apt install python3-venv`。

## 它做了什么、没做什么

**做了什么**

- **先问装到哪个目录**（回车用默认值；相对路径按当前目录解析；带引号也能识别）。
  目录里已有别的东西时会先提示、要求确认，不会闷头覆盖
- 从 GitHub Release 下载**正式版**（自动跳过草稿和预发布），校验大小上限、限制成员数、
  拒绝绝对路径与 `..`，防止压缩包搞坏事
- 在安装目录里建一个 `.venv`，把主程序的依赖装进去 —— **不污染你的系统 Python**
- 问几个必填项（AppID / AppSecret / AI 配置 / 端口），生成 `config.json`；
  **留空也能装完**，之后在网页「设置」里改
- 自动生成网页访问令牌，装完直接打印带令牌的地址
- 用 `<安装目录>/.venv` 里的 python 跑 `run.py`

**没做什么**

- **不修改主程序的任何代码**：拉下来就是原来那个完整项目目录，
  它的自动更新、插件、云同步全都照常工作
- 不接管主程序的配置：主程序启动时会按自己的 schema 补全缺项
- 程序和数据都在你选的安装目录里，删目录即卸载；
  启动器只在系统配置目录里留一个小文件，记住「上次装在哪」
  （`qqbot uninstall` 会一并清掉，删掉也不影响使用 —— 只是得用 `--dir` 指定目录）

## 为什么装进虚拟环境

主程序需要 `Werkzeug>=3.1.9`（一个安全修复版本）。直接装进系统 Python 可能和已有包冲突，
也可能因为版本不对起不来。每个安装目录一个 `.venv`，互不干扰。

启动器自己只依赖 `requests`，所以适合用 `pipx` 装（和 NoneBot 的 `nb-cli` 一样）：

```bash
pipx install qqbot-cli
```

## 目录结构

```
<安装目录>/
├── run.py               ← 主程序入口
├── config.json          ← 启动器向导生成的，之后由主程序读写
├── core/ web/ ...       ← 主程序代码（随版本更新整体替换）
├── data/                ← 你的数据（消息库、媒体、日志）—— 更新时保留
├── plugins/             ← 插件
├── .venv/               ← 这个安装专用的虚拟环境
└── .qqbot-install.json  ← 启动器的安装记录（版本、sha256）
```

## 许可证

AGPL-3.0（与主程序一致）。
