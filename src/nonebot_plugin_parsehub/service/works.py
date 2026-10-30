# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""作品流水线：从链接到「卡片 + 媒体发出去」。

``deliver`` 按固定顺序推进，每一步都是一个小阶段函数：要么带着 :class:`Result`
就地终结，要么把阶段产物填进 :class:`_Work` 交给下一阶段；媒体获取与
「卡片或文字」的发送在 :mod:`message`，下载在 :mod:`download`。顺序：直播间
封面卡片 → 缓存命中
→ 支持性 / 预算 / 解析 / 二次去重 → 时长上限 / 无媒体 / 截断 → 单媒体
体积预检 → 卡片与下载并行，投递，写缓存。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageEvent

from .. import audio, bilibili_live, errors, feedback, keys, media, platforms
from ..budget import Budget
from ..cache import CacheEntry
from ..delivery import Sender
from ..media import MAX_MEDIA_ITEMS, MediaFile, MediaRef
from ..parsehub_client import platform_cookie
from . import download, message
from .result import Outcome, Result

if TYPE_CHECKING:
    from ..app import App


@dataclass
class _Work:
    """作品流水线的阶段间上下文：后面的阶段只读前面的阶段填好的字段。"""

    url: str
    platform: str
    session: str
    pre_key: str
    manual: bool
    music: bool = False  # YouTube Music：发卡片 + record，不走视频下载
    post_key: str = ""  # 解析结果链接的作品 key
    all_keys: tuple[str, ...] = ()  # pre_key 与 post_key 去重后的组合
    platform_id: str = ""  # 解析结果里的平台 id（拿不到时退回 platform）
    display: str = ""
    parsed: Any = None  # ParseHub 解析结果
    refs: list[MediaRef] = field(default_factory=list)  # 截断后的媒体引用
    total: int = 0  # 截断前的媒体总数（拼提示用）
    seconds: int = 0  # 最长媒体的时长（秒）
    budget: Budget | None = None
    proxy: str = ""
    cookie: str = ""


async def deliver(
    app: App,
    sender: Sender,
    event: MessageEvent,
    url: str,
    platform: str,
    session: str,
    pre_key: str,
    manual: bool,
) -> Result:
    """在并发信号量内完成一次作品处理（不含表情与去重收尾）。

    Args:
        app: 插件实例。
        sender: 协议端接口。
        event: 触发消息事件。
        url: 待解析的链接。
        platform: 平台 id（可能为空）。
        session: 会话键。
        pre_key: 原始链接的作品 key。
        manual: 是否为命令触发。

    Returns:
        本次处理的结局。
    """
    work = _Work(
        url=url,
        platform=platform,
        session=session,
        pre_key=pre_key,
        manual=manual,
    )

    live = await _try_live(
        app, sender, event, url, pre_key, app.settings.proxy_for(platform)
    )
    if live is not None:
        return live

    entry = await _hit_cache(app, pre_key)
    if entry is not None:
        return await _deliver_cached(app, sender, event, entry, pre_key)

    result = await _parse_work(app, sender, event, work)
    if result is not None:
        return result

    result = await _apply_limits(app, sender, event, work)
    if result is not None:
        return result

    result = await _size_gate(app, work)
    if result is not None:
        return result
    return await _download_and_deliver(app, sender, event, work)


async def _try_live(
    app: App,
    sender: Sender,
    event: MessageEvent,
    url: str,
    pre_key: str,
    proxy: str,
) -> Result | None:
    """直播间只发一张封面卡片，不走 ParseHub、不下载媒体。

    状态随时在变，因此**不读也不写作品缓存**，只靠会话去重拦重复；任何
    一步失败都返回 ``None``，由调用方退回常规失败路径。
    """
    if not bilibili_live.is_live_url(url):
        return None
    if not app.settings.card_enabled:
        return None

    info = await asyncio.to_thread(bilibili_live.fetch_live, url, proxy)
    if info is None:
        return None

    result = bilibili_live.build_result(info)
    out_path = await app.cards.render(result, [], None, proxy)
    if out_path is None:
        return None
    if not await app.delivery.send_card(sender, event, out_path):
        return None

    post_key = keys.content_key(bilibili_live.live_url(info.room_id))
    logger.info(f"[parsehub] 直播间封面卡片已发送: {url}")
    return Result(Outcome.DELIVERED, keys=(pre_key, post_key))


async def _hit_cache(app: App, key: str) -> CacheEntry | None:
    """查作品缓存；超时长上限的条目当没命中。

    短链命中（解析前）与 post_key 命中（解析后）都走这里，共用同一道时长
    上限检查——超长条目从哪条路都不能直接发。
    """
    entry = await asyncio.to_thread(app.cache.get, key)
    if entry is None:
        return None
    limit = app.settings.max_seconds
    if limit and entry.seconds > limit:
        return None
    return entry


