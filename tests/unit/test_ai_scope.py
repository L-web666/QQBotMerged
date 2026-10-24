# -*- coding: utf-8 -*-
"""单元测试：「内置 AI 可用性」必须按**有效配置**判断，不能只看全局

背景（用户报的问题）：AI 明明配好了，刷新网页却提示「内置 AI 未配置完整」。
原因是 `ai` 是**按机器人**的配置段（`config_schema.PER_BOT_SECTIONS`），设置页里也写着
"以下设置只作用于当前选中的机器人"，用户填的密钥通常落在 `bots[].overrides.ai`，
而全局 `ai.api_key` 是空的。判断可用性的地方如果只读全局客户端，就会误报。

受影响的三处（本文件逐个覆盖）：
  ① `Runtime.ai_status()` —— 网页启动提示 / 运行状态页 `/api/admin/status` 与 `/health`；
  ② `_ProviderShim.usable` —— 插件（本地 Ollama）据此决定要不要"让给内置 AI"；
  ③ `AstrBotHost.current_bot_id` —— 宿主把"正在处理哪个机器人"给插件用。

分层约定见 `PROGRESS.md`：`tests/unit/` 快、只依赖被测模块；不连网、不碰真实 `data/`。

运行：`python tests/unit/test_ai_scope.py`
"""

import logging
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(BASE))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from core.astrbot_shim.host import AstrBotHost                 # noqa: E402
from core.astrbot_shim.provider import _ProviderShim           # noqa: E402
from core.runtime_pkg.config import RuntimeConfigMixin         # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [OK] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


class FakeClient:
    """AI 客户端桩：只按构造时给的值汇报状态。"""

    def __init__(self, usable=False, model="", enabled=True, last_error=""):
        self.usable = usable
        self.model = model
        self.enabled = enabled
        self.last_error = last_error

    def status(self):
        return {"enabled": self.enabled, "usable": self.usable, "model": self.model,
                "has_key": self.usable, "last_error": self.last_error}


class FakeBot:
    def __init__(self, bot_id, enabled=True):
        self.id = bot_id
        self.name = bot_id
        self.enabled = enabled


class FakeRuntime(RuntimeConfigMixin):
    """只挂 ai_status() 需要的那几个属性。"""

    def __init__(self, global_client, bot_clients, bots):
        self.log = logging.getLogger("test-ai-scope")
        self.ai_client = global_client
        self._bot_clients = bot_clients
        self.bots = bots
        self.config = None

    def bot_ai_client(self, bot_id):
        if bot_id not in self._bot_clients:
            raise KeyError(bot_id)
        return self._bot_clients[bot_id]


