# QQBotMerged 项目进度

> 每次开新 AI 会话 / 派新子代理前，先读这份文件，不要读全历史。
>
> **历史变更记录已归档**：`docs/PROGRESS-archive-2026-11-15.md`
> （2026-10-06 ~ 2026-11-07 的 47 条记录，日常不需要读；只有想查"某个改动是哪次、
> 为什么做的"时才去翻）

---

## 目标

一个进程同时运行：
- 多个 QQ 官方机器人（各自独立 WebSocket，互不影响）
- 统一 Web 后台（11 个页面：聊天/状态/统计/设置/插件/群管理/日志/上下文/指令面板/媒体/说明）
- AstrBot 官方插件格式兼容
- Cloudflare D1 云同步

---

## 当前状态

- 程序版本：**v1.4.4**（唯一来源 `core/version.py` 的 `__version__`；
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
3. 收工时更新「最近变更」与「下一步」，再开新会话。

---

## 下一步（按优先级）

- [x] P1：依赖安全基线（2026-11-18）：`requirements.txt` 显式钉 `Werkzeug>=3.1.9`
      （CVE-2026-102598），最低 Python 提到 3.9；静态路由改为白名单实现
- [x] P1：管理员旧配置不再继承全局（2026-11-18，审计 AUTH-SCOPE-001）
- [ ] P2：指令标签真机验证（`reference=true` 的引用行为、urlencode 后客户端点击发送的
      到底是解码文本还是编码串；官方都未写），以及 @ 标签在纯文本消息里的渲染结论再确认一次
- [ ] P2：给 `core/alerts.py` 补"发不出去时怎么办"（见技术债第 7 条，具体方案待定）
- [ ] P3：生成依赖锁文件 / SBOM，在 CI 里做安全公告扫描（现在只钉了 Werkzeug 一个）
- [ ] P3：`tests/test_offline.py` 逐节迁移到 `tests/unit/`（老用例没有搬动过）

---

## 禁止事项

- 不要重写已完成的模块，先读代码 + `docs/AI_INDEX.md` 再改
- 不要删除 `tests/` 下任何测试
- 不要改自带插件对外暴露的指令名（`/astrbot`、`/骰子`、`/签到`、`/积分`、`/抽奖`）
- 不要动 `data/` 下真实数据（测试用 `data/_selftest/`）
- 不要在没有更新 PROGRESS.md 的情况下开新会话
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

只读 PROGRESS.md + docs/AI_INDEX.md + 相关代码片段

---

## 最近变更

> **书写规范（每次更新本文件都必须遵守）**
> 每条记录只写「**日期 + 改了什么 + 为什么改 + 怎么验证**」四件事。
> **不要写入**：谁提出/谁反馈的需求来源、对话过程、个人身份或口径、真实配置内容、
> 真实数据规模（群数/成员数/消息条数等）、实例环境的操作过程、截图来源。
>
> **只保留最近若干轮**；更早的记录已在归档文件里，本文件不再堆叠。

- 2026-11-22：**静态路由白名单补齐 Windows 上标数字设备名（审计 SEC-STATIC-001，中危）**：
  - **问题**：`_WINDOWS_DEVICE_NAMES` 只含 ASCII 写法，而 Windows 还认
    **上标数字**形式：`COM¹`/`COM²`/`COM³`/`LPT¹`/`LPT²`/`LPT³`（U+00B9/B2/B3）。
    实测 `COM¹.js` 这类路径**能通过白名单**（`.js` 后缀匹配、主干不在集合里），
    随后进入 `send_from_directory()`。这是上一轮白名单实现的漏网。
  - **改了什么**：不再只靠"枚举设备名 + 字符串精确匹配"，而是加两层：
    ① `_normalize_device_stem()` 先把上标数字归一成普通数字再判设备名；
    ② **文件名只允许 ASCII 字母/数字/点/横线/下划线**（允许清单而非禁止清单）——
    这样 `COM¹`、全角 `ＣＯＮ`、`COM⁴`、零宽字符/不换行空格/组合重音等
    Unicode 变体全部进不来，不必逐个枚举。
  - **为什么改成允许清单**：设备名的 Unicode 变体是开放的（上标 ¹²³ 之外还有全角、
    组合字符、零宽符等），逐个往禁止清单里加永远会漏。静态资源本来只需要服务
    9 个前端文件，允许清单足够且不会误伤。
  - **怎么验证**：`test_static_deps.py` 从 59 项扩到 **122 项**：
    新增 [B2b] 上标数字设备名 36 项（3 种上标 × 4 个前缀 × 3 种后缀）、
    [B2c] 其它 Unicode 变体 6 项（全角/上标 4/零宽/不断行空格/组合重音/软连字符）、
    [B2d] 正常资源不误伤 13 项、[B6b] HTTP 面 8 项（带上标变体的真实请求也 404）。
    全套回归：`test_offline` 576 项、`check_frontend` 全绿。

