<!-- 由 PROGRESS.md 拆出；改动接口时同步更新这里 -->
> **用法**：改代码前先看这里，只列函数名 + 一句话 + 所在文件，不贴实现。
> 需要细节时用 `sed -n '起,止p' 文件` 读片段，不要全量读大文件。
> 新增/改名接口后，请把对应行补到本文档（PROGRESS.md 只放进度与约定）。

# AI 接口索引

> 改代码前先看这里。只列函数名 + 一句话 + 所在文件，不贴实现。
> 需要细节时再用 `sed -n '起,止p' 文件` 读片段，不要全量读文件。

---

## 一、程序入口

| 名称 | 文件 | 说明 |
|---|---|---|
| `main()` | `run.py` | 启动入口；解析 --check/--port/--host/--no-bots/--config |
| `self_check(config_path)` | `run.py` | 离线自检：配置/存储/媒体/文本/Web 路由 |
| `InstanceLock` | `run.py` | 单实例保护：文件锁 + 目录锁 + 独占端口 |
| `build_logger(config, config_manager)` | `run.py` | 构建 Logger（含访问日志总闸） |
| `create_app(config_manager, logger_obj)` | `web/server.py` | 构建 Flask 应用；`app.attach_state(rt, logger)` 注入运行时 |

---

## 二、配置

| 名称 | 文件 | 说明 |
|---|---|---|
| `DEFAULT_CONFIG` | `core/config_schema.py` | 全部默认值（按分组嵌套） |
| `FIELDS` | `core/config_schema.py` | 每个配置路径的元信息（type/label/hint/min/max/options） |
| `SECTION_LABELS` / `SECTION_FIELDS` / `SECTION_ORDER` | `core/config_schema.py` | 设置页分组结构 |
| `PER_BOT_SECTIONS` | `core/config_schema.py` | 按机器人隔离的分组：ai/reply/filters/features/send/scheduler/panels/plugins |
| `GLOBAL_ONLY_PATHS` | `core/config_schema.py` | 例外：plugins.enabled / plugins.dir 仍是全局 |
| `RESTART_REQUIRED` | `core/config_schema.py` | 需重启才生效的路径 |
| `SECRET_FIELDS` / `MASKED_FIELDS` / `SECRET_MASK` | `core/config_schema.py` | 密钥字段与掩码 |
| `is_per_bot(path)` / `per_bot_paths()` | `core/config_schema.py` | 判断路径是否按机器人 |
| `flatten(data)` / `field_paths()` | `core/config_schema.py` | 拍平配置 / 列出可编辑路径 |
| `sections_payload()` | `core/config_schema.py` | 给设置页的分组结构 |
| `Config` | `core/config_manager.py` | 运行期配置对象；`str_of/int_of/float_of/bool_of/list_of/abs_path` |
| `ConfigManager` | `core/config_manager.py` | 读写/迁移/环境变量/按机器人覆盖 |
| `ConfigManager.apply_incoming(incoming, bot_id)` | `core/config_manager.py` | 网页保存入口；返回 (applied, need_restart, changed) |
| `ConfigManager.payload()` | `core/config_manager.py` | 给设置页的完整数据 |
| `ConfigManager.get_for_bot(bot_id, path)` / `set_for_bot` | `core/config_manager.py` | 按机器人读/写（per-bot 路径走覆盖，其余走全局） |
| `ConfigManager.resolve_for_bot(bot_id)` | `core/config_manager.py` | 合成"全局 + 该机器人覆盖"的完整配置 |
| `ConfigManager.mask_secrets(data)` | `core/config_manager.py` | 密钥打码 |
| `ConfigManager.copy_bot_settings_to_all(from_bot)` | `core/config_manager.py` | 一键复制到所有机器人 |
| `ConfigManager.clear_bot_overrides(bot_id)` | `core/config_manager.py` | 清空某机器人覆盖 |
| `ConfigManager.active_bot_id()` / `set_active_bot(bot_id)` | `core/config_manager.py` | 后台当前选中的机器人 |
| `load_config(path)` | `core/config_manager.py` | 快捷加载 |
| `resolve_env_path(env_key)` | `core/config_manager.py` | 环境变量名 → 配置路径（支持 `__` 与键名下划线） |
| `CLEAR_MARKER` | `core/config_manager.py` | `__CLEAR__`：网页"清空密钥"魔法值 |

---

## 三、日志

| 名称 | 文件 | 说明 |
|---|---|---|
| `Logger` | `core/logger.py` | 日志管理器；`get_logger/log/set_level/set_console_color` |
| `Logger.list_files()` / `read_tail()` / `read_file()` / `clear_older_than()` | `core/logger.py` | 后台日志页用 |
| `mask_transfer_code(text)` | `core/logger.py` | 6 位数字脱敏 |

---

## 四、存储

