# 插件目录（AstrBot 官方插件格式）

> ⚠️ **安全警告：插件是任意 Python 代码。**
> 插件在**主进程里直接执行**，没有沙箱：它可以读写你机器上的任意文件
> （包括 `config.json` 里的机器人 AppSecret、AI 密钥、`data/messages.db` 以及你的其它文件），
> 也能联网、起线程/进程、安装依赖。本程序提供的 `PluginBot` / `PluginRuntime` 只是
> **便利门面**，拦不住 `import os` 这类直接越权访问。
> 因此：**只放你自己写的、或来源完全可信的插件**，不要从不明出处拷插件进来。
> 插件管理页会列出每个插件注册的处理器与用到的、本程序暂不支持的能力，可先用来核对。

本程序**只支持 AstrBot 官方插件格式**，目录约定与 AstrBot 官方一致：

| 内容 | 位置 |
|---|---|
| 插件本体 | `plugins/<插件名>/`（必须含 `metadata.yaml` + `main.py`） |
| 插件配置项定义 | `plugins/<插件名>/_conf_schema.json`（可选） |
| 插件配置值 | `data/config/<插件名>_config.json`（自动生成，可手改） |
| 插件数据 / 大文件 | `data/plugin_data/<插件名>/` |
| 插件依赖 | `plugins/<插件名>/requirements.txt`（可选） |

> 旧版把插件数据放在 `data/plugins_data/`，启动时会**自动迁移**到官方目录
> `data/plugin_data/`（不覆盖已有文件），迁移完删除旧目录。
> 本程序早期那套「原生插件」（`PLUGIN` + `on_message` 的单文件 `.py`）**已彻底移除**：
> `plugins/` 里不是标准 AstrBot 插件目录的东西一律忽略。

## 目录长什么样

```
plugins/
├── astrbot_demo/                  # 示例插件：文字 / 图片 / 管理员指令（可删）
│   ├── metadata.yaml
│   └── main.py
├── astrbot_plugin_dice/           # 骰子：/骰子
├── astrbot_plugin_daily_checkin/  # 签到：/签到 /积分 /查询 /抽奖
└── astrbot_plugin_ollama/         # 本地 Ollama 接管普通消息（默认让给内置 AI）
```

## 插件最小写法（与官方一致）

```python
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger


class MyPlugin(Star):
    def __init__(self, context: Context, config=None):   # 有 _conf_schema.json 时官方会传 config
        super().__init__(context)
        self.config = config or {}

    @filter.command("hello")
    async def hello(self, event: AstrMessageEvent):
        """这是指令说明（会显示在指令面板里）"""
        yield event.plain_result(f"你好，{event.get_sender_name()}！")

    async def terminate(self):
        """插件卸载/停用时调用，可选"""
        pass
```

要点：

- 处理器必须写在继承 `Star` 的插件类里，函数前两个参数是 `self, event`；
- 发消息用 `yield event.plain_result(...)`（图片用 `event.image_result(...)`）；
- 读插件配置用 `self.config`（`AstrBotConfig`，需要落盘时 `self.config.save_config()`）；
- 数据文件按官方约定放 `data/plugin_data/<插件名>/`：

  ```python
  from pathlib import Path
  from astrbot.core.utils.astrbot_path import get_astrbot_data_path

  data_dir = Path(get_astrbot_data_path()) / "plugin_data" / self.name
  ```

- 简单键值存储：`await self.put_kv_data(...)` / `get_kv_data(...)` / `delete_kv_data(...)`；
- 网络请求请用异步库（`aiohttp` / `httpx`）；本程序自带的 Ollama 插件用
  `asyncio.to_thread` 包住同步请求，避免卡住事件循环。

## `metadata.yaml` 常用字段

```yaml
name: astrbot_plugin_xxx        # 必填，建议 astrbot_plugin_ 开头、全小写
display_name: 中文展示名         # 可选（v4.5.0+）
desc: 功能说明
short_desc: 卡片上的一句短介绍   # 可选
version: 1.0.0
author: 你的名字
repo: https://github.com/...    # 可选
astrbot_version: ">=4.0.0"      # 可选；版本不满足时本程序只给警告，不阻止加载
support_platforms:              # 可选；不含 qq_official 时会提示可能不工作
  - qq_official
```

## 安装 / 调试

1. 把插件目录整个复制进 `plugins/`；
2. 打开网页后台「插件管理」→ 右上角「丢弃修改并重载」（停用/启用后要点「保存插件设置」）；
3. 加载失败时页面会直接列出原因（缺文件、语法错误、没有处理器、缺依赖等）。

## 兼容范围与已知差异

兼容层实现了 `astrbot.api.*` 的常用子集（`Star` / `filter` / 事件 / 消息链 / 配置 /
KV / `StarTools` / provider 调用）。以下能力**暂不支持**，插件页会逐个列出：

- 插件自带 Web 接口（`register_web_api`）与插件 Pages；
- 给 LLM 注册函数工具（`register_llm_tool`）；
- 文转图模板、会话控制器、知识库、Agent 执行器等 AstrBot 专有组件。

更多官方写法见 AstrBot 文档：插件开发 / 插件配置 / 插件存储。
