# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""音频流水线：bm / ym 指令的取数、下载与投递。

B 站与 YouTube 的差异全部收进 :class:`AudioSource`：取数、命名、体积预估、
下载各一个函数，流水线本身是线性的一串。音频不设时长上限（纯音频远小于
视频），只按 ``max_file_bytes`` 体积上限；但取数与下载都要吃操作预算
（``PARSEHUB_TIMEOUT``），和常规视频下载同等待遇。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageEvent

from .. import audio, errors, feedback, media
from ..budget import Budget
from ..cache import CacheEntry
from ..delivery import MediaReport, Sender
from ..parsehub_client import platform_cookie
from .result import Outcome, Result

if TYPE_CHECKING:
    from ..app import App

# 两个平台的音频元信息。
type AudioInfo = audio.BiliAudio | audio.YtAudio

# 取数：async (target, proxy, cookie, cookie_dir, budget) -> AudioInfo
type _Fetch = Callable[[str, str, str, Path, Budget], Awaitable[AudioInfo]]
# 体积预估：(info) -> 字节数；None 表示这个平台估不出来
type _Estimate = Callable[[AudioInfo], int] | None
# 下载：async (info, task_dir, proxy, cookie, cookie_dir, budget, max_bytes)
# -> 落盘路径
type _Download = Callable[
    [AudioInfo, Path, str, str, Path, Budget, int], Awaitable[Path]
]


@dataclass(frozen=True)
class AudioSource:
    """一个平台的音频获取方式。

    Attributes:
        fetch: 拿元信息（自带超时预算）。
        estimate: 按码率 × 时长预估体积；估不出来为 ``None``。
        download: 下载到任务目录并返回最终路径。
    """

    fetch: _Fetch
    estimate: _Estimate
    download: _Download


async def _fetch_bilibili(
    target: str,
    proxy: str,
    cookie: str,
    _cookie_dir: Path,
    budget: Budget,
) -> AudioInfo:
    """B 站 playurl 接口取音频元信息（吃操作预算）。"""
    async with budget.timeout():
        return await asyncio.to_thread(audio.bilibili_audio, target, proxy)


async def _download_bilibili(
    info: audio.BiliAudio,
    task_dir: Path,
    proxy: str,
    cookie: str,
    _cookie_dir: Path,
    budget: Budget,
    max_bytes: int,
) -> Path:
    """下载 B 站音频到任务目录（吃操作预算；cookie 不参与）。"""
    stem = audio.file_stem(info.title, info.bvid, info.part)
    dest = task_dir / f"{stem}.m4a"
    async with budget.timeout():
        return Path(
            await asyncio.to_thread(
                audio.download_bilibili,
                info.audio_url,
                dest,
                proxy,
                max_bytes,
                max(0.1, budget.remaining),
            )
        )


async def _fetch_youtube(
    target: str,
    proxy: str,
    cookie: str,
    cookie_dir: Path,
    budget: Budget,
) -> AudioInfo:
    """yt-dlp 取 YouTube 音频元信息（吃操作预算）。"""
    async with budget.timeout():
        return await asyncio.to_thread(
            audio.youtube_audio,
            target,
            proxy,
            cookie,
            cookie_dir,
            max(0.1, budget.remaining),
        )


async def _download_youtube(
    info: audio.YtAudio,
    task_dir: Path,
    proxy: str,
    cookie: str,
    cookie_dir: Path,
    budget: Budget,
    max_bytes: int,
) -> Path:
    """yt-dlp 下载音频并命名成清洗后的标题。"""
    async with budget.timeout():
        path = await asyncio.to_thread(
            audio.download_youtube,
            info.webpage_url,
            task_dir,
            proxy,
            max(0.1, budget.remaining),
            max_bytes,
            cookie,
            cookie_dir,
        )
    # 命名成清洗后的标题（yt-dlp 落盘的是 <id>.<ext>）
    stem = audio.file_stem(info.title, info.video_id)
    named = path.with_name(f"{stem}{path.suffix}")
    try:
        await asyncio.to_thread(path.rename, named)
        return named
    except OSError as exc:
        logger.debug(f"[parsehub] 音频重命名失败（沿用原名）: {exc}")
        return path


