# AGENTS.md —— 面向 AI 开发者的项目说明

> **这份文件是给 AI 编码代理 / 新接手的开发者看的。**
> 动这个项目之前先读它：里面写了**现在是什么状态**、**哪些设计不能改**、
> **有哪些坑踩过了**、**下一步该做什么**。
>
> 建议用法：
> - 开新会话 / 派子代理时，把这份文件（和 `docs/AI_INDEX.md`）一起给它；
> - **不要**让它去读完整对话历史或整个仓库，按下面的「会话预算规则」来；
> - **本文件只放"当前状态 + 规矩"**，变更历史在 `docs/CHANGELOG.md`
>   —— 除非要查"某个改动是哪次做的"，否则**不用读**。

## 这个项目的文档分工

| 文件 | 给谁看 | 什么时候读 |
|---|---|---|
| **AGENTS.md**（本文件） | AI 编码代理 / 开发者 | **开工前必读**：状态、决策、坑、下一步 |
| `docs/CHANGELOG.md` | 维护者 / AI | 只在查"某功能是哪次改的"时读 |
| `docs/AI_INDEX.md` | AI | 找函数/类在哪个文件时读 |
| `README.md` | **使用者** | 怎么安装、怎么用 |
| `PACKAGING.md` | 使用者 / 维护者 | 启动器怎么装、怎么发 PyPI |
| `docs/QQ_MESSAGE_NOTES.md` | 开发者 | QQ 平台接口的字段与错误码（带官方来源） |
| `docs/PROGRESS-archive-*.md` | —— | **只读快照**，除非查很久以前的改动 |

---

## 目标

一个进程同时运行：
- 多个 QQ 官方机器人（各自独立 WebSocket，互不影响）
- 统一 Web 后台（11 个页面：聊天/状态/统计/设置/插件/群管理/日志/上下文/指令面板/媒体/说明）
- AstrBot 官方插件格式兼容
- Cloudflare D1 云同步

---

## 当前状态

- 程序版本：**v1.5.0**（唯一来源 `core/version.py` 的 `__version__`；
  网页侧栏/关于页/接口都从这里取）

### 常用命令

```bash
python run.py                  # 正常启动
python run.py --check          # 装配自检（不连 QQ、不开端口）
python run.py --no-bots        # 只开网页后台（调界面用）
python run.py --port 9000      # 临时换端口
```

### 测试

```bash
python tests/test_offline.py       # 端到端 576 项（集成/端到端，跑得慢）
python tests/check_frontend.py     # 前端契约：元素 id / 字段类型 / 分组完整性
python tests/test_instance_lock.py # 单实例锁
```

单元测试在 `tests/unit/`（**新用例请放这里**，不要往 `test_offline.py` 里加）：

