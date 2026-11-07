# QQBotMerged 项目进度

> 每次开新 AI 会话 / 派新子代理前，先读这份文件，不要读全历史。

---

## 目标

一个进程同时运行：
- 多个 QQ 官方机器人（各自独立 WebSocket，互不影响）
- 统一 Web 后台（11 个页面：聊天/状态/统计/设置/插件/群管理/日志/上下文/指令面板/媒体/说明）
- AstrBot 官方插件格式兼容
- Cloudflare D1 云同步

---

## 当前状态

- 程序版本：**v1.3.0**（唯一来源 `core/version.py` 的 `__version__`；网页侧栏/关于页/接口都从这里取）
- 启动命令：
  - 正常：`python run.py`
  - 自检：`python run.py --check`
  - 调试（不连 QQ）：`python run.py --no-bots`
  - 临时端口：`python run.py --port 9000`
- 测试命令：
  - 端到端：`python tests/test_offline.py`（575 项）
  - 单元：`python tests/unit/test_markdown_send.py`、`test_at_mentions.py`、`test_cmd_tags.py`、
    `test_plugin_safety.py`、`test_cloud_secrets.py`、`test_media_dedup.py`、`test_ai_scope.py`、
    `test_member_role.py`（含身份解析 / gateway 解析 / 历史补齐）、`test_security_fixes.py`（审计回归）、
    `test_csp_boot.py`（CSP 与前端启动数据）、`test_plugin_sandbox.py`（插件软限制）、
    `test_dispatch_isolation.py`（插件分发状态线程隔离）、`test_ssrf_guard.py`（媒体下载 SSRF）、
    `test_updater.py`（版本比较 / 自动更新安全检查与回滚）
  - 单实例锁：`python tests/test_instance_lock.py`
  - 前端契约：`python tests/check_frontend.py`
  - DOM 会话列表：`node tests/dom_test_conversations.js`
  - DOM 消息渲染：`node tests/dom_test_messages.js`（40 项）
  - DOM 全局保护：`node tests/dom_test_globals.js`
  - DOM 设置页：`node tests/dom_test_settings.js`（重复插入 / 事件只绑一次）
  - DOM 版本与更新：`node tests/dom_test_update.js`（版本显示 / 更新进度 / 待重启提示）
  - 测试分层说明：`tests/README.md`
  - 模拟注入：`python tests/simulate_message.py`
  - 插件安全自查：`python -m core.plugin_safety plugins`
- 接口索引：`docs/AI_INDEX.md`（本文件不再内嵌索引）
- 平台接口事实/错误码：`docs/QQ_MESSAGE_NOTES.md`（markdown / @ 标签；每条带官方来源）
- 默认监听：`127.0.0.1:8666`（当前 `config.json` 里是 `0.0.0.0`，且未设 token —— 局域网可访问，注意安全）
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
| QQ API | `core/qq_api.py`（+`core/qq_upload.py`/`core/qq_errors.py`） | 完成 | 发送 / 分片上传 / 禁言 / 撤回 / 指令面板 / 富媒体；上传逻辑与错误分类已拆模块 |
| 群管理 | `core/group_manager.py` | 完成 | 群列表 / 成员 / 禁言（本地+官方）/ 群名 / 群配置 |
| 运行时 | `core/runtime_pkg/`（`core/runtime.py` 只是兼容层） | 完成 | 多机器人调度 / 消息总线 / 发送出口 / 配置热更新；按职责拆成 base/bot/config/messaging/names/moderation/media/panels/scheduler/lifecycle/core |
| 插件管理 | `core/plugin_manager.py` | 完成 | 只支持 AstrBot 格式 / 按机器人隔离 / 旧数据迁移 |
| 插件安全扫描 | `core/plugin_safety.py` | 完成 | AST 静态扫描（不执行代码）+ 插件页徽章/汇总；命令行 `python -m core.plugin_safety plugins` |
| AstrBot 兼容 | `core/astrbot_shim/`（`core/astrbot_compat.py` 只是兼容层） | 完成 | Star / filter / 事件 / 消息链 / Provider / KV；拆成 base/runner/components/filters/star/provider/compat_utils/install/host |
| 云同步 | `core/cloud_sync.py` | 完成 | D1 / 墓碑 / 兼容旧明文 / 安全闸 / **上传前密钥字段扫描**（`find_sensitive_keys`） |
| 统计 | `core/stats.py` | 完成 | 今日 / 7 天 / 累计 |
| Web 后台 | `web/server.py` | 完成 | Flask 单端口 / SSE / 令牌鉴权 |
| 前端 | `web/static/js/*` `web/templates/index.html` | 完成 | Chat / Settings / Admin / Groups / App / Util / Effects |
| 启动入口 | `run.py` | 完成 | 单实例保护 / 自检 / 关闭按钮 |
| 自带插件 | `plugins/astrbot_demo` `dice` `daily_checkin` `ollama` | 完成 | 示例 + 骰子 + 签到 + 本地 AI |

---

## 关键设计决策（不要改）

1. **单实例保护**：文件锁 + 目录锁 + 独占端口，防止两个进程各回一条。
2. **消息去重**：`storage.has_message(bot_id, msg_id)`，断线重连不重复回复。
3. **按机器人隔离**：会话键 `bot_id:scope:openid`，上下文/禁言/插件/配置覆盖都带 bot_id。
4. **配置热更新**：只重载真的改了的分组（`_changed_sections`），改动画不重启云同步。
5. **插件只支持 AstrBot 格式**：裸 `.py` 原生插件已彻底移除，不再支持。
6. **密钥不同步**：`data/config/<插件>_config.json` 故意不在 SYNC_PATHS 里。
7. **消息库/媒体不上云**：`messages.db` 和 `media/` 在 NEVER_SYNC 里。
8. **群名以 QQ 为准**：绝不用"最后发言者昵称"当群名。
9. **引用不受 2 分钟限制**：2 分钟只属于"撤回"；引用用 `message_reference`。
10. **图片三来源去重**：attachments / image_url / content_images 按 fileid 或文件名去重，优先本地留存。
    补充（2026-10-11）：**留存文件本身按内容 sha256 去重** —— 多个机器人同在一个群时，同一条消息里的
    图片会分别下发给每个机器人，且每份下载地址都带各自的 rkey 签名（URL 不同），只按 URL/文件名
    去重拦不住；同一份内容只落一份文件、只登记一条媒体记录（见 `MediaStore.save_bytes`）。
10.1 **主题全部走变量**：浅色/深色都按 QQ 桌面端实测配色，两套值分别集中写在
    `web/static/css/app.css` 的 `:root` 与 `body[data-theme="dark"]`（含
    `@media (prefers-color-scheme: dark) body[data-theme="auto"]` 的复制块）。
    组件样式里**不要再写死颜色**，需要新颜色就加变量并在**两处**都赋值 ——
    只写在 `:root` 会让深色模式漏底色（历史上踩过）。
11. **markdown 发送**：`send.message_format` 决定默认格式，网页发送框可单次覆盖；markdown 走
    `msg_type=2 + markdown.content`（此时**不能**带顶层 `content`）；平台拒绝 markdown 时
    **自动降级为纯文本**并把原因写进 `_note`/日志——宁可发成纯文本，也不能不发。
12. **群内 @ 某人**：正文里嵌官方标签 `<qqbot-at-user id="对方openid" />`，
    所有发送出口统一把旧写法 `<@openid>` 转成它；`@全体成员` 官方仅文字子频道可用，
    群里退化成可读文字；@ 候选名单 = 群成员缓存 + 最近发言者（官方成员接口要管理员）。
13. **@ / 指令标签必须走 Markdown（实测结论）**：官方写 @某人「文本消息也支持」，但用户真机实测
    **纯文本消息里标签不渲染**。因此后端 `Runtime.send_text()` 在请求为纯文本时，只要正文里有
    @某人 / 指令标签就**自动改用 markdown**（只影响这一次发送、不写配置，原因写进 `_note` 与
    `auto_format`），前端插入标签时也立刻把发送格式控件切到 Markdown。判定集中在
    `core/message_text.py` 的 `markdown_only_tags()`，两边规则一致。
14. **指令标签**：`build_cmd_enter()` / `build_cmd_input()` 生成官方 `<qqbot-cmd-enter/>` /
    `<qqbot-cmd-input/>`，值统一 `quote(safe="")`（官方要求 urlencode）并按 **100 字符**截断；
    网页发送框「⌘ 指令」面板插入，范围判断交给用户（官方对回车指令的群聊限制是客户端行为，
    程序不假装能校验）。

---

## 已知问题 / 技术债

