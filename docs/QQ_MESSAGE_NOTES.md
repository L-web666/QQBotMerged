# QQ 官方消息能力 —— 事实笔记（Markdown / @ / 指令标签，含来源）

改消息发送相关代码前看这份。**每条都注明来源**；官方没写的写「官方未写」，不要猜。

来源（均为 QQ 开放平台官方文档，2026-10-06 抓取）：

- [消息类型](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/overview.html) —— `msg_type` 发送表、各场景收发支持矩阵
- [Markdown 消息](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/markdown.html) —— 开放说明、支持语法、字段结构
- [文本交互](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/text-chain.html) —— **@某人 / @全部成员 / 指令标签**的嵌入格式与适用范围
- [发送群聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html) —— 字段表、示例、群聊错误码
- [发送单聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html) —— 单聊字段与错误码
- [流式发送单聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_stream_messages.post.html) —— 流式字段
- [公共错误码](https://bot.q.qq.com/wiki/develop/api-v2/openapi/error/error.html) —— 50037 / 50041 / 50055 / 50056 / 50057 等

## 一、发送 markdown 的载荷

| 字段 | 值 |
|---|---|
| `msg_type` | `2`（发送侧只有 `0`=文本/`content`、`2`=Markdown/`markdown`、`7`=富媒体/`media`；单聊另有 `6`=输入中） |
| 正文 | **顶层 `markdown` 对象里的 `markdown.content`**（不是平铺的 `content`） |
| 硬性约束 | **传了 `markdown` 后顶层 `content` 必须为空**（官方原文：「传了 markdown 后此字段必须为空」） |
| 可与谁搭配 | `keyboard`（内嵌按钮）、`msg_id`/`event_id` + `msg_seq`（被动回复）、`message_reference`（引用） |

```jsonc
POST /v2/groups/{group_openid}/messages
{ "msg_type": 2,
  "markdown": { "content": "## 标题\n\n**加粗**内容" },
  "msg_id": "ROBOT1.0_xxx", "msg_seq": 1 }
```

## 二、权限

- **原生 markdown（直接传 `markdown.content`）不需要申请**：官方 2026/04/23 说明「单聊场景、群聊场景自定义
  Markdown 消息能力已开放到所有机器人均可使用，无需单独申请 Markdown 模版；频道场景目前需要内邀开通」。
- 模板 markdown 需要申请 `custom_template_id`；但 v2 发送接口字段表中 `template_id` / `custom_template_id`
  均被标注 **【已废弃】** —— 本项目只用原生 markdown。

## 三、支持的语法（官方「支持格式」逐项列出）

`#`/`##` 标题 · `**加粗**` · `__下划线加粗__` · `_斜体_`/`*斜体*` · `***加粗斜体***` · `~~删除线~~` ·
有序/无序列表（嵌套二级前面空 **4 个空格**）· `>` 块引用 · `***` 水平分割线 ·
`[文字](url)` 与 `<url>` · `![alt #宽px #高px](url)` · 正文里直接写 `\n` 换行（需要额外空行时官方示例在空行间插入零宽字符 `\u200B`）

**官方清单里没有**（不保证渲染，可能原样显示或报错）：代码块 ```、行内代码 `` `x` ``、表格、颜色/字号，
以及 markdown 的**转义规则**（官方未给）。

## 四、图片

- 语法 `![](url)`；**URL 必须公网可访问**，开放平台会去下载并转存。
- **不需要**先走富媒体接口换 `file_info`（那是 `msg_type=7` 的另一条链路）。
- `markdown.force_verify_image_resource=true`（2026-08-10 新增）时转存失败会直接报错、消息不发；默认 false。

## 五、限制与错误码

- **长度上限：官方未给出具体数字**，只有错误码 `40054007 消息长度超限` / `40054018 消息过长或异常`。
  本项目仍按 `send.message_max_length`（默认 4000）在发送前拦一道。
- 与 markdown 相关的错误码（本项目 `core/qq_errors.py` 的 `MARKDOWN_ERROR_CODES` 据此而来）：
  `22006` 消息类型与内容不匹配 · `340069` 消息类型无效 · `40034124` markdown 消息参数错误 ·
  `40034011` 无效的 markdown 内容 · `40034008/40034009/40034010`（模板参数有空值/换行/含 markdown 语法）·
  `304036` / `40034127` 无 markdown 模板权限 · `40054007` / `40054018` 长度 · `40054010` 不允许发送 URL ·
  `50037` 带 markdown 的消息只支持 markdown 或 keyboard 组合 · `50041/50055/50056/50057`（空值/无效 content/不允许/原生与模板二选一）
- 被动回复时效（与格式无关）：单聊 60 分钟、每条最多回 4 次；群聊 **5 分钟**、每条最多回 5 次。

## 六、流式 markdown

- 存在，**仅单聊**：`POST /v2/users/{user_openid}/stream_messages`（50 QPS）；
  字段 `input_mode`(append/replace)、`input_state`(1=生成中/10=结束)、`index`、`content_type`(text/markdown)、
  `content_raw`、`stream_msg_id`、`msg_seq`，响应带 `remain_msg_len`。
- **群聊不支持**（官方群聊接口页原文：「注意: 群消息不支持流式参数」）。本项目**暂不实现**。

## 七、@ 某人 / @全部成员（文本交互）

官方原文（[文本交互](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/text-chain.html)）：
「群聊&文字子频道，支持含有文本文字的消息类型，如：文本消息、图文消息、markdown 消息。」

| 能力 | 嵌入格式 | 适用场景 | 备注 |
|---|---|---|---|
| @某人 | `<qqbot-at-user id="用户openid" />` | **群聊、文字子频道** | 客户端显示为 `@用户` 标签；旧协议 `<@userid>` **即将弃用** |
| @全部成员 | `<qqbot-at-everyone />` | **仅文字子频道** | 需要机器人拥有「发送 @全部成员 消息」的权限；群聊用不了 |
| 回车指令 | `<qqbot-cmd-enter text="…" />` | 仅 markdown | **群聊和文字子频道不支持**；`text` 必填、≤100 字符、需 urlencode |
| 参数指令 | `<qqbot-cmd-input text="…" show="…" reference="false" />` | 仅 markdown | `text`/`show` ≤100 字符、需 urlencode；`reference=true` 时带引用 |
| 跳转子频道 | `<#channel_id>` | 仅频道 | — |
| 表情 | `<emoji:id>` | 仅频道、`type=1` | `type=2` 直接写字符串 |

本项目的处理（`core/message_text.py`）：

- **发出去**：`normalize_outgoing_at()` 把旧写法 `<@openid>` / `<@!openid>` 换成新标签；
  `<@all>` / `<@!all>` / `@everyone` 变成**纯文本** `@全体成员`（群聊发真标签不生效，退化成可读文字）；
  已经是新标签的原样保留（幂等）。所有发送出口都会过这一层（网页、定时任务、插件、AI）。
- **收进来**：`mention_markup_to_text()` 把新/旧两种 @ 标记显示成 `@昵称`（`@全体成员` 同理），
  聊天记录里不会出现原始尖括号标记。
- **@ 候选名单**：`/api/chat/mentions` 返回群成员缓存 + 「最近在群里说过话的人」，
  因为官方「查询群成员」接口要求机器人是群管理员（拉不到时用最近发言者兜底）。
- **私聊**：官方不支持 @ 标签；程序**不擅自删除**用户写的标签（会当普通文字发出去），
  网页端的 @ 选择器在私聊里禁用。
- **⚠️ 实测与官方文档不一致的地方**：官方写 @某人「群聊&文字子频道，支持含有文本文字的消息类型，
  如：文本消息、图文消息、markdown 消息」，但**实测纯文本消息里标签不会被渲染**（用户真机反馈，
  2026-10-07）。因此本项目把「带 @ 的纯文本消息」**自动升级为 markdown**（见下一节的
  `markdown_only_tags()`），不依赖官方这句话。

## 八、指令标签的实际处理（`qqbot-cmd-enter` / `qqbot-cmd-input`）

官方原文（[文本交互](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/text-chain.html)
「指令操作」章节）：

- **目前仅在 markdown 支持**；
- `<qqbot-cmd-enter text="xxx" />`：客户端展示为 `/回车指令` 可点击标签，用户点击后**直接发送** `text`；
  **群聊和文字子频道不支持该能力**；`text` 必填、最长 100 字符、**传值时需要 urlencode**；
- `<qqbot-cmd-input text="xxx" show="xxx" reference="false" />`：展示为 `/参数指令`，用户点击后把 `text`
  **插入输入框**自行编辑发送；`show` 选填（默认取 `text`）、`reference` 选填（默认 false，true 时带原文引用）。

本项目的处理（`core/message_text.py`）：

- **发出去**：`build_cmd_enter()` / `build_cmd_input()` 生成官方标签，自动 urlencode（`quote(safe="")`）、
  按 100 字符截断、`show` 与 `text` 相同时不重复写；`read_cmd_tag()` / `cmd_tags()` 反向解析（值自动解码）。
  网页发送框的「⌘ 指令」面板（`chat.js` 的 `insertCmdTag()`）插入时同样编码并校验长度。
- **收进来 / 给 AI 看**：`cmd_markup_to_text()` 换成 `/回车指令 xxx`、`/参数指令 xxx`；
  `preview()`、`format_for_ai()` 与网页消息渲染（`.cmd-tag`）都会转换，不会把裸标签给人或 AI 看。
- **格式自动升级**：见下一节的 `markdown_only_tags()`。

## 九、`markdown_only_tags()`：什么时候必须改用 Markdown

正文里出现**@某人**（新标签或旧写法 `<@openid>`）或**任何指令标签**时，`Runtime.send_text()` 会把
「纯文本」请求自动升级成 markdown，理由与实测依据见第七节。行为约定：

- 只升级**这一次发送**，**不写配置**（该机器人的 `send.message_format` 不变）；
- 返回值里带 `format`（实际用的格式）与 `auto_format`（是否自动升级），`_note` 写明原因；
- `@全体成员`（`<@all>` / `@everyone` 退化成的纯文字）**不算**，仍是纯文本；
- 已经请求 markdown 时不重复提示；markdown 被平台拒绝时照旧回退纯文本（`MARKDOWN_FALLBACK_NOTE`）。

## 十、官方未写 / 未核实

- markdown 字数上限的具体数值、纯文本与 markdown 上限是否不同；
- 禁止使用的标签/图片/链接清单（只有 `40054010 不允许发送URL` 这类错误码，边界未说明）；
- `msg_type` 取 `1/3/4` 在 v2 发送接口的定义（`type/overview` 正文提过 0/2/3/7，但同页表格只有 0/2/7，官方自相矛盾；`3`=结构化卡片标注「发 ❌ / 收 ✅」）；
- 群聊里 `@某人` 标签最多能放几个、与 `keyboard` 同时使用的边界（官方未写）；
- 单聊里写 `<qqbot-at-user … />` 到底会被原样显示还是被忽略（官方只说「群聊、文字子频道可用」）；
- **@ 标签在纯文本消息里到底渲不渲染**：官方写「支持文本消息」，实测不渲染（见第七节），以实测为准；
- 指令标签的 `text` 做 urlencode 后，客户端点击发送的是**解码后的原文本**还是编码串（官方只说传值要编码，
  未写客户端行为）；
- 一条消息里放多个指令标签、指令标签与 `keyboard`/`markdown` 组合的边界（官方未写）；
- 参数指令 `reference=true` 时引用的"消息原文"指当前消息还是被回复的那条（官方未写）。