| 名称 | 文件 | 说明 |
|---|---|---|
| `SQLiteStore` | `core/storage.py` | 消息/会话/媒体/群成员/禁言/机器人状态 |
| `MemoryStore` | `core/storage.py` | 内存实现（storage.enabled=false） |
| `SQLiteStore.add(msg)` | `core/storage.py` | 写入消息（自动归一化 + 更新会话 + 定期裁剪） |
| `SQLiteStore.get_conversation(key, limit, before_id)` | `core/storage.py` | 按会话取历史 |
| `SQLiteStore.conversations(limit, bot_id)` | `core/storage.py` | 会话列表 |
| `SQLiteStore.has_message(bot_id, msg_id)` | `core/storage.py` | 去重 |
| `SQLiteStore.find_by_msg_id(msg_id, bot_id)` | `core/storage.py` | 按平台消息 ID 找记录（引用用） |
| `SQLiteStore.update_message(id, **fields)` | `core/storage.py` | 更新消息（撤回标记等） |
| `SQLiteStore.set_user_name(openid, name)` / `user_names()` | `core/storage.py` | 跨会话昵称 |
| `SQLiteStore.set_alias(openid, name)` / `aliases()` / `get_alias(openid)` | `core/storage.py` | 手动命名 |
| `SQLiteStore.set_meta(key, value)` / `get_meta(key, default)` | `core/storage.py` | 键值杂项 |
| `SQLiteStore.repair_legacy_data()` | `core/storage.py` | 修幽灵会话 + 错误昵称 |
| `SQLiteStore.repair_group_conversation_names(force, names)` | `core/storage.py` | 修群会话名 |
| `SQLiteStore.group_name_suspects()` | `core/storage.py` | 统计可疑群名数 |
| `SQLiteStore.add_media(record)` / `find_media_by_hash(hash)` / `list_media(...)` | `core/storage.py` | 媒体表 |
| `SQLiteStore.upsert_members(bot_id, group, members)` / `list_members(group, bot_id)` | `core/storage.py` | 群成员 |
| `SQLiteStore.add_muting(record)` / `list_mutings(...)` / `is_muted(...)` / `release_muting(id)` / `release_member_muting(...)` / `expire_mutings()` | `core/storage.py` | 禁言 |
| `SQLiteStore.set_bot_status(bot_id, online, error, started_at)` / `get_bot_status()` | `core/storage.py` | 机器人状态 |
| `SQLiteStore.stats_summary()` | `core/storage.py` | 今日/累计统计 |
| `SQLiteStore.apply_limits(...)` / `trim()` / `vacuum()` | `core/storage.py` | 裁剪与维护 |
| `conversation_key(type, group, openid, bot_id)` / `conv_key_for(bot_id, type, group, openid)` | `core/storage.py` | 会话键 |
| `is_image_attachment(att)` | `core/storage.py` | 附件是否图片（关键：防 .exe 当图片渲染） |
| `normalize_attachments(raw)` / `normalize_quote(raw)` / `normalize_message(msg)` | `core/storage.py` | 归一化 |
| `preview_text(msg)` | `core/storage.py` | 会话列表预览 |

---

## 五、媒体

| 名称 | 文件 | 说明 |
|---|---|---|
| `MediaStore` | `core/media_store.py` | 下载/留存/查询/清理 |
| `MediaStore.save_bytes(blob, ...)` | `core/media_store.py` | 落盘 + 登记 |
| `MediaStore.fetch(url)` | `core/media_store.py` | 下载 URL（只允许公网 http/https） |
| `MediaStore.download(url, ...)` / `download_async(url, ...)` | `core/media_store.py` | 下载并留存（去重） |
| `MediaStore.persist_attachments(message, bot_id, conv_key, blocking)` | `core/media_store.py` | 一条消息的全部附件留存 |
| `MediaStore.cleanup(keep_days)` / `resolve_local(name)` / `decode_data_url(data)` | `core/media_store.py` | 清理 / 解析本地文件 / 解析 data URL |
| `url_hash(url)` | `core/media_store.py` | URL 去重键 |
| `guess_ext(url, content_type, fallback)` | `core/media_store.py` | 猜扩展名 |
| `sniff_image_type(blob)` / `looks_like_image(blob)` | `core/media_store.py` | 魔数校验 |
| `host_allowed(url)` | `core/media_store.py` | SSRF 防护 |
| `safe_name(name)` | `core/media_store.py` | 防路径穿越 |
| `is_probably_image(url, content_type)` | `core/media_store.py` | 图片判断 |

---

## 六、上下文

| 名称 | 文件 | 说明 |
|---|---|---|
| `ContextManager` | `core/context_manager.py` | 按 bot+会话隔离的上下文 |
| `ContextManager.get(bot, type, openid, max_history)` | `core/context_manager.py` | 读（max_history 只影响本次调用） |
| `ContextManager.append(bot, type, openid, role, content, name, max_history)` | `core/context_manager.py` | 追加 |
| `ContextManager.clear(bot_id, type, openid)` | `core/context_manager.py` | 清空（可限定机器人） |
| `ContextManager.summary(bot_id)` | `core/context_manager.py` | 后台列表 |
| `ContextManager.delete_file(scope, name, bot_id)` | `core/context_manager.py` | 删除单个（按归属校验） |
| `ContextManager.format_for_prompt(bot, type, openid, system, max_history)` | `core/context_manager.py` | 拼 OpenAI messages |
| `ContextManager.key(bot, type, openid)` | `core/context_manager.py` | 缓存键 |

