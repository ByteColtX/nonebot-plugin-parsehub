# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""ParseHub 调用封装。

ParseHub 全程同步阻塞（内部还用了 requests），所以所有调用都通过
``asyncio.to_thread`` 执行，避免阻塞 NoneBot 的事件循环。首次 import 会拉起
numpy / opencv（数秒），因此实例是懒加载的，并由 ``App`` 在启动时预热。

登录态 cookie 优先读 ParseHub 自己的 ``cookies.toml``（由
``ph set cookie <平台>`` 写入），没有再退回配置里的兜底值。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from nonebot import logger

# 多文件并发下载数（parsehub 2.3.10 起支持）。下载以网络等待为主，并发比串行快
# 很多；但内存只有 1G 上下的机器别开太大。
DOWNLOAD_CONCURRENCY = 8

# parsehub 2.3.15 的微博 GIF 解析把 AniRef.ext 固定成 "gif"，但 media_url
# 实际是 gif_mp4 的 .mp4；下载后按 .gif 调 PIL 会抛 UnidentifiedImageError。
# 只按 URL 后缀纠正，不猜没有后缀的链接。
_ANIMATED_VIDEO_SUFFIXES = frozenset({"mp4", "m4v", "mov", "webm"})


def _normalise_animated_ext(result: Any) -> None:
    """把「动图」引用里实际是视频的扩展名改对。

    Args:
        result: ParseHub 的解析结果。
    """
    media = getattr(result, "media", None)
    items = media if isinstance(media, (list, tuple)) else [media]
    for item in items:
        if not item or str(getattr(item, "ext", "")).lower() != "gif":
            continue
        suffix = Path(urlsplit(str(getattr(item, "url", "") or "")).path).suffix
        suffix = suffix.lower().lstrip(".")
        if suffix in _ANIMATED_VIDEO_SUFFIXES:
            item.ext = suffix