- 2026-11-18：**修 Werkzeug 设备名路径风险（CVE-2026-102598）+ 静态路由白名单 + 管理员旧配置不再继承全局**：
  - **① 依赖约束**：`requirements.txt` 原来只声明 `flask>=2.0`，把 Werkzeug 当传递依赖，
    允许装到受影响的版本。本机实测装的是 **3.1.8**，正好落在受影响范围。
    Werkzeug <3.1.9 在 Windows NTFS 上，形如 `NUL:` 的空 ADS 标记设备名会让
    静态文件请求**挂起**（GHSA-g6x2-hccm-hh4m）。现在显式钉 `Werkzeug>=3.1.9`，
    并把本机升级到 3.1.9。因为 3.1.9 要求 Python ≥3.9，README 的最低 Python 从 3.8+ 改为 **3.9+**。
  - **② 静态路由白名单**：Flask 自动注册的 `/static/<path:filename>` 不做令牌校验
    （GET 属安全方法，CSRF 钩子直接放行），最终交给 Werkzeug 解析路径。
    改成自己实现的受限路由：先过 `_static_path_allowed()` 白名单
    （拒 Windows 设备名 / ADS / 控制字符 / 反斜杠 / `..` / 非白名单扩展名），
    再 `send_from_directory`。**不合法的请求根本不会碰到文件系统**。
    **踩到的坑**：只在构造后设 `app.config["static_folder"] = None` **撤不掉**
    Flask 在构造时注册的路由，会并存两条同名路由，而 Flask 走先注册的那条（不受限）——
    实测 `/static/js/util.js::$DATA` 仍返回 200。必须在 `Flask(...)` 构造时就不传 `static_folder`。
  - **③ 管理员旧配置不再继承全局**（审计 AUTH-SCOPE-001）：
    `security` 自 v1.4.1 起按机器人隔离，但升级前的旧配置里可能还留着全局
    `admin_openids`。`resolve_for_bot()` 从全局深拷贝再覆盖，于是**没单独配过的机器人
    会继承全局名单** —— 旧名单对所有机器人继续有效（跨机器人越权）。
    现在 `security` 段中**没被该机器人 overrides 覆盖的键，一律取默认值**
    （没有管理员 / 不启用告警），逼用户给每个机器人单独配；
    显式配 `[]` 表示"这个机器人没有管理员"，与未配置区分开。
    其它 per-bot 分组（ai/reply/...）仍照旧继承全局，没有误伤。
  - **怎么验证**：新增 `tests/unit/test_static_deps.py`（59 项）：依赖下限、
    README 的 Python 版本声明、白名单函数逐项（设备名/ADS/穿越/控制字符/扩展名）、
    **只有一条 static 路由且 endpoint 是受限版本**、正常资源无令牌可取、
    可疑路径带令牌也 404、以及管理员旧配置不继承 / 各机器人自己的配置仍生效 /
    非 security 段照旧继承。全套回归：`test_offline` 576 项、`check_frontend`、
    `run.py --check` 全绿（升级 Werkzeug 后复跑通过）。

- 2026-11-15：**统一 README 的三级标题编号，修正失效的目录锚点**：
  - 问题：第三章用 `### 1. 安装依赖`（像章号），第五/七/十章用 `### 5.1 xxx`，两套并存；
    `3.1 版本与自动更新` 还排在了 `4. 其他命令` 之后；编号有 `3.1、`（顿号）与 `5.1 `（空格）
    两种写法；目录里 `[版本与自动更新](#31版本与自动更新)` 是**坏锚点**
    （实际是 `#35-版本与自动更新`），点了跳不过去。
  - 改了什么：第三章四个步骤统一为 `3.1~3.4`，版本说明顺延 `3.5` 并归位；目录锚点同步；
    三处没标语言的代码块补 `text`；README 里过期的「117 个配置项」改为实际的 118。
  - 怎么验证：新增 `tests/unit/test_readme_toc.py`（21 项）—— 按 GitHub 锚点规则把标题转 slug
    后与目录逐一比对，并校验编号格式、章号连续、围栏成对、README 里的统计数字与代码一致。

- 2026-11-15：**修复管理员权限仍读全局配置导致的跨机器人越权（审计 SEC-AUTH-003，中危）**：
  - 问题：上一轮把 `security` 加进 `PER_BOT_SECTIONS` 后，`message_processor.py` 里判定管理员的
    **两处**仍读全局（`_handle_command()` 与 `_is_admin()`）—— 各机器人单独配的名单不生效，
    且 **bot A 的管理员在 bot B 下照样有权限**。属于"配置作用域改了、读取路径没跟上"。
  - 改了什么：两处都改为 `runtime.bot_config(bot_id).list_of("security", "admin_openids")`；
    `_is_admin()` 增加 `bot_id` 参数，两个调用点都传入。
  - 怎么验证：新增 `tests/unit/test_admin_scope.py`（18 项），含**双机器人交叉验证**
    （bot1 的管理员在 bot2 下无权限，反之亦然）与真配置读写核对。