---

## 七、文本

| 名称 | 文件 | 说明 |
|---|---|---|
| `set_name_resolver(fn)` / `resolve_display_name(openid)` | `core/message_text.py` | 昵称解析器注入 |
| `extract_face_info(content)` | `core/message_text.py` | 表情标记 → 可读文本 + 图片 URL |
| `is_pure_face_markup(content)` / `face_markup_to_text(content)` | `core/message_text.py` | 纯表情判断 / 转文本 |
| `mention_markup_to_text(content, mentions)` | `core/message_text.py` | @ 标记 → @昵称 |
| `format_for_ai(content, mentions)` | `core/message_text.py` | 给 AI 看的纯文本 |
| `strip_markdown(text)` | `core/message_text.py` | 去 Markdown 符号 |
| `split_message(text, max_length)` | `core/message_text.py` | 分段 |
| `looks_meaningless(text)` | `core/message_text.py` | 无意义判定 |
| `preview(content, limit)` | `core/message_text.py` | 预览 |
| `MENTION_RE` / `AT_USER_NEW_RE` / `AT_EVERYONE_NEW_RE` / `AT_ALL_LEGACY_RE` | `core/message_text.py` | @ 相关正则 |

---

## 八、消息过滤与链路

| 名称 | 文件 | 说明 |
|---|---|---|
| `MessageFilter` | `core/message_filter.py` | 关键词 / 敏感词 / 无意义 / 时长解析 |
| `MessageFilter.match_keyword(content)` | `core/message_filter.py` | 精确优先，其次模糊 |
| `MessageFilter.contains_sensitive(text)` / `mask_sensitive(text)` | `core/message_filter.py` | 敏感词 |
| `MessageFilter.is_meaningless(content)` | `core/message_filter.py` | 无意义 |
| `MessageFilter.parse_duration(text)` | `core/message_filter.py` | 10m / 2h / 600 → 秒 |
| `MessageProcessor` | `core/message_processor.py` | 回复链路（每机器人一条队列与工作线程） |
| `MessageProcessor.process(message)` | `core/message_processor.py` | 主链路：过滤→指令→关键词→插件→限速→AI→分段 |
| `MessageProcessor._handle_command(...)` | `core/message_processor.py` | 内置指令 |
| `MessageProcessor._ai_reply(...)` | `core/message_processor.py` | AI 回复（含插件 on_llm_request 钩子） |
| `MessageProcessor._reply(message, text)` | `core/message_processor.py` | 分段发送 |
| `MessageProcessor._send_plugin_result(...)` | `core/message_processor.py` | 发送插件结果（保序） |
| `HELP_TEXT` | `core/message_processor.py` | /帮助 内容 |
| `MENTION_PREFIX_RE` | `core/message_processor.py` | 去掉消息开头的 @提及 |

---

## 九、网关

| 名称 | 文件 | 说明 |
|---|---|---|
| `QQGateway` | `core/gateway.py` | 一个机器人的 WebSocket 长连接 |
| `QQGateway.start()` / `stop()` / `restart()` | `core/gateway.py` | 生命周期 |
| `QQGateway.status_text()` / `ready` / `connected` | `core/gateway.py` | 状态 |
| `QQGateway._handle_dispatch(data)` | `core/gateway.py` | 事件解析 → 内部消息字典 |
| `QQGateway.build_event(msg, runtime)` | `core/gateway.py` | 构造 AstrMessageEvent（AstrBot 兼容层用） |
| `INTENT_*` / `DEFAULT_INTENTS` / `FALLBACK_INTENTS` | `core/gateway.py` | intents 常量 |
| `MESSAGE_EVENTS` / `QUOTE_FIELDS` | `core/gateway.py` | 事件与引用字段名 |

---

## 十、QQ API

> **2026-10-06 已拆包**：上传相关方法（19 个）与上传常量在 `core/qq_upload.py`
> （`QQUploadMixin`，由 `QQApiClient` 继承）；错误类型与错误分类在 `core/qq_errors.py`。
> `core/qq_api.py` 继续再导出这些名字，老 import 照旧可用。

