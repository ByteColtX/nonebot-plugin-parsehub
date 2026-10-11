"""构造假事件，供测试使用。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import (
        GroupMessageEvent as GroupMessageEventV11,
    )
    from nonebot.adapters.onebot.v11 import (
        PrivateMessageEvent as PrivateMessageEventV11,
    )


def make_group_event(
    text: str = "test",
    *,
    group_id: int = 123456,
    user_id: int = 20000,
    message_id: int = 1,
    role: str = "member",
) -> GroupMessageEventV11:
    """构造一条群消息事件。"""
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
    from nonebot.adapters.onebot.v11.event import Sender

    return GroupMessageEvent(
        time=1700000000,
        self_id=10000,
        post_type="message",
        message_type="group",
        sub_type="normal",
        message_id=message_id,
        group_id=group_id,
        user_id=user_id,
        message=Message(text),
        raw_message=text,
        font=0,
        sender=Sender(
            user_id=user_id, nickname="tester", card="", role=role, title=""
        ),
    )


def make_private_event(
    text: str = "test", *, user_id: int = 20000, message_id: int = 1
) -> PrivateMessageEventV11:
    """构造一条私聊消息事件。"""
    from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
    from nonebot.adapters.onebot.v11.event import Sender

    return PrivateMessageEvent(
        time=1700000000,
        self_id=10000,
        post_type="message",
        message_type="private",
        sub_type="friend",
        message_id=message_id,
        user_id=user_id,
        message=Message(text),
        raw_message=text,
        font=0,
        sender=Sender(user_id=user_id, nickname="tester"),
    )