按优先级：

1. ~~`core/astrbot_compat.py` 单文件过大（> 1300 行）~~（2026-10-06 已拆成 `core/astrbot_shim/`
   包，原文件变成 139 行兼容层；接口/垫片结构逐字比对过，无差异）
2. ~~`core/runtime.py` 单文件过大（> 1200 行）~~（2026-10-06 已拆成 `core/runtime_pkg/` 包，
   `Runtime` 由 8 个 mixin 组合，原文件变成 66 行兼容层；成员集合与拆分前一致）
3. `core/qq_api.py` 单文件过大（> 1100 行），上传逻辑可独立
4. `tests/test_offline.py` 单文件过大（> 3800 行），跑一次慢（新用例请放 `tests/unit/`）
5. 插件是任意 Python 代码，`PluginRuntime` 门面拦不住越权（`import os` 仍可读 config.json）；
   `core/plugin_safety.py` 只是**提示**，不是沙箱
6. ~~云同步 `data/plugin_data/` 可能含插件写入的敏感数据~~（2026-10-06 已加"上传前按字段名扫描"：
   命中 `api_key/secret/token/password/...` 就**跳过上传**、本地文件不动，并在日志与同步结果里
   说明原因；可用 `cloud_sync.skip_secret_files=false` 关掉。注意：只按**字段名**判断，
   密钥直接写进正文（不是字段）时扫不出来）
7. Windows 上 `msvcrt.locking` 强杀后可能残留锁（已做自愈，但边界情况仍在）
8. ~~前端全局对象 `window.Chat/Admin/Groups/Settings` 可被覆盖~~（2026-10-06 已修：
   `util.js` 的 `protectGlobal` 把这些全局绑定设成 `writable:false/configurable:false`，
   其它脚本文件通过 `window.__qbmProtect` 调用；**没有用 `Object.freeze`** ——
   `Chat.byKey = {}` / `Admin.pluginChanges = {}` 这类整字段赋值到处都是，冻结会直接打断页面。
   测试：`node tests/dom_test_globals.js`）
9. `data/user_context/` 上云涉及用户隐私，未做脱敏
10. ~~配置解析失败会用默认配置覆盖用户 `config.json`~~（2026-10-06 已修：`utf-8-sig` 容忍 BOM +
    解析失败先备份 `.broken-<时间>` 且不写盘；测试见 `test_offline.py` 第 [41] 节）
11. markdown 相关未做完的边角：① `Runtime.send_text` 把格式塞进 `_record_outgoing(extra=...)`，
    但 `extra` **本来就不落库**（`messages` 表没有该列，`send_file` 的 `fallback` 也一样），
    所以"这条消息当时是用 markdown 发的"只体现在接口返回/日志里；② 真实平台 markdown 发送**未联网实测**
    （本地只有桩测试）；③ 官方不在支持清单里的语法（代码块/表格/行内代码）会被原样显示或触发降级；
    ④ 未做流式 markdown（官方仅单聊支持）、未把本地留存图片自动转成 markdown 可用的公网 URL。
12. @ 相关未做完的边角：① @ 标签真机效果**只由用户自测过一次反馈**（"纯文本不渲染"），
    程序据此自动改用了 markdown（决策 #13），但**没有往用户群里发过测试消息**；
    ② 群里 @全体成员官方不支持，已退化成文字；
    ③ 私聊写 @ 标签不生效（程序不擅自删，原样发出）；
    ④ @ 候选名单里的名字可能仍是「用户 xxxx」占位（QQ 私聊/群事件常不给昵称）。
13. 指令标签未做完的边角：① `reference=true`（点击后带引用回复）**未真机验证**，官方也没写清引用的是
    哪条消息；② 回车指令官方标注「群聊和文字子频道不支持」，程序**不做场景拦截**（写了不生效而已）；
    ③ urlencode 后客户端点击发送的是解码文本还是编码串，官方未写，未实测；
    ④ 一条消息放多个指令标签、指令标签与 `keyboard` 组合的边界未测；
    ⑤ 网页端只提供"插入标签"，没有"预览用户点下去会怎样"的模拟。

---

## 会话开工前先做

1. 读本文件 + `docs/AI_INDEX.md`；
2. 跑一遍 `python tests/test_offline.py`、`python tests/check_frontend.py` 确认基线是绿的；
3. 收工时把「最近变更」和「下一步」的勾选更新掉，再开新会话。

---

## 下一步（按优先级）

- [x] P0：拆分 `core/astrbot_compat.py` 为 `core/astrbot_shim/` 包（2026-10-06，2035 → 139 行兼容层）
- [x] P0：拆分 `core/runtime.py` 为 `core/runtime_pkg/` 包（2026-10-06，1969 → 66 行兼容层）
- [x] P0：新建 `docs/AI_INDEX.md`（已从本文件拆出，本文件只留进度与约定）
- [x] P1：新建 `core/plugin_safety.py`（AST 静态扫描）+ `tests/unit/test_plugin_safety.py`；
      已接线到插件页（`PluginManager._scan_safety` / `diagnostics()["safety"]`、插件行徽章 + 顶部汇总，
      测试见 `test_offline.py` 第 [42] 节）
- [x] P1：云同步加敏感字段扫描（上传前对文本/JSON 做 key 匹配）：`cloud_sync.find_sensitive_keys`
      + `cloud_sync.skip_secret_files` 开关 + `result["sensitive"]/["sensitive_files"]`；
      测试 `tests/unit/test_cloud_secrets.py`（19 项）+ `test_offline.py` 第 [43] 节
- [x] P1：测试分层：`tests/unit/`（`test_plugin_safety.py`、`test_cloud_secrets.py`）+ `tests/README.md`
      写明三层约定；老集成用例**没有搬动**（`test_offline.py` 仍是集成/端到端），后续可逐节迁移
- [x] P2：拆 `core/qq_api.py` 的上传逻辑（2026-10-06：19 个上传方法 + 上传常量 → `core/qq_upload.py`
      的 `QQUploadMixin`；错误类型/分类 → `core/qq_errors.py`；`core/qq_api.py` 831 行、再导出全部公开名字，
      `QQApiClient` 成员与拆分前逐名一致）
- [x] P2：前端全局对象保护（2026-10-06：`protectGlobal` 把全局绑定设成只读；不用 `Object.freeze`，
      理由见技术债第 8 条；测试 `tests/dom_test_globals.js` 24 项）
- [x] P2：`plugins/README.md` 补"插件是任意 Python 代码"警告（2026-10-06）
- [x] P2：markdown 发送 + 纯文本/Markdown 切换（2026-10-06，配置项 `send.message_format`，
      `tests/unit/test_markdown_send.py` 50 项 + `test_offline.py` 第 [44] 节）
- [x] P2：群聊 @ 某人（2026-10-06，`qqbot-at-user` 官方标签 + 候选名单兜底，
      `tests/unit/test_at_mentions.py` 23 项 + 第 [45] 节）
- [x] P1：带 @ / 指令标签的消息自动改用 Markdown + 指令标签（`qqbot-cmd-enter` /
      `qqbot-cmd-input`）支持（2026-10-07，`tests/unit/test_cmd_tags.py` 26 项 + 第 [46] 节）
- [ ] P2：指令标签真机验证（`reference=true` 的引用行为、urlencode 后客户端点击发送的到底是解码文本
      还是编码串；官方都未写），以及 @ 标签在纯文本消息里的渲染结论找官方/真机再确认一次

---

## 禁止事项

- 不要重写已完成的模块，先读代码 + `docs/AI_INDEX.md` 再改
- 不要删除 `tests/` 下任何测试
- 不要改自带插件对外暴露的指令名（`/astrbot`、`/骰子`、`/签到`、`/积分`、`/抽奖`）
- 不要动 `data/` 下真实数据（测试用 `data/_selftest/`）
- 不要在没有更新 PROGRESS.md 的情况下开新会话
- 不要全量读超过 300 行的文件，用 `sed -n` 读片段
- 不要改 `config.json` 里默认值（改 `core/config_schema.py` 的 DEFAULT_CONFIG）
- 不要把密钥写进代码或测试
- 不要让子代理读完整历史对话

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
## 接口索引（改代码前必读）

函数/类清单已拆到 `docs/AI_INDEX.md`（只列名字 + 一句话 + 文件，不贴实现）。
本文件只保留：目标、当前状态、已完成后模块、关键决策、技术债、下一步、禁止事项、会话预算。

---

## 最近变更（新会话从这里接着看）

