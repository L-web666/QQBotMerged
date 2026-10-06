"""
每日签到插件 - 签到领积分、连签加成、今日签到序号、趣味抽奖
================================================================
指令：
  /签到            当天签到，显示今天第几位签到并领积分
  /积分  /查询     查询积分、护盾等
  /抽奖            消耗 10 积分抽一次趣味奖

数据目录（AstrBot 官方约定）：data/plugin_data/<插件名>/
  ├── checkin.json        签到数据（自动生成）
  └── lottery.json        抽奖奖项与概率（首次自动生成，可直接编辑后重新加载插件生效）
"""

import json
import os
import random
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_daily_checkin"

_lock = threading.Lock()

# 默认抽奖奖项模板
_DEFAULT_LOTTERY = [
    {"name": "空气奖", "icon": "🍃", "weight": 120,
     "text": "抽到了新鲜空气一份…含量 100%，环保又有益健康！", "effect": "nothing"},
    {"name": "原地转三圈", "icon": "💫", "weight": 60,
     "text": "获得「原地转三圈」体验券：现在、立刻、马上转！晕了别找我～", "effect": "nothing"},
    {"name": "夸夸卡", "icon": "🌈", "weight": 50,
     "text": "获得「群主夸夸卡」一张：可在群里点名让群主夸你一句", "effect": "nothing"},
    {"name": "摸鱼许可", "icon": "🐟", "weight": 55,
     "text": "获得「今日摸鱼许可」：有效期至下班，被抓包请出示本卡（无效版）", "effect": "nothing"},
    {"name": "表情包自由", "icon": "😂", "weight": 50,
     "text": "获得今日「表情包自由权」：快去群里斗图，把收藏夹清空！", "effect": "nothing"},
    {"name": "免打扰一小时", "icon": "🔕", "weight": 40,
     "text": "获得「免打扰一小时」特权…虽然只是嘴上说说", "effect": "nothing"},
    {"name": "主角光环", "icon": "✨", "weight": 35,
     "text": "接下来 10 分钟你自带主角光环：走路带风、泡面必香", "effect": "nothing"},
    {"name": "隐形斗篷", "icon": "🫥", "weight": 35,
     "text": "获得「隐形斗篷」一件…可惜是电子的，群主照样看得见你", "effect": "nothing"},
    {"name": "好运倒计时", "icon": "⏳", "weight": 30,
     "text": "你的好运正在路上：预计 3 个群消息内送达", "effect": "nothing"},
    {"name": "神秘锦囊", "icon": "🎐", "weight": 30,
     "text": "获得「神秘锦囊」：里面装着一句话——'多喝水，少熬夜，明天会更好'", "effect": "nothing"},
    {"name": "一键暴富券", "icon": "💸", "weight": 15,
     "text": "获得「一键暴富券」…使用方式：把它放进钱包，每天看一眼", "effect": "nothing"},
    {"name": "鸽子精附体", "icon": "🕊️", "weight": 40,
     "text": "你被「鸽子精」附体了：今天说'马上到'时，请自行-1小时", "effect": "nothing"},
    {"name": "真香警告", "icon": "🍜", "weight": 40,
     "text": "触发「真香定律」：今天若说'我不吃/我不玩'，结局大概率是真香", "effect": "nothing"},
    {"name": "网抑云时刻", "icon": "🌧️", "weight": 30,
     "text": "今晚 22:00 你将进入短暂「网抑云」：记得带伞，心里那种", "effect": "nothing"},
    {"name": "赛博饺子", "icon": "🥟", "weight": 30,
     "text": "获得「赛博饺子」一笼：吃了不顶饱，但能回 1% 的精神力", "effect": "nothing"},
    {"name": "打工魂觉醒", "icon": "🧱", "weight": 35,
     "text": "「打工魂」觉醒：今天的你搬砖效率 +50%，摸鱼被抓概率也 +50%", "effect": "nothing"},
    {"name": "社牛附体", "icon": "🎤", "weight": 25,
     "text": "今日「社牛」附体：路过任何群都可以大胆开麦", "effect": "nothing"},
    {"name": "早睡提醒器", "icon": "🛌", "weight": 35,
     "text": "获得「早睡提醒器」：今晚 23:00 它会在你心里响——响不响看缘分", "effect": "nothing"},
    {"name": "免费续杯", "icon": "☕", "weight": 45,
     "text": "获得「今日份快乐续杯」：再忙也记得给自己倒杯水", "effect": "nothing"},
    {"name": "好运反弹", "icon": "🪃", "weight": 30,
     "text": "抽到「好运反弹」：今天别人向你丢的烦恼，都会变成双倍好运弹回去", "effect": "nothing"},
    {"name": "天气之子", "icon": "🌤️", "weight": 25,
     "text": "今日起你与天气系统绑定：心情晴，外面的天就晴（玄学）", "effect": "nothing"},
    {"name": "锦鲤路过", "icon": "🎏", "weight": 20,
     "text": "一条锦鲤刚好路过你头顶：接下来 24 小时许愿灵验度 +1", "effect": "nothing"},
    {"name": "连签护盾", "icon": "🛡️", "weight": 30,
     "text": "获得「连签护盾」×1：明天忘签也不断连签！（在 /积分 可见）",
     "effect": "shield", "amount": 1},
]


