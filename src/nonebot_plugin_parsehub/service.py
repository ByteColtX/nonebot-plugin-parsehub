# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""解析编排：从链接到「发出去什么」。

一次处理固定按这个顺序走，每一步的结局都是 :class:`Outcome` 里的一个值：

1. 平台白名单（命中才继续，否则连「收到」都不点）；
2. 会话去重（claim，失败不记窗口）；
3. 点「收到」表情；
4. 缓存命中（作品级，跨会话共享）；
5. 解析（共享超时预算）；
6. 时长上限（超了只发封面 + 提示）；
7. 下载（只在网络类失败时重试，超时不重试）；
8. 卡片与下载并行，卡片失败再用下载好的文件补一次；
9. 投递（体积过滤、合并转发、逐条兜底都在 delivery）；
10. 只把**真正发出去**的媒体写进缓存。

最后按结局点成功 / 失败表情，并把要补发给用户的话放在 ``Result.notice``。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

from . import errors, feedback, keys, media, platforms
from .budget import Budget
from .cache import CacheEntry
from .delivery import MediaReport, Sender
from .media import MAX_MEDIA_ITEMS, MediaFile, MediaRef
from .parsehub_client import platform_cookie
from .switches import group_key, private_key

if TYPE_CHECKING:
    from .app import App

# 下载失败重试次数与间隔：CDN 偶发 403 / 超时靠重试能救回来（重试会重新解析，
# 拿到的是新签名的地址）。
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY = 1.5


class Outcome(StrEnum):
    """一次处理的结局。"""

    DELIVERED = "delivered"
    FROM_CACHE = "from_cache"
    DUPLICATE = "duplicate"
    PLATFORM_OFF = "platform_off"
    UNSUPPORTED = "unsupported"
    PARSE_FAILED = "parse_failed"
    TOO_LONG = "too_long"
    TOO_LARGE = "too_large"
    NO_MEDIA = "no_media"
    DOWNLOAD_FAILED = "download_failed"
    DELIVERY_FAILED = "delivery_failed"
    INTERNAL_ERROR = "internal_error"


# 这些结局算「处理成功」，点成功表情。
_OK_OUTCOMES = frozenset(
    {
        Outcome.DELIVERED,
        Outcome.FROM_CACHE,
        Outcome.NO_MEDIA,
        Outcome.TOO_LONG,
    }
)


@dataclass
class Result:
    """一次处理的完整结果。

    Attributes:
        outcome: 结局。
        notice: 要补发给用户的一句话；没有为 None。
        report: 媒体投递明细（没走到投递时为 None）。
        keys: 这次处理涉及的作品 key（用于去重提交）。
    """

    outcome: Outcome
    notice: str | None = None
    report: MediaReport | None = None
    keys: tuple[str, ...] = field(default=(), repr=False)

    @property
    def ok(self) -> bool:
        """这次处理算不算成功。

        Returns:
            成功结局且媒体没有失败时为 True。
        """
        if self.outcome not in _OK_OUTCOMES:
            return False
        if self.report is not None and not self.report.ok:
            return False
        return True