> **书写规范（每次更新本文件都必须遵守）**
> 每条记录只写「**日期 + 改了什么 + 为什么改 + 怎么验证**」四件事。
> **不要写入**：谁提出/谁反馈的需求来源、对话过程、个人身份或口径、真实配置内容、
> 真实数据规模（群数/成员数/消息条数等）、实例环境的操作过程、截图来源。
> 修复类问题用中性描述写"问题现象"（例如「正文含 @ 某人时不会自动切到 Markdown」），
> 不引用任何人的原话。只保留"修复后行为正确"这类可验证的结论。

- 2026-11-07：**修「点检查更新后最新版本仍显示未检查」**：
  - **问题现象**：在运行状态页点「检查更新」，接口其实成功返回了最新版本，
    但「最新版本」那一格仍然显示"未检查"。
  - **根因**：后端 `Updater.check()` 返回的字段是 `latest` / `latest_display`，
    前端却读 `updateLatest.version` —— 这个字段从来不存在，读出来是 `undefined`，
    所以永远走"未检查"分支。
  - **改法**：前端抽出 `latestVersion()` / `latestDisplay()` 两个取值函数
    （`latest` → `version` 兜底、`latest_display` 优先），所有用到"最新版本"的地方
    统一走它们，不再各处手写字段名；点完检查后先按检查结果立即刷新，再拉一次真实状态
    （避免把"待重启"提示冲掉）。
  - **顺带修**：检查结果原本只存在前端内存里，**刷新页面就退回"未检查"**。
    现在 `Updater.status()` 会把上次检查结果（`last_check`，读本地缓存文件，不联网）
    一起返回，前端内存为空时用它补回来。
  - **怎么验证**：`tests/dom_test_update.js` 新增 [7] 点检查后必须显示最新版本、
    [8] 刷新后仍能显示（共 24 项）。[7] 故意**只给 `latest` 不给 `latest_display`**，
    强制走回退逻辑；**把 `latestVersion()` 改回读 `.version` 时该测试会红**
    （实测「最新版本」变回"未检查"、提示变成"新版本"没具体版本号）。
    全套回归：`test_offline` 576 项、`check_frontend`、14 个单元测试、5 个 DOM、单实例锁全绿。

- 2026-11-07：**加回版本号 + 从 GitHub 检查/下载/安装更新（发布 v1.3.0）**：
  - **版本号统一到 `core/version.py`**：`__version__` 是唯一来源，
    网页侧栏、关于页、`/api/admin/status`、启动日志全部从它取（以前那一处删掉后
    各处都没有版本可显示）。另含版本比较工具（`normalize_version` / `compare_versions`
    / `pick_latest`），支持 `v1.2.7` / `release-1.2.7` / `version_2.0` 等写法，
    并能正确判断 `1.10.0 > 1.9.9`（按段比，不是字符串比）。
  - **新增 `core/updater.py`**：检查 GitHub release/tag → 下载 tarball → 安全解压 →
    备份当前代码 → 覆盖安装 → 失败回滚。**更新源写死在 `core/version.py`**，
    不放 `config.json`：能改配置就能改下载源，等于远程代码执行。
  - **安全防线（每条都有对应测试）**：只允许 HTTPS + 固定域名白名单，重定向逐跳校验；
    下载体积上限 64MB；解压拒绝绝对路径 / `..` / 符号链接 / 硬链接，限制成员数与
    解压总量（防 tar 穿越与 zip bomb）；安装前自动备份到 `data/update/backup/`，
    任一步失败自动回滚；**绝不覆盖** `config.json` / `data/` / `plugins/` / `.git/`；
    安装完只写标记 + 提示重启，不静默重启进程。
  - **网页**：「运行状态 → 版本与更新」卡片显示当前版本 / 最新版本 / 状态，
    「检查更新」查 GitHub，「立即更新」二次确认后后台安装并轮询进度（1 秒一次），
    装好后明确提示需要重启并指出备份位置；侧栏与关于页显示版本号。
    版本号一律来自启动数据，前端不写死。
  - **启动时检查**：`run.py` 新增 `startup_update_check()`，异步查一次（默认开启），
    有新版本在日志里提示；若开了 `update.auto_install` 则自动下载安装。
    另会提示"已装好新版本但还在跑旧代码，请重启"。
  - **配置**：新增 `update.enabled` / `update.check_on_start` / `update.auto_install` /
    `update.check_interval_minutes`（默认 开/开/关/30）。
  - **怎么验证**：新增 `tests/unit/test_updater.py`（77 项：版本比较、更新源校验、
    解压防穿越/符号链接、受保护路径、安装保留用户数据、回滚、并发拒绝、状态机、
    exe 拒绝自我更新）与 `tests/dom_test_update.js`（24 项：版本显示、状态渲染、
    进度、待重启提示、检查后显示最新版本、刷新后从缓存恢复）；
    `check_frontend.py` 增加"版本来自服务端不许写死""安装必须二次确认"两条断言。
    两个测试都**不写死版本号**（从 `core/version.py` 读，实测临时改成 2.0.0 / 10.20.30 仍全绿），
    避免发版后测试过期。
    另做了一次**真实端到端实测**（在项目副本上）：从 GitHub 真实下载 v1.2.5、
    解压、备份、写入 93 个文件，确认 `config.json` / `data/` / `plugins/` 未被覆盖，
    用备份回滚后文件逐字节回到原样。全套回归：`test_offline` 576 项、
    `check_frontend`、14 个单元测试、5 个 DOM、单实例锁全绿。

- 2026-11-07：**修复插件域名白名单的子串匹配绕过 + DNS 校验改为 fail-closed**：
  - **域名白名单可被伪造域名绕过（高危，已实测）**：`plugin_sandbox.host_allowed()` 用
    `suffix in name`（子串包含）判断，实测 `qq.com.evil.com`、`evilqq.com`、`myqq.com`、
    `notqq.com.cn`、`api.deepseek.com.evil.net` **5 个恶意域名全部被放行**。
    改法：只保留"精确匹配"与"以 `.` 开头的合法子域匹配"（`. ` 前缀保证
    `evilqq.com` 不命中 `qq.com`），并统一小写、去掉尾随点、剥掉 IPv6 方括号。
  - **顺带修一个被掩盖的白名单遗漏**：沙箱的 `ALLOWED_HOSTS` 缺 `qq.com.cn`
    （`media_store` 那边有），以前靠子串匹配才碰巧放行官方富媒体域名
    `multimedia.nt.qq.com.cn`；改成精确匹配后会误拒，已补齐并与
    `media_store.ALLOWED_HOST_SUFFIX` 对齐。
  - **DNS 校验改为 fail-closed**：`media_store.resolve_public()` 原来在 DNS 解析失败时
    返回 `True`（放行），属 fail-open —— 解析被干扰/投毒时内网校验形同虚设。
    现在解析失败、结果为空、任一条落到内网/回环/链路本地，一律拒绝并记 warning；
    IP 字面量不再触发 DNS 查询。DNS Rebinding 的时间差风险已在 docstring 说明
    （彻底修法"用解析出的 IP 直连"会破坏腾讯媒体的 SNI/Host 签名校验，故保留
    先校验再连接，另由 `fetch()` 的域名白名单作第一道闸门）。
  - **标准库白名单补漏**：`copyreg`（被 pickle/copy 间接依赖）、`threading`、
    `selectors`、`select`、`signal` 之前不在白名单，strict 档下会被误拦；
    已放入白名单（`threading` 放行不改变"插件自建线程不受约束"这一事实）。
  - **核查后不成立的两项**：报告称 `importlib.import_module()` 可绕过导入限制 ——
    实测其内部仍走 `builtins.__import__`，`subprocess`/`ctypes`/`pickle`/`multiprocessing`
    全部被拦住；报告称线程隔离是"设计缺陷" —— 这是 NEW4-01 已写明的取舍
    （插件自建线程不受约束），不是可修复的 bug。
  - **怎么验证**：`tests/unit/test_plugin_sandbox.py` 新增 [2b] 域名伪造 6 项、
    [2c] 标准库白名单 18 项；新增 `tests/unit/test_ssrf_guard.py`（16 项，用猴子补丁
    替换 DNS，不联网）。**两条新测试都回退验证过会红**（域名 6 项失败、DNS 1 项失败）。
    全套回归：`test_offline` 576 项、`check_frontend`、13 个单元测试、4 个 DOM、
    单实例锁全绿。