# 皮皮虾的 RENDER_DATA 里 note.multi_image 才是完整图集；ParseHub 2.3.15
# 只读了 cover.download_list，导致多图帖只解析出第一张。
_PIPIX_RENDER = re.compile(
    r'<script[^>]*\bid=["\']RENDER_DATA["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _pipix_images(html: str) -> list[tuple[str, int, int]]:
    """从皮皮虾详情页里取完整图集。

    Args:
        html: 详情页 HTML。

    Returns:
        ``(url, width, height)`` 列表；解析失败时为空列表。
    """
    match = _PIPIX_RENDER.search(html)
    if match is None:
        return []
    try:
        data = json.loads(unquote(match.group(1)))
    except (TypeError, ValueError):
        return []
    item = (data.get("ppxItemDetail") or {}).get("item") or {}
    note = item.get("note") or {}
    images: list[tuple[str, int, int]] = []
    for image in note.get("multi_image") or []:
        if not isinstance(image, dict):
            continue
        candidates = image.get("url_list") or image.get("download_list") or []
        url = next(
            (
                str(candidate.get("url"))
                for candidate in candidates
                if isinstance(candidate, dict) and candidate.get("url")
            ),
            "",
        )
        if not url:
            continue
        images.append(
            (
                url,
                int(image.get("width") or 0),
                int(image.get("height") or 0),
            )
        )
    return images


def _fetch_pipix_images(
    url: str, proxy: str = ""
) -> list[tuple[str, int, int]]:
    """同步抓取皮皮虾详情页并解析完整图集。

    Args:
        url: 详情页链接。
        proxy: 代理。

    Returns:
        ``(url, width, height)`` 列表。
    """
    with httpx.Client(
        timeout=30,
        follow_redirects=True,
        proxy=proxy or None,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        return _pipix_images(response.text)


async def _enrich_pipix(
    result: Any,
    url: str,
    proxy: str,
) -> None:
    """把皮皮虾多图帖补成完整图集。

    ParseHub 只从 ``cover.download_list`` 取一张；``note.multi_image`` 里才
    有全部图片。拿不到时保留原结果，不影响主流程。

    Args:
        result: ParseHub 解析结果。
        url: 原始链接。
        proxy: 解析代理。
    """
    if getattr(getattr(result, "platform", None), "id", "") != "pipix":
        return
    media = getattr(result, "media", None)
    current = list(media) if isinstance(media, (list, tuple)) else [media]
    current = [item for item in current if item]
    from parsehub.types import ImageRef

    if len(current) != 1 or not isinstance(current[0], ImageRef):
        return

    target = str(getattr(result, "raw_url", "") or url)
    try:
        images = await asyncio.to_thread(_fetch_pipix_images, target, proxy)
    except Exception as exc:  # 补图失败就退回 ParseHub 的原结果
        logger.warning(
            f"[parsehub] 皮皮虾图集补全失败 {target}: "
            f"{type(exc).__name__}: {exc}"
        )
        return
    if len(images) <= len(current):
        return

    result.media = [
        ImageRef(url=image_url, ext="jpg", width=width, height=height)
        for image_url, width, height in images
    ]
    logger.info(f"[parsehub] 皮皮虾图集补全为 {len(images)} 张")


def platform_cookie(platform: str, fallback: str) -> str:
    """取该平台要用的 cookie。

    Args:
        platform: ParseHub 的平台 id。
        fallback: 配置里提供的兜底 cookie。

    Returns:
        cookie 字符串；都没有时返回空字符串。
    """
    if platform:
        cookie = _read_cookie_file(platform)
        if cookie:
            return cookie
    return fallback or ""


def _read_cookie_file(platform: str) -> str:
    """从 ParseHub 的 cookies.toml 读取指定平台的 cookie。

    parsehub 的 import 代价很高，因此这里延迟导入，避免插件加载时就把它拉起来。

    Args:
        platform: ParseHub 的平台 id。

    Returns:
        文件里记录的 cookie；没读到返回空字符串。
    """
    try:
        from parsehub.cli_config import default_cookie_path

        path = default_cookie_path()
        if not path.exists():
            return ""
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, ValueError):
        return ""
    value = (data.get("cookie") or {}).get(platform)
    return str(value) if value else ""


class ParseHubClient:
    """ParseHub 的懒加载封装。

    首次调用 ``hub`` 时会在线程里导入 parsehub（numpy + opencv，数秒开销），
    之后复用同一个实例。

    Attributes:
        _hub: 缓存的 ParseHub 实例。
        _lock: 保护懒加载过程的锁。
    """

    def __init__(self) -> None:
        """初始化空的客户端。"""
        self._hub: Any | None = None
        self._lock = asyncio.Lock()

    async def hub(self) -> Any:
        """返回 ParseHub 实例，必要时先加载。

        Returns:
            已就绪的 ParseHub 实例。
        """
        if self._hub is None:
            async with self._lock:
                if self._hub is None:
                    self._hub = await asyncio.to_thread(_import_parsehub)
        return self._hub

    async def platforms(self) -> list[dict]:
        """列出 ParseHub 支持的平台。

        Returns:
            平台信息字典的列表，每项含 ``id`` / ``name`` / ``supported_types``。
        """
        hub = await self.hub()
        return await asyncio.to_thread(hub.get_platforms)

    async def is_supported(self, url: str) -> bool:
        """判断链接是否有对应的解析器。

        Args:
            url: 待检查的链接。

        Returns:
            有解析器返回 True。
        """
        hub = await self.hub()
        return await asyncio.to_thread(lambda: hub.get_parser(url) is not None)

    async def parse(
        self,
        url: str,
        proxy: str = "",
        cookie: str = "",
    ) -> Any:
        """解析链接或分享文案。

        Args:
            url: 链接或分享文案。
            proxy: 解析代理，空字符串表示直连。
            cookie: 登录态 cookie，空字符串表示不带。

        Returns:
            ParseHub 的解析结果对象。

        Raises:
            Exception: ParseHub 抛出的解析异常，由调用方决定如何提示用户。
        """
        hub = await self.hub()
        result = await asyncio.to_thread(
            hub.parse_sync,
            url,
            proxy=proxy or None,
            cookie=cookie or None,
        )
        _normalise_animated_ext(result)
        await _enrich_pipix(result, url, proxy)
        return result

    async def download(
        self,
        url: str,
        path: str,
        proxy: str = "",
        cookie: str = "",
        concurrency: int = DOWNLOAD_CONCURRENCY,
    ) -> Any:
        """解析并下载媒体到指定目录。

        Args:
            url: 链接或分享文案。
            path: 下载目录。
            proxy: 解析与下载共用的代理。
            cookie: 登录态 cookie。
            concurrency: 多文件并发下载数。

        Returns:
            ParseHub 的下载结果对象，``media`` 里是本地文件。

        Raises:
            Exception: 下载失败时由 ParseHub 抛出的异常。
        """
        result = await self.parse(url, proxy, cookie)
        return await self.download_parsed(result, path, proxy, concurrency)

    async def download_parsed(
        self,
        result: Any,
        path: str,
        proxy: str = "",
        concurrency: int = DOWNLOAD_CONCURRENCY,
    ) -> Any:
        """用已经解析好的结果下载媒体，不再解析一遍。

        ParseHub 的 ``download(url)`` 内部会重新解析一次（库源码：
        ``result = await self.parse(url, ...)`` 再 ``result.download(...)``）；
        手里已经有解析结果时走这里，省掉那次重复解析。

        注意：媒体直链的签名每次都变（实测同一链接解析两次，路径相同、签名不同），
        所以**重试**必须回到 ``download(url)`` 重新解析拿新签名。

        Args:
            result: ``parse()`` 返回的解析结果。
            path: 下载目录。
            proxy: 下载代理。
            concurrency: 多文件并发下载数。

        Returns:
            ParseHub 的下载结果对象，``media`` 里是本地文件。

        Raises:
            Exception: 下载失败时由 ParseHub 抛出的异常。
        """
        _normalise_animated_ext(result)
        return await asyncio.to_thread(
            result.download_sync,
            path,
            proxy=proxy or None,
            concurrency=concurrency,
        )


def ffmpeg_exe() -> str | None:
    """找可用的 ffmpeg：系统装的优先，其次 ``imageio-ffmpeg`` 自带的。

    Returns:
        可执行文件路径；都没有时返回 ``None``。
    """
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # 没装 bundle / 平台不支持
        return None


def _ensure_ffmpeg_available() -> str | None:
    """确保 yt-dlp 能找到 ffmpeg，否则音视频不会合并。

    ParseHub 的 yt-dlp 下载默认走 ``bestvideo+bestaudio``，没有 ffmpeg 时
    会落成两个文件，随后只取到 video-only 的那个——用户看到的就是无声视频，
    或者把 audio-only 当视频发出去。系统没装 ffmpeg 时用 ``imageio-ffmpeg``
    自带的二进制，并给下载命令注入 ``--ffmpeg-location``。

    Returns:
        实际使用的 ffmpeg 路径；不需要或拿不到时为 ``None``。
    """
    system = shutil.which("ffmpeg")
    if system:
        return system
    exe = ffmpeg_exe()
    if exe is None:
        logger.warning("[parsehub] 找不到 ffmpeg，音视频可能无法合并")
        return None

    try:
        from parsehub.parsers.base.ytdlp import YtVideoParseResult
        from parsehub.parsers.parser.youtube import YtbVideoParseResult
    except Exception as exc:  # parsehub 版本变了就退回原行为
        logger.warning(f"[parsehub] 无法注入 ffmpeg 路径: {exc}")
        return None

    for cls in (YtVideoParseResult, YtbVideoParseResult):
        if getattr(cls, "_parsehub_ffmpeg_patched", False):
            continue
        original = cls.cli_args.fget
        if original is None:
            continue

        def cli_args(
            self: Any,
            _original: Any = original,
            _exe: str = exe,
        ) -> list[str]:
            return [*_original(self), "--ffmpeg-location", _exe]

        cls.cli_args = property(cli_args)
        cls._parsehub_ffmpeg_patched = True
    logger.info(f"[parsehub] 使用内置 ffmpeg: {exe}")
    return exe


def _import_parsehub() -> Any:
    """在线程里导入 parsehub 并构造实例。

    Returns:
        ParseHub 实例。
    """
    _ensure_ffmpeg_available()
    from parsehub import ParseHub

    return ParseHub()
