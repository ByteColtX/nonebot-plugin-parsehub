# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""服务入口：两条流水线共用的骨架（白名单、去重、表情、兜底、收尾）。

``handle``（作品）与 ``handle_audio``（音频）只负责各自的门禁与参数准备，
拿到流水线函数后交给 :func:`_execute` 统一收尾：并发信号量、INTERNAL_ERROR
兜底、notice 发送、终态表情、去重提交 / 释放与日志。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

from .. import audio, bilibili_live, feedback, keys
from ..delivery import Sender
from ..switches import group_key, private_key
from . import works
from .audio import deliver_audio
from .result import Outcome, Result

if TYPE_CHECKING:
    from ..app import App


def session_key(event: MessageEvent) -> str:
    """取这条消息所属的会话键。

    Args:
        event: 消息事件。

    Returns:
        群按群号、私聊按对方 QQ。
    """
    if isinstance(event, GroupMessageEvent):
        return group_key(event.group_id)
    return private_key(int(event.get_user_id()))


def session_name(event: MessageEvent) -> str:
    """给用户看的会话称呼。

    Args:
        event: 消息事件。

    Returns:
        ``本群`` 或 ``本会话``。
    """
    return "本群" if isinstance(event, GroupMessageEvent) else "本会话"


async def handle(
    app: App,
    sender: Sender,
    event: MessageEvent,
    url: str,
    *,
    manual: bool = False,
) -> Result:
    """解析一个链接并把结果发出去。

    Args:
        app: 插件实例。
        sender: 协议端接口。
        event: 触发消息事件。
        url: 待解析的链接。
        manual: 是否为命令触发。手动触发不做去重拦截，失败时也会给反馈。

    Returns:
        本次处理的结局。
    """
    settings = app.settings
    session = session_key(event)
    if bilibili_live.is_live_url(url):
        # 直播间不在 ParseHub 的平台表里，手动归到 bilibili，
        # 让白名单 / 代理设置跟普通 B 站链接走同一套
        platform = "bilibili"
    else:
        platform = keys.platform_of(url)

    # 平台白名单：被关掉的平台连「收到」都不点，免得看起来像在解析。
    if platform and not settings.platform_allowed(platform):
        logger.info(f"[parsehub] {platform} 不在解析白名单里，跳过")
        notice = feedback.platform_off_notice(platform) if manual else None
        if notice:
            await app.delivery.send_text(sender, event, notice)
        return Result.fail(Outcome.PLATFORM_OFF)

    pre_key = keys.content_key(url)
    if not manual and not app.dedupe.claim(
        session, [pre_key], settings.dedupe_seconds
    ):
        logger.info(f"[parsehub] {pre_key} 在本会话刚处理过，跳过")
        return Result.fail(Outcome.DUPLICATE, keys=(pre_key,))

    start_reaction = asyncio.create_task(
        feedback.react(sender, event, feedback.Reaction.START)
    )

    async def pipeline() -> Result:
        return await works.deliver(
            app, sender, event, url, platform, session, pre_key, manual
        )

    return await _execute(
        app,
        sender,
        event,
        pipeline,
        start_reaction=start_reaction,
        manual=manual,
        label=f"{url}（{platform or '未知平台'}）",
        dedupe=True,
        session=session,
        base_key=pre_key,
    )


async def handle_audio(
    app: App,
    sender: Sender,
    event: MessageEvent,
    target: str,
    platform: str,
    *,
    as_file: bool = False,
) -> Result:
    """下载一个视频的音频并发出去（bm / ym 指令）。

    与 :func:`handle` 共用结局、表情与提示约定，但不走 ParseHub：B 站直接调
    playurl 接口，YouTube 直接调 yt-dlp。缓存 key 加 ``audio:`` 前缀，与
    视频缓存互不干扰；音频不参与会话去重。时长上限不适用（音频按体积限制）。

    Args:
        app: 插件实例。
        sender: 协议端接口。
        event: 触发消息事件。
        target: 目标链接（已归一成标准链接 / 短链）。
        platform: 平台 id（``bilibili`` / ``youtube``）。
        as_file: True 以群文件 / 私聊文件发送，否则发 record 语音段。

    Returns:
        本次处理的结局。
    """
    settings = app.settings

    # 平台白名单：被关掉的平台连「收到」都不点
    if not settings.platform_allowed(platform):
        logger.info(f"[parsehub] {platform} 不在解析白名单里，跳过音频")
        await app.delivery.send_text(
            sender, event, feedback.platform_off_notice(platform)
        )
        return Result.fail(Outcome.PLATFORM_OFF)

    key = audio.cache_key(target)
    start_reaction = asyncio.create_task(
        feedback.react(sender, event, feedback.Reaction.START)
    )

    async def pipeline() -> Result:
        return await deliver_audio(
            app, sender, event, target, platform, key, as_file
        )

    return await _execute(
        app,
        sender,
        event,
        pipeline,
        start_reaction=start_reaction,
        # 音频不参与会话去重（dedupe=False），manual 只在去重分支里被读
        manual=False,
        label=f"音频 {target}（{platform}）",
        dedupe=False,
        session=session_key(event),
        base_key=key,
    )


async def _execute(
    app: App,
    sender: Sender,
    event: MessageEvent,
    pipeline: Callable[[], Awaitable[Result]],
    *,
    start_reaction: asyncio.Task[None],
    manual: bool,
    label: str,
    dedupe: bool,
    session: str,
    base_key: str,
) -> Result:
    """在并发信号量内跑一条流水线并统一收尾。

    Args:
        app: 插件实例。
        sender: 协议端接口。
        event: 触发消息事件。
        pipeline: 无参协程，信号量内执行的实际处理。
        start_reaction: 已发出的 START 表情任务；收尾前 join，保证表情顺序。
        manual: 是否为命令触发（不参与去重拦截）。
        label: 写日志用的描述。
        dedupe: 是否做去重提交 / 释放（音频不参与）。
        session: 会话键。
        base_key: 兜底结局（INTERNAL_ERROR）记的 key。

    Returns:
        本次处理的结局。
    """
    settings = app.settings
    try:
        async with app.semaphore:
            result = await pipeline()
    except Exception as exc:  # 兜底：任何路径都要有终点
        logger.exception(f"[parsehub] 处理 {label} 时出现未预期异常: {exc}")
        result = Result.fail(
            Outcome.INTERNAL_ERROR,
            notice=feedback.internal_error_notice(),
            keys=(base_key,),
        )

    # START 是后台发的：收尾前先 join，保证提示和终态表情都排在它后面。
    await start_reaction
    if result.notice:
        await app.delivery.send_text(sender, event, result.notice)
    # 走到这里的都点过「收到」：重复也要收尾，否则消息会一直停在 START。
    reaction = (
        feedback.Reaction.OK
        if result.ok or result.outcome is Outcome.DUPLICATE
        else feedback.Reaction.FAIL
    )
    await feedback.react(sender, event, reaction)

    if dedupe:
        # 手动命令不参与去重：既不拦也不记，和旧行为一致。
        # 窗口为 0 时 commit 只会让 _seen 无限增长，所以只 release。
        if not manual:
            if result.ok and settings.dedupe_seconds > 0:
                app.dedupe.commit(session, result.keys)
            else:
                app.dedupe.release(session, result.keys)
    logger.info(f"[parsehub] {result.outcome}: {label}")
    return result