| 文件 | 覆盖 |
|---|---|
| `test_markdown_send.py` | Markdown 发送与格式回退 |
| `test_at_mentions.py` | 群 @ 官方标签与旧写法转换 |
| `test_cmd_tags.py` | 指令标签与「只在 Markdown 生效」判定 |
| `test_plugin_safety.py` | 插件源码 AST 静态安全扫描 |
| `test_plugin_sandbox.py` | 插件进程内软限制（loose/strict） |
| `test_dispatch_isolation.py` | 插件分发状态线程隔离 |
| `test_cloud_secrets.py` | 云同步上传前的敏感字段扫描 |
| `test_media_dedup.py` | 媒体留存去重 |
| `test_ai_scope.py` | 内置 AI 按机器人解析 |
| `test_member_role.py` | 群成员身份解析 |
| `test_ssrf_guard.py` | 媒体下载 SSRF |
| `test_security_fixes.py` | 历次审计修复的回归 |
| `test_security_audit.py` | 配置安全体检 / 审计日志与脱敏 |
| `test_security_v135.py` | 同源判定 / 令牌失败拒绝启动 / 审计取证 / 回滚清理 |
| `test_auth.py` | 令牌常数时间比较 / 爆破防护 / 弱令牌告警 |
| `test_csrf.py` | 跨站请求防护 / 首次运行生成令牌 |
| `test_token_page.py` | 无令牌不加载页面 / 控制台与日志分离 |
| `test_csp_boot.py` | CSP 与前端启动数据 |
| `test_updater.py` | 版本比较 / 自动更新安全与回滚 |
| `test_versioning.py` | 版本号规则 / 语义化版本优先层级 |
| `test_bot_probe.py` | 云同步日志降级 / `bot_info()` 客户端 |
| `test_bot_names.py` | 机器人名称自动维护 / 更新安装时序 |
| `test_plugin_scope.py` | 插件范围写白名单 / 添加行按钮位置 |
| `test_alerts.py` | 出错告警 / openid 按机器人隔离 |
| `test_admin_scope.py` | 管理员权限按机器人隔离 |
| `test_static_deps.py` | Werkzeug 依赖下限 / 静态路由白名单 / 管理员不继承全局 |
| `test_launcher.py` | qqbot-cli 启动器：下载安全 / tarball 壳目录 / 向导 / 安装目录可选 |
| `test_uninstall.py` | qqbot-cli 卸载 / 数据导出 / Windows·Linux·macOS 跨平台 |
| `test_cli_hardening.py` | 启动器安全加固（审计 2026-11-25 的 9 项：解压越界 / 状态文件 / 权限 / 导出链接 / 重定向 / 依赖下限 / 卸载 / CI / pip） |
| `test_readme_toc.py` | README 目录锚点与排版 / 统计数字 |

DOM 测试（`node`）：

```bash
node tests/dom_test_messages.js        # 消息渲染 40 项
node tests/dom_test_globals.js         # 全局命名空间保护 24 项
node tests/dom_test_update.js          # 版本显示与更新 27 项
node tests/dom_test_conversations.js   # 会话列表 7 项
node tests/dom_test_settings.js        # 设置页重复插入 5 项
node tests/dom_test_settings_nav.js    # 设置页导航结构 23 项
node tests/dom_test_settings_nav_render.js  # 导航真实渲染 15 项
```

其它：`tests/README.md`（分层约定）、`tests/simulate_message.py`（注入模拟消息）、
`python -m core.plugin_safety plugins`（插件安全自查）

### 目录与配置

- 接口索引：`docs/AI_INDEX.md`（本文件不再内嵌）
- 平台接口事实/错误码：`docs/QQ_MESSAGE_NOTES.md`（每条带官方来源）
- 配置文件：`config.json`（首次运行自动生成）
- 数据目录：`data/`（消息库、媒体、日志、上下文、插件数据）
- 插件目录：`plugins/`（只支持 AstrBot 官方格式）

---

## 已完成模块（不要再重写，先读代码再改）