async def _parse_work(
    app: App, sender: Sender, event: MessageEvent, work: _Work
) -> Result | None:
    """支持性判断、预算与 cookie 准备、解析与二次去重。

    成功时把解析产物填进 ``work`` 并返回 ``None``；终结时返回对应结局。
    """
    settings = app.settings
    url = work.url
    try:
        supported = await app.client.is_supported(url)
    except Exception as exc:  # 判断失败就交给解析去报错
        logger.warning(
            f"[parsehub] 平台支持判断失败 {url}: {type(exc).__name__}: {exc}"
        )
        supported = True
    if not supported:
        notice = feedback.unsupported_notice() if work.manual else None
        return Result.fail(
            Outcome.UNSUPPORTED, notice=notice, keys=(work.pre_key,)
        )

    work.budget = Budget(settings.timeout)
    work.proxy = settings.proxy_for(work.platform)
    # cookies.toml 是同步磁盘读，放线程里，别卡住事件循环
    work.cookie = await asyncio.to_thread(
        platform_cookie, work.platform, settings.cookie
    )
    logger.debug(f"[parsehub] {work.platform} 代理={work.proxy or '直连'}")

    started = time.monotonic()
    try:
        assert work.budget is not None
        async with work.budget.timeout():
            work.parsed = await app.client.parse(url, work.proxy, work.cookie)
    except Exception as exc:  # parsehub 异常类型很多，统一兜底
        failure = errors.translate(exc)
        logger.warning(
            f"[parsehub] 解析失败 {url}: {type(exc).__name__}: {exc}"
        )
        # B 站短链展开后可能是直播间，ParseHub 认不出来但封面卡片还能做
        if bilibili_live.is_short_link(url):
            expanded = await asyncio.to_thread(
                bilibili_live.expand_short, url, work.proxy
            )
            if expanded:
                live = await _try_live(
                    app, sender, event, expanded, work.pre_key, work.proxy
                )
                if live is not None:
                    return live
        notice = None
        if work.manual or failure.kind is errors.ErrorKind.UNAVAILABLE:
            notice = feedback.failure_notice(failure)
        return Result.fail(
            Outcome.PARSE_FAILED, notice=notice, keys=(work.pre_key,)
        )

    _fill_keys(work)
    raw_url = str(getattr(work.parsed, "raw_url", "") or "")
    work.music = platforms.is_youtube_music(
        work.url
    ) or platforms.is_youtube_music(raw_url)
    if work.music:
        # 卡片元信息据此显示「音乐」，而不是 ParseHub 默认的「视频」。
        work.parsed.music = True
    logger.info(
        f"[parsehub] {work.display} 解析成功"
        f"（耗时 {time.monotonic() - started:.1f}s）: {url}"
    )

    # 同一个作品的另一种写法（短链展开后变了）也要在会话内拦一次。
    if (
        not work.manual
        and work.post_key != work.pre_key
        and app.dedupe.seen(
            work.session, work.post_key, settings.dedupe_seconds
        )
    ):
        logger.info(f"[parsehub] {work.post_key} 在本会话刚发过，跳过发送")
        return Result.fail(Outcome.DUPLICATE, keys=work.all_keys)

    work.refs = media.refs_from(work.parsed)
    work.seconds = platforms.duration_seconds(
        work.platform_id, (ref.duration for ref in work.refs)
    )
    return None


def _fill_keys(work: _Work) -> None:
    """按解析结果补全 key 与平台信息（``parsed`` 必须已经填好）。"""
    parsed = work.parsed
    work.post_key = keys.content_key(getattr(parsed, "raw_url", "") or work.url)
    work.all_keys = tuple(dict.fromkeys([work.pre_key, work.post_key]))
    # 平台 id 以解析结果为准（client 可能拿不到），时长单位也跟着它。
    work.platform_id = str(
        getattr(getattr(parsed, "platform", None), "id", "") or work.platform
    )
    work.display = platforms.display_name(work.platform_id)


