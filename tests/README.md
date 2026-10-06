# 测试分层与运行方式

本项目**不删旧测试**，只按下面三层逐步补齐；每层都能单独跑、互不依赖。

| 层级 | 位置 | 特点 | 命令 |
|---|---|---|---|
| 单元 | `tests/unit/` | 快（秒级）、只依赖被测模块本身、不连网、不碰真实 `data/` | `python tests/unit/test_plugin_safety.py`<br>`python tests/unit/test_cloud_secrets.py` |
| 集成 / 端到端 | `tests/test_offline.py` | 把机器人 API 换成桩，走完"收消息→落库→广播→回复"整条链路；覆盖 40+ 场景 | `python tests/test_offline.py` |
| 前端行为 | `tests/dom_test_*.js` | Node + DOM 桩，验证消息渲染与会话列表不跳动 | `node tests/dom_test_messages.js`<br>`node tests/dom_test_conversations.js` |
| 契约 / 静态 | `tests/check_frontend.py` | JS 引用的元素 id 是否都在模板里、字段类型是否都有渲染分支、按机器人隔离是否接好 | `python tests/check_frontend.py` |
| 单实例锁 | `tests/test_instance_lock.py` | 端口探测、已有实例时立刻退出 | `python tests/test_instance_lock.py` |
| 手动注入 | `tests/simulate_message.py` | 往**正在运行**的程序注入一条模拟消息（排查用，不算回归测试） | `python tests/simulate_message.py` |

## 规矩（来自 `PROGRESS.md`）

- 不删除 `tests/` 下任何测试；新用例优先放 `tests/unit/`。
- 测试只能写 `data/_selftest/`，**不许动真实 `data/`**（消息库、上下文、插件数据）。
- 不要把真实密钥写进测试；需要"像密钥"的内容时用假的（如 `sk-abcdefghijklmnop`）。
- 源码与测试文件一律 **UTF-8 无 BOM**（用编辑器/写文件工具写，别用 PowerShell 的
  `Out-File` / `Set-Content`——它们会加 BOM，而 `config.json` 带 BOM 曾经导致配置被覆盖）。
- 一次只跑一个测试进程：`test_offline.py` 开头的清理会删掉 `data/_selftest/` 里的临时文件，
  并发跑会互相删。

## 提交前建议跑一遍

```powershell
$env:PYTHONIOENCODING="utf-8"
python tests/test_offline.py
python tests/unit/test_plugin_safety.py
python tests/unit/test_cloud_secrets.py
python tests/check_frontend.py
python tests/test_instance_lock.py
node tests/dom_test_messages.js
node tests/dom_test_conversations.js
python run.py --check
```