| 模块 | 文件 | 状态 | 说明 |
|---|---|---|---|
| 路径工具 | `core/paths.py` | 完成 | BASE_DIR / RESOURCE_DIR / 目录常量 / PyInstaller 兼容 |
| 配置 Schema | `core/config_schema.py` | 完成 | DEFAULT_CONFIG / FIELDS / SECTION_LABELS / PER_BOT_SECTIONS |
| 配置管理 | `core/config_manager.py` | 完成 | 读写 / 迁移 / 环境变量 / 按机器人覆盖 / 密钥打码 |
| 日志 | `core/logger.py` | 完成 | 彩色控制台 + 按大小分割 + 后台读取 + 访问日志总闸 |
| 消息存储 | `core/storage.py` | 完成 | SQLiteStore / MemoryStore / 会话 / 媒体 / 群成员 / 禁言 |
| 媒体留存 | `core/media_store.py` | 完成 | 收到即下载 / 去重 / 防盗链 / 魔数校验 |
| 上下文 | `core/context_manager.py` | 完成 | 按 bot+会话隔离 / 文件落地 / 云同步 |
| 文本处理 | `core/message_text.py` | 完成 | 表情标记 / @提及 / Markdown 清理 / 分段 |
| 消息过滤 | `core/message_filter.py` | 完成 | 关键词 / 敏感词 / 无意义过滤 / 时长解析 |
| 消息链路 | `core/message_processor.py` | 完成 | 过滤→指令→关键词→插件→限速→AI→分段发送 |
| WebSocket 网关 | `core/gateway.py` | 完成 | 鉴权 / 心跳 / 重连 / intents 自动降级 / @ 判定 |
| QQ API | `core/qq_api.py`（+`qq_upload.py`/`qq_errors.py`） | 完成 | 发送 / 分片上传 / 禁言 / 撤回 / 指令面板 / 富媒体 |
| 群管理 | `core/group_manager.py` | 完成 | 群列表 / 成员 / 禁言（本地+官方）/ 群名 / 群配置 |
| 运行时 | `core/runtime_pkg/`（`core/runtime.py` 是兼容层） | 完成 | 多机器人调度 / 消息总线 / 发送出口 / 热更新 |
| 插件管理 | `core/plugin_manager.py` | 完成 | 只支持 AstrBot 格式 / 按机器人隔离 / 旧数据迁移 |
| 插件安全扫描 | `core/plugin_safety.py` | 完成 | AST 静态扫描（不执行代码）+ 插件页徽章 |
| AstrBot 兼容 | `core/astrbot_shim/`（`astrbot_compat.py` 是兼容层） | 完成 | Star / filter / 事件 / 消息链 / Provider / KV |
| 云同步 | `core/cloud_sync.py` | 完成 | D1 / 墓碑 / 安全闸 / 上传前密钥字段扫描 |
| 自动更新 | `core/updater.py` | 完成 | 检查/下载/解压/备份/安装/回滚 + 双源校验 |
| 版本号 | `core/version.py` | 完成 | 唯一来源 + SemVer 2.0.0 解析与比较 |
| 名称自动维护 | `core/bot_names.py` | 完成 | 保存时取名称 + 每小时校正 |
| 出错告警 | `core/alerts.py` + `core/alert_checks.py` | 完成 | 按机器人隔离；离线/插件失败/存储降级 |
| 配置安全体检 | `core/security_audit.py` | 完成 | 启动时报告危险配置组合（只报告不拦截） |
| 审计日志 | `core/audit_log.py` | 完成 | 敏感操作留痕 + 脱敏 |
| 统计 | `core/stats.py` | 完成 | 今日 / 7 天 / 累计 |
| Web 后台 | `web/server.py` | 完成 | Flask 单端口 / SSE / 令牌鉴权 / CSRF 防护 |
| 前端 | `web/static/js/*` `web/templates/index.html` | 完成 | Chat / Settings / Admin / Groups / App / Util / Effects |
| 启动入口 | `run.py` | 完成 | 单实例保护 / 自检 / 令牌生成 / 启动保护 |
| 自带插件 | `plugins/astrbot_demo` `dice` `daily_checkin` `ollama` | 完成 | 示例 + 骰子 + 签到 + 本地 AI |

---

## 关键设计决策（不要改）

1. **单实例保护**：文件锁 + 目录锁 + 独占端口，防止两个进程各回一条。
2. **消息去重**：`storage.has_message(bot_id, msg_id)`，断线重连不重复回复。
3. **按机器人隔离**：会话键 `bot_id:scope:openid`，上下文/禁言/插件/配置覆盖都带 bot_id。
4. **配置热更新**：只重载真的改了的分组（`_changed_sections`），改动画不重启云同步。
5. **插件只支持 AstrBot 格式**：裸 `.py` 原生插件已彻底移除。
6. **密钥不同步**：`data/config/<插件>_config.json` 故意不在 SYNC_PATHS 里。
7. **消息库/媒体不上云**：`messages.db` 和 `media/` 在 NEVER_SYNC 里。
8. **群名以 QQ 为准**：绝不用"最后发言者昵称"当群名。
9. **引用不受 2 分钟限制**：2 分钟只属于"撤回"；引用用 `message_reference`。
10. **图片去重按内容 sha256**：多个机器人同在一个群时，同一条消息的图片会分别下发、
    且下载地址带各自 rkey 签名（URL 不同），只按 URL/文件名去重拦不住 ——
    同一份内容只落一份文件（见 `MediaStore.save_bytes`）。
