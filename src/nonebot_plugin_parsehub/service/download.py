# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""作品流水线的下载侧：下载重试与下载前的体积预检。

parsehub 是同步阻塞库，下载与探测都放进线程。重试策略只在这里：只在网络类
失败时重试，超时不重试——线程无法取消，再开一次只会多一份下载。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from nonebot import logger

from .. import audio, errors, media
from ..budget import Budget
from ..media import MediaFile, MediaKind, MediaRef

if TYPE_CHECKING:
    from ..app import App

# 下载失败重试次数与间隔：CDN 偶发 403 / 超时靠重试能救回来（重试会重新解析，
# 拿到的是新签名的地址）。
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY = 1.5


async def oversized_size(
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
        超过上限时返回其字节数；没超过或探测不到时返回 ``None``。
    """
    limit = app.settings.max_file_bytes
    if not limit:
        return None
    size = await media.probe_size(ref.source_url, proxy=proxy)
    if not size or size <= limit:
        return None
    logger.info(f"[parsehub] 媒体约 {size / 1024 / 1024:.0f}MB，超过上限")
    return size


async def download_media(
    app: App,
    url: str,
    proxy: str,
    cookie: str,
    budget: Budget,
    parsed: Any,  # ParseHub 解析结果
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
        task_dir = await asyncio.to_thread(app.relay.new_task_dir)
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
        except Exception as exc:  # 下载失败原因很多，统一兜底
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


async def download_music(
    app: App,
    url: str,
    proxy: str,
    cookie: str,
    budget: Budget,
) -> tuple[list[MediaFile] | None, BaseException | None]:
    """把 YouTube Music 链接下载成一条音频。

    ParseHub 把 music.youtube.com 也当视频解析，但这里只要音频：用 yt-dlp
    的 ``bestaudio``，落盘后按 record 投递。

    Args:
        app: 插件实例。
        url: YouTube Music 链接。
        proxy: 下载代理。
        cookie: 登录态 cookie。
        budget: 本次工作的超时预算。

    Returns:
        ``(本地音频列表, 失败原因)``；成功时失败原因为 ``None``。
    """
    task_dir = await asyncio.to_thread(app.relay.new_task_dir)
    try:
        async with budget.timeout():
            info = await asyncio.to_thread(
                audio.youtube_audio,
                url,
                proxy,
                cookie,
                app.tmp_dir,
                max(0.1, budget.remaining),
            )
        async with budget.timeout():
            path = await asyncio.to_thread(
                audio.download_youtube,
                info.webpage_url,
                task_dir,
                proxy,
                max(0.1, budget.remaining),
                app.settings.max_file_bytes,
                cookie,
                app.tmp_dir,
            )
    except Exception as exc:  # 取数 / 下载失败都交给上层统一提示
        return None, exc

    # 文件名用清洗后的标题，缓存和群文件里更好认。
    stem = audio.file_stem(info.title, info.video_id)
    named = path.with_name(f"{stem}{path.suffix}")
    try:
        await asyncio.to_thread(path.rename, named)
        path = named
    except OSError:
        pass
    return [
        MediaFile(path=path, kind=MediaKind.RECORD, duration=info.duration)
    ], None