| 名称 | 文件 | 说明 |
|---|---|---|
| `QQApiClient` | `core/qq_api.py` | 一个机器人的 HTTP 客户端 |
| `QQApiClient.get_access_token(force_refresh)` | `core/qq_api.py` | Token 缓存 + 单飞刷新 |
| `QQApiClient.request(method, url, json_data, params, retries)` | `core/qq_api.py` | 带重试与 token 刷新的请求 |
| `QQApiClient.send_text(target_type, openid, content, reply_msg_id, msg_type, reply_style)` | `core/qq_api.py` | 发文本（引用逐级降级） |
| `QQApiClient.send_media(target_type, openid, file_info, content, reply_msg_id, reply_style)` | `core/qq_api.py` | 发富媒体 |
| `QQApiClient.send_image_by_data(...)` / `send_image_by_url(...)` | `core/qq_api.py` | 发图片 |
| `QQApiClient.send_file(...)` | `core/qq_api.py` | 发文件（小文件一次上传 / 大文件分片） |
| `QQApiClient.upload_media(...)` / `upload_with_fallback(...)` | `core/qq_api.py` | 上传 |
| `QQApiClient.upload_prepare(...)` / `upload_part_finish(...)` / `upload_by_parts(...)` | `core/qq_api.py` | 官方分片上传 |
| `QQApiClient.group_info(group)` / `group_members(group)` / `group_member(group, member)` | `core/qq_api.py` | 群信息 |
| `QQApiClient.bot_state(group)` | `core/qq_api.py` | 机器人在群里的身份 |
| `QQApiClient.restrict_chat_setting(group, members)` / `mute_member(...)` / `unmute_member(...)` / `list_muted_members(group)` | `core/qq_api.py` | 禁言 |
| `QQApiClient.recall_group_message(group, msg_id)` / `recall_private_message(openid, msg_id)` | `core/qq_api.py` | 撤回 |
| `QQApiClient.list_command_panels(scope, with_raw)` / `create_command_panel(...)` / `update_command_panel(...)` / `delete_command_panel(id)` / `set_panel_targets(...)` | `core/qq_api.py` | 指令面板 |
| `QQApiClient.normalize_panel_items(items)` | `core/qq_api.py` | 面板项规范化 |
| `QQApiClient.file_download_url(target_type, openid, file_info)` | `core/qq_api.py` | file_info → 下载地址 |
| `QQApiClient.extract_message_id(result)` | `core/qq_api.py` | 从响应取消息 ID |
| `normalize_mute_state(raw)` / `format_weekdays(days)` | `core/qq_api.py` | 禁言状态整理 |
| `is_param_error(exc)` / `is_url_upload_error(exc)` | `core/qq_api.py` | 错误分类 |
| `FILE_TYPE_*` / `MEDIA_SOFT_LIMIT_MB` / `MEDIA_HARD_LIMIT_MB` / `CHUNKED_UPLOAD_THRESHOLD` | `core/qq_api.py` | 常量 |
| `TransientError` / `APIError` | `core/qq_api.py` | 异常类型 |

---

## 十一、群管理

| 名称 | 文件 | 说明 |
|---|---|---|
| `GroupManager` | `core/group_manager.py` | 群列表/成员/禁言/群配置 |
| `GroupManager.groups(bot_id)` | `core/group_manager.py` | 群列表（按机器人过滤） |
| `GroupManager.group_bot_ids(group)` | `core/group_manager.py` | 哪些机器人看到过这个群 |
| `GroupManager.group_names()` | `core/group_manager.py` | 群名缓存 |
| `GroupManager.refresh_group_name(group, bot_id)` / `refresh_names(bot_id, force, limit, pace)` | `core/group_manager.py` | 刷新群名 |
| `GroupManager.stale_name_groups(bot_id, ttl, ignore_backoff)` | `core/group_manager.py` | 待刷新群名 |
| `GroupManager.trigger_name_refresh(bot_id, limit)` | `core/group_manager.py` | 后台补刷（节流） |
| `GroupManager.members(group, bot_id, refresh)` | `core/group_manager.py` | 成员列表（缓存 + 平台 + 历史） |
| `GroupManager.refresh_members(group, bot_id)` | `core/group_manager.py` | 拉成员 |
| `GroupManager.platform_mute_state(group, bot_id, refresh)` | `core/group_manager.py` | 官方禁言状态 |
| `GroupManager.mute(...)` / `unmute(...)` / `mutings(...)` / `is_muted(...)` / `release_by_id(id)` | `core/group_manager.py` | 禁言 |
| `GroupManager.get_settings(group)` / `set_settings(group, settings)` / `clear_settings(group)` / `effective(group, key, default)` | `core/group_manager.py` | 群级配置 |
| `GroupManager.mute_mode(mode)` | `core/group_manager.py` | 禁言方式（local/api/both） |
| `GroupManager.repair_suspect_names()` | `core/group_manager.py` | 手动修群会话名 |
| `GroupManager.resync_conversation_names()` | `core/group_manager.py` | 启动时回写群名 |
| `GROUP_SETTING_FIELDS` | `core/group_manager.py` | 群配置字段白名单 |
| `MUTE_STATE_TTL_SECONDS` / `NAME_TTL_SECONDS` / `NAME_FAIL_BACKOFF_SECONDS` / `NAME_REFRESH_PACE_SECONDS` | `core/group_manager.py` | 缓存与节流常量 |

---

## 十二、运行时

> **2026-10-06 已拆包**：下表符号的实现分别在 `core/runtime_pkg/<模块>.py`——
> `base`（常量与小工具）、`bot`（`BotRuntime`）、`config`（配置解析/按机器人取配置/热更新）、
> `messaging`（收消息/广播/发送出口/网页视图）、`names`（昵称与群名）、
> `moderation`（限速/禁言/群内身份/撤回）、`media`（对外地址与本机媒体）、
> `panels`（指令面板）、`scheduler`（定时任务）、`lifecycle`（生命周期/后台线程/状态/维护）、
> `core`（`Runtime` 本体，由上述 mixin 组合）。
> `core/runtime.py` 现在只是**兼容再导出层**，`from core.runtime import Runtime` 照旧可用。
> `Runtime` 的 MRO：Runtime → ConfigMixin → MessagingMixin → NameMixin → ModerationMixin →
> MediaMixin → PanelMixin → SchedulerMixin → LifecycleMixin。