11. **主题全部走变量**：浅色/深色分别集中写在 `app.css` 的 `:root` 与
    `body[data-theme="dark"]`（含 `@media (prefers-color-scheme: dark)` 的复制块）。
    组件样式里**不要写死颜色**，新颜色要在**三处**都赋值 —— 只写 `:root`
    会让深色模式漏底色（历史踩过）。
12. **markdown 发送**：`send.message_format` 决定默认格式，网页可单次覆盖；
    markdown 走 `msg_type=2 + markdown.content`（**不能**带顶层 `content`）；
    平台拒绝时**自动降级纯文本**并把原因写进 `_note`/日志。
13. **群内 @ 某人**：正文嵌官方标签 `<qqbot-at-user id="..." />`，所有发送出口
    统一把旧写法 `<@openid>` 转成它；`@全体成员` 群里退化成可读文字；
    @ 候选名单 = 群成员缓存 + 最近发言者。
14. **@ / 指令标签必须走 Markdown（实测结论）**：官方写"文本消息也支持 @某人"，
    但真机实测**纯文本里标签不渲染**。所以 `Runtime.send_text()` 在纯文本请求里
    检测到这类标签就**自动改用 markdown**（只影响本次发送，原因写进 `_note`）。
    判定集中在 `core/message_text.py` 的 `markdown_only_tags()`。
15. **指令标签**：`build_cmd_enter()` / `build_cmd_input()` 生成官方标签，
    值统一 `quote(safe="")` 并按 **100 字符**截断；群聊限制是客户端行为，程序不假装能校验。
16. **版本号用语义化版本 2.0.0**：修 bug → 修订号；**加功能 → 次版本号并把修订号归零**；
    不兼容 → 主版本号。已发布的版本号与内容**不得更改**。详见 README §10.1。
17. **openid 按机器人隔离**：同一个人在每个机器人下的 openid 完全不同。
    因此 `security` 整组（管理员名单 / 告警开关 / 告警 openid / 冷却）**按机器人隔离**，
    告警由**出事的那个机器人自己发**。任何时候读这些配置都必须走
    `runtime.bot_config(bot_id)`，**不要读全局 `runtime.config`**。
18. **令牌与 CSRF**：`web.token` 为空 = 不校验；非空时用常数时间比较 + 爆破锁定。
    同源判定比较 `(scheme, host, port)` 三元组，`same-site` 不单独放行。
    令牌为空且监听非本机时**拒绝启动**（除非显式 `--allow-no-token`）。

---

## 已知问题 / 技术债

按优先级：

1. `core/qq_api.py` 单文件仍偏大（约 900 行），群管理相关可再拆
2. `tests/test_offline.py` 单文件过大（约 4800 行），跑一次慢（**新用例请放 `tests/unit/`**）
3. 插件是任意 Python 代码，`PluginRuntime` 门面拦不住越权（`import os` 仍可读 config.json）；
    `core/plugin_safety.py` 只是**提示**，`plugin_sandbox` 只是**软限制**，都不是真沙箱
4. 云同步的敏感字段扫描**只按字段名**判断；密钥直接写进正文（不是字段）时扫不出来
5. Windows 上 `msvcrt.locking` 强杀后可能残留锁（已做自愈，边界情况仍在）
6. `data/user_context/` 上云涉及用户隐私，未做脱敏
7. **出错告警的固有缺陷**：出事的机器人如果本身就断了（凭据失效等），
   就发不出告警。这是"由出事的机器人自己发"这一设计的代价。
   目前只在日志里写明原因，不会假装成功。
   若要根治，方向是：兜底发送机器人 / 多机器人互相兜底 / 告警落网页 / 外部通道。
