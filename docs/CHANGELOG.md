# 变更记录（CHANGELOG）

> 这份文件记录**每一次改了什么、为什么改、怎么验证**。
> `AGENTS.md` 只保留最近 2 条摘要，更早的都在这里。
>
> 面向使用者的"这个版本改了什么"看 [GitHub Releases](https://github.com/L-web666/QQBotMerged/releases)；
> 这份是给维护者和 AI 看的开发记录。

## 书写规范

每条记录只写**四件事**：

1. **日期**
2. **改了什么**
3. **为什么改**（问题是什么、怎么发现的）
4. **怎么验证**（测试项数、手动验证方式）

**不要写入**：谁提出/谁反馈的需求来源、对话过程、个人身份或口径、
真实配置内容、真实数据规模（群数/成员数/消息条数等）、
实例环境的操作过程、截图来源。

## 目录结构

- **[CHANGELOG.md](CHANGELOG.md)** —— 本文件，全部变更记录
- **[CHANGELOG-archive-2026-11-15.md](PROGRESS-archive-2026-11-15.md)** ——
  2026-10-06 ~ 2026-11-07 的 47 条早期记录（只读快照）

---

- 2026-11-28：**v1.6.0 —— 新增「回复门卫」+ 修首页 500 + 修机器人消息重复入库**：
  - **① 回复门卫（新功能，`core/ai_gate.py`）**：
    群聊里开着「接收所有消息」时，机器人会对「哈哈哈」「666」「对」这类消息
    也一本正经地接话，显得很傻。门卫先用一个小模型判断「这条值不值得回」，
    再决定往不往下走。
    - **三种模式**：`off`（默认，行为不变）/ `observe`（只记录不拦截，先观察）
      / `enforce`（真的拦）
    - **两种后端**：`llm`（对话模型，不用训练数据）/ `local`（本地训好的分类器）
    - **接入点**：放在**指令/关键词/插件之后、限速和 AI 之前** ——
      前面的处理不受影响，被拦时能省下一次 AI 调用
    - **@ 了机器人就不判断**：用户已经点名了，再问是多余的
    - **私聊不设门卫**：一对一聊天，对方说话就该回应
    - **判断失败默认「当该回」**：模型挂了机器人还能用，只是多回几句 ——
      比「模型一挂机器人就哑了」好得多
    - **网页统计**：状态页新增「回复门卫」一行，显示
      「只记录不拦截 · 37 次：12 该回 / 25 不回」；出错时单独提示
  - **② 首页 500（线上故障修复）**：`url_for('static', ...)` 报
    `BuildError: Could not build url for endpoint 'static'`，**整页打不开**。
    根因是 v1.4.x 做静态资源白名单时把 Flask 默认的 `static` 端点关掉了
    （`Flask(static_folder=None)`），自定义路由的端点名成了 `static_file`，
    而模板里 9 处仍写着 `url_for('static', ...)`。
    修法：给自定义路由加 `endpoint="static"`，**白名单逻辑一行没动**。
    **这个故障有价值的地方在于**：`test_offline` 不 GET `/`、
    `check_frontend` 只看 JS 引用的 id、`test_static_deps` 只验白名单函数 ——
    **页面根本打不开，所有测试却是绿的**。已补 [D] 节 7 项：
    `url_for('static')` 能解析、首页 200、9 个静态资源逐个 GET、
    白名单没被削弱。
  - **③ 机器人自己的消息被当成用户消息入库（bug A）**：
    平台会把机器人发出的消息也作为群消息事件推回来（`author.bot=true`），
    以前一律标 `direction="in"` 入库，于是**同一条回复在库里存两份**
    （一份 out、一份 in）。后果：聊天记录里机器人自己的话显示成"用户说的"、
    "收到消息数"虚高。
    修法：`gateway` 读 `author.bot` → `messaging._handle_incoming()`
    **在落库之前**拦掉 → `message_processor` 再拦一道。
    **注意拦截必须放在落库之前**：只放 `process()` 里是没用的，
    那时消息已经写进库了。
  - **④ 配置补全日志按分组归类**：以前只列前 10 个键，加一组配置就把后面的挤出去了。
    现在每个分组最多列 3 个键，一眼能看出补了哪几组。
  - **⑤ 训练工具（新增 `tools/`）**：
    `anonymize.py`（脱敏器：昵称→`USER_n`、路径→`<PATH>`、数字→`<NUM>`，
    先从整批数据学出人名再替换正文）+ `train_gate.py`
    （bge 冻结 + 逻辑回归，交叉验证 + 留出集评估）。
    默认基座 `bge-base-zh-v1.5`（768 维）。
  - **怎么验证**：`test_offline` 576 → **584 项**（新增 [47] 节 7 项）、
    `test_static_deps` 122 → **129 项**（新增 [D] 节）、
    新增 `test_ai_gate` **69 项**。全套通过。
    README 的统计数字同步更新（18 组/118 项 → **19 组/128 项**）。

- 2026-11-26：**文档全面审查：修掉 5 处漂移、重复和自相矛盾**：
  - **背景**：文档已经散到 10 个文件、3500 多行，做一次系统性核对。
    核对方式是逐项实测（跑测试取真实项数、按 `FIELDS` 的扁平键口径重算配置项、
    用 6 行滑窗比对跨文档重复段落），不靠肉眼。
  - **① 测试项数漂移（3 处）**：README 写 `test_offline` **575** 项，
    实际 **576**；`test_plugin_safety` 写 26、实际 **28**；
    `test_instance_lock` 写 12、实际 **10**。
    **根因**：`test_readme_toc` 会校验分组数/配置项数/接口数/页面数，
    唯独漏了测试项数，所以写错永远不报警。
    改法：数字全部改正，并在 `test_readme_toc` 里加 **[9] 节**——
    现跑 5 个单元测试（每个几百毫秒）取真实项数比对；
    `test_offline` 跑得慢，改为与 AGENTS.md 交叉比对。
    测试从 21 项扩到 **27 项**。
  - **② README §7 与 `plugins/README.md` 重复了示例代码**：
    同一份「最小插件写法」在两个文件里各有一份，改一处忘另一处就会打架。
    改法：`plugins/README.md` 作为插件文档的唯一权威来源，
    README §7 只保留路径表 + 链接 + 一句安全提醒。
  - **③ CHANGELOG 里 5 处「上一轮」**：这是没有信息量的时间参照
    （违反了本文件自己定的书写规范）。
    改成具体版本：「v1.4.4 那次 `COM¹` 的漏网」「v1.0.1 为了让受限环境记住安装目录」等。
  - **④ `AI_INDEX.md` 的测试章节严重过时**：只列了 4 个测试文件，实际有 30 个，
    而且 `test_cloud_secrets.py` 被列了**两次**（一处还漏了「不连网」的说明）。
    改法：这份不再维护完整清单，改为指向 `AGENTS.md` 的测试表（唯一来源），
    自己只保留入口和共用辅助函数。
  - **⑤ AGENTS.md 的「不要读超过 300 行」与自身冲突**：它自己 370+ 行。
    改法：阈值提到 500 行，并**明确写出本文件是唯一例外**（它就是要整篇读的），
    同时说明为什么它会慢速增长（历史已经拆到 CHANGELOG）。
  - **核实过没问题的**：README 的「18 个分组、118 个配置项」是对的
    （要用 `FIELDS` 的扁平键口径数，`DEFAULT_CONFIG` 的 dict 段只有 17 段、
    但 `bots` 是 `FIELDS` 里单独一组）；
    11 个页面、57 个接口；`PACKAGING.md` 面向用户 / `PUBLISHING.md` 面向维护者，职责不冲突；
    CHANGELOG 15 条都写清了「为什么改 + 怎么验证」。
  - **怎么验证**：`test_readme_toc` 27 项全过（新增的 6 项就是这次加的校验）；
    复审脚本确认 5 处修复全部生效（README/AGENTS 的 576 一致、README 里已无示例代码、
    「上一轮」剩 0 处、`test_cloud_secrets` 不再重复出现、「300 行」已改为 500 行 + 例外说明）。

- 2026-11-25：**启动器说明随包分发（`qqbot_cli/PACKAGING.md`）**：
  - **为什么**：`pipx install qqbot-cli` 装完后，`PACKAGING.md` 只在仓库根，
    不在 site-packages 里。用户想查"有哪些命令、怎么卸载、装到哪了"，
    要么回 GitHub，要么只能看 `qqbot --help` 里那几行。
  - **怎么做的**：把 `PACKAGING.md` **复制**一份到 `qqbot_cli/`。
    是复制不是移动 —— `pyproject.toml` 的 `readme = "PACKAGING.md"` 指的是仓库根那份
    （PyPI 页面的长描述靠它），`MANIFEST.in` 也 include 了它（sdist 要带上），
    这两处不能动。
  - **配套改动**：
    ① `[tool.setuptools.package-data]` 加上 `PACKAGING.md` ——
    不声明的话 setuptools **不会**把这个 `.md` 打进 wheel（实测确认过）；
    ② `qqbot --help` 的 epilog 末尾打出说明文件的实际路径，
    用户在终端就知道去哪儿读；
    ③ 测试里加了 [J] 节校验**两份文件内容完全一致** ——
    改了根那份忘了同步包里的，用户装完看到的还是旧说明。
  - **没影响的**：`.md` 不是 `.py`，不会被当成模块 import；
    wheel 顶层目录仍是 `qqbot_cli` + dist-info，发布工作流里那条
    「只打包启动器」的断言照常通过。
  - **怎么验证**：真实 `python -m build` 后开 wheel 确认
    `qqbot_cli/PACKAGING.md` 在里面、顶层目录没多；
    装进干净 venv 后文件落在 `site-packages/qqbot_cli/PACKAGING.md`，
    `qqbot --help` 末行打出了这个路径。
    `test_cli_hardening` 81 → **87 项**全部通过。

- 2026-11-25：**补上缺失的 PyYAML 依赖（CI 的干净环境发现的）**：
  - **问题**：项目一直用 `yaml` 解析插件的 `metadata.yaml`
    （`core/astrbot_shim/compat_utils.py` 的 `_load_yaml`），
    但 `requirements.txt` 里**从来没有 PyYAML**。
    开发机碰巧装着（可能是别的包带进来的），所以本地一直能跑。
    没有它时会退回一个极简的"只认 `key: value`"兜底解析 ——
    遇到 YAML 多行块（`description: |`）就把值读成字面量 `|`，
    插件的标题/描述/平台列表全都不对。
  - **怎么发现的**：发布工作流新加的 `test` job 在干净的 ubuntu runner 上
    跑 `test_offline.py`，报 1 项失败：
    `metadata.yaml 的 display_name / astrbot_version / 支持平台都能读到`。
    本地用干净 venv 复现同样的问题。
  - **改了什么**：`requirements.txt` 加 `PyYAML>=6.0`；
    启动器的 `paths.PROGRAM_REQUIRES` 同步加上（它装依赖时要用）；
    `runner.verify_dependencies()` 也检查它 ——
    缺了不会报错、只会静默解析错，所以必须显式查。
  - **怎么验证**：干净 venv 只装 `requirements.txt` 时复现失败；
    补上 PyYAML 后 `test_offline.py` **576 项全过**。
    这条也写进了 `AGENTS.md` 的「提交前必须做的验证 → 干净环境验证」。

- 2026-11-25：**按审计报告修完启动器的 9 项安全问题（qqbot-cli 1.0.1 → 1.0.2）**：
  - **报告来源**：第三方审查（基线 `ddf6763` → HEAD `98bb5f6`），9 项里
    2 项高危、5 项中危、2 项低危、1 项条件性严重。**报告的证据全部核实成立**。
  - **① CLI-ARCHIVE-001（高危）解压可越出安装目录**：
    `_safe_member()` **只检查整个路径开头是不是盘符**（`^[A-Za-z]:`），
    所以 `a/D:outside.txt` 能通过；而 Windows 的
    `ntpath.join('C:\\install', 'a', 'D:outside.txt')` **直接得到 `D:outside.txt`**
    —— 路径被重置到别的盘去了。这与 v1.4.4 那次 `COM¹` 的漏网是同一类错误（见本文「静态路由白名单补齐 Windows 上标数字设备名」）：
    **开头匹配挡不住中间组件**。
    改法：新增 `_bad_component()` **逐组件**校验（盘符 / 冒号ADS / 设备名含上标 /
    控制字符 / 尾随点空格 / `..`），并在 TAR 与 ZIP 落盘前再做一次
    `_within()` 真实路径包含性检查 + `_has_symlink_parent()` 拒绝链接父目录。
  - **② CLI-STATE-001（高危）不可信工作目录状态文件**：
    v1.0.1 为了让受限环境也能记住安装目录，加了 `cwd/.qqbot-cli.json` 兜底 ——
    **这正是漏洞**：攻击者往一个目录放伪造状态文件，用户从那儿跑 `qqbot`
    就会执行攻击者指定的程序。
    改法：**删掉 cwd 候选**；状态文件改为写到安装目录内部（肯定可写）；
    写入用原子替换 + 拒绝符号链接 + POSIX 0600；
    `is_installed()` 现在要求 **marker 里的 `install_dir` 与实际路径一致**
    （以前只看"文件存不存在"，抄一份 marker 就能冒充）；
    `launch()` 执行前再确认解释器和 `run.py` 都在安装目录内。
  - **③ CLI-REDIRECT-001（中危）重定向未复核**：`requests.get()` 默认自动跟随重定向，
    白名单只校验了第一个 URL。改成 `allow_redirects=False` + 逐跳 `_check_host()`，
    并且**只允许 HTTPS**、拒绝内网/回环/链路本地地址（`169.254.169.254` 这类云元数据端点）。
  - **④ CLI-EXPORT-001（中危）导出跟随符号链接**：`bundle.write()` 会读链接目标内容，
    备份里可能混进安装目录外的任意文件。现在用 `islink` 拒绝链接文件、链接目录不遍历、
    真实路径必须在安装目录内，跳过的项列给用户看。
  - **⑤ CLI-CONFIG-001（中危）config.json 权限**：以前按 umask 建成 0644，
    共享主机上别人能读到 AppSecret/API Key/令牌。POSIX 下改用
    `os.open(..., 0o600)` 一步创建（不留中间窗口），并补 `chmod` 修正已存在的文件。
  - **⑥ DEP-REQUESTS-001（中危）requests 下限**：4 处清单从 `>=2.28.0` 提到
    **`>=2.32.4`**（CVE-2024-47081 的修复版本）。**这一条在 v1.4.4 的报告里就提过，当时只改了 README 的说法、清单没跟上。**
  - **⑦ CLI-UNINSTALL-001（低危）卸载识别过宽**：以前「`run.py` / marker / `config.json`
    有一个就算」，`run.py` 这么常见的名字很容易误删别人的项目。现在要求
    **marker 存在 + 里面记录的路径与目标一致 + `run.py` 正常存在**，
    三条同时满足；错误提示会写出两个路径便于排查。
  - **⑧ CI-PUBLISH-001（低危）工作流加固**：7 个 `uses:` 全部固定到 **40 位 commit SHA**
    （可移动的 `@v4` 被替换时不会悄悄换成别的代码）；默认权限只给 `contents: read`，
    发布 job 才加 `id-token: write`；`persist-credentials: false`；
    **新增独立的 `test` job，构建前先跑测试**（报告指出"构建成功不等于测试通过"）。
  - **⑨ DEP-PIP-001（条件性严重）pip 版本**：新建 venv 用的是 `ensurepip` 自带的 pip，
    低于 26.2.2 的 pip 内置证书库仍信任已出事的证书。
    **刻意只提示、不自动升级** —— 升级本身要联网装包，在信任链有问题时
    "自动去装一个包"反而扩大风险面。安装时会打印 pip 版本并写进 marker
    （同时记录 Python 版本与依赖版本，方便审计）。
  - **顺手修的**：装好后向导被取消的状态下再跑 `qqbot`，现在走
    `_finish_config_only()` **只补配置不重新下载**；`launch()` 遇到跑不起来的解释器
    会给出友好错误（以前抛裸 `OSError`）。
  - **怎么验证**：新增 `tests/unit/test_cli_hardening.py`（**81 项**）逐条覆盖上述 9 项，
    含真实 tar/zip 包验证危险成员不落盘、伪造 marker/状态文件的三种情形、
    内网地址拒绝、SHA 固定检查等。`test_launcher.py` 60 → **67 项**
    （[E3] 断言改为"传 install_dir 能读回来"，因为受限环境下状态落在安装目录内部）。
    全套：`test_offline` 576、`test_uninstall` 43、`test_cli_hardening` 81、
    `test_launcher` 67，全部通过。
- 2026-11-25：**`qqbot-cli` 已发到 PyPI，并修掉实测发现的一个 bug（1.0.0 → 1.0.1）**：
  - **发布结果**：`pypi.org/project/qqbot-cli/1.0.0` 已上线（GitHub Actions 两次
    `workflow_dispatch` 都 success）。从 PyPI 真装一遍验证过：
    `pip install qqbot-cli` → `qqbot --version` 正常；
    用这个从 PyPI 装的启动器完整跑通「下载 v1.5.0 → 建 venv → 装依赖」。
  - **实测发现的 bug**：**向导被取消时不写 marker**。`do_install()` 的
    `except (EOFError, KeyboardInterrupt)` 分支直接 `return 0`，跳过了后面的
    `write_marker()`。后果是程序文件明明装好了，`is_installed()` 却判为没装 ——
    下次运行 `qqbot` 会**重新下载一遍并重装依赖**（白费几分钟）。
    在管道/CI 里跑更容易撞上（输入被关掉就是 EOF）。
  - **改了什么**：取消分支现在也写 marker，但带上 `"needs_config": True`；
    `main()` 的 auto 分支看到这个标记且没有 `config.json` 时，
    走「补配置」而不是 `do_run` —— 既不重复下载，也不会启动一个空配置的程序。
    提示语改成「运行 `qqbot init` 补完配置即可（不会重新下载）」。
  - **怎么验证**：`test_launcher.py` 新增 [E4] 6 项（取消前判未安装 / 取消后仍判已安装 /
    marker 带 needs_config / 缺 config 时要求补配置 / 有 config 后不再要求 /
    正常安装不带该标记），总项数 **60 → 66**。另外真机端到端验证过整个流程。
- 2026-11-25：**加好 PyPI 自动发布（GitHub Actions + Trusted Publishing），本地实测打包通过**：
  - **要解决什么**：启动器要能被 `pipx install qqbot-cli` 装上，就得发到 PyPI。
    手打 token 上传既要保管密钥、又容易漏。改用 PyPI 的 **Trusted Publishing（OIDC）**：
    GitHub 运行时给 job 签一张短期身份票据，PyPI 验过发一次性上传令牌 ——
    **仓库里不存任何 token**。
  - **新增文件**：`.github/workflows/publish-cli.yml`（构建 → `twine check` → 校验包内容 →
    发布）、`docs/PUBLISHING.md`（配 PyPI 的步骤与常见失败原因）、`MANIFEST.in`。
  - **触发方式**：Actions 页面手动 `Run workflow`（带 `dry_run` 开关可只构建不发），
    或推 `cli-v*` tag。**刻意与主程序的 `v*` 前缀区分开**，两套版本号互不影响。
  - **踩到的坑（都已修正）**：
    ① `[tool.setuptools.sdist]` **不是合法配置**（schema 里没有这个键），
    想限制 sdist 内容只能用 `MANIFEST.in`；
    ② 不限制的话 sdist 会把 `tests/` 打进去 —— 里面有 4800 行的 `test_offline.py`，
    且那些测试依赖主程序的 `core/`，在别人机器上跑不起来。加 `MANIFEST.in` 后
    **sdist 从 125.9 KB 降到 53.8 KB**；
    ③ `license = { text = "AGPL-3.0" }` 这种写法**已弃用**（官方公告 2027-02 后构建直接失败），
    改成 SPDX 表达式 `license = "AGPL-3.0-or-later"` + `license-files`，
    同时把 `setuptools>=61` 提到 `>=77`；
    ④ **仓库里的 `build/` 目录会遮蔽 pip 的 `build` 包** —— 在仓库根跑
    `python -m build` 会报 `No module named build.__main__`。本地构建要从仓库**外面**
    调用（`python -m build <项目路径>`）；GitHub Actions 是干净环境，不受影响。
  - **怎么验证**：本地实跑 `python -m build` 成功，`twine check` 两个产物都 `PASSED`；
    打开 wheel 确认顶层**只有 `qqbot_cli/` 与 dist-info**（主程序代码没进去）；
    把 wheel 装进干净 venv，`qqbot --version` / `qqbot --help` 正常输出。
  - **还差一步（在 PyPI 网页上，代码侧做不了）**：注册账号 → Account settings → Publishing
    → Add a new pending publisher，填 `qqbot-cli` / `L-web666` / `QQBotMerged` /
    `publish-cli.yml` / `pypi`。**这五项必须与工作流完全一致**。
    注意 pending publisher **不会预留名字**，所以配好后要尽快发一次。
- 2026-11-25：**新增 `qqbot-cli` 启动器（`qqbot` 命令），可以 `pipx install qqbot-cli` 装**：
  - **要解决什么**：本项目原来只能「克隆仓库 / 下载压缩包 → 手动装依赖 → 改 config.json」，
    对只想用的人门槛太高。参照 NoneBot 的 `nb-cli` 做法，加一个独立的启动器：
    ```bash
    pipx install qqbot-cli
    qqbot                # 首次：选目录 → 下载 → 建 venv → 装依赖 → 问几个必填项 → 启动
    ```
  - **核心取舍：启动器不打包主程序**。主程序仍然是从 GitHub Release 拉下来的
    **完整项目目录**，所以 `core/paths.py`、自动更新、插件机制全都原样工作 ——
    **主程序一行代码都不用改**（避免把 `core` 改名的 253 处 `from core` 连锁改动，
    也避免数据写进 site-packages 的问题）。
  - **新增文件**：`qqbot_cli/`（`cli.py` 入口、`fetch.py` 下载解压、`runner.py` 建环境、
    `wizard.py` 向导、`paths.py` 常量）、`pyproject.toml`、`PACKAGING.md`。
    启动器**只依赖 requests**，主程序的 flask/werkzeug 装在它自己建的 `.venv` 里。
  - **踩到的坑（都写成了测试）**：
    ① `_strip_root()` 判断 tarball 壳目录时不能只看「第一段」—— GitHub 顶层目录名是
    `L-web666-QQBotMerged-<sha>`（含 `-`），而且没有壳目录时 `core/` 会被误当成壳；
    ② `_safe_member()` 会去掉末尾的 `/`，导致目录条目认不出来、壳目录剥不掉，
    结果所有文件被埋进 `xxx-<sha>/` 子目录。两处都在 `test_launcher.py` 里留了用例；
    ③ 下载缓冲原来用 `tempfile.mkdtemp()`，在 `%TEMP%` 不可写的机器上直接失败 ——
    改为优先放在安装目录里的临时子目录。
  - **安装目录由用户自己选**：向导第一步就问（回车用 `~/QQBotMerged`），
    也支持 `--dir` 直接指定、相对路径、带引号的路径（资源管理器粘贴）；
    上级目录不存在会要求重问；目录里已有别的东西时**先提示、要求确认**再动，
    不闷头覆盖。装完会把进目录的 `cd` 命令打印出来。
  - **记住装在哪**：`qqbot` 不带参数时按「`--dir` > 上次记住的 > 默认目录」选目录。
    记录分好几处落盘（用户配置目录 / 主目录隐藏文件 / 当前工作目录），
    因为前两处在受限环境里可能不可写 —— 只记一处会导致用户的目录"下次就找不到了"。
    记住的目录被删掉时会自动回落。
  - **卸载与数据导出**：`qqbot uninstall` 先列出装了什么/占多大，再问
    「先导出 config.json + data/ 吗（默认要）」「确认删除吗（默认否）」。
    导出的 zip 里**只含配置与数据**，不含程序代码、`.venv`、`__pycache__`；
    导出目标不允许落在 `data/` 里面（边写边打包会把自己装进去）。
    `qqbot export` 可以只备份不卸载。
    **删除前有硬性安全检查**：目录里必须有 `run.py` / 安装记录 / `config.json` 之一，
    否则拒绝删；再挡一次根目录、盘符、用户主目录，防止手滑把整个盘删掉。
    删不掉的文件会被列出来（Windows 上文件被占用很常见）。
    卸载成功后清掉「记住装在哪」的记录。
  - **跨平台（Windows / Linux / macOS）**：平台判断集中在 `paths._platform()` 一处
    （可用环境变量 `QQBOT_CLI_FORCE_PLATFORM` 覆盖，便于测试），
    因此 `cd /d` vs `cd`、`Scripts/python.exe` vs `bin/python`、
    `activate.bat` vs `source` 都能自动切换。
    **注意**：`venv_python()` 用 `os.path.join`，路径分隔符跟随**真实运行系统** ——
    在 Windows 上模拟 Linux 时它仍是反斜杠，这是对的。
  - **安全边界**：主机白名单（同主程序）、压缩包大小/成员数/解压总量上限、
    拒绝绝对路径与 `..`、跳过链接与设备文件。向导生成的 `config.json` 只写用户填的项，
    其余由主程序 schema 自动补全（已验证 `reply.max_segment_length` 等会被补上）。
  - **怎么验证**：新增 `tests/unit/test_launcher.py`（**60 项**）：主机白名单、路径穿越、
    壳目录识别（7 种结构）、真实 tar.gz 解压后壳目录被剥掉、向导产出能被
    `load_config()` 正常加载、留空也能装完、marker 判定、路径展开，
    以及 [E1] 安装目录询问（回车/绝对/相对/带引号/上级不存在重问）、
    [E2] 目录非空要确认、[E3] 记住与回落。
    新增 `tests/unit/test_uninstall.py`（**43 项**）：导出内容正确（含消息库且逐字节一致、
    不含程序代码/缓存/.venv）、导出路径安全、陌生目录拒绝删除、
    卸载的三种路径（不导出/先导出/取消）、清理记录、三种平台的命令差异与模块导入。
    另外做过一次联网端到端：下载真实 v1.4.4 → 向导 → `run.py --check` 通过。
    全套回归 `test_offline` 576 项全绿。
- 2026-11-22：**静态路由白名单补齐 Windows 上标数字设备名（审计 SEC-STATIC-001，中危）**：
  - **问题**：`_WINDOWS_DEVICE_NAMES` 只含 ASCII 写法，而 Windows 还认
    **上标数字**形式：`COM¹`/`COM²`/`COM³`/`LPT¹`/`LPT²`/`LPT³`（U+00B9/B2/B3）。
    实测 `COM¹.js` 这类路径**能通过白名单**（`.js` 后缀匹配、主干不在集合里），
    随后进入 `send_from_directory()`。这是 v1.4.4 那版白名单实现的漏网。
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
  - 问题：v1.5.0 把 `security` 加进 `PER_BOT_SECTIONS` 后，`message_processor.py` 里判定管理员的
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

---

**更早的变更记录（2026-10-06 ~ 2026-11-07，共 47 条）见归档：
`docs/PROGRESS-archive-2026-11-15.md`**