- 2026-11-07：**沙箱安装失败改为原子回滚 + 显式状态；补齐"自建线程不受约束"的说明**：
  - **NEW4-02 安装失败不再留残留**：`_install()` 原为"边装边设 `_active`"，
    任一步失败即 `_active=False`，而 `__exit__` 是 `if self._active: self._uninstall()` ——
    于是**已打上的补丁永久残留**在进程里（后续沙箱会把包装函数当成 original 保存，
    嵌套层数越积越多），且界面上看不出"防护没生效"。
    改法：`_install()` 变成**原子操作**（失败即回滚已打补丁），`__exit__` **无条件**尝试还原；
    新增 `installed` / `install_error` / `effective` / `status_note()`，安装失败用 `log.error`
    明确报出；`_uninstall()` 增加 `_looks_wrapped()` 防御，若保存的 original 本身是沙箱包装函数
    则跳过还原并告警，避免把历史残留固化。
  - **安装失败在插件页可见**：`PluginManager.diagnostics()` 新增 `sandbox` 字段
    （档位、是否生效、边界说明），插件页顶部按 `off` / 生效 / 未生效三种状态显示。
  - **NEW4-01 边界写进文档**：沙箱的线程作用域意味着**插件自建线程不受任何约束**
    （默认 `loose` 档的唯一防线"拦执行系统命令"可被一行 `threading.Thread` 绕过）。
    这不是 bug 而是取舍——没有线程作用域时宿主的后台线程会被误伤（NEW3-02）。
    已在 `plugin_sandbox.py` 的类 docstring、`plugins.sandbox` 配置项提示、以及插件页
    的沙箱状态说明里都写明"**不要把它当安全边界**"。
  - **删除死代码**：`_last_dispatch_bot` 自改为按目标回查后只写不读，已移除。
  - **核查结论（无需修改）**：报告推测 NEW4-02 的触发条件是"`_patch_network` 里的
    `import socket` 连锁导入 `selectors` 被拦"。实测 `socket` 在白名单内，且
    `import socket` **不会**导入 `selectors`（干净进程验证），三条路径（正常 / 清掉
    socket 相关模块 / loose 档）安装均成功。但"安装失败不还原"这个缺陷本身是真的，
    已按上面的原子化修复。
  - **怎么验证**：`tests/unit/test_plugin_sandbox.py` 扩到 **61 项**（新增 [12] 安装失败
    回滚、[13] 正常安装状态、[14] off 档不谎报）。**回退修复后该测试会红**
    （实测 3 项失败，`open` 停留在 `guarded_open`）。全套回归：`test_offline` 576 项、
    `check_frontend`、12 个单元测试、4 个 DOM、单实例锁全绿。

- 2026-10-31：**修「设置页点一次『添加机器人』会插入多个编辑框」**：
  - **问题现象**：在设置页点一次「＋ 添加机器人」，会出现 2 个甚至更多个机器人编辑框。
  - **根因**：`bindFormEvents()` 在**每次 `render()` 时都会被调用**，而 `#settingsForm`
    这个元素本身不会被替换（`render()` 只改它的 `innerHTML`）。在同一个元素上反复
    `addEventListener('click', ...)` 会**累积监听器**：渲染 N 次后点一次就插入 N 个编辑框。
    （`settings.js` 里 `render()` 至少被调用 3 处：初次加载、重新加载、搜索框输入防抖。）
  - **改法**：`bindFormEvents()` 用 `data-events-bound` 标记做成"只绑定一次"，
    重绘只换内容、不再重复挂事件。同一个函数里的 `change` 监听器一并受这个判断保护。
  - **怎么验证**：新增 `tests/dom_test_settings.js`（5 项），用能记录监听器的 DOM 桩断言
    "渲染 5 次后 click 监听器仍为 1 个""点一次只插入 1 个编辑框""反复渲染后仍只插 1 个"。
    **回退修复后该测试会红**（实测：监听器 5 个、点一次插入 5 个、反复渲染后插入 8 个），
    与线上现象一致。全套回归：`test_offline` 576 项、`check_frontend`、12 个单元测试、
    4 个 DOM、单实例锁全绿。

- 2026-10-31：**移除程序版本号 + 修复分发状态跨线程串扰 + 沙箱补丁加线程作用域**：
  - **移除版本号**：`core/__init__.py` 的 `__version__` 已删除（程序不再维护版本号）；
    `web/server.py` 的 `_version()` 改为返回空串，`/api/admin/status` 仍保留 `version` 字段
    以兼容外部脚本。网页此前已不再显示版本。
  - **NEW3-01 分发状态跨线程串扰**：`_emit_sink` / `_emit_used` / `stop` 原为实例属性，
    而每个机器人一条工作线程、handler 又是异步的，两个机器人并发 `dispatch()` 会互相覆盖，
    导致 A 的 `await event.send()` 按 B 的会话与账号发出。
    改法：三者全部迁入已有的 `self._current`（`threading.local()`），改用属性读写器代理，
    调用点无需改动。
  - **NEW3-02 沙箱补丁是进程级的**：补丁装在全局 `builtins` / `os` / `socket` 上且无锁、
    无线程作用域，插件执行期间宿主自己的其它线程（网页请求、日志落盘、云同步、群名刷新）
    也会被拦，违规还会记到插件名下。改法：安装时记录 `threading.get_ident()`，
    每个 guarded 入口先判 `_in_scope()`，非本线程直接调用原函数。
    同步修正了与实现不符的 docstring，并在配置项提示里写明"补丁只对插件自己的线程生效"。
  - **NEW3-03 兜底顺序与注释不一致**：`send_to_umo` 原来无条件采用"最近一次分发"的机器人，
    多机器人时会发错号。改为按目标回查：新增 `(会话类型, openid) → bot_id` 映射
    （`remember_target_bot` / `target_bot_id`，带回滚上限），回退顺序为
    线程局部 → 按目标回查 → 只有一个启用机器人时用它 → 否则拒绝发送并告警。
  - **自测中发现并修复的沙箱真实漏洞**：`_path_allowed` 用 `path.startswith(root)` 做字符串
    前缀比较，`.../plugin` 会把 `.../plugin_evil/x` 误判为"在插件目录内"，
    写文件限制形同虚设。改为按**目录边界**判断（`_inside()`，比较规范化后的
    realpath，要求以 `根 + 分隔符` 开头），并补了兄弟目录、前缀伪造两类断言。
  - **怎么验证**：新增 `tests/unit/test_dispatch_isolation.py`（14 项：状态在线程局部、
    双线程 sink 不串、按目标回查、目标表上限）；`tests/unit/test_plugin_sandbox.py` 扩到
    **48 项**（新增线程作用域与目录边界两组）。**两条新测试都回退验证过会红**。
    全套回归：`test_offline` 576 项、`check_frontend`、12 个单元测试、3 个 DOM、
    单实例锁全绿。

- 2026-10-31：**修复 CSP 拦死自身启动脚本（P0 回归）+ 脱敏规则统一 + 插件进程内软限制**：
  - **P0 CSP 回归**：上一轮为防 XSS 加的 `script-src 'self'`（无 `'unsafe-inline'`、无 nonce）
    会**拒绝执行内联脚本**，而页面当时靠一段内联 `<script>` 注入 `window.__BOOT__` 与访问令牌 ——
    结果：配了令牌时前端拿不到 token（所有接口 401，后台空壳），不配令牌时 `BOOT` 退化为空对
    （主题/分页等设置全部失效）。
    **改法**：启动数据改为 `<script type="application/json" id="boot-data">`（该类型不会被执行，
    不受 `script-src` 约束），`util.js` 新增 `readBoot()` 从该块 `JSON.parse`，令牌改为在
    `readToken()` 里从 URL 读取；保留 `window.__BOOT__` 兜底以兼容离线单测。**没有放宽 CSP**。
  - **配套回归测试**（`tests/unit/test_csp_boot.py`，22 项）：断言 CSP 仍无 `unsafe-inline`、
    模板无**可执行**内联脚本、`#boot-data` 是合法 JSON 且含 `ui`/`limits`、前端确实从该块读取、
    静态资源是站内路径且可取。**已回退成旧写法验证过该测试会红（6 项同时失败）**，
    旧的安全测试只断言"响应头里有 CSP"，不会在破坏页面时变红。
  - **P2 脱敏规则统一**：`ConfigManager._is_secret_key()` 原为固定名字**全等**比较，
    `access_token` / `api_token` / `client_secret` / `app_token` / `bot_token` 等带前后缀的名字
    全部漏检（当前无实际泄露，但新增按机器人密钥字段时会静默绕过打码）。
    改为"去分隔符 + 后缀/前缀"的宽松匹配，并显式排除 `max_tokens` /
    `token_refresh_buffer_seconds` / `skip_secret_files` 这类普通配置项。
  - **P3 插件发送不许发错号**：`send_to_umo()` 在无法确定机器人时**拒绝发送并告警**，
    不再静默退回"任选一个可用机器人"。补充回退顺序：线程局部 →
    本次 dispatch 记录的机器人 → **仅当只有一个启用机器人时**才用它兜底。
  - **P3 关键词检索范围**：`read_tail()` 带 `level`/`keyword` 时改为**分块顺序扫描**
    （新增 `_iter_lines`，增量解码避免块边界截断多字节字符），很旧的条目也能搜到；
    无过滤时仍走尾部倒读。内存占用与 `limit` 成正比，不会整文件入内存。
  - **插件进程内软限制**（新增 `core/plugin_sandbox.py` + 配置 `plugins.sandbox`）：
    - 档位 `off` / `loose`（默认）/ `strict`；`loose` 只拦"执行系统命令 / 起子进程"，
      不影响现有插件；`strict` 另加导入白名单、只允许写插件目录与插件数据目录、
      联网域名白名单（QQ / 常见 AI 服务）。
    - 在**加载期**与**事件派发期**各包一层；离开上下文后所有补丁**原样还原**，
      不污染宿主与其它插件（恢复逻辑按 key 统一解析，避免漏还原）。
    - 定位说明写在模块 docstring 与配置项提示里：这是**进程内软限制**（降低误伤范围），
      **不是**可运行不可信代码的保证；真正的隔离需要独立进程 + 独立低权限账户。
  - **验证**：新增 `tests/unit/test_csp_boot.py`（22 项）与 `tests/unit/test_plugin_sandbox.py`
    （41 项）；全套回归 `test_offline` **576 项**、`check_frontend`、11 个单元测试、
    3 个 DOM、单实例锁全绿；另实测严格 CSP 下首页样式与启动数据正常、4 个内置插件在
    `loose` 沙箱下全部正常加载。