8. markdown 相关边角：① 发送格式没落库（`messages` 表无该列），只在接口返回与日志里；
   ② 真实平台 markdown **未联网实测**（只有桩测试）；③ 官方不支持清单里的语法
   （代码块/表格/行内代码）会原样显示或触发降级；④ 未做流式 markdown、未把本地图片转公网 URL。
9. @ 标签边角：① "纯文本不渲染"只由真机自测确认过一次；② 群里 @全体成员官方不支持，已退化；
   ③ 私聊写 @ 标签不生效（程序原样发出，不擅自删）；④ 候选名单里的名字可能仍是占位。
10. 指令标签边角：① `reference=true` **未真机验证**；② 回车指令的群聊限制程序不拦截；
    ③ urlencode 后客户端发送的是解码文本还是编码串，官方未写、未实测；
    ④ 多个指令标签组合、与 `keyboard` 组合的边界未测。

---

## 会话开工前先做

1. 读本文件 + `docs/AI_INDEX.md`；
2. 跑 `python tests/test_offline.py`、`python tests/check_frontend.py` 确认基线是绿的；
3. 收工时更新 `docs/CHANGELOG.md` 的「最近变更」与「下一步」，再开新会话。

---

## 提交前必须做的验证

### ① 常规回归（每次改代码都要跑）

```bash
python tests/test_offline.py       # 端到端 576 项
python tests/check_frontend.py     # 前端契约
python tests/test_instance_lock.py # 单实例锁
python run.py --check              # 装配自检
```

改到启动器（`qqbot_cli/`）时，还要跑：

```bash
python tests/unit/test_launcher.py
python tests/unit/test_uninstall.py
python tests/unit/test_cli_hardening.py
```

### ② 干净环境验证（**发版前必做**）

**为什么必须**：开发机的 Python 环境里往往"碰巧"装着一些包，
它们不在 `requirements.txt` 里，但代码依赖它们 —— 本地能跑，用户装完就崩。

这个坑**真的发生过**：项目一直用 PyYAML 解析插件的 `metadata.yaml`，
但 `requirements.txt` 里从来没有它。开发机碰巧装着，所以从没暴露；
CI 的干净环境一跑 `test_offline.py` 就挂（`metadata.yaml` 那项把
多行块 `description: |` 读成了字面量 `|`）。

**怎么做**：

```bash
# 1. 建一个干净的环境（不要用开发用的那个）
python -m venv /tmp/clean_check

# 2. 只装 requirements.txt 里的东西
/tmp/clean_check/bin/pip install -r requirements.txt

# 3. 用干净环境跑测试
/tmp/clean_check/bin/python tests/test_offline.py
/tmp/clean_check/bin/python tests/check_frontend.py
```

Windows 上路径是 `/tmp/clean_check/Scripts/python.exe`。

**判定标准**：干净环境的结果必须和开发机**完全一致**
（同样是 576 项通过）。少过一项，多半就是某个依赖没写进 `requirements.txt`。

> 这条已经在 CI 里自动化了：`.github/workflows/publish-cli.yml` 的 `test` job
> 会在干净的 ubuntu runner 上跑一遍。**但 CI 只在发版时跑**，
> 平时改完代码还是要自己用干净环境验一次，尤其是**新加了 import 之后**。

### ③ 检查有没有带入不该有的东西

```bash
git status --short                      # 确认没有 data/ 或 config.json 被加进来
git grep --cached -n -E "gho_|sk-[A-Za-z0-9]{20,}"   # 密钥（exit 1 = 没有）
```


---

## 下一步（按优先级）

- [x] P1：依赖安全基线（2026-11-18）：`requirements.txt` 显式钉 `Werkzeug>=3.1.9`
      （CVE-2026-102598），最低 Python 提到 3.9；静态路由改为白名单实现