@dataclass
class _SentInfo:
    """一次「卡片或文字」发送的结果，顺手带上内容好写进媒体缓存。

    Attributes:
        sent: 是否真的发出去了。
        card: 发出去的卡片图路径，没出卡为 ``None``。
        caption: 发出去的文字，没发文字为空串。
    """

    sent: bool = False
    card: Path | None = None
    caption: str = ""


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
    try:
        platform = await app.client.platform_id(url) or ""
    except Exception as exc:  # noqa: BLE001 - 平台识别失败按未知平台继续
        logger.warning(
            f"[parsehub] 平台识别失败 {url}: {type(exc).__name__}: {exc}"
        )
        platform = ""

    # 平台白名单：被关掉的平台连「收到」都不点，免得看起来像在解析。
    if platform and not settings.platform_allowed(platform):
        logger.info(f"[parsehub] {platform} 不在解析白名单里，跳过")
        notice = feedback.platform_off_notice(platform) if manual else None
        if notice:
            await app.delivery.send_text(sender, event, notice)
        return Result(Outcome.PLATFORM_OFF)

    pre_key = keys.content_key(url)
    if not manual and not app.dedupe.claim(
        session, [pre_key], settings.dedupe_seconds
    ):
        logger.info(f"[parsehub] {pre_key} 在本会话刚处理过，跳过")
        return Result(Outcome.DUPLICATE, keys=(pre_key,))

    await feedback.react(sender, event, feedback.Reaction.START)

    try:
        async with app.semaphore:
            result = await _deliver(
                app, sender, event, url, platform, session, pre_key, manual
            )
    except Exception as exc:  # noqa: BLE001 - 兜底：任何路径都要有终点
        logger.exception(f"[parsehub] 处理 {url} 时出现未预期异常: {exc}")
        result = Result(
            Outcome.INTERNAL_ERROR,
            notice=feedback.internal_error_notice(),
            keys=(pre_key,),
        )

    if result.notice:
        await app.delivery.send_text(sender, event, result.notice)
    # 重复是静默的：前面已经点过「收到」的话也到此为止，不再补一个失败表情。
    if result.outcome is not Outcome.DUPLICATE:
        reaction = feedback.Reaction.OK if result.ok else feedback.Reaction.FAIL
        await feedback.react(sender, event, reaction)

    # 手动命令不参与去重：既不拦也不记，和旧行为一致。
    # 窗口为 0 时 commit 只会让 _seen 无限增长，所以只 release。
    if not manual:
        if result.ok and settings.dedupe_seconds > 0:
            app.dedupe.commit(session, result.keys)
        else:
            app.dedupe.release(session, result.keys)
    logger.info(
        f"[parsehub] {result.outcome}（{platform or '未知平台'}）: {url}"
    )
    return result


