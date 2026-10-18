# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""解析开关命令的 matcher 级测试：权限、开关生效范围、手动解析的连带限制。

走真实的 rule / matcher 分发（nonebug），超管号是 ``.env.test`` 里的 ``SUPERUSERS``。
"""

from __future__ import annotations

import nonebot
import pytest
from fake import make_group_event, make_private_event
from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter
from nonebot.adapters.onebot.v11 import Bot, Message
from nonebug import App

# 与 .env.test 里的 SUPERUSERS 保持一致
SUPERUSER_ID = 20001
# 普通成员 / 群管理 / 另一个普通成员
MEMBER_ID = 20000
ADMIN_ID = 20002


async def _send(app: App, plugin, instance, event, expect: str) -> None:
    """把一个事件喂给开关命令，断言它回了什么。

    注意：插件用 ``finish("文本")`` 发的是裸字符串，所以这里也要传字符串；
    传 ``Message(...)`` 会因为类型不同被判成「回复不一样」。
    """
    matchers = plugin.triggers.create_matchers(instance)
    async with app.test_matcher(matchers.switch) as ctx:
        bot = ctx.create_bot(
            base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, expect, result=None, bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_member_cannot_switch(
    app: App, plugin, instance, switches
) -> None:
    """普通成员改不动开关，状态也不变。"""
    event = make_group_event("解析关", user_id=MEMBER_ID, message_id=101)
    await _send(
        app, plugin, instance, event, "只有超管或本群管理员能改解析开关。"
    )
    assert instance.switches.is_on(switches.group_key(123456)) is True


@pytest.mark.asyncio
async def test_member_can_read_status(app: App, plugin, instance) -> None:
    """状态谁都能看。"""
    event = make_group_event("解析状态", user_id=MEMBER_ID, message_id=102)
    await _send(app, plugin, instance, event, "本群的解析当前是：开启")


@pytest.mark.asyncio
async def test_admin_closes_group(app: App, plugin, instance, switches) -> None:
    """群管理能把本群关掉，回复里说清楚后果。"""
    event = make_group_event(
        "解析关", user_id=ADMIN_ID, message_id=103, role="admin"
    )
    await _send(
        app,
        plugin,
        instance,
        event,
        "已关闭本群的解析：链接不再自动解析，手动解析也只有超管和群管理员能用。",
    )
    assert instance.switches.is_on(switches.group_key(123456)) is False


@pytest.mark.asyncio
async def test_owner_can_switch(app: App, plugin, instance) -> None:
    """群主也算群管理。"""
    event = make_group_event(
        "解析开", user_id=ADMIN_ID, message_id=104, role="owner"
    )
    await _send(app, plugin, instance, event, "已开启本群的解析。")


@pytest.mark.asyncio
async def test_superuser_can_close_private(
    app: App, plugin, instance, switches
) -> None:
    """私聊默认也开，超管可以把某个私聊单独关掉。"""
    assert instance.switches.is_on(switches.private_key(SUPERUSER_ID)) is True
    event = make_private_event("解析关", user_id=SUPERUSER_ID, message_id=105)
    await _send(
        app,
        plugin,
        instance,
        event,
        "已关闭本会话的解析：链接不再自动解析，手动解析也只有超管和群管理员能用。",
    )
    assert instance.switches.is_on(switches.private_key(SUPERUSER_ID)) is False


@pytest.mark.asyncio
async def test_member_cannot_switch_private(
    app: App, plugin, instance, switches
) -> None:
    """私聊里普通成员不行（私聊没有群管理这一说），状态也不变。"""
    event = make_private_event("解析关", user_id=MEMBER_ID, message_id=113)
    await _send(
        app, plugin, instance, event, "只有超管或本群管理员能改解析开关。"
    )
    assert instance.switches.is_on(switches.private_key(MEMBER_ID)) is True


@pytest.mark.asyncio
async def test_toggle_without_argument(
    app: App, plugin, instance, switches
) -> None:
    """不带参数就是翻转。"""
    event = make_group_event(
        "解析开关", user_id=ADMIN_ID, message_id=106, role="admin"
    )
    await _send(
        app,
        plugin,
        instance,
        event,
        "已关闭本群的解析：链接不再自动解析，手动解析也只有超管和群管理员能用。",
    )
    assert instance.switches.is_on(switches.group_key(123456)) is False

    event2 = make_group_event(
        "解析开关 开", user_id=ADMIN_ID, message_id=107, role="admin"
    )
    await _send(app, plugin, instance, event2, "已开启本群的解析。")
    assert instance.switches.is_on(switches.group_key(123456)) is True


@pytest.mark.asyncio
async def test_bad_argument_shows_usage(app: App, plugin, instance) -> None:
    """参数看不懂就回用法。"""
    event = make_group_event(
        "解析开关 也许", user_id=ADMIN_ID, message_id=108, role="admin"
    )
    await _send(
        app,
        plugin,
        instance,
        event,
        "用法：解析开关 [开|关]，不带参数就翻转；解析状态 看当前值",
    )


@pytest.mark.asyncio
async def test_auto_rule_stops_when_closed(
    plugin, instance, switches, triggers
) -> None:
    """关掉之后，这个群的链接不再自动解析。"""
    instance.switches.set(switches.group_key(123456), False)
    event = make_group_event("【好女孩刻猫猫】 https://b23.tv/ZlF8X92")
    assert await triggers.should_auto(instance, event) is False


@pytest.mark.asyncio
async def test_auto_rule_unaffected_in_other_group(
    plugin, instance, switches, triggers
) -> None:
    """关的是「这个会话」，别的群照旧。"""
    instance.switches.set(switches.group_key(123456), False)
    event = make_group_event("https://b23.tv/ZlF8X92", group_id=654321)
    assert await triggers.should_auto(instance, event) is True


@pytest.mark.asyncio
async def test_auto_rule_default_on(plugin, instance, triggers) -> None:
    """没动过开关的群默认还是解析。"""
    event = make_group_event("https://b23.tv/ZlF8X92")
    assert await triggers.should_auto(instance, event) is True


@pytest.mark.asyncio
async def test_private_auto_rule_on_by_default(
    plugin, instance, switches, triggers
) -> None:
    """私聊默认也自动解析，关掉之后才不解析。"""
    event = make_private_event("https://b23.tv/ZlF8X92", user_id=MEMBER_ID)
    assert await triggers.should_auto(instance, event) is True
    instance.switches.set(switches.private_key(MEMBER_ID), False)
    assert await triggers.should_auto(instance, event) is False


@pytest.mark.asyncio
async def test_manual_parse_denied_when_closed(
    app: App, plugin, instance, switches, matchers
) -> None:
    """关掉后普通成员手动解析也不行。"""
    instance.switches.set(switches.group_key(123456), False)
    event = make_group_event(
        "解析 https://b23.tv/abcd", user_id=MEMBER_ID, message_id=111
    )
    async with app.test_matcher(matchers.parse) as ctx:
        bot = ctx.create_bot(
            base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            "本群的解析已关闭，只有超管或群管理员能手动解析。",
            result=None,
            bot=bot,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_manual_parse_allowed_for_admin_when_closed(
    app: App, plugin, monkeypatch, instance, switches, matchers
) -> None:
    """关掉后群管理手动解析仍然可用。"""
    instance.switches.set(switches.group_key(123456), False)

    async def fake_is_supported(url: str):
        return False  # 走到「不支持」分支就够了，证明没被开关拦下

    monkeypatch.setattr(instance.client, "is_supported", fake_is_supported)
    event = make_group_event(
        "解析 https://b23.tv/abcd",
        user_id=ADMIN_ID,
        message_id=112,
        role="admin",
    )
    async with app.test_matcher(matchers.parse) as ctx:
        bot = ctx.create_bot(
            base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
        )
        ctx.receive_event(bot, event)
        ctx.should_call_api(
            "set_msg_emoji_like",
            {"message_id": 112, "emoji_id": plugin.REACT_START, "set": True},
            result=None,
        )
        ctx.should_call_send(
            event, Message("这个链接 ParseHub 不支持。"), result=None, bot=bot
        )
        ctx.should_call_api(
            "set_msg_emoji_like",
            {"message_id": 112, "emoji_id": plugin.REACT_FAIL, "set": True},
            result=None,
        )
        ctx.should_finished()