- 2026-10-29：**「关闭程序」按钮从「使用说明」页移到「运行状态」页右上角**：
  - **改了什么**：`index.html` 里 `#shutdownBtn` 从 `page-about` 的 `page-head` 挪到
    `page-status` 的 `page-head`，和「刷新」并排（`head-actions` 自带 `margin-left:auto`，
    自然靠右）；使用说明页保留一段说明文字指出按钮新位置，并去掉那里的红色警告边框。
    `app.js` 的绑定方式从"启动时 `U.el('shutdownBtn')` 直接绑定"改成**事件委托**
    （`document` 上监听 + `closest('#shutdownBtn')`）。
  - **为什么改**：关闭按钮属于"运维操作"，跟运行状态放在一起更符合使用路径；
    事件委托让按钮以后再换页面/位置都不用改 JS，也避免"绑定早于 DOM 就绪"导致点了没反应。
  - **怎么验证**：离线渲染两页核对（运行状态页右上角出现「⏻ 关闭程序」，使用说明页无按钮，
    全站 `#shutdownBtn` 只出现 1 次）；`check_frontend` / `test_offline` 576 项 /
    9 个单元 / 3 个 DOM 全绿。

- 2026-10-29：**按《安全审计报告》修复全部问题**（SEC-01 ~ SEC-16、NEW-01 ~ NEW-07）：
  - **P0 SEC-03/NEW-01 密钥脱敏**：`ConfigManager.mask_secrets()` 现在会**递归打码 `bots[].overrides`**
    （新增 `_mask_sensitive_fields()` + `_is_secret_key()`，按字段名判断），
    `payload()` 里的 `bot_overrides` 也过一遍。实测 `/api/admin/config` 不再出现明文 AI 密钥。
  - **P0 SEC-02/SEC-04/SEC-13**：`/media/<file>` 加 `check_token()`（前端本来就用 `U.apiUrl()` 拼图，
    自动带 `?token=`）；**SVG 一律 `as_attachment` + octet-stream**（不内联，杜绝同源 XSS）；
    `after_request` 补 **CSP**（含 `frame-ancestors 'none'`）与 **X-Frame-Options: DENY**。
  - **P0 SEC-01/NEW-02**：`config.example.json` 的 `web.host` 改回 `127.0.0.1`；
    `run.py` 在「非本机监听 + 无令牌」时**拒绝启动**（退出码 2，给出三条指引），
    要裸跑必须显式加 `--allow-open-web`。
  - **P1 SEC-05**：`send_to_umo()` 不再硬编码 `bot_id=""`，改用宿主线程内的
    `current_bot_id`，插件主动发消息不会再串到别的机器人。
  - **P1 SEC-06/NEW-07**：云同步敏感正则会话补 `token` / `secret`（用词边界匹配，
    `max_tokens`/`token_refresh_buffer_seconds`/`skip_secret_files` 不误伤），纯文本值门槛 12 → 8。
  - **P1 SEC-07**：插件扫描失败返回 `level="unknown"`（不再是 `"ok"`），
    `LEVEL_ORDER` 里 `unknown` 排在 `ok` 前面；插件页显示「安全扫描未完成」徽章。
  - **P1 SEC-08**：上传改为分块读取 + 业务上限（`_read_upload_capped`），超限直接 413，
    不再先把整个请求体读进内存。
  - **P1 SEC-09/NEW-03**：`fetch()` 改 `allow_redirects=False` **逐跳校验**（最多 5 跳）；
    `host_allowed(allowlist_only=True)` 把**腾讯系域名白名单下沉到 fetch 内部**；
    新增 `ipaddress` 判私有/回环/链路本地 + `resolve_public()` 做 **DNS 解析校验**。
  - **P1 SEC-10**：启动日志不再输出 `?token=...`，只提示"已设置访问令牌"。
  - **P1 SEC-11/NEW-06**：`ai_client.redact_text()` 脱敏错误正文（Bearer/sk-*/key=）；
    `/api/admin/status` 的日志尾巴也过同一套脱敏。
  - **P1 SEC-16/NEW-04**：`scan_plugin_dir()` **递归子目录**（跳过 `.git`/`__pycache__`/venv），
    `main.py` 优先、超限/解析失败/读不到都记进 `incomplete`，结论变 `unknown`。
  - **P2 SEC-12**：`read_tail()` 改**从尾部按块倒读**（`_tail_lines`），不再整文件 `readlines()`；
    日志下载改 `send_file` 流式。
  - **P2 SEC-14**：413 文案改为跟随 `MAX_CONTENT_LENGTH` 动态生成（不再写死 64MB）。
  - 测试：新增 `tests/unit/test_security_fixes.py`（**56 项**，每节对应一个审计编号）；
    **更新了 3 处旧断言**（它们断言的正是被审计判为缺陷的行为：扫描失败返回 ok / 语法错误返回空列表）。
    全套回归：`test_offline` **576 项**、`check_frontend`、9 个单元测试、3 个 DOM、单实例锁全绿；
    另用临时沙箱配置**实测**了启动拒绝、`/media` 401/200、SVG 强制下载、CSP 头、
    `mask_secrets` 不返回明文。
  - **未做（说明理由）**：SEC-15 插件沙箱是架构性风险，需要独立进程/独立账户才能真解决，
    目前只保留"扫描 + 提示"；README 的 DOC-01/DOC-02 数字漂移另行处理。

- 2026-10-25：**群成员身份改为从群消息里取（官方 `author.member_role`）**：
  - **为什么改**：群成员的 owner / admin / member 身份在官方**群消息报文**里每条都带
    （`author.member_role`），且**不要求机器人是群管理员**；而官方群成员接口需要管理员身份 +
    平台白名单，拿不到时群管理页的「身份」列只能显示「未知」。
  - **改了什么**：
    - `core/gateway.py`：入站消息新增 `member_role`；`mentions[]` 也带 `member_role`
      （被 @ 的人的身份，其中 `is_you` 的那条就是机器人自己在群里的身份）。
    - `core/runtime_pkg/messaging.py`：新增 `_remember_member_role()`，收到群消息时把
      "发消息的人 + 消息里被 @ 的人"的身份写进 `group_members`（`upsert_members`）。
    - `core/group_manager.py`：新增 `member_role(value)`（只解析单个值，认不出返回空串）
      + `ROLE_UNKNOWN` 常量；`_normalize_role()` 对平台成员接口仍可用。
    - `core/storage.py`：`upsert_members()` 改成**不拿空身份/unknown 覆盖已知身份**
      （`CASE WHEN excluded.role NOT IN ('', 'unknown')`），昵称同理不拿空串覆盖；
      新增 `member_role_rows()` 读历史报文。
    - **历史数据可补齐**（不联网）：新增 `core/member_roles.py` 的 `backfill_from_history()`
      —— 老版本丢弃了 `member_role`，但原始报文一直存在 `messages.raw_event` 里，解析即可补齐；
      `members()` 首次用到某个群时自动跑一次（`GroupManager._roles_backfilled` 防重复扫描）。
  - **怎么验证**：`tests/unit/test_member_role.py` **29 项**（用官方群消息报文样例喂 gateway →
    解析出 owner/admin → 落库 → 空身份不降级 → 身份变化能更新 → 私聊不写 → 历史补齐 + 幂等）。
    全套回归（576 项 + 契约 + 9 个单元 + 3 个 DOM）全绿。
