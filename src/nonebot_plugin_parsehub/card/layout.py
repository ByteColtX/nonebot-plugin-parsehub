# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""卡片版式：把解析结果画成一张卡片图。

文字怎么画在 :mod:`text`，遮罩与 logo 在 :mod:`compose`，这里只保留版面本身：
尺寸、字号、渐变区间与三段式布局（封面 → 渐变 → 平台名 + 标题 + 元信息）。
全部同步阻塞，调用方必须放进线程池。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ..platforms import PLATFORMS, uses_milliseconds
from .compose import logo, shadowed_logo, vertical_mask
from .fonts import font_for
from .text import draw_text, fit_title

TYPE_LABELS: dict[str, str] = {
    "video": "视频",
    "image": "图集",
    "multimedia": "图文",
    "richtext": "图文",
    "live": "LIVE",
    "offline": "OFFLINE",
}

# ---- 版面参数（数值即渲染效果，改动会直接反映到成品上） ----
WIDTH = 1080  # 卡片宽度
INSET = 48  # 四周留白
TITLE_SIZE = 58  # 标题字号
META_SIZE = 27  # 元信息行字号
KICKER_SIZE = 27  # 平台名一行（kicker）字号
LOGO_HEIGHT = 54  # logo 高度
MIN_RATIO = 0.62  # 封面允许的最小宽高比
MAX_RATIO = 1.25  # 封面允许的最大宽高比

TITLE_LINE_HEIGHT = 70  # 标题行高
TITLE_META_GAP = 8  # 标题块与元信息行之间
META_BOTTOM = 34  # 元信息行下方预留（含 INSET 之外的呼吸空间）
FADE_RESERVE = 50  # 文字区上方的渐变额外预留
KICKER_TOP_GAP = 44  # 平台名一行相对标题块的上方间距
KICKER_TEXT_GAP = 18  # logo 右缘到平台名文字的横向间距
COVER_FOCUS = 0.28  # 封面裁剪聚焦在画面纵向 28% 处（主体通常偏上）

FADE_START = 0.34  # 渐变从封面 34% 高度处开始
FADE_PEAK = 200  # 文字区上缘的不透明度
FADE_BOTTOM = 252  # 渐变结束时的不透明度
FADE_COLOR = (6, 6, 10)  # 渐变颜色


def format_duration(seconds: int, *, is_ms: bool = False) -> str:
    """把时长格式化成 ``mm:ss`` / ``h:mm:ss``。

    Args:
        seconds: 时长。
        is_ms: 传进来的值是不是毫秒（抖音、TikTok 是毫秒）。

    Returns:
        格式化后的时长；无法解析时返回空串。
    """
    if seconds <= 0:
        return ""
    if is_ms:
        seconds //= 1000
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def platform_uses_milliseconds(result: Any) -> bool:
    """该平台的 ``duration`` 是不是毫秒。

    Args:
        result: ParseHub 解析结果。

    Returns:
        抖音 / 小红书返回 True。
    """
    platform_id = str(getattr(getattr(result, "platform", None), "id", ""))
    return uses_milliseconds(platform_id)


def platform_of(result: Any) -> tuple[str, tuple[int, int, int], str]:
    """取平台展示名、品牌色和 logo 名。

    Args:
        result: ParseHub 解析结果。

    Returns:
        ``(展示名, 品牌色, logo 文件名)``。
    """
    platform = getattr(result, "platform", None)
    key = str(getattr(platform, "id", "") or "").lower()
    if key in PLATFORMS:
        return PLATFORMS[key]
    name = str(getattr(platform, "display_name", "") or key or "未知平台")
    return name, (150, 150, 150), "x"


def meta_line(result: Any, media_count: int, is_ms: bool) -> str:
    """拼装元信息行（类型 · 时长 · 媒体数）。

    直播间卡片走另一套文案：``LIVE · 主播名 · 房间 22603245``，
    没有时长和媒体数可拼。

    Args:
        result: ParseHub 解析结果。
        media_count: 媒体数量。
        is_ms: 时长单位是不是毫秒。

    Returns:
        形如 ``视频 · 00:33`` 的一行文本。
    """
    post_type = str(getattr(getattr(result, "type", None), "value", ""))
    if post_type in ("live", "offline"):
        parts = [TYPE_LABELS[post_type]]
        author = str(getattr(result, "author", "") or "")
        room_id = getattr(result, "room_id", "")
        if author:
            parts.append(author)
        if room_id:
            parts.append(f"房间 {room_id}")
        return " · ".join(parts)

    parts = [TYPE_LABELS.get(post_type, "媒体")]
    media = getattr(result, "media", None)
    items = list(media) if isinstance(media, (list, tuple)) else [media]
    first = next((item for item in items if item), None)
    duration = format_duration(
        int(getattr(first, "duration", 0) or 0), is_ms=is_ms
    )
    if duration:
        parts.append(duration)
    if media_count > 1:
        parts.append(f"{media_count} 个媒体")
    return " · ".join(parts)


