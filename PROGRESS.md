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

- 程序版本：1.0.0（见 `core/__init__.py`）
- 启动命令：
  - 正常：`python run.py`
  - 自检：`python run.py --check`
  - 调试（不连 QQ）：`python run.py --no-bots`
  - 临时端口：`python run.py --port 9000`
- 测试命令：
  - 端到端：`python tests/test_offline.py`（534 项）
  - 单元：`python tests/unit/test_plugin_safety.py`、`python tests/unit/test_cloud_secrets.py`
  - 单实例锁：`python tests/test_instance_lock.py`
  - 前端契约：`python tests/check_frontend.py`
  - DOM 会话列表：`node tests/dom_test_conversations.js`
  - DOM 消息渲染：`node tests/dom_test_messages.js`
  - DOM 全局保护：`node tests/dom_test_globals.js`
  - 测试分层说明：`tests/README.md`
  - 模拟注入：`python tests/simulate_message.py`
  - 插件安全自查：`python -m core.plugin_safety plugins`
- 接口索引：`docs/AI_INDEX.md`（本文件不再内嵌索引）
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