- 2026-10-11：**群成员「身份」不再一律显示「成员」+ 群管理页没变化就不重播动画**：
  - **身份全是「成员」的根因**：`role` 在多处兜底写成 `"member"` ——
    `_merge_known_members()` 把"从历史消息 / 禁言名单认出来的人"也填成 `member`，
    `refresh_members()` 用 `item.get("role") or ("owner" if is_owner else "member")`，
    页面又按"不是 owner/admin 就是成员"渲染。于是**平台没有给出身份的成员**（从历史消息识别出来的
    那批）全部落到默认值「成员」，看起来像"整个群都是普通成员"。
  - **改了什么**：`core/group_manager.py` 新增 `_normalize_role()`（`role`/`member_role`/
    `is_owner`/`is_admin`/中文写法都认，**先判 is_owner 再判 role**，认不出来 → `ROLE_UNKNOWN`），
    两处默认值从 `"member"` 改成 `ROLE_UNKNOWN`；`groups.js` 新增 `roleLabel()`，
    显示「未知」，并在整列都是未知时给一句说明（平台没给身份 / 点「🔄 拉取成员」再试）。
  - **动画重播的根因与改法**：`openGroup()` 每 20 秒（`DETAIL_POLL_MS`）都把整个详情 `innerHTML`
    重写一遍，卡片入场动画（`anim.css` 的 `.page.active .card` → fadeInUp）跟着重播。
    改为按 `detailSignature()` 内容指纹（成员/禁言/群级配置/群名/错误…）判断，**没变就不重绘**；
    倒计时文字（`remaining_text`/`remaining_seconds`）**故意不进指纹**（否则每次都"变"），
    改用"最多每 `DETAIL_POLL_MS` 让文案刷新一次"（`Groups.detailShownAt`）保证剩余时长不过期。
  - **怎么验证**：新增 `tests/unit/test_member_role.py`（20 项；写这条测试时发现
    "is_owner=True + role=admin"会被判成管理员，已一并修正）；`tests/check_frontend.py`
    新增一组规则（不许再有 `"role": "member"` 兜底、必须有 roleLabel、详情必须用指纹判断重绘），
    **两条规则均回退验证过会变红**。

- 2026-10-11：**修「选了官方禁言，点禁言却还是本地禁言」**：
  - **根因**：`groups.js` 的群列表刷新里写着 `Groups.muteMode = data.mute_mode || Groups.muteMode`，
    而 `/api/groups` 返回的 `mute_mode` 是**后端默认值 local**（`GroupManager.mute_mode()`
    没传参时就是 local）。页面选过「官方接口」并把 `Groups.muteMode` 设对了，
    **15 秒后群列表自动刷新（LIST_POLL_MS）又把它覆盖回 local** —— 于是点禁言发出去的
    `mode=local`，自然只做本地禁言。详情面板每 20 秒重绘也会用被污染的 `Groups.muteMode` 回填下拉框。
  - **改法**：加 `Groups.muteModeReady` 标记 —— 服务端的 `mute_mode` 只在"页面还没表过态"时
    作为初始值采用；一旦改过下拉框就置位，之后不再被自动刷新覆盖。切换方式时给一条明确
    toast（避免"选了没反应"的错觉）。
  - **顺带修「官方禁言失败看不见」**：官方模式失败过去只弹 6 秒 toast，超过时长就看不到原因。
    现在 `Groups.lastMuteNotice` 把平台原话 + `hint`（例如"机器人在该群不是管理员，
    官方禁言接口会被拒绝"）常驻显示在「禁言方式」栏下面，直到下一次操作。
  - **怎么验证**：契约测试 `tests/check_frontend.py` 新增一组规则（禁言方式不能被自动刷新覆盖、
    请求带 mode 且透传到群管理），**已用"把修复回退成旧写法"验证过这条规则真会变红**。
  - 注意：方式选对后**官方禁言仍要求机器人是该群管理员**（且账号在群成员管理白名单内，
    否则平台返回 11253）；失败原因现在会常驻显示在页面上。

- 2026-10-11：**运行状态页「内置 AI」改成跟随当前机器人 + 去掉「版本」项**：
  - **切机器人不变的原因**：`/api/admin/status` 调的是 `rt.status()` → `ai_status()` **不带 bot_id**，
    给的是"整体结论"（全局可用就报全局、否则报第一个可用的机器人），跟左侧选的机器人无关；
    而且 `app.js` 的 `switchBot()` 只刷新了会话/设置/群管理，**没刷新运行状态页**。
  - **改了什么**：`ai_status(bot_id="")` 新增可选参数（非空 → 返回该机器人解析后的有效 AI 状态，
    带 `scope="bot"` / `bot_id` / `bot_name`；为空 → 保持原来的整体结论，启动提示仍用它）；
    `Runtime.status(bot_id="")` 透传给 `ai_status`；`/api/admin/status` 改成
    `rt.status(current_bot_id())`；`switchBot()` 在运行状态页时补一次 `Admin.loadStatus()`；
    状态页那一格显示成 `模型 · 机器人名`。
  - **去掉「版本」项**：`admin.js` 里删掉 `kv('版本', data.version)`（页面上不再显示；
    `/api/admin/status` 的 `version` 字段保留，不影响其它用途）。
  - **怎么验证**：`tests/unit/test_ai_scope.py` 新增 [1b] 一组（指名的机器人 → 不同结果、取不到客户端时
    回退不抛异常，共 25 项）；`tests/check_frontend.py` 新增「运行状态页」契约规则（不许再出现
    版本项、不许用整体状态显示 AI、`/api/admin/status` 必须传当前机器人、切换后必须重拉）——
    **已把修复回退验证过这条规则会红**。全套回归全绿。

- 2026-10-11：**修「群管理 → 群级配置」里标签和勾选框不在同一行**：
  - **根因**：那一组用的是设置页的 `.field`，而 `.field` 是**纵向 flex**（标签压控件上面，
    设置页那样一段一项没问题），塞进群管理的三列 `.settings-grid` 里就成了"文字一行、
    勾选框另起一行"；数字框也因为要被 `justify-content: space-between` 推到右边而贴边。
  - **改了什么**：`app.css` 新增 `.field.inline`（横向 flex + 控件右对齐 + 数字框固定宽度），
    `.settings-grid .field.inline` 里标签 `white-space: nowrap`；
    `groups.js` 的 `settingRow()` 给 `<label>` 加 `inline` 类，并把
    `.settings-grid` 的列宽下限从 180px 提到 230px（"回复限速（秒）"不再换行、数字框不贴边）。
    只影响群级配置这一处；设置页的 `.field` 保持原来的竖向排版。
  - **怎么验证**：离线渲染浅色/深色两套主题核对（六个字段全部标签+控件同行）；
    `test_offline.py` / `check_frontend.py` / 三个 DOM 测试全绿。

- 2026-10-11：**浅色模式改成 QQ（NT）桌面端配色**（只动
  `web/static/css/app.css` 的变量块 + 几处写死的颜色，没有改逻辑）：
  - 取色（从 QQ 客户端界面取值）：底色 `#F5F5F5` / 会话列表 `#EBEBEB` / 选中项 `#E2E2E2` /
    主蓝 `#0099FF`（按钮、选中导航项、发送键）/ 深蓝 `#0085FF`（链接、@）/ 工具栏 `#5AB6F4` /
    **自己发的气泡 `#CCEBFF` 浅蓝底 + 深色字**（原来是自己调色板里的靛蓝渐变白字）/
    分隔线 `#E6E6E6` / 主文字 `#000000` / 次文字 `#666666` / 在线绿 `#12B7F5`（原来是绿）/
    未读胶囊红 `#F74C30`（和深色一致）/ 滚动条 `#D6D6D6`；
  - 结构上新增 `--bubble-out-text`（深色仍是白字，浅色改成深色字）、`--grad-soft`（浅色用
    纯灰 `#E2E2E2`）、`--glow: none` + `--brand-bg: #F5F5F5`（QQ 两种模式都没有背景光晕，
    品牌条也从深蓝渐变改成 QQ 那种"顶栏跟底色一样"）；`--ok` 语义色在浅色下是蓝（QQ 用蓝表示
    正常/在线），`.mention` / `.cmd-tag` 改用 `--primary-dark` 保证浅底上可读；
  - 浅色的绿色 tint 一并改为蓝（`--ok-rgb`）；
  - **深色模式逐字未变**（所有深色变量值与上一版一致）；
  - **怎么验证**：离线渲染浅色（使用说明页：白卡片 + 浅灰底 + 蓝选中项）与深色（聊天页）
    两套主题核对，临时文件已清理。