def render(
    result: Any,  # ParseHub 解析结果
    cover: bytes,
    out_path: Path,
) -> bool:
    """把解析结果渲染成卡片图。

    同步阻塞，调用方必须放进线程池。

    Args:
        result: ParseHub 解析结果。
        cover: 封面图字节。
        out_path: 输出文件路径（JPEG）。

    Returns:
        渲染成功为 ``True``。
    """
    from PIL import Image, ImageDraw

    name, _color, slug = platform_of(result)
    media = getattr(result, "media", None)
    items = [
        item
        for item in (
            list(media) if isinstance(media, (list, tuple)) else [media]
        )
        if item
    ]
    is_ms = platform_uses_milliseconds(result)

    cover_image = Image.open(io.BytesIO(cover)).convert("RGB")

    ratio = min(
        max(cover_image.height / cover_image.width, MIN_RATIO), MAX_RATIO
    )
    card_w = WIDTH
    card_h = int(card_w * ratio)

    scale = max(card_w / cover_image.width, card_h / cover_image.height)
    big = cover_image.resize(
        (int(cover_image.width * scale), int(cover_image.height * scale)),
        Image.BICUBIC,  # LANCZOS 在慢机器上要 150ms+
    )
    left = (big.width - card_w) // 2
    top = int((big.height - card_h) * COVER_FOCUS)
    pic = big.crop((left, top, left + card_w, top + card_h))

    title_font = font_for(TITLE_SIZE, 700)
    meta_font = font_for(META_SIZE, 400)
    kicker_font = font_for(KICKER_SIZE, 600)
    title_lines = fit_title(
        getattr(result, "title", "") or "(无标题)",
        title_font,
        card_w - INSET * 2,
        3,
        emoji_size=TITLE_SIZE,
    )
    meta = meta_line(result, len(items), is_ms)

    # 渐变要保证文字所在的位置已经够暗：文字上方到 FADE_PEAK，再往下到
    # FADE_BOTTOM
    text_top = (
        card_h
        - INSET
        - len(title_lines) * TITLE_LINE_HEIGHT
        - TITLE_META_GAP
        - META_BOTTOM
        - FADE_RESERVE
    )
    start = max(0, int(card_h * FADE_START))
    ramp_end = max(start + 1, text_top)
    upper = vertical_mask(
        (card_w, ramp_end), start, ramp_end, peak=FADE_PEAK, curve=1.25
    )
    lower = vertical_mask(
        (card_w, card_h - ramp_end),
        0,
        card_h - ramp_end,
        peak=FADE_BOTTOM,
        floor=FADE_PEAK,
    )
    layer = Image.new("RGB", (card_w, ramp_end), FADE_COLOR)
    pic.paste(layer, (0, 0), upper)
    layer = Image.new("RGB", (card_w, card_h - ramp_end), FADE_COLOR)
    pic.paste(layer, (0, ramp_end), lower)

    canvas = pic.convert("RGBA")
    mark = shadowed_logo(slug, LOGO_HEIGHT)
    pad = mark.width - logo(slug, LOGO_HEIGHT).width
    mark_w = mark.width - pad
    mark_h = mark.height - pad

    y = (
        card_h
        - INSET
        - len(title_lines) * TITLE_LINE_HEIGHT
        - TITLE_META_GAP
        - META_BOTTOM
    )
    kicker_y = y - KICKER_TOP_GAP
    # 图标与「平台名」那一行垂直居中，左边缘贴 INSET
    canvas.alpha_composite(
        mark,
        (
            INSET - pad // 2,
            kicker_y + KICKER_SIZE // 2 - mark_h // 2 - pad // 2,
        ),
    )

    draw = ImageDraw.Draw(canvas)
    draw_text(
        draw,
        (INSET + mark_w + KICKER_TEXT_GAP, kicker_y),
        # 保留官方大小写（YouTube / TikTok / bilibili），不要整行转大写
        name,
        kicker_font,
        (226, 226, 232),
        emoji_size=KICKER_SIZE,
    )
    for line in title_lines:
        draw_text(
            draw,
            (INSET, y),
            line,
            title_font,
            (255, 255, 255),
            emoji_size=TITLE_SIZE,
        )
        y += TITLE_LINE_HEIGHT
    draw_text(
        draw,
        (INSET, y + TITLE_META_GAP),
        meta,
        meta_font,
        (214, 214, 222),
        emoji_size=META_SIZE,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, "JPEG", quality=92)
    return True


def available() -> bool:
    """卡片功能是否可用（有字体、有 Pillow）。

    Returns:
        可用时为 ``True``。
    """
    try:
        font_for(TITLE_SIZE, 700)
    except Exception:  # 缺 Pillow 或缺字体都算不可用
        return False
    return True