_SOURCES: dict[str, AudioSource] = {
    "bilibili": AudioSource(
        fetch=_fetch_bilibili,
        estimate=lambda info: info.bandwidth // 8 * info.duration,
        download=_download_bilibili,
    ),
    "youtube": AudioSource(
        fetch=_fetch_youtube,
        estimate=None,
        download=_download_youtube,
    ),
}


def _audio_file(path: Path) -> media.MediaFile:
    """音频链路的文件固定按 record 投递。

    yt-dlp 在无 m4a 时可能落音频-only 的 ``.webm``，按扩展名会被判成
    video；音频链路已知是音频，不参与扩展名推断。
    """
    return media.MediaFile(path=path, kind=media.MediaKind.RECORD)


async def deliver_audio(
    app: App,
    sender: Sender,
    event: MessageEvent,
    target: str,
    platform: str,
    key: str,
    as_file: bool,
) -> Result:
    """在并发信号量内完成一次音频处理（不含表情与收尾）。

    Args:
        app: 插件实例。
        sender: 协议端接口。
        event: 触发消息事件。
        target: 目标链接（已归一成标准链接 / 短链）。
        platform: 平台 id（``bilibili`` / ``youtube``）。
        key: 音频缓存 key（带 ``audio:`` 前缀）。
        as_file: True 以群文件 / 私聊文件发送，否则发 record 语音段。

    Returns:
        本次处理的结局。
    """
    settings = app.settings
    source = _SOURCES[platform]

    # 缓存命中：直接把那份音频发出去
    cached = await asyncio.to_thread(app.cache.get, key)
    if cached is not None:
        return await _deliver_cached_audio(
            app, sender, event, cached, key, as_file
        )

    proxy = settings.proxy_for(platform)
    # cookies.toml 是同步磁盘读，放线程里
    cookie = await asyncio.to_thread(platform_cookie, platform, settings.cookie)
    cookie_dir = app.tmp_dir
    budget = Budget(settings.timeout)

    try:
        info = await source.fetch(target, proxy, cookie, cookie_dir, budget)
    except Exception as exc:
        logger.warning(
            f"[parsehub] 音频元信息获取失败 {target}: "
            f"{type(exc).__name__}: {exc}"
        )
        return Result.fail(
            Outcome.PARSE_FAILED,
            notice=feedback.audio_failed_notice(errors.translate(exc)),
            keys=(key,),
        )

    estimated = _estimated_bytes(app, source, info)
    if estimated is not None:
        logger.info(
            f"[parsehub] 音频预估 {estimated / 1024 / 1024:.0f}MB "
            f"超过上限，不下载: {target}"
        )
        return Result.fail(
            Outcome.TOO_LARGE,
            notice=feedback.audio_too_large_notice(estimated),
            keys=(key,),
        )

    downloaded = await _download_audio(
        app, source, info, target, proxy, cookie, cookie_dir, budget, key
    )
    if isinstance(downloaded, Result):
        return downloaded
    path = downloaded

    file = _audio_file(path)
    report = await _send_audio(app, sender, event, file, as_file)
    # 只缓存真正发出去的音频
    if report.ok:
        await asyncio.to_thread(
            app.cache.store, (key,), [path], seconds=info.duration
        )
    return Result.delivered(report, (key,))