- 2026-10-11：**修「内置 AI 已配好，刷新网页却提示未配置完整」**：
  - **根因**：`ai` 是**按机器人**的配置段（`config_schema.PER_BOT_SECTIONS`，设置页里也写着
    "以下设置只作用于当前选中的机器人"），「设置 → AI 服务」里填的密钥/接口/模型落在
    `bots[].overrides.ai`，**全局 `ai.api_key` 往往是空的**；而判断"内置 AI 可用吗"的地方
    （`Runtime.status()` 的 `ai` 字段 → 网页启动提示 + 运行状态页、`/health` 的 `ai_usable`、
    插件侧的 `_ProviderShim.usable`）全都只读**全局**客户端，于是误报。
    真实发送走的是 `bot_ai_client(bot_id)`（读按机器人的有效配置），所以 AI 本身是好的。
  - **改法**：新增 `Runtime.ai_status()` —— 全局可用就返回全局（`scope="global"`），
    否则挨个看**启用的机器人**解析出来的有效配置，谁配全了就返回谁的状态并标
    `scope="bot"` / `bot_id` / `bot_name`；都没有才是 `scope="none"`（前端据此提示）。
    `status()` 与 `/health` 都改用它；`AstrBotHost` 用**线程局部**记录"当前正在处理的机器人"
    （`current_bot_id`，不能用实例属性：多机器人各一条线程，会串号），`Context.get_using_provider()`
    把它传给 `_ProviderShim`，`_ProviderShim._client()` 先按当前机器人取客户端 ——
    这样本地 Ollama 插件的"内置 AI 可用就让给核心"才不会再误判。
  - 前端：启动提示文案改成说明"AI 服务这一组只作用于当前选中的机器人"，
    并区分"已关闭"与"未配置完整"；运行状态页在内置 AI 是按机器人配置时显示 `· 按机器人`。
  - **怎么验证**：`tests/unit/test_ai_scope.py`（20 项：全局/按机器人/都没有/停用机器人/
    取客户端抛异常/provider 按机器人/线程不串号/Context 带 bot_id）；修复后
    `/api/admin/status` 返回的 AI 状态为可用（`scope="bot"`）；`test_offline.py` 575 项、
    `check_frontend.py`、三个 DOM 测试全绿。
  - 注意：**全局 `ai` 段仍然是"兜底"用的**（没有单独配 AI 的机器人会沿用它）；
    想让所有机器人共用一份配置，就在「设置 → 机器人账号」不选具体机器人时改全局段。

- 2026-10-11：**修「几个机器人同在一个群时，群里的一张图被重复保存好几份」**：
  - **根因有两层**：① 群里发一张图，每个机器人都各自收到这条消息并各自下载，而**每份下载地址
    都带各自的 rkey 签名，URL 完全不同** —— 原来只按 `url_hash` 去重根本拦不住；
    ② `download()` 的「查重 → 落盘」之间没有锁，两条线程会同时查到"还没存过"然后各写一份
    （`MediaStore._locks` / `_inflight` 这两个字段一直存在，但 `download()` 之前根本没用到）。
  - **改法**：`core/storage.py` 的 `media` 表加 `sha256` 列（`find_media_by_sha256()`）+
    老库自动 `ALTER TABLE` 补列并建索引（**索引不能写进 SCHEMA**：老库执行
    `CREATE INDEX ... (sha256)` 会因为列不存在直接报错，程序起不来）；
    `core/media_store.py` 新增 `content_hash(blob)`，`save_bytes()` 落盘前先按内容指纹查重，
    **命中就复用原文件、也不再登记第二条记录**（返回带 `reused=True` 的记录）；
    `download()` 用 `_key_lock()` 把同一个 URL 的下载串行化，`_save_lock` 把
    "查重 → 落盘 → 登记"这一小段串行化（网络下载仍在锁外，不拖慢并发）。
  - **效果**：同群 N 个机器人收到同一张图 → 磁盘 1 个文件、媒体表 1 条记录、每个机器人的消息
    附件都指向同一份本地文件；同名不同内容仍按老规矩加 `_1`/`_2` 后缀，不会互相盖掉。
  - **怎么验证**：`tests/unit/test_media_dedup.py`（22 项：双机器人 / 4 线程并发 / 不同内容 /
    同 URL / 文件被删后不复用空记录 / 老库补列 / 失败登记）；`test_offline.py` 575 项、
    `check_frontend.py`、三个 DOM 测试全绿。
  - 未做：**已经存下来的历史重复文件不会自动清理**（避免误删真实数据）；需要时再写一次性去重脚本。

- 2026-10-10：**深色模式整体换成 QQ（NT）深色主题配色**（只动样式，未改任何逻辑）：
  - 取色（从 QQ 客户端界面取值）：底色 `#222222` / 面板 `#2F2F2F` / 抬升面（气泡·输入框·hover）`#3B3B3B` /
    主蓝 `#0066CC`（按钮、发送键）/ 亮蓝 `#5FB4FF`（强调字、描边、链接）/ 渐变次色 `#4D94DB` /
    主文字 `#EAEAEA` / 次文字 `#9C9C9C` / 分隔线 `#3B3B3B` / 在线绿 `#1EE288` / 数字胶囊 `#595959`；
  - 换肤方式：把「语义色的不同透明度」收进变量——`--accent-rgb / --ok-rgb / --err-rgb / --warn-rgb`
    + `rgba(var(--accent-rgb), .12)`，再加 `--grad / --grad-v / --grad-soft / --brand-bg / --glow /
    --focus-ring / --ripple / --chat-bg / --chat-bar / --pre-bg / --scrim / --scroll-thumb /
    --unread-bg` 等；深色只改这两块变量（`body[data-theme="dark"]` 与
    `@media (prefers-color-scheme: dark) body[data-theme="auto"]`），**没有**再写一堆深色覆盖规则；
  - 具体改动：聊天区/顶栏/输入区在深色下统一为 `#222222`（会话列表仍是 `#2F2F2F`，和 QQ 一致）、
    深色去掉原来的靛紫背景光晕、选中会话改成灰底 + 蓝描边、未读数字胶囊改成 QQ 的灰底白字、
    日志 `pre` 改成 `#191919` 底 + `#DCDCDC` 字、滚动条 `#3B3B3B`；
    规则文件 `web/static/css/app.css`（含变量块）+ `web/static/css/anim.css`（涟漪、聚焦底色）；
  - **浅色模式保持原样**（所有浅色变量值与原来逐字一致）；
  - 唯一顺带的小改动：`.cmd-tag` 底色由绿色 tint 改成跟正文一致的 accent tint（深色下才是蓝）；
  - **怎么验证**：`python tests/check_frontend.py` 全绿；另外把后台首页渲染成静态页，
    离线核对聊天/运行状态/使用说明三页的层次与对比度，临时文件已清理；
  - 未做：统计页柱状图仍是靛蓝/紫/绿的固定色（`web/static/js/admin.js` 内联渐变），
    要和 QQ 配色统一的话得把它也改成读 CSS 变量。

