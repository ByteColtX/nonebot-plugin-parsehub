# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""触发层：matcher 注册与命令处理。

这一层只做三件事：判断该不该处理、从消息里取链接、把结果交给
:func:`service.handle`。表情、提示、投递都在 service / delivery / feedback 里。

matcher 由 :func:`create_matchers` 按传入的 ``App`` 创建，测试可以拿自己的实例
造一套，不用碰模块全局。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot import get_driver, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
)
from nonebot.matcher import Matcher
from nonebot.params import CommandArg, RawCommand
from nonebot.rule import Rule

from . import audio, feedback, keys
from .service import handle, handle_audio, session_key, session_name

if TYPE_CHECKING:
    from .app import App

MAX_LINKS_PER_MESSAGE = 3

# 「解析开关」命令能接受的参数写法。
ON_WORDS = frozenset({"开", "开启", "打开", "on", "true", "1"})
OFF_WORDS = frozenset({"关", "关闭", "关上", "off", "false", "0"})
STATUS_WORDS = frozenset({"状态", "status"})

# QQ 小程序 / 分享卡片把链接藏在 json / xml 段的 data 里，纯文本取不到。
LINK_SEGMENT_TYPES = ("json", "xml")


@dataclass(frozen=True)
class Matchers:
    """本插件注册的 matcher。

    Attributes:
        auto: 自动解析。
        parse: ``解析`` 命令。
        platforms: ``解析平台`` 命令。
        switch: ``解析开关`` 命令。
        bm: ``bm`` 命令（B 站音频）。
        ym: ``ym`` 命令（YouTube 音频）。
    """

    auto: type[Matcher]
    parse: type[Matcher]
    platforms: type[Matcher]
    switch: type[Matcher]
    bm: type[Matcher]
    ym: type[Matcher]


def link_sources(event: MessageEvent) -> str:
    """拼出消息里所有可能含链接的内容。

    除了纯文本，还要看 json / xml 段（QQ 小程序、分享卡片）：里面的链接在 JSON
    里斜杠是被转义的，先还原成正常 URL 再交给 :func:`keys.extract_urls`。

    Args:
        event: 收到的消息事件。

    Returns:
        用于提取链接的文本。
    """
    parts = [event.get_plaintext()]
    for segment in event.get_message():
        if segment.type not in LINK_SEGMENT_TYPES:
            continue
        raw = str(segment.data.get("data") or "")
        parts.append(raw.replace("\\/", "/").replace("\\u0026", "&"))
    return "\n".join(parts)


def is_superuser(event: MessageEvent) -> bool:
    """发消息的人是不是 NoneBot 超管。

    Args:
        event: 消息事件。

    Returns:
        在 ``SUPERUSERS`` 名单里为 ``True``。
    """
    return event.get_user_id() in get_driver().config.superusers


def is_group_admin(event: MessageEvent) -> bool:
    """发消息的人是不是本群群主或管理员。

    Args:
        event: 消息事件。

    Returns:
        群主 / 管理员为 ``True``；私聊永远是 ``False``。
    """
    return isinstance(event, GroupMessageEvent) and event.sender.role in (
        "owner",
        "admin",
    )


def can_control(event: MessageEvent) -> bool:
    """有没有权限操作解析开关。

    Args:
        event: 消息事件。

    Returns:
        超管或群管理为 ``True``。
    """
    return is_superuser(event) or is_group_admin(event)


def command_boundary() -> Rule:
    """要求命令名后面是一个词边界。

    ``TrieRule`` 只做前缀匹配：``COMMAND_START`` 里带空字符串时，``ph`` 会把
    「php是世界上最好的语言」也当成命令，``解析`` 会把「解析一下」当成命令，
    于是普通聊天也会收到用法提示。这里要求命令名后紧跟空白、标点或消息结尾，
    字母 / 数字 / 汉字都算命令的延续。

    Returns:
        可传给 ``on_command(rule=...)`` 的规则。
    """

    async def _checker(
        event: MessageEvent, raw: str | None = RawCommand()
    ) -> bool:
        if not raw:
            return False
        rest = event.get_plaintext().lstrip()[len(raw) :]
        if not rest:
            return True
        head = rest[0]
        return not (head.isalnum() or head == "_")

    return Rule(_checker)


async def should_auto(app: App, event: MessageEvent) -> bool:
    """判断是否应该自动解析这条消息。

    命令不需要在这里排除：``解析`` / ``ph`` 等命令匹配器 ``priority=10`` 且
    ``block=True``，命中后事件不会流到 ``priority=20`` 的自动匹配器。反过来，
    ``COMMAND_START`` 没配空前缀时裸写的「解析 <链接>」本来就不是命令，必须按
    普通消息自动解析，否则两个匹配器都不管、消息被静默丢弃。

    Args:
        app: 插件实例（需要 ``settings`` 与 ``switches``）。
        event: 收到的消息事件。

    Returns:
        需要自动解析时返回 True。
    """
    settings = app.settings
    switches = app.switches
    if not settings.auto:
        return False
    if not switches.is_on(session_key(event)):
        return False  # 这个会话被关掉了
    return bool(keys.extract_targets(link_sources(event)))


def auto_rule(app: App) -> Rule:
    """构造自动解析的规则。

    Args:
        app: 插件实例。

    Returns:
        可传给 ``on_message`` 的规则。
    """

    async def _checker(event: MessageEvent) -> bool:
        return await should_auto(app, event)

    return Rule(_checker)


