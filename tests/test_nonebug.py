"""matcher 级别的端到端测试：走真实的 rule / matcher 分发（官方测试工具 nonebug）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import nonebot
import pytest
from fake import make_group_event, make_private_event
from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter
from nonebot.adapters.onebot.v11 import Bot, Message
from nonebug import App


class FakeParseResult:
    """只有文字、没有媒体的解析结果。"""

    def __init__(self, *, title: str = "标题", content: str = "正文") -> None:
        self.title = title
        self.content = content
        self.media = None
        self.raw_url = "https://b23.tv/abcd"
        self.platform = SimpleNamespace(id="bilibili", display_name="哔哩哔哩")


def _patch_parsehub(
    instance,
    monkeypatch,
    *,
    supported: bool = True,
    result=None,
) -> None:
    """把 ParseHub 的解析替换成假实现。"""

    async def fake_parse(url, proxy="", cookie=""):
        return result

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=supported),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)


@pytest.mark.asyncio
async def test_auto_matcher_parses_group_link(
    app: App, plugin, monkeypatch, instance, matchers
):
    _patch_parsehub(instance, monkeypatch, result=FakeParseResult())
    event = make_group_event(
        "【好女孩刻猫猫】 https://b23.tv/ZlF8X92", message_id=1
    )

    async with app.test_matcher(matchers.auto) as ctx:
        bot = ctx.create_bot(
            base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
        )
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_api(
            "set_msg_emoji_like",
            {
                "message_id": 1,
                "emoji_id": plugin.feedback.Reaction.START,
                "set": True,
            },
            result=None,
        )
        ctx.should_call_send(
            event, Message("📦 哔哩哔哩\n标题\n正文"), result=None, bot=bot
        )
        ctx.should_call_api(
            "set_msg_emoji_like",
            {
                "message_id": 1,
                "emoji_id": plugin.feedback.Reaction.OK,
                "set": True,
            },
            result=None,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_auto_matcher_ignores_plain_message(
    app: App, plugin, monkeypatch, instance, matchers
):
    _patch_parsehub(instance, monkeypatch, result=FakeParseResult())
    event = make_group_event("今天天气不错", message_id=2)

    async with app.test_matcher(matchers.auto) as ctx:
        bot = ctx.create_bot(
            base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
        )
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_command_matcher_in_private(
    app: App, plugin, monkeypatch, instance, matchers
):
    """私聊默认开，手动解析可用。"""
    _patch_parsehub(instance, monkeypatch, result=FakeParseResult())
    event = make_private_event("解析 https://b23.tv/abcd", message_id=3)

    async with app.test_matcher(matchers.parse) as ctx:
        bot = ctx.create_bot(
            base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
        )
        ctx.receive_event(bot, event)
        ctx.should_call_api(
            "set_msg_emoji_like",
            {
                "message_id": 3,
                "emoji_id": plugin.feedback.Reaction.START,
                "set": True,
            },
            result=None,
        )
        ctx.should_call_send(
            event, Message("📦 哔哩哔哩\n标题\n正文"), result=None, bot=bot
        )
        ctx.should_call_api(
            "set_msg_emoji_like",
            {
                "message_id": 3,
                "emoji_id": plugin.feedback.Reaction.OK,
                "set": True,
            },
            result=None,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_unsupported_link_gives_feedback(
    app: App, plugin, monkeypatch, instance, media_mod, matchers
):
    _patch_parsehub(instance, monkeypatch, supported=False)
    event = make_group_event("解析 https://b23.tv/abcd", message_id=4)

    async with app.test_matcher(matchers.parse) as ctx:
        bot = ctx.create_bot(
            base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
        )
        ctx.receive_event(bot, event)
        ctx.should_call_api(
            "set_msg_emoji_like",
            {
                "message_id": 4,
                "emoji_id": plugin.feedback.Reaction.START,
                "set": True,
            },
            result=None,
        )
        ctx.should_call_send(
            event, Message("这个链接 ParseHub 不支持。"), result=None, bot=bot
        )
        ctx.should_call_api(
            "set_msg_emoji_like",
            {
                "message_id": 4,
                "emoji_id": plugin.feedback.Reaction.FAIL,
                "set": True,
            },
            result=None,
        )
        ctx.should_finished()
