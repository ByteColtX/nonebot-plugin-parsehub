# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""封面获取：thumb_url → 图片媒体 → 本地图片直读 / 视频抽帧。

同步阻塞，调用方必须放进线程池。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import httpx
from nonebot import logger

from .fonts import _pil


def _is_placeholder(data: bytes, image: Any) -> bool:  # PIL Image
    """判断封面是不是纯色占位图。

    Args:
        data: 原始图片字节。
        image: 已解码的 RGB 图像。

    Returns:
        像占位图时为 ``True``。
    """
    if len(data) < 24_000:
        return True
    colors = image.resize((16, 16)).getcolors(16 * 16) or []
    return bool(colors) and max(count for count, _ in colors) > 16 * 16 * 0.9


def _frame_from_video(path: Path) -> bytes | None:
    """从本地视频里抽第一帧当封面。

    同步阻塞，调用方必须放进线程池。opencv 首次 import 要几百毫秒，所以延迟到
    函数里加载。

    Args:
        path: 本地视频路径。

    Returns:
        JPEG 字节；抽不出来时为 ``None``。
    """
    import cv2

    Image = _pil()  # noqa: N806 - 与 PIL 模块名保持一致
    cap = cv2.VideoCapture(str(path))
    try:
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok:
        return None
    buffer = io.BytesIO()
    Image.fromarray(frame[:, :, ::-1]).save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def fetch_cover(
    result: Any,
    local_files: list[Path],
    proxy: str = "",
) -> bytes | None:
    """取封面图，三级兜底。

    顺序：``thumb_url`` → 本身就是图片的媒体 → 本地图片 → 本地视频首帧。

    Args:
        result: ParseHub 解析结果。
        local_files: 已下载到本地的媒体文件。
        proxy: 取封面用的代理；海外平台（如 TikTok）的封面 CDN 直连会超时。

    Returns:
        JPEG/PNG 字节；都拿不到时为 ``None``。
    """
    Image = _pil()  # noqa: N806 - 与 PIL 模块名保持一致
    media = getattr(result, "media", None)
    items = [
        item
        for item in (
            list(media) if isinstance(media, (list, tuple)) else [media]
        )
        if item
    ]
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        with httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers=headers,
            proxy=proxy or None,
        ) as client:
            for item in items:
                url = getattr(item, "thumb_url", None)
                if not url:
                    continue
                resp = client.get(url)
                if resp.status_code != 200 or len(resp.content) < 1024:
                    continue
                try:
                    image = Image.open(io.BytesIO(resp.content)).convert("RGB")
                except Exception:  # 不是图片就换下一个来源
                    continue
                if not _is_placeholder(resp.content, image):
                    return resp.content
            for item in items:
                if getattr(item, "ext", "") not in (
                    "jpg",
                    "jpeg",
                    "png",
                    "webp",
                ):
                    continue
                url = getattr(item, "url", "")
                if not url:
                    continue
                resp = client.get(url)
                if resp.status_code == 200 and len(resp.content) > 24_000:
                    return resp.content
    except Exception as exc:  # 网络问题都算取封面失败
        logger.warning(f"[parsehub] 取封面失败: {type(exc).__name__}: {exc}")

    for path in local_files:
        if path.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm"):
            frame = _frame_from_video(path)
            if frame:
                return frame
        elif path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            try:
                return path.read_bytes()
            except OSError:
                continue
    return None