def create_matchers(app: App) -> Matchers:
    """按给定实例注册 matcher 与处理函数。

    Args:
        app: 插件实例（``App``）。

    Returns:
        注册好的 matcher 集合。
    """
    settings = app.settings
    switches = app.switches

    auto = on_message(rule=auto_rule(app), priority=20, block=False)
    parse = on_command(
        "解析",
        aliases={"ph", "解析链接"},
        rule=command_boundary(),
        priority=10,
        block=True,
    )
    platform_cmd = on_command(
        "解析平台",
        aliases={"ph平台", "解析支持"},
        rule=command_boundary(),
        priority=10,
        block=True,
    )
    switch = on_command(
        "解析开关",
        aliases={"解析开", "解析关", "解析状态"},
        rule=command_boundary(),
        priority=10,
        block=True,
    )

    bm = on_command(
        "bm",
        rule=command_boundary(),
        priority=10,
        block=True,
    )
    ym = on_command(
        "ym",
        rule=command_boundary(),
        priority=10,
        block=True,
    )

    @auto.handle()
    async def _handle_auto(bot: Bot, event: MessageEvent) -> None:
        """自动解析消息里的链接。"""
        urls = keys.extract_targets(link_sources(event))
        session = session_key(event)
        for url in urls[:MAX_LINKS_PER_MESSAGE]:
            # 只看不记：记录由 service 负责；这里先静默跳过，免得白发「收到」
            if app.dedupe.seen(
                session, keys.content_key(url), settings.dedupe_seconds
            ):
                continue
            await handle(app, bot, event, url)

    @parse.handle()
    async def _handle_parse(
        bot: Bot,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        """处理 ``解析 <链接>`` 命令。"""
        if not switches.is_on(session_key(event)) and not can_control(event):
            await parse.finish(
                f"{session_name(event)}的解析已关闭，"
                "只有超管或群管理员能手动解析。"
            )
        text = args.extract_plain_text().strip() or link_sources(event)
        urls = keys.extract_targets(text)
        if not urls:
            await parse.finish("用法：解析 <链接、BV 号或分享文案>")
        for url in urls[:MAX_LINKS_PER_MESSAGE]:
            await handle(app, bot, event, url, manual=True)

    @platform_cmd.handle()
    async def _handle_platforms() -> None:
        """处理 ``解析平台`` 命令。"""
        listed = await app.client.platforms()
        if not listed:
            await platform_cmd.finish("ParseHub 没有加载到任何平台。")
        names = "、".join(f"{item['name']}({item['id']})" for item in listed)
        await platform_cmd.finish(
            f"ParseHub 支持 {len(listed)} 个平台：\n{names}"
        )

    @switch.handle()
    async def _handle_switch(
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        """处理 ``解析开关`` / ``解析开`` / ``解析关`` / ``解析状态``。"""
        key = session_key(event)
        raw = event.get_plaintext().strip()
        head = raw.split(maxsplit=1)[0].lstrip("/#") if raw else ""
        arg = args.extract_plain_text().strip().lower()

        if head == "解析状态" or arg in STATUS_WORDS:
            state = "开启" if switches.is_on(key) else "关闭"
            await switch.finish(f"{session_name(event)}的解析当前是：{state}")

        if not can_control(event):
            await switch.finish("只有超管或本群管理员能改解析开关。")

        if head == "解析开" or arg in ON_WORDS:
            want = True
        elif head == "解析关" or arg in OFF_WORDS:
            want = False
        elif arg:
            await switch.finish(
                "用法：解析开关 [开|关]，不带参数就翻转；解析状态 看当前值"
            )
            return
        else:
            want = not switches.is_on(key)

        # 落盘是同步磁盘 IO，交给线程池，别卡住事件循环
        want = await asyncio.to_thread(switches.set, key, want)
        name = session_name(event)
        logger.info(
            f"[parsehub] {name}解析开关 -> {'开' if want else '关'}"
            f"（{key}，操作人 {event.get_user_id()}）"
        )
        if want:
            await switch.finish(f"已开启{name}的解析。")
        await switch.finish(
            f"已关闭{name}的解析：链接不再自动解析，"
            "手动解析也只有超管和群管理员能用。"
        )

    @bm.handle()
    async def _handle_bm(
        bot: Bot,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        """处理 ``bm <BV 号或 B 站链接>`` 命令。"""
        if not switches.is_on(session_key(event)) and not can_control(event):
            await bm.finish(
                f"{session_name(event)}的解析已关闭，"
                "只有超管或群管理员能手动解析。"
            )
        opts, rest = audio.parse_command(args.extract_plain_text())
        target = audio.extract_bilibili(rest)
        if target is None:
            if audio.has_av_number(rest):
                await bm.finish(feedback.audio_av_hint())
            await bm.finish(feedback.audio_usage_bili())
        await handle_audio(
            app, bot, event, target, "bilibili", as_file=opts.as_file
        )

    @ym.handle()
    async def _handle_ym(
        bot: Bot,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        """处理 ``ym <YouTube 链接或视频 ID>`` 命令。"""
        if not switches.is_on(session_key(event)) and not can_control(event):
            await ym.finish(
                f"{session_name(event)}的解析已关闭，"
                "只有超管或群管理员能手动解析。"
            )
        opts, rest = audio.parse_command(args.extract_plain_text())
        target = audio.extract_youtube(rest)
        if target is None:
            await ym.finish(feedback.audio_usage_yt())
        await handle_audio(
            app, bot, event, target, "youtube", as_file=opts.as_file
        )

    return Matchers(
        auto=auto,
        parse=parse,
        platforms=platform_cmd,
        switch=switch,
        bm=bm,
        ym=ym,
    )
