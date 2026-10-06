"""
骰子插件 - 群聊轻互动
======================
指令：
  /骰子           掷一个 1-6 的骰子
  /骰子 3d6       掷 3 个 6 面骰（支持 1d100 这种写法，最多 10 个、单颗最多 1000 面）
"""

import random

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_dice"
_FACES = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}
MAX_COUNT = 10
MAX_SIDES = 1000


def _parse_spec(spec: str):
    """解析 `3d6` / `2d20` / 空 → (个数, 面数)；不合法返回 None。"""
    text = str(spec or "").strip().lower().replace("D", "d").replace("Ｘ", "x")
    if not text:
        return 1, 6
    if "d" not in text:
        if not text.isdigit():
            return None
        return 1, int(text)                       # /骰子 20 → 一个 20 面骰
    left, _, right = text.partition("d")
    count = int(left) if left.strip().isdigit() else 1
    sides = int(right) if right.strip().isdigit() else 6
    if not (1 <= count <= MAX_COUNT) or not (2 <= sides <= MAX_SIDES):
        return None
    return count, sides


class DicePlugin(Star):
    def __init__(self, context: Context, config=None):
        """官方签名：`(self, context, config)`（本插件没有配置项，config 可为 None）。"""
        super().__init__(context)
        self.config = config or {}

    @filter.command("骰子")
    async def roll_dice(self, event: AstrMessageEvent, spec: str = ""):
        """掷骰子：/骰子 或 /骰子 3d6"""
        nick = event.get_sender_name() or "你"
        parsed = _parse_spec(spec)
        if parsed is None:
            yield event.plain_result(
                f"🎲 写法：/骰子 或 /骰子 3d6（1~{MAX_COUNT} 颗，每颗 2~{MAX_SIDES} 面）")
            return
        count, sides = parsed
        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls)
        if count == 1 and sides == 6:
            yield event.plain_result(f"🎲 {nick} 掷出了：{_FACES.get(total, str(total))}  （{total} 点）")
            return
        detail = " + ".join(str(item) for item in rolls)
        yield event.plain_result(
            f"🎲 {nick} 掷了 {count}d{sides}：{detail} = {total}")

    async def terminate(self):
        """插件卸载/停用时调用，可选实现"""
        pass
