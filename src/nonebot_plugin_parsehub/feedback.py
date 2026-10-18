# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""用户可见的反馈：表情回应与提示文案。

所有「点表情」「说一句话」的文案都集中在这里，上层只传事实（平台、时长、错误），
不再自己拼字符串。这样每个结局都有统一的表达，也不会出现某个分支忘了给终点。
"""

from __future__ import annotations

from enum import StrEnum

from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageEvent

from .delivery import Sender
from .errors import ErrorKind, short_error, translate
from .platforms import display_name

_BYTES_PER_MEGABYTE = 1024 * 1024


class Reaction(StrEnum):
    """给触发消息点的表情。"""

    START = "424"
    OK = "144"
    FAIL = "5"


async def react(
    sender: Sender,
    event: MessageEvent,
    reaction: Reaction,
) -> None:
    """给触发消息点一个表情回应。

    协议端不支持该 API 时只记 debug 日志，不影响主流程。

    Args:
        sender: 协议端接口。
        event: 触发消息所在的事件。
        reaction: 要点的表情。
    """
    try:
        await sender.call_api(
            "set_msg_emoji_like",
            message_id=event.message_id,
            emoji_id=str(reaction),
            set=True,
        )
    except Exception as exc:  # noqa: BLE001 - 回应只是锦上添花
        logger.debug(f"[parsehub] 表情回应 {reaction} 失败（忽略）: {exc}")


def failure_notice(exc: BaseException) -> str:
    """解析失败时发给用户的一句话。

    Args:
        exc: 捕获到的异常。

    Returns:
        提示文案。
    """
    failure = translate(exc)
    if failure.kind is ErrorKind.UNAVAILABLE:
        return "这个作品已经不可用了（可能被删了）。"
    if failure.kind is ErrorKind.TIMEOUT:
        return "解析超时，稍后再试。"
    return f"解析失败：{failure.detail}"


def download_failed_notice(exc: BaseException) -> str:
    """下载失败时发给用户的一句话。

    Args:
        exc: 捕获到的异常。

    Returns:
        提示文案。
    """
    return f"下载失败：{short_error(exc)}"


def unsupported_notice() -> str:
    """链接没有对应解析器时的提示。

    Returns:
        提示文案。
    """
    return "这个链接 ParseHub 不支持。"


def platform_off_notice(platform_id: str) -> str:
    """平台不在白名单里时给命令触发者的提示。

    Args:
        platform_id: parsehub 的平台 id。

    Returns:
        一句提示文案。
    """
    return f"{display_name(platform_id)} 暂时没开解析。"


def too_long_notice(watch: str) -> str:
    """视频超过时长上限时的提示。

    Args:
        watch: 适合念出来的平台名，例如 ``B 站``。

    Returns:
        提示文案。
    """
    return f"视频太长啦，还是去{watch}看吧"


def too_large_notice(size: int) -> str:
    """单文件超过体积上限时的提示。

    Args:
        size: 文件字节数。

    Returns:
        提示文案。
    """
    return f"文件约 {size / _BYTES_PER_MEGABYTE:.0f}MB，超过上限，已跳过。"


def truncated_notice(total: int, sent: int) -> str:
    """媒体数量超过上限时的提示。

    Args:
        total: 作品里的媒体总数。
        sent: 实际发送的数量。

    Returns:
        提示文案。
    """
    return f"（作品共 {total} 项，超过上限，只发前 {sent} 项）"