async def _deliver(  # noqa: C901 - 顺序编排本身就是一条长流水线
    app: App,
    sender: Sender,
    event: MessageEvent,
    url: str,
    platform: str,
    session: str,
    pre_key: str,
    manual: bool,
) -> Result:
    """在并发信号量内完成一次处理（不含表情与去重收尾）。

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
    settings = app.settings

    # 本地已经有这个作品的媒体就直接发，省掉解析、下载和画卡。
    cached = await asyncio.to_thread(app.cache.get, pre_key)
    if cached is not None and not _cached_over_limit(cached, app):
        return await _deliver_cached(app, sender, event, cached, pre_key)

    try:
        supported = await app.client.is_supported(url)
    except Exception as exc:  # noqa: BLE001 - 判断失败就交给解析去报错
        logger.warning(
            f"[parsehub] 平台支持判断失败 {url}: {type(exc).__name__}: {exc}"
        )
        supported = True
    if not supported:
        notice = feedback.unsupported_notice() if manual else None
        return Result(Outcome.UNSUPPORTED, notice=notice, keys=(pre_key,))

    budget = Budget(settings.timeout)
    proxy = settings.proxy_for(platform)
    # cookies.toml 是同步磁盘读，放线程里，别卡住事件循环
    cookie = await asyncio.to_thread(platform_cookie, platform, settings.cookie)
    logger.debug(f"[parsehub] {platform} 代理={proxy or '直连'}")

    started = time.monotonic()
    try:
        async with budget.timeout():
            parsed = await app.client.parse(url, proxy, cookie)
    except Exception as exc:  # noqa: BLE001 - parsehub 异常类型很多，统一兜底
        failure = errors.translate(exc)
        logger.warning(
            f"[parsehub] 解析失败 {url}: {type(exc).__name__}: {exc}"
        )
        notice = None
        if manual or failure.kind is errors.ErrorKind.UNAVAILABLE:
            notice = feedback.failure_notice(exc)
        return Result(Outcome.PARSE_FAILED, notice=notice, keys=(pre_key,))

    post_key = keys.content_key(getattr(parsed, "raw_url", "") or url)
    all_keys = tuple(dict.fromkeys([pre_key, post_key]))
    # 平台 id 以解析结果为准（client 可能拿不到），时长单位也跟着它。
    platform_id = str(
        getattr(getattr(parsed, "platform", None), "id", "") or platform
    )
    display = platforms.display_name(platform_id)
    logger.info(
        f"[parsehub] {display} 解析成功"
        f"（耗时 {time.monotonic() - started:.1f}s）: {url}"
    )

    # 同一个作品的另一种写法（短链展开后变了）也要在会话内拦一次。
    if (
        not manual
        and post_key != pre_key
        and app.dedupe.seen(session, post_key, settings.dedupe_seconds)
    ):
        logger.info(f"[parsehub] {post_key} 在本会话刚发过，跳过发送")
        return Result(Outcome.DUPLICATE, keys=all_keys)

    refs = media.refs_from(parsed)
    seconds = platforms.duration_seconds(
        platform_id, (ref.duration for ref in refs)
    )

    # 视频太长就只给封面 + 一句提示，别为几百 MB 的长片付下载和发送的代价。
    if settings.max_seconds and seconds > settings.max_seconds:
        logger.info(
            f"[parsehub] 视频 {seconds}s 超过时长上限 "
            f"{settings.max_seconds}s，只发封面"
        )
        await _send_card_or_text(app, sender, event, parsed, [], None, proxy)
        watch = platforms.watch_name(platform_id, display)
        return Result(
            Outcome.TOO_LONG,
            notice=feedback.too_long_notice(watch),
            keys=all_keys,
        )

    # 短链没命中缓存（缓存里记的是展开后的 key），解析完再查一次。
    cached = await asyncio.to_thread(app.cache.get, post_key)
    if cached is not None:
        return await _deliver_cached(app, sender, event, cached, post_key)

    if not refs:
        # 纯文字帖（比如 B 站动态）：没有媒体，只发卡片或文字
        await _send_card_or_text(app, sender, event, parsed, [], None, proxy)
        return Result(Outcome.NO_MEDIA, keys=all_keys)

    total = len(refs)
    if total > MAX_MEDIA_ITEMS:
        # 在下载之前就截断：下载走的是 parsed.media，不截照样把上百张全拉下来
        refs = refs[:MAX_MEDIA_ITEMS]
        raw = getattr(parsed, "media", None)
        items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        parsed.media = items[:MAX_MEDIA_ITEMS]

    if len(refs) == 1:
        oversized = await _oversized_size(app, refs[0], proxy)
        if oversized:
            return Result(
                Outcome.TOO_LARGE,
                notice=feedback.too_large_notice(oversized),
                keys=all_keys,
            )

    # 卡片和下载并行：两者互不依赖（封面取不到时下载完再补一次），视频下载本来
    # 就慢，让卡片渲染 + 上传的时间藏进下载里。
    info, (files, download_error) = await asyncio.gather(
        _send_card_or_text(
            app,
            sender,
            event,
            parsed,
            [],
            None,
            proxy,
            allow_text=False,
        ),
        _download(app, url, proxy, cookie, budget, parsed),
    )
    if files is None:
        return Result(
            Outcome.DOWNLOAD_FAILED,
            notice=feedback.download_failed_notice(
                download_error or TimeoutError("下载超时")
            ),
            keys=all_keys,
        )

    # 上面没出成卡（封面得从视频里抽帧）就带上下载好的文件再试一次
    if not info.sent:
        info = await _send_card_or_text(
            app, sender, event, parsed, files, None, proxy
        )

    report = await app.delivery.send_media(sender, event, files)
    if total > MAX_MEDIA_ITEMS:
        await app.delivery.send_text(
            sender,
            event,
            feedback.truncated_notice(total, MAX_MEDIA_ITEMS),
        )

    # 只缓存真正发出去的媒体：被体积上限挡掉的不该占缓存配额。
    await asyncio.to_thread(
        app.cache.store,
        all_keys,
        [file.path for file in report.sent],
        card=info.card,
        caption=info.caption,
        seconds=seconds,
    )
    outcome = Outcome.DELIVERED if report.ok else Outcome.DELIVERY_FAILED
    return Result(outcome, report=report, keys=all_keys)


async def _deliver_cached(
    app: App,
    sender: Sender,
    event: MessageEvent,
    entry: CacheEntry,
    key: str,
) -> Result:
    """把缓存里的卡片 / 文字 + 媒体直接发出去。

    卡片被配置关掉、或者发卡失败时，仍然要发缓存里的文字——这是之前会丢掉正文
    的地方。

    Args:
        app: 插件实例。
        sender: 协议端接口。
        event: 触发消息事件。
        entry: 缓存条目。
        key: 这次命中的 key。

    Returns:
        本次处理的结局。
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
    outcome = Outcome.FROM_CACHE if report.ok else Outcome.DELIVERY_FAILED
    return Result(outcome, report=report, keys=all_keys)


def _cached_over_limit(entry: CacheEntry, app: App) -> bool:
    """缓存条目里记的时长是否超过上限。

    Args:
        entry: 缓存条目。
        app: 插件实例。

    Returns:
        超过上限返回 True；没开时长限制或没记录时长时为 False。
    """
    limit = app.settings.max_seconds
    if not limit:
        return False
    return entry.seconds > limit