| 名称 | 文件 | 说明 |
|---|---|---|
| `Runtime` | `core/runtime_pkg/core.py` | 全局运行时（组合各 mixin） |
| `Runtime.start()` / `stop()` | `core/runtime.py` | 生命周期 |
| `Runtime.on_gateway_message(message)` | `core/runtime.py` | 网关回调入口 |
| `Runtime._handle_incoming(message)` | `core/runtime.py` | 落库 → 留存附件 → 广播 → 提交处理器 |
| `Runtime.subscribe(cb)` / `unsubscribe(cb)` / `broadcast(event)` | `core/runtime.py` | 网页推送 |
| `Runtime.bot_config(bot_id)` | `core/runtime.py` | 按机器人取生效配置 |
| `Runtime.bot_ai_client(bot_id)` | `core/runtime.py` | 按机器人取 AI 客户端 |
| `Runtime.bot_message_filter(bot_id)` | `core/runtime.py` | 按机器人取过滤器 |
| `Runtime.invalidate_bot_configs()` | `core/runtime.py` | 配置缓存作废 |
| `Runtime.get_client(bot_id)` / `pick_bot_id(bot_id)` | `core/runtime.py` | 取客户端 |
| `Runtime.send_text(...)` / `send_image(...)` / `send_file(...)` | `core/runtime.py` | 发送出口 |
| `Runtime._record_outgoing(...)` | `core/runtime.py` | 发送落库 |
| `Runtime.public_message(msg, bot_id)` / `public_messages(list)` / `public_conversations(bot_id)` | `core/runtime.py` | 给网页的结构 |
| `Runtime.lookup_name(openid)` / `short_label(openid)` / `short_group_label(group)` | `core/runtime.py` | 昵称解析 |
| `Runtime.rate_limited(key, interval)` / `mark_replied(key)` | `core/runtime.py` | 限速 |
| `Runtime.is_muted(bot_id, group, member)` | `core/runtime.py` | 禁言查询 |
| `Runtime.group_bot_role(bot_id, group, refresh)` / `group_bot_is_admin(...)` / `cached_group_role(...)` / `warm_group_role(...)` | `core/runtime.py` | 群内身份 |
| `Runtime.recall_message(message_id, msg_id, bot_id, as_admin)` / `recall_own_message(...)` | `core/runtime.py` | 撤回 |
| `Runtime.request_shutdown(reason)` / `is_shutdown_requested()` / `set_shutdown_callback(cb)` | `core/runtime.py` | 关闭 |
| `Runtime.on_config_changed(source)` | `core/runtime.py` | 配置热更新 |
| `Runtime._reconcile_bots()` | `core/runtime.py` | 按配置新建/停止机器人 |
| `Runtime._changed_sections(before, after)` / `_describe_changes(...)` | `core/runtime.py` | 判断改了哪些分组 |
| `Runtime._config_watch_tick()` | `core/runtime.py` | 配置监视一拍 |
| `Runtime.set_bots_disabled(disabled)` | `core/runtime.py` | 调试模式 |
| `Runtime.set_bind(host, port)` / `web_url` / `public_base_url()` / `lan_ip()` | `core/runtime.py` | 网页地址 |
| `Runtime.register_command_panels()` | `core/runtime.py` | 注册指令面板 |
| `Runtime._load_panel_ids()` / `_save_panel_ids(ids)` / `_panel_ids_lock` | `core/runtime.py` | 面板 ID 缓存 |
| `Runtime.cleanup_media(days)` / `clear_messages(conv_key, bot_id)` / `status()` | `core/runtime.py` | 维护与状态 |
| `Runtime.start_port_watcher()` / `start_name_refresher()` | `core/runtime.py` | 后台线程 |
| `BotRuntime` | `core/runtime.py` | 单机器人运行时（client + gateway + 状态） |
| `BotRuntime.status()` | `core/runtime.py` | 状态字典 |

---

## 十三、插件

> **2026-10-06 已拆包**：AstrBot 兼容层的实现分布在 `core/astrbot_shim/<模块>.py`——
> `base`（`logger`/`ASTRBOT_VERSION`/`SHIM_MARK`）、`runner`（`AsyncRunner`/`RUNNER`）、
> `components`（消息组件/消息对象/事件）、`filters`（`_FilterNamespace`/`_CommandGroup`/`_mark`/`register`）、
> `star`（`PluginRuntime`/`Star`/`StarTools`/`AstrBotConfig`）、`provider`（`ProviderRequest`/`Context` 等）、
> `compat_utils`（`_module`/版本比较/`_load_yaml`）、`install`（`install_shim`）、`host`（`AstrBotPlugin`/`AstrBotHost`）。
> `core/astrbot_compat.py` 现在只是**兼容再导出层**（含私有名），老 import 与 `install_shim()` 的
> `astrbot.*` 垫片结构都不变。

