"""matcher 级别的端到端测试：走真实的 rule / matcher 分发（官方测试工具 nonebug）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import nonebot
import pytest
from fake import make_group_event, make_private_event
from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter
from nonebot.adapters.onebot.v11 import Bot, Message
from nonebot.rule import PREFIX_KEY, RAW_CMD_KEY
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


@pytest.mark.asyncio
async def test_auto_matcher_parses_bare_bv(
    app: App, plugin, monkeypatch, instance, matchers
):
    """纯 BV 号消息：拼成标准链接再解析，不把作品号原样丢给 parsehub。"""
    parsed_urls: list[str] = []

    async def fake_parse(url, proxy="", cookie=""):
        parsed_urls.append(url)
        return FakeParseResult()

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    event = make_group_event("BV1xx411c7mD", message_id=5)

    async with app.test_matcher(matchers.auto) as ctx:
        bot = ctx.create_bot(
            base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
        )
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_api(
            "set_msg_emoji_like",
            {
                "message_id": 5,
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
                "message_id": 5,
                "emoji_id": plugin.feedback.Reaction.OK,
                "set": True,
            },
            result=None,
        )
        ctx.should_finished()

    assert parsed_urls == ["https://www.bilibili.com/video/BV1xx411c7mD"]


@pytest.mark.asyncio
async def test_command_boundary_ignores_longer_word(
    app: App, plugin, instance, matchers
):
    """「php...」不是 /ph 命令：命令名后紧贴字母不算边界，不能回用法。"""
    event = make_group_event("php是世界上最好的语言。", message_id=6)

    async with app.test_matcher(matchers.parse) as ctx:
        bot = ctx.create_bot(
            base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
        )
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_command_boundary_ignores_chinese_continuation(
    app: App, plugin, instance, matchers
):
    """「解析一下这个视频」是普通聊天，不是解析命令。"""
    event = make_group_event("解析一下这个视频", message_id=7)

    async with app.test_matcher(matchers.parse) as ctx:
        bot = ctx.create_bot(
            base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
        )
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_command_boundary_keeps_alias_with_space(
    app: App, plugin, monkeypatch, instance, matchers
):
    """命令名后是空白时照常匹配，别名 ph 不受影响。"""
    _patch_parsehub(instance, monkeypatch, result=FakeParseResult())
    event = make_private_event("ph https://b23.tv/abcd", message_id=8)

    async with app.test_matcher(matchers.parse) as ctx:
        bot = ctx.create_bot(
            base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
        )
        ctx.receive_event(bot, event)
        ctx.should_call_api(
            "set_msg_emoji_like",
            {
                "message_id": 8,
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
                "message_id": 8,
                "emoji_id": plugin.feedback.Reaction.OK,
                "set": True,
            },
            result=None,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_command_boundary_with_slash_prefix(
    app: App, plugin, instance, matchers
):
    """默认的 / 前缀下，/php... 也不能被当成 /ph 命令。"""
    event = make_group_event("/php是世界上最好的语言。", message_id=9)

    async with app.test_matcher(matchers.parse) as ctx:
        bot = ctx.create_bot(
            base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
        )
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_command_boundary_rule_handles_slash_prefix(plugin, triggers):
    """直接检查规则：/ph 后接空白放行，紧贴字母拒绝。"""
    rule = triggers.command_boundary()
    bot = Bot(adapter=MagicMock(), self_id="10000")
    state = {PREFIX_KEY: {RAW_CMD_KEY: "/ph"}}

    assert await rule(bot, make_group_event("/ph https://b23.tv/abcd"), state)
    assert not await rule(
        bot, make_group_event("/php https://b23.tv/abcd"), state
    )
