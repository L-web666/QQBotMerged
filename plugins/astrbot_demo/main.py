# -*- coding: utf-8 -*-
"""AstrBot 插件格式示例（本程序可直接加载）

目录里放 `metadata.yaml` + `main.py` 就够了；不用改主程序。
命令都带 `ab` 前缀，避免和群里正常聊天/其它插件撞车；不需要它时直接删掉这个目录。

对照表（AstrBot 写法 → 本程序里的效果）：
  @filter.command("x")            → 收到 `/x` 或 `x`（也支持别名）
  @filter.regex(r"...")           → 内容匹配正则时触发
  @filter.event_message_type(...) → 只处理群聊 / 只处理私聊
  @filter.permission_type(ADMIN)  → 只让管理员触发
  yield event.plain_result("...") → 回一段文字
  yield event.image_result(path)  → 回一张图片
  StarTools.get_data_dir("名字")   → 插件自己的数据目录（官方约定 data/plugin_data/名字）
"""

import os

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register


@register("astrbot_demo", "QQBOT 合并版", "AstrBot 插件格式示例", "1.0.0")
class AstrBotDemo(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir("astrbot_demo")

    # ---------------- 最基础的指令：/astrbot 或 /ab ----------------
    @filter.command("astrbot", alias={"ab"})
    async def hello(self, event: AstrMessageEvent):
        name = event.get_sender_name() or event.get_sender_id() or "你"
        where = "群聊" if event.get_group_id() else "私聊"
        yield event.plain_result(
            "你好，%s！这是 AstrBot 格式插件在回复你（%s）。\n当前会话：%s"
            % (name, where, event.unified_msg_origin))

    # ---------------- 回一张图片：/ab图 ----------------
    @filter.command("ab图", alias={"abpic"})
    async def picture(self, event: AstrMessageEvent):
        path = os.path.join(self.data_dir, "demo.png")
        with open(path, "wb") as handle:
            handle.write(bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                "1f15c4890000000a49444154789c6300010000050001"
                "0d0a2db40000000049454e44ae426082"))
        yield event.image_result(path)

    # ---------------- 只有管理员能用：/ab管理 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ab管理", alias={"abadmin"})
    async def admin_only(self, event: AstrMessageEvent):
        yield event.plain_result("管理员你好，这条只有你能看到。")

    # ---------------- 生命周期 ----------------
    async def initialize(self):
        self.context.logger.info("astrbot_demo 已初始化（数据目录：%s）", self.data_dir)

    async def terminate(self):
        self.context.logger.info("astrbot_demo 已卸载")