| 名称 | 文件 | 说明 |
|---|---|---|
| `PluginManager` | `core/plugin_manager.py` | AstrBot 插件加载/启停/分发 |
| `scan_source(source)` / `scan_file(path)` / `scan_plugin_dir(dir)` / `scan_container(root)` | `core/plugin_safety.py` | 插件源码 AST 静态安全扫描（不执行代码） |
| `summarize(findings)` / `format_report(result)` | `core/plugin_safety.py` | 扫描结果摘要 / 文本报告 |
| `PluginManager.scan()` | `core/plugin_manager.py` | 扫描插件目录 |
| `PluginManager.load_plugins(force)` / `reload()` | `core/plugin_manager.py` | 加载/重载 |
| `PluginManager.diagnostics()` | `core/plugin_manager.py` | 诊断信息（给后台插件页） |
| `PluginManager.list_plugins()` / `names()` | `core/plugin_manager.py` | 列表 |
| `PluginManager.set_disabled(name, disabled)` / `is_disabled(name)` / `apply_changes()` | `core/plugin_manager.py` | 启停 |
| `PluginManager.dispatch_message(msg)` | `core/plugin_manager.py` | 分发消息给插件 |
| `PluginManager.send_steps(runtime, msg, steps)` | `core/plugin_manager.py` | 按步骤发送 |
| `PluginManager.bot_policy(bot_id)` / `allowed_names(bot_id)` | `core/plugin_manager.py` | 按机器人隔离 |
| `PluginManager.migrate_legacy_data()` | `core/plugin_manager.py` | 旧插件数据迁移 |
| `PluginManager.set_bot(bot)` / `refresh_bot_config(config)` | `core/plugin_manager.py` | 注入机器人能力 |
| `PluginBot` | `core/plugin_manager.py` | 给插件的白名单门面 |
| `PluginBot.runtime` / `config` | `core/plugin_manager.py` | 门面 + 去密钥配置副本 |
| `PluginBot.send_message(...)` / `send_group_message(...)` / `send_image(...)` / `bot_ids()` | `core/plugin_manager.py` | 发送能力 |
| `OFFICIAL_DATA_DIR` / `LEGACY_DATA_DIR` / `LEGACY_PLUGIN_ALIASES` | `core/plugin_manager.py` | 目录与别名常量 |
| `AstrBotHost` | `core/astrbot_compat.py` | AstrBot 插件加载/分发 |
| `AstrBotHost.load(name, dir_path)` | `core/astrbot_compat.py` | 加载一个插件 |
| `AstrBotHost.dispatch(msg, runtime, sink, allowed)` | `core/astrbot_compat.py` | 分发消息 |
| `AstrBotHost.build_event(msg, runtime)` | `core/astrbot_compat.py` | 构造 AstrMessageEvent |
| `AstrBotHost.build_llm_request(prompt, system, image_urls, bot_id)` | `core/astrbot_compat.py` | 构造 ProviderRequest 并跑 on_llm_request |
| `AstrBotHost.llm_request_hooks(bot_id)` / `llm_response_hooks(bot_id)` | `core/astrbot_compat.py` | LLM 钩子 |
| `AstrBotHost.send_to_umo(umo, components)` | `core/astrbot_compat.py` | context.send_message 实现 |
| `AstrBotHost.emit(components)` | `core/astrbot_compat.py` | event.send 实现 |
| `AstrBotHost.list_plugins()` | `core/astrbot_compat.py` | 列表 |
| `AstrBotHost.unload()` / `call_loaded_hooks()` / `call_initialize()` | `core/astrbot_compat.py` | 生命周期 |
| `AstrBotPlugin` | `core/astrbot_compat.py` | 已加载的插件对象 |
| `PluginRuntime` | `core/astrbot_compat.py` | 给插件的白名单门面 |
| `Star` | `core/astrbot_compat.py` | 插件基类（KV 存储 / initialize / terminate） |
| `StarTools` | `core/astrbot_compat.py` | 数据目录 / save_json / load_json |
| `Context` | `core/astrbot_compat.py` | 插件 context（配置 / 发消息 / AI provider） |
| `AstrBotConfig` | `core/astrbot_compat.py` | 插件配置（dict 子类 + save_config） |
| `AstrMessageEvent` | `core/astrbot_compat.py` | 消息事件 |
| `AstrBotMessage` / `MessageMember` / `Group` / `MessageType` | `core/astrbot_compat.py` | 消息对象 |
| `MessageEventResult` / `MessageChain` | `core/astrbot_compat.py` | 结果与消息链 |
| `Plain` / `Image` / `At` / `AtAll` / `Face` / `Reply` / `Poke` / `Node` / `Nodes` / `Record` / `Video` / `File` | `core/astrbot_compat.py` | 消息组件 |
| `_FilterNamespace` / `_CommandGroup` / `filter` / `Filter` | `core/astrbot_compat.py` | 过滤器命名空间 |
| `register(name, author, desc, version, repo)` | `core/astrbot_compat.py` | @register 装饰器 |
| `install_shim(force)` | `core/astrbot_compat.py` | 把 `astrbot.*` 垫片装进 sys.modules |
| `EventMessageType` / `PermissionType` / `PlatformAdapterType` | `core/astrbot_compat.py` | 过滤器枚举 |
| `AsyncRunner` / `RUNNER` | `core/astrbot_compat.py` | 常驻事件循环 |
| `ASTRBOT_VERSION` | `core/astrbot_compat.py` | 模拟的 AstrBot 版本 |