- [x] P1：管理员旧配置不再继承全局（2026-11-18，审计 AUTH-SCOPE-001）
- [ ] P2：指令标签真机验证（`reference=true` 的引用行为、urlencode 后客户端点击发送的
      到底是解码文本还是编码串；官方都未写），以及 @ 标签在纯文本消息里的渲染结论再确认一次
- [ ] P2：给 `core/alerts.py` 补"发不出去时怎么办"（见技术债第 7 条，具体方案待定）
- [x] P1：`qqbot-cli` 已发布到 PyPI 并实测可装（2026-11-25）。
      以后发新版：改 `pyproject.toml` 的 `version` → 推 `cli-v*` tag 或在 Actions 手动触发
- [ ] P3：生成依赖锁文件 / SBOM，在 CI 里做安全公告扫描（现在只钉了 Werkzeug 一个）
- [ ] P3：`tests/test_offline.py` 逐节迁移到 `tests/unit/`（老用例没有搬动过）

---

## 禁止事项

- 不要重写已完成的模块，先读代码 + `docs/AI_INDEX.md` 再改
- 不要删除 `tests/` 下任何测试
- 不要改自带插件对外暴露的指令名（`/astrbot`、`/骰子`、`/签到`、`/积分`、`/抽奖`）
- 不要动 `data/` 下真实数据（测试用 `data/_selftest/`）
- 不要在没有更新 AGENTS.md 的情况下开新会话
- 不要全量读超过 300 行的文件，用片段读取
- 不要改 `config.json` 里默认值（改 `core/config_schema.py` 的 DEFAULT_CONFIG）
- 不要把密钥写进代码或测试
- 不要让子代理读完整历史对话
- **不要往归档文件 `docs/PROGRESS-archive-*.md` 里追加内容**（它是只读快照）

---

## AI 会话预算规则

写进每次开新会话的第一条消息：

【会话预算】

最大步数：100 步

每步最大输出：4k token

每步最大上下文：5 万 token（不要全量读仓库）

最大子代理数：同时 3 个

单子代理最大：30 步 / 100 万 token

【上下文规则】

只读 AGENTS.md + docs/AI_INDEX.md + 相关代码片段

---

## 最近变更（摘要）

> **这里只放最近 2 条的一句话摘要**。要细节就去
> [`docs/CHANGELOG.md`](docs/CHANGELOG.md) 搜日期 —— 那里面有完整的问题描述、
> 代码证据和验证方式。
>
> **加新记录时**：
> 1. 在 `docs/CHANGELOG.md` 顶部写完整版；
> 2. 这里顶部加一句话摘要，最旧的挤出去，只留 2 条。
>
> **无论写哪个文件，都不要写入**：
> 谁提出/谁反馈的需求来源、对话过程、个人身份或口径、
> **真实配置内容**、真实数据规模（群数/成员数/消息条数等）、
> **本机路径**（`C:\Users\...`、`/home/...` 之类）、实例环境的操作过程、截图来源。
> 只写「改了什么 + 为什么改 + 怎么验证」。

- **2026-11-25** — 启动器说明随包分发：`qqbot_cli/PACKAGING.md` 与仓库根那份**内容必须一致**，
  `[tool.setuptools.package-data]` 已声明，`qqbot --help` 会打出它的路径。
  改根那份记得同步复制（测试 [J] 会查）。详见 CHANGELOG。
- **2026-11-25** — 按第三方审计修完启动器的 9 项问题（qqbot-cli **1.0.2**）。
  重点是两项高危：解压路径穿越（`_safe_member` 只看路径开头，漏了中间组件）、
  以及不可信 cwd 状态文件可导致执行任意程序。
  详见 CHANGELOG「按审计报告修完启动器的 9 项安全问题」。

---

**更早的记录**：完整变更历史见 [`docs/CHANGELOG.md`](docs/CHANGELOG.md)；
2026-10-06 ~ 2026-11-07 的早期记录在
[`docs/PROGRESS-archive-2026-11-15.md`](docs/PROGRESS-archive-2026-11-15.md)（只读快照）。
