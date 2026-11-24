# 发布启动器到 PyPI（`qqbot-cli`）

> 这份讲的是**启动器**（`qqbot_cli/`，PyPI 包名 `qqbot-cli`）怎么发到 PyPI。
> 主程序自己的发布（打 tag、建 GitHub Release）见 `AGENTS.md` 的「最近变更」里历次记录。

## 一次配好，以后自动

发布由 `.github/workflows/publish-cli.yml` 完成，走 PyPI 的
**Trusted Publishing（OIDC）** —— **仓库里不存任何 token**。
GitHub 在运行时给 job 签一张短期身份票据，PyPI 验过就发一次性上传令牌。

### 你需要做的一次性配置

**① 在 PyPI 注册账号**（如果还没有）

<https://pypi.org/account/register/>

> 建议先在 <https://test.pypi.org/account/register/> 练一遍，站点完全独立。

**② 配置 Trusted Publisher**

PyPI → 右上角头像 → **Account settings** → 左侧 **Publishing**
→ **Add a new pending publisher** → 选 **GitHub**，按下表填：

| 字段 | 填什么 |
|---|---|
| PyPI Project Name | `qqbot-cli` |
| Owner | `L-web666` |
| Repository name | `QQBotMerged` |
| Workflow name | `publish-cli.yml` |
| Environment name | `pypi` |

> **这五项必须和工作流里写的完全一致**，错一个字符 OIDC 校验就会失败。
>
> 「pending publisher」的意思是：项目还不存在也能先配好，
> **第一次发布成功时会自动创建项目**并把这条配置转正。
> 但在此之前**项目名没有被占住**——如果别人抢先注册了 `qqbot-cli`，
> 这条 pending 配置会失效，所以配好之后尽快发一次。

**③（可选但推荐）给环境加保护**

GitHub 仓库 → **Settings** → **Environments** → 新建 `pypi`
→ 勾上 **Required reviewers**（发布前要你点一下同意）。
这样即使有人改了 workflow，也不能偷偷往 PyPI 推包。

### 发布

**方式一：手动触发（推荐，第一次用这个）**

GitHub 仓库 → **Actions** → 左侧 **Publish qqbot-cli to PyPI** → **Run workflow**

- 勾上 `dry_run` 就只构建、不发布，用来先验证打包正常
- 不勾就会真的传到 PyPI

**方式二：推 tag 自动发**

```bash
git tag cli-v1.0.1
git push origin cli-v1.0.1
```

> `cli-v*` 前缀是刻意和主程序的 `v*`（如 `v1.5.0`）区分开的，
> 两套版本号互不影响。

## 发新版本要改哪儿

PyPI **不允许覆盖已发布的版本**。每次发新版本，改 `pyproject.toml` 里的这一行：

```toml
version = "1.0.0"      # ← 改成新的
```

然后重新触发工作流。建议启动器版本号跟主程序解耦，只在自己有改动时才升：

| 情况 | 怎么升 |
|---|---|
| 修启动器 bug | 修订号 `1.0.0 → 1.0.1` |
| 启动器加功能 | 次版本号 `1.0.1 → 1.1.0` |
| 启动器改为不兼容 | 主版本号 `1.1.0 → 2.0.0` |

## 工作流做了什么

1. **构建**：`python -m build` 生成 sdist + wheel
2. **检查元数据**：`twine check`，README 渲染有问题会在这里失败
3. **校验包内容**：用脚本打开 wheel，确认里面**只有 `qqbot_cli/`**——
   主程序的 `core/`、`web/`、`plugins/` 都不该进 pip 包（它们由启动器运行时从
   GitHub Release 拉取）。这一条是防呆：万一 `pyproject.toml` 的
   `[tool.setuptools] packages` 被改错，这里会直接失败而不是悄悄发出一个几十 MB 的包
4. **发布**：`pypa/gh-action-pypi-publish` 用 OIDC 上传

发完之后在 <https://pypi.org/project/qqbot-cli/> 能看到，
用户就可以 `pipx install qqbot-cli` 了（PyPI 索引刷新一般要 1～2 分钟）。

## 常见失败原因

| 现象 | 原因 |
|---|---|
| `OIDC token exchange failed` / `Trusted publisher` 相关报错 | PyPI 上那五项填得和工作流不一致（注意大小写、`publish-cli.yml` 要带后缀） |
| `Invalid or non-existent authentication information` | 没用 Trusted Publishing，或 workflow 里缺 `permissions: id-token: write` |
| `File already exists` | 版本号没升，PyPI 不允许覆盖 |
| `twine check` 失败 | 描述文件（`PACKAGING.md`）里的相对链接、图片路径有问题 |
| 包体积异常大 | `[tool.setuptools] packages` 被改过，把主程序打进去了 |
| `id-token: write` 报权限不足 | 该权限必须写在 **job 级**，不能只写在 workflow 级 |

## 本地手动发（备选）

不想走 Actions 的话，本地也能发：

```bash
pip install build twine
python -m build
python -m twine check dist/*
python -m twine upload dist/*        # 会问 token，用户名填 __token__
```

API token 在 PyPI → Account settings → **API tokens** 生成，只显示一次。
**不要把它写进仓库**。

## 可信发布（PiPY attestations）

用 Trusted Publishing 发布时，PyPI 会自动生成**来源证明**
（证明这个包确实由这个仓库的这次 commit 构建），
在包页面的「Provenance」处可以看到。这是 Trusted Publishing 顺带的好处，
不需要额外配置。
