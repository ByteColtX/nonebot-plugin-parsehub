# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""作品流水线的消息侧：「卡片或文字」的发送与文案拼装。

一张卡片发不出去就退回文字；发出去的卡片路径与文字要写进媒体缓存，
所以返回 :class:`SentInfo` 把内容一并带上。渲染是阻塞活（Pillow），
调用方放进线程池。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageEvent

from .. import platforms
from ..delivery import Sender
from ..media import MediaFile

if TYPE_CHECKING:
    from ..app import App


@dataclass
class SentInfo:
    """一次「卡片或文字」发送的结果，顺手带上内容好写进媒体缓存。

    Attributes:
        sent: 是否真的发出去了（延迟正文时，卡片成功也算 True）。
        card: 发出去的卡片图路径，没出卡为 ``None``。
        caption: 要写进缓存的文字；长正文可能由调用方放进合并转发。
    """

    sent: bool = False
    card: Path | None = None
    caption: str = ""


async def send_card_or_text(
    app: App,
    sender: Sender,
    event: MessageEvent,
    parsed: Any,  # ParseHub 解析结果
    files: list[MediaFile],
    proxy: str = "",
    *,
    allow_text: bool = True,
    caption: str | None = None,
    defer_caption: bool = False,
) -> SentInfo:
    """优先发卡片图，卡片不可用时退回文字。

    Args:
        app: 插件实例。
        sender: 协议端接口。
        event: 触发消息事件。
        parsed: ParseHub 解析结果。
        files: 已下载的本地媒体。
        proxy: 取封面用的代理。
        allow_text: 出不了卡时是否立刻退回文字。下载前那次调用要传 ``False``，
            否则文字先发了，等下载完再抽帧出卡就成了重复消息。卡片成功时，
            没有标题的正文帖仍会补发正文（卡片只放得下摘要）。
        caption: 调用方指定的正文（长正文平台会放进合并转发）。为 ``None``
            时沿用「无标题才补正文」的旧规则。
        defer_caption: 只返回正文、不单独发送；由调用方把它和媒体合并转发。

    Returns:
        发出去了什么（卡片路径 / 文字），用来写媒体缓存。
    """
    settings = app.settings
    body = _body_only_caption(parsed, settings) if caption is None else caption
    if settings.card_enabled:
        out_path = await app.cards.render(
            parsed, [file.path for file in files], None, proxy
        )
        if out_path is None:
            if files or allow_text:
                logger.info("[parsehub] 没有可用封面，卡片跳过")
            else:
                # 下载前那次没封面是正常的：下载后还会用视频帧重试。
                logger.debug("[parsehub] 暂时没有封面，下载后重试")
        if out_path is not None and await app.delivery.send_card(
            sender, event, out_path
        ):
            if body and not defer_caption:
                await app.delivery.send_text(sender, event, body)
            return SentInfo(sent=True, card=out_path, caption=body)
    if allow_text and settings.send_text:
        if caption is not None:
            fallback = body
        else:
            fallback = build_caption(parsed, settings.max_chars)
        if defer_caption:
            return SentInfo(caption=fallback)
        await app.delivery.send_text(sender, event, fallback)
        return SentInfo(sent=True, caption=fallback)
    return SentInfo(caption=body if defer_caption else "")


def forward_body(parsed: Any, settings: Any) -> str:
    """取要跟媒体一起合并转发的正文。

    酷安 / 微博 / 知乎等正文型平台的正文可能长到卡片放不下；正文为空、
    平台只适合短文案或用户关了文字发送时返回空串。

    Args:
        parsed: ParseHub 解析结果。
        settings: 插件设置。

    Returns:
        要放进合并转发的正文；不需要时为空串。
    """
    if not settings.send_text:
        return ""
    platform = getattr(parsed, "platform", None)
    platform_id = str(getattr(platform, "id", "") or "")
    post_type = str(getattr(getattr(parsed, "type", None), "value", "") or "")
    content = str(getattr(parsed, "content", "") or "")
    if not platforms.needs_body_forward(platform_id, post_type, content):
        return ""
    return excerpt(content, settings.max_chars)


def _body_only_caption(parsed: Any, settings: Any) -> str:
    """没有标题、只有正文时，卡片后补发的文字。

    卡片标题最多三行，微博 / 酷安这类正文帖会被截断；补一条文字把正文
    发全。有标题的帖子不补，避免和卡片重复。

    Args:
        parsed: ParseHub 解析结果。
        settings: 插件设置。

    Returns:
        要补发的文字；不需要时为空串。
    """
    if not settings.send_text:
        return ""
    title = str(getattr(parsed, "title", "") or "").strip()
    content = str(getattr(parsed, "content", "") or "").strip()
    if title or not content:
        return ""
    return build_caption(parsed, settings.max_chars)


def build_caption(parsed: Any, max_chars: int) -> str:
    """拼装解析结果的文字消息（不含原始链接）。

    Args:
        parsed: ParseHub 的解析结果。
        max_chars: 正文最多保留的字符数。

    Returns:
        待发送的文字。
    """
    platform = getattr(parsed, "platform", None)
    platform_id = str(getattr(platform, "id", "") or "")
    display = str(getattr(platform, "display_name", "") or "")
    display = display or platforms.display_name(platform_id)
    lines = [f"📦 {display}"]
    title = getattr(parsed, "title", "")
    content = getattr(parsed, "content", "")
    if title:
        lines.append(title)
    if content:
        lines.append(excerpt(content, max_chars))
    return "\n".join(lines)


def excerpt(text: str, limit: int) -> str:
    """把正文压成单行并截断。

    Args:
        text: 原始正文。
        limit: 最大字符数。

    Returns:
        压缩后的正文，超长时以省略号结尾。
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"
