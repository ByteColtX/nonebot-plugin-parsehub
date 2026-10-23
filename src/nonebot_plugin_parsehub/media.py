# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""媒体类型、下载结果收集与体积探测。

ParseHub 的解析结果给的是远程引用（``MediaRef``），下载结果给的是本地文件
（``MediaFile``）。这里负责两者之间的转换与类型判断，让上层只处理一种形态。

模块不依赖 nonebot。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

# 判断媒体类型用的后缀集合。
IMAGE_EXTENSIONS = frozenset(
    {
        ".avif",
        ".bmp",
        ".gif",
        ".heic",
        ".ico",
        ".jpeg",
        ".jpg",
        ".png",
        ".webp",
    }
)
VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".flv",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".ts",
        ".webm",
        ".wmv",
    }
)

# 一次最多发多少个媒体。图集动辄上百张，而每张都要等协议端上传到 QQ（实测约
# 2s/张且串行），下载也慢：全发既会顶穿总超时，也会把合并转发拖到超时后逐条
# 刷屏。超出部分直接不发，只发前 MAX_MEDIA_ITEMS 个。
MAX_MEDIA_ITEMS = 20

# 探测文件体积的超时（秒）。
PROBE_TIMEOUT_SECONDS = 15.0


class MediaKind(StrEnum):
    """媒体在消息里的投递方式。"""

    IMAGE = "image"
    VIDEO = "video"
    FILE = "file"


@dataclass(frozen=True)
class MediaRef:
    """解析结果里的远程媒体引用。

    Attributes:
        source_url: 媒体直链（签名会变）。
        thumb_url: 封面地址，可能为空。
        ext: 扩展名（不带点），可能为空。
        duration: 上报的时长原始值，单位由平台决定。
    """

    source_url: str = ""
    thumb_url: str = ""
    ext: str = ""
    duration: int = 0


@dataclass(frozen=True)
class MediaFile:
    """已下载到本地的媒体文件。

    Attributes:
        path: 本地路径。
        kind: 投递方式。
        duration: 时长（秒），未知为 0。
        live_photo: 是否来自实况照片的短视频。
    """

    path: Path
    kind: MediaKind
    duration: int = 0
    live_photo: bool = False


def kind_of(path: Path) -> MediaKind:
    """按扩展名判断投递方式。

    Args:
        path: 本地文件路径。

    Returns:
        图片 / 视频 / 其他文件。
    """
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return MediaKind.IMAGE
    if suffix in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    return MediaKind.FILE


def _items(value: Any) -> list[Any]:  # parsehub 的结果对象
    """把可能为单个值的 ``media`` 统一成列表。

    Args:
        value: ``result.media`` 或 ``downloaded.media``。

    Returns:
        去掉空值后的列表。
    """
    if isinstance(value, (list, tuple)):
        return [item for item in value if item]
    return [value] if value else []


def refs_from(result: Any) -> list[MediaRef]:  # parsehub 结果
    """把解析结果里的媒体转成 :class:`MediaRef`。

    Args:
        result: ParseHub 的解析结果。

    Returns:
        远程媒体引用列表。
    """
    refs: list[MediaRef] = []
    for item in _items(getattr(result, "media", None)):
        try:
            duration = int(getattr(item, "duration", 0) or 0)
        except (TypeError, ValueError):
            duration = 0
        refs.append(
            MediaRef(
                source_url=str(getattr(item, "url", "") or ""),
                thumb_url=str(getattr(item, "thumb_url", "") or ""),
                ext=str(getattr(item, "ext", "") or "").lstrip(".").lower(),
                duration=duration,
            )
        )
    return refs


def files_from(downloaded: Any) -> list[MediaFile]:  # 下载结果
    """把下载结果里的本地媒体收集成列表。

    实况照片会同时产出图片和一段短视频，两个都要发，否则实况照片会退化成静态图。

    Args:
        downloaded: ParseHub 的 DownloadResult。

    Returns:
        本地媒体列表，可能为空。
    """
    files: list[MediaFile] = []
    for item in _items(getattr(downloaded, "media", None)):
        path = getattr(item, "path", None)
        if path:
            file = Path(path)
            files.append(MediaFile(path=file, kind=kind_of(file)))
        video_path = getattr(item, "video_path", None)
        if video_path:
            video = Path(video_path)
            files.append(
                MediaFile(path=video, kind=MediaKind.VIDEO, live_photo=True)
            )
    return files


async def probe_size(
    url: str,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    proxy: str = "",
) -> int | None:
    """在下载前尽量问出文件体积。

    先试 HEAD，部分 CDN 不支持，再用 ``Range: bytes=0-0`` 从 Content-Range
    里取总长度。任何失败都视为未知，不影响主流程。

    Args:
        url: 媒体直链。
        timeout: 单次请求超时（秒）。
        proxy: 需要走代理时传入代理地址。

    Returns:
        文件字节数；探测不出来时返回 None。
    """
    if not url:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy or None,
        ) as client:
            response = await client.head(url)
            length = response.headers.get("content-length", "")
            if response.status_code < 400 and length.isdigit():
                return int(length)

            response = await client.get(url, headers={"Range": "bytes=0-0"})
            content_range = response.headers.get("content-range", "")
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None

    tail = content_range.rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None