---

## 十四、云同步

| 名称 | 文件 | 说明 |
|---|---|---|
| `CloudSync` | `core/cloud_sync.py` | 按间隔同步到 D1 |
| `CloudSync.sync_once(force)` | `core/cloud_sync.py` | 同步一次 |
| `CloudSync.start(immediate)` / `stop(final_sync)` | `core/cloud_sync.py` | 后台线程 |
| `CloudSync.test_connection()` | `core/cloud_sync.py` | 测连接 |
| `CloudSync.pause_info()` / `resume(why)` | `core/cloud_sync.py` | 暂停与恢复 |
| `CloudSync.in_scope(rel)` / `is_blocked(rel)` | `core/cloud_sync.py` | 范围判定 |
| `find_sensitive_keys(rel, blob)` | `core/cloud_sync.py` | 上传前按**字段名**扫密钥（`api_key/secret/token/password/...`），命中即跳过上传 |
| `CloudSync.skip_secret_files` | `core/cloud_sync.py` | 开关：含密钥字段的文件不上传（`cloud_sync.skip_secret_files`，默认 true） |
| `CloudSync.mark_deleted(path)` | `core/cloud_sync.py` | 写墓碑 |
| `D1Backend` | `core/cloud_sync.py` | D1 REST 客户端 |
| `D1Backend.query(sql, params)` / `ensure_table()` / `fetch_all()` / `put(key, value, ts, deleted)` | `core/cloud_sync.py` | D1 操作 |
| `encode_value(blob)` / `decode_value(value)` | `core/cloud_sync.py` | 我们自己的 value 格式（`b64:` 前缀） |
| `check_text_payload(rel, blob)` | `core/cloud_sync.py` | 文本/JSON 安全闸 |
| `looks_like_base64(text)` / `is_text_like(rel)` | `core/cloud_sync.py` | 判断工具 |
| `SYNC_PATHS` / `NEVER_SYNC` / `VALUE_PREFIX` / `D1_ENDPOINT` | `core/cloud_sync.py` | 常量 |

---

## 十五、统计

| 名称 | 文件 | 说明 |
|---|---|---|
| `StatsCollector` | `core/stats.py` | 按天累计 |
| `StatsCollector.record(key, count)` / `record_many(**counts)` | `core/stats.py` | 记录 |
| `StatsCollector.today()` / `recent(days)` / `summary()` | `core/stats.py` | 读取 |
| `StatsCollector.save()` | `core/stats.py` | 落盘 |
| `STAT_KEYS` | `core/stats.py` | 全部统计键 |

---

## 十六、Web 后台

| 名称 | 文件 | 说明 |
|---|---|---|
| `create_app(config_manager, logger_obj)` | `web/server.py` | 构建 Flask 应用 |
| `app.attach_state(runtime, logger)` | `web/server.py` | 注入运行时与日志管理器 |
| 页面路由 | `web/server.py` | `/`、`/health`、`/media/<file>` |
| 聊天接口 | `web/server.py` | `/api/chat/conversations`、`/messages`、`/send`、`/clear`、`/read`、`/raw`、`/group_name`、`/media`、`/alias`、`/participants`、`/stream`、`/image` |
| 运维接口 | `web/server.py` | `/api/admin/status`、`/stats`、`/config`、`/plugins`、`/plugins/bot`、`/plugins/apply`、`/plugins/reload`、`/plugins/force_load`、`/logs`、`/logs/list`、`/logs/download`、`/context`、`/context/delete`、`/media`、`/panels`、`/panels/delete`、`/panels/reload`、`/receiver/<bot>/<action>`、`/maintenance`、`/shutdown`、`/bots`、`/config/copy_to_all`、`/config/reset_bot`、`/ping`、`/cloud`、`/cloud/<action>` |
| 群管理接口 | `web/server.py` | `/api/groups`、`/detail`、`/refresh`、`/mute`、`/unmute`、`/mutings`、`/settings`、`/recall` |
| 机器人接口 | `web/server.py` | `/api/bots`、`/api/bots/active` |

---

## 十七、前端

