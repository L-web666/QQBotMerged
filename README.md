# QQBotMerged —— QQ 官方机器人多开后台（合并版）

一个**单进程、单网页后台**的 QQ 官方机器人管理程序：多机器人同时在线、AI 自动回复、
即时通讯式聊天界面、群管理、指令面板、AstrBot 插件、云同步、统计与日志。
由工作区里两个独立程序合并而来（`API_qqbot` 的 AI/插件/云同步/面板/定时/统计 +
`app` 的网页控制台/消息持久化/图片文件收发），原目录未改动。

- **入口**：`python run.py`
- **网页后台**：<http://127.0.0.1:8666/>
- **依赖**：Python 3.8+（开发/测试环境 3.14.7）、`flask`、`requests`、`websocket-client`
- **平台**：Windows / Linux / macOS 都能跑（路径全部相对程序目录，无硬编码绝对路径）

---

## 目录

1. [功能一览](#一功能一览)
2. [快速开始](#二快速开始)
3. [目录结构](#三目录结构)
4. [配置说明](#四配置说明)
5. [回复链路与消息规则](#五回复链路与消息规则)
6. [插件系统（AstrBot 官方格式）](#六插件系统astrbot-官方格式)
7. [数据、备份与云同步](#七数据备份与云同步)
8. [测试与自检](#八测试与自检)
9. [常见问题](#九常见问题)
10. [打包分发](#十打包分发)
11. [用 Git 上传到 GitHub](#十一用-git-上传到-github)

---

## 一、功能一览

| 分类 | 能力 |
|---|---|
| 机器人 | 多机器人同时在线（每个机器人一条独立 WebSocket 连接、独立 AppID/密钥），互不串消息 |
| 消息 | 私聊 + 群聊；群聊支持「需要 @ 才回复」或「群里所有消息都参与」；全量模式下也能正确识别 @ |
| AI | 任意 OpenAI 兼容接口（DeepSeek / OpenAI / Ollama…）、多模态看图、每机器人独立人设与模型 |
| 记忆 | 上下文按「机器人 + 会话」隔离，可设条数；`/清空上下文` 随时重置 |
| 回复链路 | 过滤 → 敏感词 → 内置指令 → 关键词 → 插件 → 限速 → AI，每一步跳过都有原因日志 |
| 网页聊天 | 即时通讯式界面、未读计数、实时推送（SSE）+ 轮询兜底、点击消息弹操作框 |
| 发送 | 文本、图片链接、本地上传图片/表情包/GIF、任意文件（≤200MB，大文件自动走官方分片上传）、引用回复 |
| 媒体 | 收到的图片/表情包/文件**自动下载留存**（QQ 链接会过期），入库去重，网页只引用本地副本 |
| 群管理 | 群列表（按机器人隔离）、群成员、本地禁言 + 官方禁言接口、群级配置覆盖、群名自动刷新 |
| 指令面板 | 按机器人向 QQ 注册可点指令列表（每个机器人独立 panel_id） |
| 定时任务 | 按机器人配置定时发送 |
| 插件 | **只支持 AstrBot 官方插件格式**（`astrbot.api.*` 兼容层，可直接放 AstrBot 插件） |
| 运维 | 运行状态、统计（今日/7 天/累计）、日志查看与下载、上下文文件管理、媒体留存管理、网页端一键关闭程序 |
| 云同步 | Cloudflare D1：上下文 / 插件数据 / 统计等按清单双向同步，冲突取时间戳新者，删除走墓碑 |
| 安全 | 网页访问令牌、管理员 openid、密钥掩码显示、图片代理 SSRF 白名单 |

> 网页后台共 11 个页面：聊天窗口 / 运行状态 / 统计 / 设置 / 插件管理 / 群管理 / 日志查看 /
> 上下文 / 指令面板 / 媒体留存 / 使用说明；设置页 17 个分组、107 个配置项。

---

## 二、快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动

```bash
python run.py            # 正常启动
```

Windows 也可以直接双击 **`start.bat`**（会自动检查并安装依赖，程序退出后 3 秒自动重启；
不想要自动重启就把文件末尾的 `:loop` / `goto loop` 删掉）。Linux/macOS 用 `./start.sh`。

### 3. 首次配置

打开 <http://127.0.0.1:8666/>，然后：

1. **「设置 → 机器人账号」**：填 QQ 开放平台的 **AppID / AppSecret**，勾选「启用」
   （可以加多个机器人，每个都独立连接）；
2. **「设置 → AI 服务」**：填 API Key / 接口地址 / 模型（任意 OpenAI 兼容服务；
   留空也能跑，交给插件和关键词回复）；
3. 保存 —— 页面会明确区分「保存即生效」和「需重启生效」；
4. 用 QQ 私聊机器人，或在群里 @ 它，消息就会出现在「聊天窗口」。

### 4. 其他命令

```bash
python run.py --check              # 装配自检（不连 QQ、不开端口）
python run.py --no-bots            # 只开网页后台，方便调界面
python run.py --port 9000          # 临时换端口
python run.py --config my.json     # 使用指定的配置文件
python tests/simulate_message.py --type group --text "你好"   # 注入模拟消息看界面效果
```

---

## 三、目录结构

```
QQBotMerged/
├── run.py                     # 启动入口（单实例保护 + 网页服务）
├── start.bat / start.sh       # 依赖检查 + 崩溃自动重启
├── requirements.txt           # flask / requests / websocket-client
├── config.json                # 首次运行自动生成（含全部配置，默认不进 Git）
├── config配置说明文件.txt       # 自动生成的中文配置说明（默认不进 Git）
├── core/                      # 后端
│   ├── paths.py               # 路径与目录
│   ├── config_schema.py       # 默认配置 + 字段元信息 + 设置页分组
│   ├── config_manager.py      # 读写、补全迁移、环境变量覆盖、密钥掩码
│   ├── logger.py              # 控制台着色 + 按大小分割 + 日志页读取
│   ├── storage.py             # SQLite：消息/会话/媒体/群成员/禁言/机器人状态
│   ├── media_store.py         # 媒体下载留存（去重、防盗链、SSRF 白名单）
│   ├── qq_api.py              # QQ 开放平台 API（token/文本/富媒体/文件/群管理/面板）
│   ├── gateway.py             # WebSocket 网关（鉴权/心跳/重连/RESUME/intents 降级）
│   ├── runtime.py             # 多机器人调度 + 消息总线 + 发送出口 + 热更新
│   ├── message_processor.py   # 回复链路（过滤→指令→关键词→插件→限速→AI）
│   ├── message_text.py        # 表情/@/Markdown/分段处理
│   ├── ai_client.py           # OpenAI 兼容客户端（含多模态）
│   ├── message_filter.py      # 关键词、敏感词、无意义消息
│   ├── context_manager.py     # 按机器人 + 会话隔离的上下文
│   ├── plugin_manager.py      # 插件系统（只支持 AstrBot 官方插件格式）
│   ├── astrbot_compat.py      # AstrBot 兼容层（astrbot.* 垫片 + 事件桥接）
│   ├── group_manager.py       # 群列表/成员/禁言/群配置/群名
│   ├── cloud_sync.py          # Cloudflare D1 云同步
│   └── stats.py               # 统计
├── web/
│   ├── server.py              # Flask 路由（50 个接口：聊天 / 运维 / 群管理 / 媒体 / SSE）
│   ├── templates/index.html   # 单页外壳 + 左侧统一导航
│   └── static/css|js/         # 样式、动画层与 6 个前端模块
├── plugins/                   # 插件目录（只识别 AstrBot 插件：<插件名>/metadata.yaml + main.py）
│   ├── README.md              # 插件目录说明（官方格式）
│   ├── astrbot_demo/          # 示例插件
│   ├── astrbot_plugin_dice/   # 骰子：/骰子、/骰子 3d6
│   └── astrbot_plugin_daily_checkin/  # 签到：/签到 /积分 /查询 /抽奖
├── data/                      # 运行时数据（消息库、媒体、日志、上下文、插件数据）
└── tests/                     # 离线测试与工具
```

---

## 四、配置说明

- 配置都在 **`config.json`**（首次运行自动生成并补全缺失项），网页「设置」页是最方便的改法；
  每个配置项的中文名称/说明/单位/范围见 `config配置说明文件.txt`。
- **分组作用域**：`AI / 回复 / 过滤与安全 / 功能开关 / 发送策略 / 定时任务 / 指令面板 / 上下文`
  属于「每个机器人自己的设置」，保存时写进该机器人的覆盖（`bots[].overrides`）；
  其余（网页后台、存储、日志、界面、云同步、权限告警）是全局的。
- **一键应用**：「设置」页右上角可把当前机器人的设置复制给其它所有机器人。
- **环境变量覆盖**（优先级最高，适合 Docker/服务器）：

  ```bash
  QQBOT_WEB_PORT=9000                  # web.port
  QQBOT_AI_API_KEY=sk-xxx              # ai.api_key
  QQBOT_WEB_TOKEN=my-secret            # web.token
  QQBOT_CLOUD_SYNC_ENABLED=1           # cloud_sync.enabled
  QQBOT_AI__API_KEY=sk-xxx             # 双下划线写法也行
  ```

  认不出来的名字会打一条 warning（不会静默忽略）。
- **需要重启才生效**：`web.host / web.port / web.debug`、`bots`、`storage.*`、重连间隔。
- **安全**：默认监听 `127.0.0.1`；改成 `0.0.0.0` 前**务必**设一个访问令牌
  （「设置 → 网页后台 → 访问令牌」），否则任何能访问该端口的人都能操作后台。

---

## 五、回复链路与消息规则

处理一条消息的顺序（每一步「为什么跳过」都会写进日志，便于排查）：

```
1 群/私聊总开关 → 2 本地禁言拦截 → 3 敏感词输入拦截 → 4 群聊是否需要 @
→ 5 去掉开头的 @提及 → 6 附件转文本 → 7 无意义消息过滤 → 8 内置指令
→ 9 关键词回复 → 10 插件（AstrBot） → 11 限速 → 12 AI 回复
```

内置指令：`/帮助`、`/清空上下文`、`/群设置`、`/图片说明 <链接>`，管理员另有管理指令。

关于群聊的两个平台行为（容易困惑，先说清楚）：

- **「接收所有消息」（全量模式）**：如果机器人在 QQ 后台开了这个功能，群里**每条消息**都会
  推给机器人（事件名是 `GROUP_MESSAGE_CREATE`），此时「是否 @ 了机器人」是程序根据消息里的
  mentions 判断的；机器人**只在自己被 @ 时回复**（可在「群管理」里把「需要 @ 才回复」关掉，
  让它参与全部聊天）。@全体成员不算 @ 机器人，不会触发回复。
- **@ 标签**：程序会把收到的 `<@openid>` / `<@all>` / `<qqbot-at-user …/>` 这类标记在网页里
  显示成可读的 `@某人` / `@全体成员`。**网页端发送 @（@某人、@全体成员）的功能已移除**
  —— 群里实测官方新标记会被原样显示成文本，等平台稳定后再做。

---

## 六、插件系统（AstrBot 官方格式）

**只支持 AstrBot 官方插件格式**，目录约定与官方一致：

| 内容 | 位置 |
|---|---|
| 插件本体 | `plugins/<插件名>/`（必须含 `metadata.yaml` + `main.py`） |
| 插件配置项定义 | `plugins/<插件名>/_conf_schema.json`（可选） |
| 插件配置值 | `data/config/<插件名>_config.json`（自动生成，可手改） |
| 插件数据 / 大文件 | `data/plugin_data/<插件名>/` |
| 插件依赖 | `plugins/<插件名>/requirements.txt`（可选） |

没有处理器的文件、裸 `.py`、以 `_`/`.` 开头的目录都会被忽略；旧的「原生插件」格式**已彻底移除**。

最小插件：

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

- 兼容层实现的是 `astrbot.api.*` 常用子集：`Star` / `filter` / 事件 / 消息链 / 配置 /
  KV 存储（`put_kv_data` 等）/ `StarTools` / provider 调用，并模拟 `ASTRBOT_VERSION = 4.9.2`。
- **暂不支持**（插件页会逐个列出）：插件自带 Web 接口与 Pages、`register_llm_tool`、
  文转图模板、会话控制器、知识库、Agent 执行器等 AstrBot 专有组件。
- 自带 3 个示例/实用插件：`astrbot_demo`（文字/图片/管理员指令）、`astrbot_plugin_dice`（骰子）、
  `astrbot_plugin_daily_checkin`（签到/积分/抽奖）。
- 安装：把插件目录整个复制进 `plugins/` → 「插件管理」页点「丢弃修改并重载」；
  停用/启用后点「保存插件设置」即可生效。

---

## 七、数据、备份与云同步

`data/` 下都是运行时数据：

| 路径 | 内容 | 说明 |
|---|---|---|
| `messages.db` | 消息 / 会话 / 媒体 / 群成员 / 禁言 | SQLite（WAL），**唯一需要备份的数据库** |
| `media/` | 自动留存的图片、表情包、文件 | 可清理，删了网页就看不到历史图片 |
| `logs/` | 运行日志（按大小分割） | 可清理 |
| `user_context/` | AI 上下文 JSON（按机器人+会话） | 可清理 |
| `config/<插件名>_config.json` | 插件配置 | 可能含插件自己的密钥，**不上云** |
| `plugin_data/<插件名>/` | 插件数据 | 参与云同步 |
| `group_settings.json`、`group_names.json`、`stats.json`、`command_panel.json`、`plugins_disabled.json` | 群配置/群名/统计/面板 id/插件启停 | 参与云同步 |

**备份**：停掉程序后整个复制 `data/` 与 `config.json` 即可；只备份 `data/messages.db`
也能保住聊天记录（还有 `-wal`/`-shm`，运行中复制不保险，建议先关程序）。

**云同步（Cloudflare D1）**：「设置 → 云同步」填 `account_id / database_id / api_token` 并启用；
「状态」页有测试连接 / 立即同步 / 解除暂停。同步范围是上面标了"参与云同步"的那些；
**永不上云**：`config.json`（含密钥）、消息库、留存媒体、日志。
同一套 D1 不要在**多台设备同时**开启同步，否则会互相覆盖。

---

## 八、测试与自检

全部离线运行（QQ API 客户端换成记录调用的桩，不联网、不消耗 token）：

```bash
python tests/test_offline.py     # 端到端 413 项：收发、留存、上传、清空、禁言、配置、
                                 # 插件、AstrBot 官方目录、云同步、群里全量模式 @ 识别…
python tests/check_frontend.py   # 前端契约：元素 id、设置页字段类型、分组完整性、机器人切换刷新
node tests/dom_test_messages.js  # 消息渲染 21 项（图片去重、文件消息、@ 标记展示…）
node tests/dom_test_conversations.js   # 会话列表 7 项
python tests/test_instance_lock.py     # 单实例保护 12 项
python run.py --check            # 装配自检（配置、存储、路由、表情/无意义判定）
```

---

## 九、常见问题

**启动后网页打不开 / 提示"程序已在运行"**
程序有单实例保护（`data/merged.lockdir` + `data/instance.json`）。确认没有别的实例在跑后，
删掉 `data/merged.lockdir`、`data/merged.lock`、`data/merged.pid`、`data/instance.json` 再启动。
端口被占用就换一个：`python run.py --port 9000` 或在设置里改。

**机器人在群里不回复**
先看日志，程序会直接写明原因，例如：
`群消息未 @ 机器人，已跳过（事件=GROUP_MESSAGE_CREATE…）`、`群 … 已关闭自动回复`、
`成员 … 处于禁言状态`。最常见的是开了「接收所有消息」但没 @ 机器人（或者「需要 @ 才回复」被打开）。

**发文件失败**
QQ 官方限制：文件 200MB、图片 20MB；超过 4MB 会自动改用官方分片上传。
平台对"文件消息"支持有限，失败时会自动退化为「发送下载链接」并说明原因。

**撤回 / 引用**
自己发的消息 2 分钟内可撤回；撤回**群成员**的消息需要机器人是群管理员；
引用超过 2 分钟的消息用 `message_reference`（程序会自动降级，不会因为过期而整条失败）。

**群成员列表是空的**
官方 `GET /v2/groups/{gid}/members` 目前要求机器人是群管理员（否则返回 11253）。
拉不到时程序会用**历史消息里出现过的成员**补齐，禁言/解禁照样可用。

**@全体成员**
官方文档写明「仅文字子频道可用」，群聊里机器人发 @全体成员基本不生效，所以本程序没有提供该发送功能。

**网页安全**
默认只监听 `127.0.0.1`。改成 `0.0.0.0` 时必须设置访问令牌，否则会有人能操作你的后台。

---

## 十、打包分发

### 1. 该打包什么

**必须**：`run.py`、`core/`、`web/`、`plugins/`、`requirements.txt`、`start.bat`、`start.sh`、`README.md`
**不要**：`data/`（聊天记录、日志、媒体，很大）、`config.json`（含 AppSecret / API Key）、
`__pycache__/`、`*.log`

### 2. Windows 一键打包（干净副本，已实测可用）

用 `robocopy` 只拷贝需要的文件、直接排除 `data/`、`config.json`、`__pycache__` 等，
**程序正在运行也能打包**（不会碰到被占用的数据库文件）：

```powershell
cd C:\Users\L\Desktop\DeepSeek
$src = "C:\Users\L\Desktop\DeepSeek\QQBotMerged"
$tmp = "$env:TEMP\QQBotMerged-pack"
$zip = "C:\Users\L\Desktop\DeepSeek\QQBotMerged-$(Get-Date -Format yyyyMMdd).zip"

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
robocopy $src $tmp /E /XD data __pycache__ .git .venv /XF config.json "config配置说明文件.txt" *.log *.zip /NFL /NDL /NJH /NJS
Compress-Archive -Path "$tmp\*" -DestinationPath $zip -Force
"已生成：$zip（$([math]::Round((Get-Item $zip).Length/1KB,1)) KB）"
```

> `robocopy` 退出码 0~7 都表示成功（1 = 正常复制了文件），不用当报错。

打出来的包里只有代码：`run.py`、`core/`、`web/`、`plugins/`、`tests/`、`README.md`、
`requirements.txt`、`start.bat`、`start.sh`、`.gitignore`（约 340 KB，56 个文件），
**不含** `config.json`（密钥）与 `data/`（聊天记录、媒体、日志）。

别人拿到后：

```bash
pip install -r requirements.txt
python run.py          # 或双击 start.bat
```

然后到自己电脑上打开 <http://127.0.0.1:8666/> 填自己的 AppID/AppSecret 与 AI Key 即可。

### 3. 自己换设备（要保留数据）

先**关掉程序**，然后整目录复制（`data/` 和 `config.json` 一起带上），到新设备后删掉这几个运行痕迹：
`data/merged.lockdir`、`data/merged.lock`、`data/merged.pid`、`data/instance.json`，
再 `pip install -r requirements.txt` 后启动即可。注意 `config.json` 里有密钥，别外发。

> 只想留下聊天记录的话，备份 `data/messages.db`（以及同目录的 `-wal`、`-shm`）就够；
> 运行中复制不保险，先关程序再复制。

---

## 十一、用 Git 上传到 GitHub

### 0. 准备

- 已安装 Git（本机是 `git version 2.55.0.windows.5`）。没装的话：`winget install Git.Git` 或
  从 <https://git-scm.com/downloads> 下载安装。
- 在 GitHub 网页上点 **New repository** 新建一个**空仓库**（例如 `QQBotMerged`）：
  **不要**勾选 "Add a README file" / .gitignore / license，避免和本地首推冲突。
- 本目录已有 `.gitignore`，会自动排除 `config.json`、`data/`、`__pycache__`、`*.zip` 等 —— **密钥不会上传**；
- 还有 `.gitattributes`，统一把源码按 LF 换行提交（`start.sh` 在 Linux 上才能正常运行），
  Windows 上的 `start.bat` 保持 CRLF。

### 1. 第一次上传（在 QQBotMerged 目录里执行）

```powershell
cd C:\Users\L\Desktop\DeepSeek\QQBotMerged

# ① 告诉 Git 你是谁（只需设置一次；邮箱可以用 GitHub 的 noreply 地址）
git config --global user.name "你的名字"
git config --global user.email "你的邮箱@example.com"

# ② 建库、暂存、核对、提交
git init                                  # 把这里变成 Git 仓库
git add .                                 # 暂存所有文件（.gitignore 已排除敏感文件）
git status                                # ★ 核对一遍：不该出现 config.json、data/
git commit -m "首次提交：QQBotMerged QQ 机器人多开后台"
git branch -M main                        # 主分支改名 main

# ③ 关联你自己的仓库（用户名/仓库名要对）
git remote add origin https://github.com/<你的用户名>/QQBotMerged.git

# ④ 推送
git push -u origin main
```

第一次 `push` 会弹窗要求登录：

- **推荐用 Personal Access Token**：GitHub → 右上角头像 → Settings → Developer settings →
  Personal access tokens → Tokens (classic) → Generate new token，
  勾选 **repo** 权限，复制生成的 token；push 时**用户名填 GitHub 用户名，密码填这个 token**。
- 或者用 GitHub CLI：`winget install GitHub.cli` → `gh auth login`（按提示浏览器登录）后直接 push。
- 或者用 SSH：`ssh-keygen -t ed25519 -C "你的邮箱"`，把 `~/.ssh/id_ed25519.pub` 内容贴到
  GitHub → Settings → SSH and GPG keys，然后把远端地址换成 `git@github.com:<用户名>/QQBotMerged.git`。

### 2. 以后更新代码

```powershell
cd C:\Users\L\Desktop\DeepSeek\QQBotMerged
git status                       # 看改了什么
git add -A                       # 或 git add 具体文件名
git commit -m "说明这次改了什么"
git push
```

常用查看命令：

```powershell
git log --oneline -10            # 最近 10 次提交
git diff                         # 还没暂存的改动
git diff --staged                # 已暂存待提交的改动
git restore <文件>               # 撤销工作区改动（回到上次提交）
```

### 3. 常见问题

| 现象 | 处理 |
|---|---|
| `remote origin already exists` | `git remote set-url origin https://github.com/<用户名>/<仓库>.git` |
| push 被拒（`non-fast-forward` / `fetch first`） | 远端有本地没有的提交：`git pull --rebase origin main` 后再 `git push` |
| 忘了密码 / 认证失败 | 重新生成 token（勾选 repo），或 `git config --global credential.helper manager` 用 Windows 凭据管理器 |
| 公司网络/代理连不上 GitHub | `git config --global http.proxy http://127.0.0.1:7890`（改成你的代理端口；取消用 `--unset`） |
| **不小心提交了 `config.json`** | `git rm --cached config.json` → `git commit -m "移除密钥文件"` → `git push`；**并且立刻去 QQ 开放平台重置 AppSecret、换掉 AI API Key**（历史提交里仍有明文） |
| 提交了很大的 `data/` | `git rm -r --cached data` → commit → push（GitHub 单文件 >100MB 会被拒；历史里的大文件需要 `git filter-repo` 才能彻底清掉） |
| 想只提交部分文件 | `git add run.py core/ web/ plugins/ requirements.txt README.md` |

### 4. 建议的仓库结构

```
QQBotMerged/
├── .gitignore          # 已排除 config.json / data/ / __pycache__
├── README.md           # 本文件
├── requirements.txt
├── run.py  start.bat  start.sh
├── core/  web/  plugins/  tests/
└── (可选) LICENSE      # 想开源的话加一个（如 MIT）
```

顺手也可以在 GitHub 仓库页面上把 **About** 描述和 topics 填上，
再在 Release 里附上第十节的 zip 包，别人下载就能直接用。

---

## 致谢

- 合并自工作区的 `API_qqbot` 与 `app` 两个程序；
- 插件体系对齐 **AstrBot** 官方格式（`astrbot.api.*`），可直接使用其插件生态；
- QQ 接口依据官方文档：<https://bot.q.qq.com/wiki/>。