def _today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


class DailyCheckinPlugin(Star):
    def __init__(self, context: Context, config=None):
        """官方签名：`(self, context, config)`；config 即本插件的 AstrBotConfig。"""
        super().__init__(context)
        self.config = config or {}
        self._data = {}
        self._lottery = []
        self._migrated = False
        self._lock = threading.Lock()

        # 数据文件路径（AstrBot 官方约定）：data/plugin_data/<插件名>/
        # self.name 由宿主在加载时写入（AstrBot >= v4.9.2 也有这个属性）
        name = getattr(self, "name", "") or PLUGIN_NAME
        self._data_dir = Path(get_astrbot_data_path()) / "plugin_data" / name
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._data_file = self._data_dir / "checkin.json"
        self._lottery_file = self._data_dir / "lottery.json"
        self._legacy_file = self._data_dir / "checkin_legacy.json"

        self._load_lottery_config()

    # ---------- 数据读写 ----------
    def _load(self):
        if self._data:
            return
        if self._data_file.exists():
            try:
                with open(self._data_file, encoding="utf-8") as f:
                    self._data = json.load(f) or {}
                return
            except Exception:
                pass
        if not self._migrated:
            self._migrated = True
            if self._legacy_file.exists():
                try:
                    with open(self._legacy_file, encoding="utf-8") as f:
                        self._data = json.load(f) or {}
                    self._legacy_file.unlink()
                    self._save()
                    return
                except Exception:
                    pass
        self._data = {}

    def _save(self):
        try:
            with open(self._data_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _streak_bonus(self, streak: int) -> int:
        if streak <= 1:
            return 0
        return min(streak - 1, 5)

    def _next_today_order(self, today: str) -> int:
        meta = self._data.get("_meta") or {}
        if meta.get("date") != today:
            seq = 1
        else:
            seq = int(meta.get("seq", 0)) + 1
        self._data["_meta"] = {"date": today, "seq": seq}
        return seq

    # ---------- 抽奖配置 ----------
    def _load_lottery_config(self):
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            if not self._lottery_file.exists():
                with open(self._lottery_file, "w", encoding="utf-8") as f:
                    json.dump({
                        "note": "抽奖奖项配置：修改后重新加载插件生效。weight 为权重，越大越容易中。",
                        "items": _DEFAULT_LOTTERY,
                    }, f, ensure_ascii=False, indent=2)
                self._lottery = [dict(x) for x in _DEFAULT_LOTTERY]
                return
            with open(self._lottery_file, encoding="utf-8") as f:
                cfg = json.load(f) or {}
            items = cfg.get("items") or []
            self._lottery = [dict(x) for x in items
                             if isinstance(x, dict) and x.get("name")]
        except Exception:
            self._lottery = [dict(x) for x in _DEFAULT_LOTTERY]

    def _lottery_draw(self):
        weights = [max(1, int(x.get("weight", 1))) for x in self._lottery]
        r = random.randint(1, sum(weights))
        acc = 0
        for item, w in zip(self._lottery, weights):
            acc += w
            if r <= acc:
                return item
        return self._lottery[-1]

    # ---------- 指令 ----------
    @filter.command("签到")
    async def checkin(self, event: AstrMessageEvent):
        """每日签到，领积分并查看今日签到序号"""
        uid = event.get_sender_id()
        nick = event.get_sender_name() or "群友"
        with self._lock:
            self._load()
            today = _today()
            rec = self._data.get(uid)

            if rec and rec.get("last") == today:
                order = rec.get("today_order")
                order_txt = f"\n🎯 你是今天第 {order} 位签到的" if order else ""
                yield event.plain_result(
                    f"🟡 今天已经签过啦！\n"
                    f"📅 当前已连续签到 {rec.get('streak', 0)} 天\n"
                    f"💰 累计 {rec.get('points', 0)} 分 · 共签到 {rec.get('total', 0)} 次"
                    f"{order_txt}\n明天再来，连签奖励等着你～"
                )
                return

            yesterday = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=1)).strftime("%Y-%m-%d")
            shield = rec.get("shield", 0) if rec else 0
            if rec and rec.get("last") == yesterday:
                streak = rec.get("streak", 0) + 1
            elif rec and shield > 0:
                streak = rec.get("streak", 0) + 1
                rec["shield"] = shield - 1
            else:
                streak = 1
            total = (rec.get("total", 0) if rec else 0) + 1
            bonus = self._streak_bonus(streak)
            points = (rec.get("points", 0) if rec else 0) + 1 + bonus
            today_order = self._next_today_order(today)

            self._data[uid] = {
                "nick": nick, "last": today, "streak": streak,
                "total": total, "points": points,
                "shield": rec.get("shield", 0) if rec else 0,
                "today_order": today_order,
            }
            self._save()

            lines = [f"✅ 签到成功！{nick}",
                     f"🎯 你是今天第 {today_order} 位签到的"]
            if streak == 1 and not (rec and rec.get("last") == yesterday) and not shield:
                lines.append("🔥 连续签到 1 天（今天重新起算）")
            else:
                lines.append(f"🔥 连续签到 {streak} 天！")
            if shield and rec and rec.get("last") != yesterday:
                lines.append("🛡️ 使用了 1 个护盾，连签未中断")
            if bonus:
                lines.append(f"🎁 连签加成 +{bonus}")
            lines.append(f"💰 本次 +{1 + bonus} 分，当前共 {points} 分")
            lines.append(f"（累计签到 {total} 次）")
            if streak in (3, 7, 14, 30):
                lines.append(f"🎉 达成 {streak} 天连签成就！")
            yield event.plain_result("\n".join(lines))

    @filter.command("积分")
    async def query_points(self, event: AstrMessageEvent):
        """查询积分、连签、护盾等信息"""
        async for item in self._points_reply(event):
            yield item

    @filter.command("查询")
    async def query_points_alias(self, event: AstrMessageEvent):
        """/积分 的别名：查询积分、连签、护盾等信息"""
        async for item in self._points_reply(event):
            yield item

    async def _points_reply(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        nick = event.get_sender_name() or "群友"
        with self._lock:
            self._load()
            rec = self._data.get(uid)
            if not rec:
                yield event.plain_result(f"💰 {nick} 还没有积分，先去发 /签到 领 1 分吧～")
                return
            lines = [f"💰 {nick} 的积分",
                     f"积分余额：{rec.get('points', 0)} 分",
                     f"累计签到：{rec.get('total', 0)} 次 · 当前连签 {rec.get('streak', 0)} 天"]
            if rec.get("shield", 0):
                lines.append(f"🛡️ 连签护盾：{rec.get('shield', 0)} 个（断签不重置连签）")
            lines.append("可用 /抽奖 消耗 10 分碰碰运气～")
            yield event.plain_result("\n".join(lines))

    @filter.command("抽奖")
    async def lottery(self, event: AstrMessageEvent):
        """消耗 10 积分抽一次趣味奖"""
        uid = event.get_sender_id()
        nick = event.get_sender_name() or "群友"
        with self._lock:
            self._load()
            if not self._lottery:
                self._load_lottery_config()
            if not self._lottery:
                yield event.plain_result("⚠️ 抽奖配置为空，请检查 lottery.json")
                return
            rec = self._data.get(uid)
            if not rec or rec.get("points", 0) < 10:
                yield event.plain_result(
                    f"💸 抽奖需要 10 分，{nick} 当前余额不足。先发 /签到 攒分吧～"
                )
                return
            prize = self._lottery_draw()
            eff = prize.get("effect", "nothing")
            new_points = rec.get("points", 0) - 10
            got = ""
            if eff == "shield":
                n = int(prize.get("amount", 1) or 1)
                rec["shield"] = rec.get("shield", 0) + n
                got = f"\n✨ 连签护盾 +{n}（当前 {rec['shield']} 个）"
            rec["points"] = new_points
            self._save()
            text = str(prize.get("text") or f"抽中：{prize.get('name')}")
            lines = [f"🎰 {nick} 消耗 10 分抽奖",
                     f"{prize.get('icon', '🎁')} {text}"]
            if got:
                lines.append(got.strip())
            lines.append(f"💳 剩余积分：{new_points} 分")
            yield event.plain_result("\n".join(lines))

    async def terminate(self):
        """插件卸载/停用时保存数据"""
        with self._lock:
            self._save()