- 2026-11-14：**「权限与告警」整组改为按机器人隔离**：
  - 问题：openid 本身就是按机器人隔离的（同一个人在各机器人下 openid 不同），
    用一份全局配置表达不了；上一版"既按机器人隔离、又留一个全局『选哪个机器人发』"互相矛盾。
  - 改了什么：`security` 整组进 `PER_BOT_SECTIONS`；**去掉 `alert_bot_id`**，
    告警由出事的机器人自己发（凭据与 openid 天然配对）；`Alerter.notify()` 改为按 bot_id；
    冷却按 `bot_id|key` 分桶。配置项 119 → 118。
  - 怎么验证：`test_alerts.py` 重写为按机器人隔离（57 项），含真接口分别给两个机器人
    存不同 openid 后读回核对。

- 2026-11-14：**实现出错告警（此前只是个空配置项）**：
  - 问题：`security.alert_owner_openid` 一直只存在于配置定义里，**没有任何代码读它发消息** ——
    填了也不会收到任何东西。
  - 改了什么：新增 `core/alerts.py`（选定目标、单聊发送、按 key 冷却、失败留痕）与
    `core/alert_checks.py`（每 60 秒体检：机器人离线超 180 秒 / 插件加载失败 / 存储降级），
    运行时 `start()` 挂 `_start_alert_watcher()`。只报严重问题，普通警告不发。
  - 怎么验证：`test_alerts.py`（原 38 项，后重写为 57 项）。

- 2026-11-14：**插件管理页勾选改为写白名单；「添加一行」插到按钮之前**：
  - 问题：插件管理页保存时写的是黑名单（`disabled_names`），而设置页「本机器人启用的插件」
    读白名单（`enabled_names`）—— 勾了插件设置页却显示为空。
    另外新行用 `insertAdjacentHTML('beforeend')` 插到容器末尾，而按钮也在容器里，
    新行跑到了按钮**后面**。
  - 改了什么：改为写 `enabled_names`；4 个添加入口（一行/机器人/任务/指令）都改用
    `beforebegin`，按钮始终在最后。
  - 怎么验证：`test_plugin_scope.py`（19 项）。

- 2026-11-14：**版本号规则改为严格遵循语义化版本 2.0.0，并修正先行版本号解析**：
  - 问题：此前发版一律只加修订号，但 1.3.7/1.3.8 含功能新增，按第 7 条应升次版本号。
    另外 `normalize_version()` 用 `re.match(r"(\d+(?:\.\d+)*)")` **截掉了先行版本号**，
    `1.4.0-beta` 会被当成 `1.4.0`，发 beta 时装过正式版的用户收不到更新。
  - 改了什么：规则写进 `core/version.py` 与 README §10.1，并记录 1.3.7/1.3.8 的历史误标
    （第 3 条：已发布版本不得更改，从 1.4.0 起合规）；
    重写解析以保留先行版本号，按第 11.3/11.4 条实现完整优先层级，`+build` 按第 10 条丢弃。
  - 怎么验证：`test_versioning.py`（36 项），SemVer 官方优先级链逐项通过；
    `test_updater.py` 里 `normalize('1.2.7-beta')` 的期望从 `1.2.7` 改为 `1.2.7-beta`
    （原断言把 bug 固化成了预期）。

- 2026-11-14：**启动日志与体检排版整理**：
  - 改了什么：① 去掉四行的「后台访问地址」框，令牌并入「网页后台已就绪：…?token=…」一行；
    ② 配置安全体检改用纯文本标签 `[严重]/[风险]/[提示]` 并让续行缩进与正文起点严格相等
    （emoji 在终端占 2 列但 `len()` 算 1 字符，用它对齐必然错位）；
    ③ 更新安装不再打印 `====` 横线框，合并成一行；④ `install()` 拿到锁就立刻置为
    `downloading`（此前要等到下载阶段，前端轮询看到 idle 会把按钮重新点亮，
    导致"要点两次更新"）；⑤ 云同步单次完成日志降到 debug，退出前打一行总账。
  - 怎么验证：`test_security_audit.py` [A9]、`test_bot_names.py`、`test_bot_probe.py`。

---

**更早的变更记录（2026-10-06 ~ 2026-11-07，共 47 条）见归档：
`docs/PROGRESS-archive-2026-11-15.md`**
