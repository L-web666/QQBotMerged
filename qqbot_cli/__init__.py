# -*- coding: utf-8 -*-
"""QQBotMerged 启动器（PyPI 包名 `qqbot-cli`，命令 `qqbot`）。

**这个包是独立的**：它不导入主程序的任何模块（`core.*` / `web.*`），
只负责

  1. 把主程序从 GitHub Release 拉到用户选的目录；
  2. 建虚拟环境、装依赖；
  3. 问几个必填项，生成 `config.json`；
  4. 启动 `run.py`。

这样主程序仍然是原来那个完整的项目目录 —— 它自己的 `core/paths.py`、
自动更新、插件机制全都照常工作，不需要改任何一行代码。
"""

__version__ = "1.0.1"