def main():
    print("=" * 70)
    print("  单元测试：内置 AI 可用性按有效配置判断（不是只看全局）")
    print("=" * 70)

    # ---------- 1. 用户的实际场景 ----------
    print("\n[1] 全局没填密钥、每个机器人各自填好了 → 不该报「未配置完整」")
    runtime = FakeRuntime(
        global_client=FakeClient(usable=False, enabled=True),
        bot_clients={"bot1": FakeClient(usable=True, model="deepseek-chat"),
                     "bot2": FakeClient(usable=True, model="qwen-plus")},
        bots={"bot1": FakeBot("bot1"), "bot2": FakeBot("bot2")},
    )
    status = runtime.ai_status()
    check("整体判定为可用", status.get("usable") is True, str(status))
    check("标出了是按机器人配的", status.get("scope") == "bot", str(status.get("scope")))
    check("带上了是哪个机器人", status.get("bot_id") == "bot1", str(status.get("bot_id")))
    check("模型名来自该机器人的配置", status.get("model") == "deepseek-chat", str(status))

    # ---------- 1b. 指定机器人：状态页切机器人时用的是这个分支 ----------
    print("\n[1b] ai_status(bot_id) 报的是「这个机器人自己的」AI（切机器人要变）")
    status_bot2 = runtime.ai_status("bot2")
    check("bot2 的模型和 bot1 不一样（切机器人能看出区别）",
          status_bot2.get("model") == "qwen-plus", str(status_bot2))
    check("指名的机器人可用", status_bot2.get("usable") is True, str(status_bot2))
    check("scope=bot 且带 bot_id/bot_name",
          status_bot2.get("scope") == "bot" and status_bot2.get("bot_id") == "bot2"
          and status_bot2.get("bot_name") == "bot2", str(status_bot2))
    # 同一份运行时，换一个机器人必须换一个结果（这就是用户报的"切了不变"）
    check("不同机器人 → 不同结果",
          runtime.ai_status("bot1").get("model") != runtime.ai_status("bot2").get("model"),
          f"{runtime.ai_status('bot1').get('model')} vs {runtime.ai_status('bot2').get('model')}")
    check("取不到客户端时回退全局、不抛异常",
          bool(runtime.ai_status("bot_not_exist").get("scope") == "bot"), "")

    # ---------- 2. 全局配好了就按全局算 ----------
    print("\n[2] 全局配全了 → 直接用全局（scope=global）")
    runtime2 = FakeRuntime(
        global_client=FakeClient(usable=True, model="gpt-4o-mini"),
        bot_clients={},
        bots={"bot1": FakeBot("bot1")},
    )
    status = runtime2.ai_status()
    check("可用", status.get("usable") is True, str(status))
    check("scope=global", status.get("scope") == "global", str(status.get("scope")))

    # ---------- 3. 谁都没配全 → 才该提示 ----------
    print("\n[3] 全局和机器人都没配全 → 提示是应该的")
    runtime3 = FakeRuntime(
        global_client=FakeClient(usable=False, enabled=True),
        bot_clients={"bot1": FakeClient(usable=False)},
        bots={"bot1": FakeBot("bot1")},
    )
    status = runtime3.ai_status()
    check("不可用", status.get("usable") is False, str(status))
    check("scope=none（前端据此给提示）", status.get("scope") == "none", str(status.get("scope")))
    check("保留全局的 enabled 字段（前端要区分「关掉了」和「没配全」）",
          status.get("enabled") is True, str(status))

    # ---------- 4. 停用的机器人不算数 ----------
    print("\n[4] 只有停用的机器人配了 AI → 仍然算不可用")
    runtime4 = FakeRuntime(
        global_client=FakeClient(usable=False),
        bot_clients={"bot1": FakeClient(usable=True, model="deepseek-chat")},
        bots={"bot1": FakeBot("bot1", enabled=False)},
    )
    check("停用的机器人不参与判定",
          runtime4.ai_status().get("usable") is False, str(runtime4.ai_status()))

    # ---------- 5. 单个机器人解析失败不能把整体拖崩 ----------
    print("\n[5] 某个机器人取客户端抛异常 → 跳过它，继续看别人")
    runtime5 = FakeRuntime(
        global_client=FakeClient(usable=False),
        bot_clients={"bot2": FakeClient(usable=True, model="qwen-plus")},
        bots={"bot1": FakeBot("bot1"), "bot2": FakeBot("bot2")},
    )
    status = runtime5.ai_status()
    check("异常被吞掉且仍然找到可用的机器人",
          status.get("usable") is True and status.get("bot_id") == "bot2", str(status))

    # ---------- 6. 插件侧 provider：按当前机器人取 AI ----------
    print("\n[6] 插件拿到的 provider 要按「当前机器人」解析")
    runtime6 = FakeRuntime(
        global_client=FakeClient(usable=False),
        bot_clients={"bot1": FakeClient(usable=True, model="deepseek-chat")},
        bots={"bot1": FakeBot("bot1")},
    )
    provider = _ProviderShim(runtime6, {"model": "deepseek-chat"}, bot_id="bot1")
    check("有机器人上下文时：provider.usable=True（插件会让给内置 AI）",
          provider.usable is True, str(provider.usable))
    check("provider 里拿到的是该机器人的客户端",
          provider._client().model == "deepseek-chat", str(provider._client().model))

    provider_global = _ProviderShim(runtime6, {}, bot_id="")
    check("没有机器人上下文时：回退全局（老行为不变）",
          provider_global.usable is False, str(provider_global.usable))

    provider_unknown = _ProviderShim(runtime6, {}, bot_id="bot_not_exist")
    check("机器人不存在时：回退全局，不抛异常",
          provider_unknown.usable is False, str(provider_unknown.usable))

    # ---------- 7. 宿主把当前机器人放好（线程内有效） ----------
    print("\n[7] 宿主 dispatch 里记录「当前机器人」，且不跨线程串号")
    host = AstrBotHost()
    check("初始为空", host.current_bot_id == "", host.current_bot_id)
    host.dispatch({"bot_id": "bot1"}, runtime=runtime6)
    check("dispatch 之后能读到当前机器人", host.current_bot_id == "bot1", host.current_bot_id)

    seen = {}

    def worker():
        host2 = AstrBotHost()
        host2.dispatch({"bot_id": "bot_thread"}, runtime=runtime6)
        seen["thread"] = host2.current_bot_id

    import threading
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    check("另一条线程里读到的是它自己的机器人",
          seen.get("thread") == "bot_thread" and host.current_bot_id == "bot1",
          f"{seen.get('thread')} / {host.current_bot_id}")

    # ---------- 8. 宿主里真实使用的 provider 会带上 bot_id ----------
    print("\n[8] 宿主 Context 给出的 provider 带 bot_id")
    host3 = AstrBotHost()
    context = None
    try:
        from core.astrbot_shim.provider import Context
        context = Context(plugin_name="probe", runtime=runtime6, host=host3)
    except Exception as exc:  # pragma: no cover
        check("能构造插件 Context", False, repr(exc))
    if context is not None:
        host3.dispatch({"bot_id": "bot1"}, runtime=runtime6)
        provider = context.get_using_provider()
        check("Context 的 provider 认到了当前机器人",
              getattr(provider, "bot_id", "") == "bot1", str(getattr(provider, "bot_id", "")))
        check("这个 provider 是可用的", provider.usable is True, str(provider.usable))

    print("\n" + "=" * 70)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"   [FAIL] {name}  {detail}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