- 2026-10-07：**带 @ / 指令标签的消息自动改用 Markdown + 新增指令标签支持**
  （问题现象：正文含 @ 某人时不会自动切到 Markdown，导致标签不渲染；官方事实见
  `docs/QQ_MESSAGE_NOTES.md` 第七～九节）：
  - **格式自动升级**：`core/message_text.py` 新增 `markdown_only_tags()`（识别 @某人 / 指令标签）；
    `Runtime.send_text()` 在请求为纯文本时自动改用 markdown，返回值加 `format` / `auto_format` /
    `_note`（原因文案 `AUTO_MARKDOWN_NOTE`），**只影响这一次发送、不写配置**；
    `web/server.py` 的 `/api/chat/send` 把 `auto_format` 回传给页面；网页插入标签时
    `syncFormatWithContent()` 会立刻把 `#sendFormat` 切到 Markdown 并提示原因；
  - **指令标签**：`build_cmd_enter()` / `build_cmd_input()` / `read_cmd_tag()` / `cmd_tags()` /
    `cmd_markup_to_text()`；值统一 `quote(safe="")`（官方要求 urlencode）、按官方 100 字符截断、
    `show` 与 `text` 相同时不重复写、`reference=true` 才写该属性；
  - 网页：发送框新增 **`⌘ 指令`** 面板（`#cmdBtn` / `#cmdPanel`：回车指令•参数指令 `#cmdKind`、
    `#cmdText`、`#cmdShow`、`#cmdReference`、`#cmdInsert`），插入时校验空值/长度并自动切 Markdown；
    `#cmdHint` 显示「已插入 N 个指令标签」；收到的指令标签在聊天窗口渲染成绿色 `/回车指令 …` 标签
    （`formatContent()` + `readCmdTag()`，CSS `.cmd-tag`）；
  - 收消息侧：`cmd_markup_to_text()` 也接进 `preview()` 与 `format_for_ai()`，人和 AI 都看不到裸标签；
  - 测试：`tests/unit/test_cmd_tags.py`（26 项）+ `test_offline.py` 第 [46] 节（16 项）+
    `tests/dom_test_messages.js` 40 项（新增指令标签渲染 / 格式自动切换 / 插入）+ `check_frontend.py`
    新增两组契约规则。
- 2026-10-06：**新增「群聊 @ 某人」**（官方 `qqbot-at-user` 标签；官方事实与来源见
  `docs/QQ_MESSAGE_NOTES.md` 第七节）：
  - 发送侧：`core/message_text.py` 新增 `build_at_user()` / `at_user_ids()` / `normalize_outgoing_at()`；
    `Runtime.send_text()` 统一把**旧写法** `<@openid>`、`<@!openid>` 换成官方标签
    `<qqbot-at-user id="openid" />`（文本与 markdown 消息都生效，幂等），
    所以网页、定时任务、插件里写旧写法都能用；
  - `@全体成员`：官方标注 `<qqbot-at-everyone />` **仅文字子频道可用**，群里发不生效 →
    程序把 `<@all>` / `@everyone` 退化成**纯文本**「@全体成员」，不提供群里 @全体成员；
  - 网页：发送框新增 `＠ 提及`（`#mentionPicker`）→ 选中成员后在光标处插入官方标签，
    `#mentionHint` 实时显示「将 @：谁」；私聊禁用（官方只支持群聊/文字子频道）；
  - 候选名单接口 `GET /api/chat/mentions?group=…`：群成员缓存 + **最近在群里说过话的人**兜底
    （官方群成员接口要求机器人是群管理员），新增 `SQLiteStore.recent_senders()`；
  - 收到的 @ 仍显示成 `@昵称` / `@全体成员`（原有 `mention_markup_to_text()` 未变）；
  - 测试：`tests/unit/test_at_mentions.py`（23 项）+ `test_offline.py` 第 [45] 节（10 项）+
    `tests/check_frontend.py` 的 @ 契约规则（同时把"@ 已移除"的旧断言改成"@ 已接好"）。
- 2026-10-06：**新增 Markdown 消息发送 + 纯文本/Markdown 切换**（依据 QQ 官方文档；官方事实、字段表与
  错误码清单见 `docs/QQ_MESSAGE_NOTES.md`，每条都带官方来源链接）：
  - 配置：每个机器人 `send.message_format`（默认 `text`）；
  - 网页：聊天发送框旁 `#sendFormat` 下拉（只影响这一次发送，选择记在 `localStorage`）；
  - 平台载荷：`{"msg_type": 2, "markdown": {"content": ...}}` —— **用了 markdown 就不能带顶层 `content`**；
    原生 markdown **无需申请**（2026/04/23 起单聊/群聊对所有机器人开放），模板 markdown 字段官方已废弃；
  - **被拒自动降级**：`is_markdown_error()` 命中 markdown 相关错误码（`40034124/40054007/50037/304036` 等 17 个）
    时自动用纯文本重发，`log.warning` + 返回值 `_note`，网页状态栏显示「已回退为纯文本（原因…）」；
  - markdown 模式下 `reply.strip_markdown` 不再剥离 AI 回复的 markdown 符号；纯文本模式行为不变；
  - 官方**不在支持清单**里的语法：代码块 ```、行内代码、表格、颜色/字号、转义规则；图片必须公网可访问 URL；
    长度上限官方未公布（超限报 `40054007`）；流式 markdown 官方仅单聊支持，**本次不做**。
- 2026-10-06：**README.md 重写为"面向使用者"的版本**（292 → 378 行）：删掉了"打包分发 /
  打包成 exe / 用 Git 上传到 GitHub"三章与相关链接，去掉与个人环境有关的内容；
  新增「环境要求」「网页后台（11 个页面说明）」「插件安全提示（静态扫描）」等章节，
  并核对了文档里的数字（17 分组 / 110 配置项 / 52 路由 / 各测试项数）。
  注意：`build_exe.py`、`build_exe.bat` 仍在仓库里，只是 README 不再宣传打包流程。
- 2026-10-06：**`core/qq_api.py` 的上传逻辑拆出**：19 个上传方法 + 上传常量 → `core/qq_upload.py`
  的 `QQUploadMixin`（`QQApiClient` 继承获得）；错误类型与错误分类 → `core/qq_errors.py`
  （两边共用，放一起才不会循环导入）；`core/qq_api.py` 1317 → 831 行，继续再导出全部公开名字，
  `QQApiClient` 的 55 个成员与拆分前逐名一致（用 AST/dir 比对过）。
- 2026-10-06：**前端全局命名空间锁成只读**（`util.js` 的 `protectGlobal` + `window.__qbmProtect`，
  其它 6 个脚本文件在挂全局时调用）：`window.Util/Effects/Chat/Settings/Admin/Groups/App` 不能被
  整体替换或 delete，但**对象内部字段照旧可改**（所以没用 `Object.freeze`）。测试 `tests/dom_test_globals.js`。
- 2026-10-06：**云同步加了"上传前敏感字段扫描"**（`cloud_sync.find_sensitive_keys`）：
  文本/JSON 里出现 `api_key/secret/token/password/...` 这类**字段名**就跳过上传（本地文件不动），
  日志与同步结果里写明是哪些文件、命中了哪些键；新增开关 `cloud_sync.skip_secret_files`（默认开）。
  测试：`tests/unit/test_cloud_secrets.py` 19 项 + `test_offline.py` 第 [43] 节。
- 2026-10-06：`tests/README.md` 写明测试分层与提交前检查清单；新增两个单元测试文件。
- 2026-10-06（P0 收尾）：**两个大文件拆包完成** ——
  `core/astrbot_compat.py`（2035 行）→ `core/astrbot_shim/`（base/runner/components/filters/
  star/provider/compat_utils/install/host + `__init__`），原文件变 139 行兼容层；
  `core/runtime.py`（1969 行）→ `core/runtime_pkg/`（base/bot/config/messaging/names/
  moderation/media/panels/scheduler/lifecycle/core），原文件变 66 行兼容层。
  验证：521/0、前端契约、`run.py --check`、DOM 21/7、单实例锁 12 全绿；并用 AST 逐名比对过
  "Runtime 成员集合 / 模块顶层名字 / `astrbot.*` 垫片结构"与拆分前完全一致。
- 2026-10-06：新增 `core/plugin_safety.py`（插件源码 AST 安全扫描，只解析不执行）+
  `tests/unit/test_plugin_safety.py`（26 项）；已接线到插件页（每个插件一个徽章 + 顶部汇总，
  测试见 `test_offline.py` 第 [42] 节）；`plugins/README.md` 补了"插件是任意 Python 代码"警告。
  注意：`info["path"]` 可能是空的（实测为空），`_scan_safety` 会回退到
  `plugins/<模块名>/`；新增用例专门断言"确实读到了源码文件"，防止路径写错变成空扫描。
- 2026-10-06：配置热更新只重载真正变了的顶层分组（`_changed_sections`/`_describe_changes`），改动画不再重启云同步、不再谎报「过滤与安全、云同步」；外部改 `config.json` 也能正确识别。
- 2026-10-06：插件支持**按机器人隔离**（`plugins.enabled_names`/`plugins.disabled_names` + `PluginManager.bot_policy/allowed_names` + `AstrBotHost.dispatch(allowed=...)` + `POST /api/admin/plugins/bot` + 插件页「当前机器人」列）。
- 2026-10-06：配置读取加固 —— `utf-8-sig` 容忍 BOM；解析失败**绝不覆盖**，先备份 `config.json.broken-<时间>` 再报错（原因见技术债第 10 条）。