async def _apply_limits(
    app: App,
    sender: Sender,
    event: MessageEvent,
    work: _Work,
) -> Result | None:
    """时长上限、缓存二次命中、无媒体与数量截断；继续时返回 ``None``。"""
    settings = app.settings

    # 视频太长就只给封面 + 一句提示，别为几百 MB 的长片付下载和发送的代价。
    if (
        not work.music
        and settings.max_seconds
        and work.seconds > settings.max_seconds
    ):
        logger.info(
            f"[parsehub] 视频 {work.seconds}s 超过时长上限 "
            f"{settings.max_seconds}s，只发封面"
        )
        await message.send_card_or_text(
            app, sender, event, work.parsed, [], work.proxy
        )
        watch = platforms.watch_name(work.platform_id, work.display)
        return Result.fail(
            Outcome.TOO_LONG,
            notice=feedback.too_long_notice(watch),
            keys=work.all_keys,
        )

    # 短链没命中缓存（缓存里记的是展开后的 key），解析完再查一次。
    entry = await _hit_cache(app, work.post_key)
    if entry is not None:
        return await _deliver_cached(app, sender, event, entry, work.post_key)

    work.total = len(work.refs)
    if not work.refs:
        # 纯文字帖（比如 B 站动态）：没有媒体，只发卡片或文字
        await message.send_card_or_text(
            app, sender, event, work.parsed, [], work.proxy
        )
        return Result.fail(Outcome.NO_MEDIA, keys=work.all_keys)

    if work.total > MAX_MEDIA_ITEMS:
        # 在下载之前就截断：下载走的是 parsed.media，不截照样把上百张全拉下来
        work.refs = work.refs[:MAX_MEDIA_ITEMS]
        raw = getattr(work.parsed, "media", None)
        items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        work.parsed.media = items[:MAX_MEDIA_ITEMS]
    return None


async def _size_gate(app: App, work: _Work) -> Result | None:
    """单媒体时先探测体积，超上限就不下载；继续时返回 ``None``。"""
    if work.music or len(work.refs) != 1:
        return None
    oversized = await download.oversized_size(app, work.refs[0], work.proxy)
    if oversized:
        return Result.fail(
            Outcome.TOO_LARGE,
            notice=feedback.too_large_notice(oversized),
            keys=work.all_keys,
        )
    return None


async def _download_and_deliver(
    app: App,
    sender: Sender,
    event: MessageEvent,
    work: _Work,
) -> Result:
    """卡片与下载并行、投递、写缓存。"""
    assert work.budget is not None
    # 两者互不依赖：让卡片渲染 + 上传的时间藏进（本来就慢的）下载里。
    download_task = (
        download.download_music(
            app, work.url, work.proxy, work.cookie, work.budget
        )
        if work.music
        else download.download_media(
            app, work.url, work.proxy, work.cookie, work.budget, work.parsed
        )
    )
    info, (files, download_error) = await asyncio.gather(
        message.send_card_or_text(
            app, sender, event, work.parsed, [], work.proxy, allow_text=False
        ),
        download_task,
    )
    if files is None:
        if work.music and isinstance(download_error, audio.AudioTooLarge):
            return Result.fail(
                Outcome.TOO_LARGE,
                notice=feedback.audio_too_large_notice(
                    app.settings.max_file_bytes
                ),
                keys=work.all_keys,
            )
        return Result.fail(
            Outcome.DOWNLOAD_FAILED,
            notice=feedback.download_failed_notice(
                errors.translate(download_error or TimeoutError("下载超时"))
            ),
            keys=work.all_keys,
        )

    # 上面没出成卡（封面得从视频里抽帧）就带上下载好的文件再试一次
    if not info.sent:
        info = await message.send_card_or_text(
            app, sender, event, work.parsed, files, work.proxy
        )

    report = await app.delivery.send_media(sender, event, files)
    result = Result.delivered(report, work.all_keys)
    if work.total > MAX_MEDIA_ITEMS:
        # 截断提示走 notice，由统一的收尾逻辑发：媒体 → 提示 → 终态表情，
        # 不会抢在媒体前面。
        result.notice = feedback.truncated_notice(work.total, MAX_MEDIA_ITEMS)

    # 只缓存真正发出去的媒体：被体积上限挡掉的不该占缓存配额；部分失败的
    # 也不能写——下次命中会缺文件。
    if not report.failed:
        await asyncio.to_thread(
            app.cache.store,
            work.all_keys,
            [file.path for file in report.sent],
            card=info.card,
            caption=info.caption,
            seconds=work.seconds,
        )
    return result


async def _deliver_cached(
    app: App,
    sender: Sender,
    event: MessageEvent,
    entry: CacheEntry,
    key: str,
) -> Result:
    """把缓存里的卡片 / 文字 + 媒体直接发出去。

    卡片被关掉或发卡失败时，仍要发缓存里的文字——之前会丢掉正文的地方。
    """
    settings = app.settings
    await asyncio.to_thread(app.cache.remember_key, entry.root, key)

    card_sent = False
    if settings.card_enabled and entry.card is not None:
        card_sent = await app.delivery.send_card(sender, event, entry.card)
    if not card_sent and settings.send_text and entry.caption:
        await app.delivery.send_text(sender, event, entry.caption)

    files = [
        MediaFile(path=path, kind=media.kind_of(path)) for path in entry.files
    ]
    logger.info(f"[parsehub] 命中媒体缓存，直接发送 {len(files)} 个文件")
    report = await app.delivery.send_media(sender, event, files)
    all_keys = tuple(dict.fromkeys([key, *entry.keys]))
    return Result.delivered(report, all_keys, from_cache=True)