| 名称 | 文件 | 说明 |
|---|---|---|
| `Util` | `web/static/js/util.js` | 公共工具：apiUrl / qs / ActiveBot / getJSON / postJSON / upload / toast / confirm / fmtTime / humanSize / humanDuration / debounce |
| `Effects` | `web/static/js/effects.js` | 动效：applyUiPrefs / cycleTheme / restoreTheme / attachRipple / stagger / fadeImages / scrollToBottom / nearBottom / remember / recall |
| `Chat` | `web/static/js/chat.js` | 聊天窗口：会话列表 / 消息渲染 / 发送 / 操作框 / 撤回 / 清空 / SSE |
| `Chat.imageKeys(message)` | `web/static/js/chat.js` | 图片去重（测试与调试用） |
| `Chat.messageHtmlForTest(message, index)` | `web/static/js/chat.js` | 消息 HTML（测试用） |
| `Chat.renderConversations(list)` | `web/static/js/chat.js` | 会话列表渲染 |
| `Chat.loadConversations()` / `loadMessages(force)` | `web/static/js/chat.js` | 加载 |
| `Chat.onBotChanged()` | `web/static/js/chat.js` | 切换机器人回调 |
| `Settings` | `web/static/js/settings.js` | 设置页：字段渲染 / 收集 / 保存 / 一键应用到所有机器人 |
| `Admin` | `web/static/js/admin.js` | 运维页：状态 / 统计 / 插件 / 日志 / 上下文 / 指令面板 / 媒体 |
| `Groups` | `web/static/js/groups.js` | 群管理：列表 / 详情 / 成员 / 禁言 / 群配置 / 自动刷新 |
| `App` | `web/static/js/app.js` | 应用外壳：导航切换 / 机器人选择器 / 全局轮询 / 快捷键 |
| `App.go(page)` | `web/static/js/app.js` | 切页 |
| `App.restartPolling(intervalMs)` | `web/static/js/app.js` | 重启轮询 |
| `App.refreshPage(page, isRepeat)` | `web/static/js/app.js` | 刷新当前页 |
| `Util.protectGlobal(name, value)` / `window.__qbmProtect` | `web/static/js/util.js` | 把命名空间锁成只读全局（不能被整体替换/delete，内部字段仍可改） |
| `window.__BOOT__` | `web/templates/index.html` | 注入的启动数据（ui / limits / auth_required） |
| `window.__URL_TOKEN__` | `web/templates/index.html` | URL 里的访问令牌 |

---

## 十八、测试

| 名称 | 文件 | 说明 |
|---|---|---|
| `main()` | `tests/test_offline.py` | 离线端到端（40+ 场景，覆盖全部主链路） |
| `check(name, condition, detail)` | `tests/test_offline.py` | 断言辅助 |
| `FakeClient` / `FakeAI` / `FakeResponse` | `tests/test_offline.py` | 测试桩 |
| `main()` | `tests/test_instance_lock.py` | 单实例锁测试 |
| `main()` | `tests/check_frontend.py` | 前端契约校验（元素 id / 字段类型 / 机器人隔离） |
| `main()` | `tests/dom_test_conversations.js` | 会话列表"不跳动"回归测试（Node + DOM 桩） |
| `main()` | `tests/dom_test_messages.js` | 消息渲染细节测试（图片去重 / 文件消息） |
| `main()` | `tests/dom_test_globals.js` | 全局命名空间保护测试（不可替换/删除，内部字段可改） |
| `main()` | `tests/unit/test_cloud_secrets.py` | 单元测试：云同步敏感字段扫描（19 项） |
| `main()` | `tests/simulate_message.py` | 往运行中的程序注入模拟消息 |
| `main()` | `tests/unit/test_plugin_safety.py` | 单元测试：插件安全扫描（26 项，只依赖 `core/plugin_safety.py`） |
| `main()` | `tests/unit/test_cloud_secrets.py` | 单元测试：云同步敏感字段扫描（19 项，不连网、不碰真实 `data/`） |

> **测试分层（2026-10-06 起）**：`tests/unit/` 放快、只依赖单个模块的单元测试；
> `tests/test_offline.py` 是集成/端到端；`tests/dom_test_*.js` 是前端行为测试；
> `tests/check_frontend.py` 是契约/静态检查。约定与命令见 `tests/README.md`。
> 老的集成用例**没有移动**（避免动历史用例引入风险），新用例优先放 `tests/unit/`。

---

## 十九、自带插件

| 名称 | 目录 | 指令 / 行为 |
|---|---|---|
| AstrBot 示例 | `plugins/astrbot_demo/` | `/astrbot`（别名 `/ab`）、`/ab图`、`/ab管理` |
| 骰子 | `plugins/astrbot_plugin_dice/` | `/骰子`、`/骰子 3d6` |
| 每日签到 | `plugins/astrbot_plugin_daily_checkin/` | `/签到`、`/积分`、`/查询`、`/抽奖` |
| Ollama 本地 AI | `plugins/astrbot_plugin_ollama/` | 接管普通消息（默认让给内置 AI） |
| 说明 | `plugins/README.md` | 插件格式与目录约定 |

---

## 二十、常用 grep

```bash
# 找某个函数的定义
grep -rn "def send_text" core/ web/

# 找某个配置项被谁读
grep -rn "reply.max_history" core/ web/

# 找所有 API 路由
grep -n "@app.route" web/server.py

# 找所有配置字段
grep -n "^    \"" core/config_schema.py | head -50

# 找所有插件钩子
grep -rn "on_llm_request\|on_llm_response\|after_message_sent" core/ plugins/

# 找所有统计键
grep -n "record(\"" core/ | head -50

# 找所有发送出口调用
grep -rn "runtime.send_text\|runtime.send_image\|runtime.send_file" core/