async def _oversized_size(
    app: App,
    ref: MediaRef,
    proxy: str,
) -> int | None:
    """在下载前探测单媒体体积，判断是否超过上限。

    Args:
        app: 插件实例。
        ref: 媒体引用。
        proxy: 探测体积时使用的代理。

    Returns:
        超过上限时返回其字节数；没超过或探测不到时返回 None。
    """
    limit = app.settings.max_file_bytes
    if not limit:
        return None
    size = await media.probe_size(ref.source_url, proxy=proxy)
    if not size or size <= limit:
        return None
    logger.info(f"[parsehub] 媒体约 {size / 1024 / 1024:.0f}MB，超过上限")
    return size


async def _download(
    app: App,
    url: str,
    proxy: str,
    cookie: str,
    budget: Budget,
    parsed: Any,  # noqa: ANN401 - ParseHub 解析结果
) -> tuple[list[MediaFile] | None, BaseException | None]:
    """下载媒体到新的任务目录。

    只在网络类失败时重试；超时不重试——线程无法取消，再开一次只会多一份下载。

    Args:
        app: 插件实例。
        url: 待解析的链接。
        proxy: 解析与下载代理。
        cookie: 登录态 cookie。
        budget: 本次工作的超时预算。
        parsed: 已经解析好的结果，首次下载直接用它。

    Returns:
        ``(本地媒体列表, 失败原因)``；成功时失败原因为 ``None``。
    """
    last_error: BaseException | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        task_dir = app.relay.new_task_dir()
        started = time.monotonic()
        try:
            async with budget.timeout():
                if attempt == 1:
                    downloaded = await app.client.download_parsed(
                        parsed, str(task_dir), proxy
                    )
                else:
                    # 媒体直链签名每次都变，只有重解析才能拿到新签名
                    downloaded = await app.client.download(
                        url, str(task_dir), proxy, cookie
                    )
        except Exception as exc:  # noqa: BLE001 - 下载失败原因很多，统一兜底
            last_error = exc
            failure = errors.translate(exc)
            logger.warning(
                f"[parsehub] 下载失败（第 {attempt}/{DOWNLOAD_ATTEMPTS} 次）"
                f" {url}: {type(exc).__name__}: {exc}"
            )
            if (
                attempt < DOWNLOAD_ATTEMPTS
                and failure.retryable
                and not budget.expired
            ):
                await asyncio.sleep(DOWNLOAD_RETRY_DELAY)
                continue
            break
        logger.info(
            f"[parsehub] 下载完成，耗时 {time.monotonic() - started:.1f}s"
            f"（第 {attempt}/{DOWNLOAD_ATTEMPTS} 次尝试）"
        )
        return media.files_from(downloaded), None
    return None, last_error


async def _send_card_or_text(
    app: App,
    sender: Sender,
    event: MessageEvent,
    parsed: Any,  # noqa: ANN401 - ParseHub 解析结果
    files: list[MediaFile],
    task_dir: Path | None,
    proxy: str = "",
    *,
    allow_text: bool = True,
) -> _SentInfo:
    """优先发卡片图，卡片不可用时退回文字。

    Args:
        app: 插件实例。
        sender: 协议端接口。
        event: 触发消息事件。
        parsed: ParseHub 解析结果。
        files: 已下载的本地媒体。
        task_dir: 任务目录。
        proxy: 取封面用的代理。
        allow_text: 出不了卡时是否立刻退回文字。下载前那次调用要传 ``False``，
            否则文字先发了，等下载完再抽帧出卡就成了重复消息。

    Returns:
        发出去了什么（卡片路径 / 文字），用来写媒体缓存。
    """
    settings = app.settings
    if settings.card_enabled:
        out_path = await app.cards.render(
            parsed, [file.path for file in files], task_dir, proxy
        )
        if out_path is not None and await app.delivery.send_card(
            sender, event, out_path
        ):
            return _SentInfo(sent=True, card=out_path)
    if allow_text and settings.send_text:
        caption = _build_caption(parsed, settings.max_chars)
        await app.delivery.send_text(sender, event, caption)
        return _SentInfo(sent=True, caption=caption)
    return _SentInfo()


def _build_caption(parsed: Any, max_chars: int) -> str:  # noqa: ANN401
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
        lines.append(_excerpt(content, max_chars))
    return "\n".join(lines)


def _excerpt(text: str, limit: int) -> str:
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