async def _download_audio(
    app: App,
    source: AudioSource,
    info: AudioInfo,
    target: str,
    proxy: str,
    cookie: str,
    cookie_dir: Path,
    budget: Budget,
    key: str,
) -> Path | Result:
    """下载音频并复检实际体积，失败时给出对应结局。

    Args:
        app: 插件实例。
        source: 平台音频源。
        info: 元信息。
        target: 目标链接（日志用）。
        proxy: 代理地址。
        cookie: 平台 cookie。
        cookie_dir: 一次性 cookie 临时文件目录。
        budget: 操作预算。
        key: 音频缓存 key（失败结局要记去重窗口）。

    Returns:
        落盘路径；失败时为对应的 :class:`Result`。
    """
    settings = app.settings
    task_dir = await asyncio.to_thread(app.relay.new_task_dir)
    try:
        path = await source.download(
            info,
            task_dir,
            proxy,
            cookie,
            cookie_dir,
            budget,
            settings.max_file_bytes,
        )
    except audio.AudioTooLarge:
        return Result.fail(
            Outcome.TOO_LARGE,
            notice=feedback.audio_too_large_notice(settings.max_file_bytes),
            keys=(key,),
        )
    except Exception as exc:
        logger.warning(
            f"[parsehub] 音频下载失败 {target}: {type(exc).__name__}: {exc}"
        )
        return Result.fail(
            Outcome.DOWNLOAD_FAILED,
            notice=feedback.download_failed_notice(errors.translate(exc)),
            keys=(key,),
        )

    size = await _actual_bytes(path, settings.max_file_bytes)
    if size is not None:
        return Result.fail(
            Outcome.TOO_LARGE,
            notice=feedback.audio_too_large_notice(size),
            keys=(key,),
        )
    return path


async def _send_audio(
    app: App,
    sender: Sender,
    event: MessageEvent,
    file: media.MediaFile,
    as_file: bool,
) -> MediaReport:
    """发一段音频：``-f`` 走文件上传，否则按 record 语音段投递。

    Args:
        app: 插件实例。
        sender: 协议端接口。
        event: 触发消息事件。
        file: 待发送的本地音频。
        as_file: 是否以群文件 / 私聊文件发送。

    Returns:
        本次投递的结果。
    """
    if as_file:
        report = MediaReport()
        if await app.delivery.send_file_audio(sender, event, file):
            report.sent.append(file)
        return report
    return await app.delivery.send_media(sender, event, [file])


def _estimated_bytes(
    app: App,
    source: AudioSource,
    info: AudioInfo,
) -> int | None:
    """按码率 × 时长预估体积，判断是否超过上限。

    Args:
        app: 插件实例。
        source: 该平台的音频获取方式。
        info: 音频元信息。

    Returns:
        超过上限时返回预估值；没超、估不出或没开上限时为 ``None``。
    """
    limit = app.settings.max_file_bytes
    if not limit or source.estimate is None:
        return None
    estimated = source.estimate(info)
    if estimated > limit:
        return estimated
    return None


async def _actual_bytes(path: Path, limit: int) -> int | None:
    """下载完再核一次实际体积（B 站预估可能偏差）。

    Args:
        path: 落盘的音频文件。
        limit: 单文件体积上限（字节），0 表示不限。

    Returns:
        超过上限时返回实际体积；没超或读不到时为 ``None``。
    """
    try:
        actual = await asyncio.to_thread(path.stat)
        size = actual.st_size
    except OSError:
        return None
    if limit and size > limit:
        return size
    return None


async def _deliver_cached_audio(
    app: App,
    sender: Sender,
    event: MessageEvent,
    entry: CacheEntry,
    key: str,
    as_file: bool,
) -> Result:
    """把缓存里的音频按当次模式发出去。

    Args:
        app: 插件实例。
        sender: 协议端接口。
        event: 触发消息事件。
        entry: 缓存条目。
        key: 这次命中的 key。
        as_file: 是否以群文件 / 私聊文件发送。

    Returns:
        本次处理的结局。
    """
    await asyncio.to_thread(app.cache.remember_key, entry.root, key)
    files = [_audio_file(path) for path in entry.files]
    if not files:
        return Result.fail(Outcome.NO_MEDIA, keys=(key,))
    report = await _send_audio(app, sender, event, files[0], as_file)
    logger.info(f"[parsehub] 命中音频缓存: {key}")
    return Result.delivered(report, (key, *entry.keys), from_cache=True)
