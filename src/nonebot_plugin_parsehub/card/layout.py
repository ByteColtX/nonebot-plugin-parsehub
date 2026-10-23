# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""卡片版式：排版、绘制与渲染。

全部是同步阻塞的 CPU 活（Pillow），调用方必须放进线程池。
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..platforms import PLATFORMS, duration_seconds, uses_milliseconds
from .fonts import (
    _emoji_strike_font,
    _fallback_fonts,
    _font,
    _has_glyph,
)

# logo 资源在包根目录的 assets/logos 下（card 包比原单文件深一层）。
ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "logos"

EMOJI_DROP = 0.06

# 算作 emoji 的码点区间（含 ZWJ、变体符、区域指示符，组合序列不会被切开）。
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),
    (0x1F1E6, 0x1F1FF),
    (0x2600, 0x27BF),
    (0x2300, 0x23FF),
    (0x25A0, 0x25FF),
    (0x2B00, 0x2BFF),
    (0x2190, 0x21FF),
    (0x200D, 0x200D),
    (0xFE00, 0xFE0F),
    (0x20E3, 0x20E3),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x24C2, 0x24C2),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
)

TYPE_LABELS: dict[str, str] = {
    "video": "视频",
    "image": "图集",
    "multimedia": "图文",
    "richtext": "图文",
}

# 版面参数
WIDTH = 1080
INSET = 48
TITLE_SIZE = 58
META_SIZE = 27
KICKER_SIZE = 27
LOGO_HEIGHT = 54
MIN_RATIO = 0.62
MAX_RATIO = 1.25
FADE_START = 0.34
FADE_PEAK = 200
FADE_BOTTOM = 252
FADE_COLOR = (6, 6, 10)


EMOJI_RATIO = 0.92


# emoji 底边落在基线下多少（占字号），跟文字重心对齐
def _is_emoji_char(ch: str) -> bool:
    """这个字符是不是 emoji 码点。

    Args:
        ch: 单个字符。

    Returns:
        落在 emoji 区间里为 ``True``。
    """
    code = ord(ch)
    return any(low <= code <= high for low, high in _EMOJI_RANGES)


def _font_runs(
    text: str,
    font: Any,  # PIL FreeTypeFont
    emoji_size: int = 0,
) -> list[tuple[str, Any]]:
    """把文本切成 (片段, 用哪个字体) 的序列。

    字体为 ``None`` 表示这一段要用彩色 emoji 贴图。逐字符挑第一个有字形的字体：
    主字体 → 彩色 emoji（emoji 码点）→ 回退字体；都没有就还回主字体
    （会画成方块）。

    Args:
        text: 文本。
        font: 主字体对象。
        emoji_size: emoji 字号；0 表示不处理 emoji。

    Returns:
        ``(片段, 字体对象或 None)`` 的列表，相邻同字体的片段已合并。
    """
    size = int(getattr(font, "size", 0) or emoji_size or 0)
    emoji = _emoji_strike_font() if emoji_size else None
    fallbacks = _fallback_fonts(size) if size else ()
    runs: list[tuple[str, Any]] = []
    for chunk, is_emoji in _split_emoji(text):
        if is_emoji:
            # 变体选择符只是"按图形呈现"的提示，emoji 字体不需要它；有些
            # FreeType 会把它当整字宽的可见字形，把 emoji 之间的间距撑得很大。
            chunk = _strip_variation(chunk)
        if is_emoji and emoji is not None and _has_glyph(emoji, chunk):
            runs.append((chunk, None))
            continue
        for ch in chunk:
            slot = font
            if not ch.isspace() and not _has_glyph(font, ch):
                for candidate in fallbacks:
                    if _has_glyph(candidate, ch):
                        slot = candidate
                        break
            if runs and runs[-1][1] is slot:
                runs[-1] = (runs[-1][0] + ch, slot)
            else:
                runs.append((ch, slot))
    return runs


def _advance(text: str, font: Any, emoji_size: int = 0) -> float:
    """算一段文字实际画出来有多宽（跟 :func:`draw_text` 的推进保持一致）。

    Args:
        text: 文本。
        font: 主字体对象。
        emoji_size: emoji 字号；0 表示不处理 emoji。

    Returns:
        宽度（像素）。
    """
    total = 0.0
    for chunk, slot in _font_runs(text, font, emoji_size):
        if slot is None:
            tile = _emoji_tile(chunk, emoji_size)
            if tile:
                total += tile[3]
            else:
                total += font.getlength(chunk)
        else:
            total += slot.getlength(chunk)
    return total


def _strip_variation(text: str) -> str:
    """去掉变体选择符（U+FE0E / U+FE0F）。

    它们只影响"按文字还是按图形呈现"，画 emoji 时不需要；留着反而会被某些
    FreeType 当成整字宽的可见字形，把间距撑开。

    Args:
        text: 文本。

    Returns:
        去掉变体选择符的文本。
    """
    return text.replace("\ufe0f", "").replace("\ufe0e", "")


def _split_emoji(text: str) -> list[tuple[str, bool]]:
    """把文本切成 (片段, 是否 emoji) 的序列。

    Args:
        text: 原始文本。

    Returns:
        片段列表，相邻同类字符合并，组合序列（ZWJ、变体符）不会被切开。
    """
    out: list[tuple[str, bool]] = []
    buf = ""
    flag: bool | None = None
    for ch in text:
        cur = _is_emoji_char(ch)
        if flag is None or cur == flag:
            buf += ch
        else:
            out.append((buf, flag))
            buf = ch
        flag = cur
    if buf and flag is not None:
        out.append((buf, flag))
    return out


@lru_cache(maxsize=256)
def _emoji_tile(chunk: str, size: int) -> tuple[Any, int, int, float]:
    """把一段 emoji 画在透明小图上，缩到目标大小。

    位图字体只有一档，所以先按档位画，再按比例缩到目标高度。推进宽度用字体自己的
    advance（含左右边距），不然 emoji 会贴着旁边的字。

    个别字体单独画一个 emoji 不上色，这时垫一个空格再画（空格画到裁剪区外）。

    Args:
        chunk: emoji 片段。
        size: 正文字号。

    Returns:
        ``(透明小图, 基线在图中 y, 墨迹左偏移, 推进宽度)``；不可用时为空元组。
    """
    font = _emoji_strike_font()
    if font is None:
        return ()
    from PIL import Image, ImageDraw

    ascent, descent = font.getmetrics()
    baseline = ascent + 2
    width = max(1, int(round(font.getlength(chunk))))
    canvas = Image.new("RGBA", (width + 8, ascent + descent + 8), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text(
        (0, baseline),
        chunk,
        font=font,
        fill=(255, 255, 255),
        anchor="ls",
        embedded_color=True,
    )
    box = canvas.getbbox()
    if box is None:
        # 单独画不上色：垫一个空格（画到裁剪区外）再来一次
        pad = font.getlength(" ") or width
        canvas = Image.new(
            "RGBA", (width + int(pad) + 8, ascent + descent + 8), (0, 0, 0, 0)
        )
        ImageDraw.Draw(canvas).text(
            (-pad, baseline),
            " " + chunk,
            font=font,
            fill=(255, 255, 255),
            anchor="ls",
            embedded_color=True,
        )
        box = canvas.getbbox()
        if box is None:
            return ()
    art = canvas.crop(box)
    target_h = max(1, int(round(size * EMOJI_RATIO)))
    scale = target_h / art.height
    art = art.resize(
        (max(1, int(round(art.width * scale))), target_h), Image.LANCZOS
    )
    return (
        art,
        target_h - int(round(size * EMOJI_DROP)),
        int(round(box[0] * scale)),
        max(1.0, font.getlength(chunk) * scale),
    )


def draw_text(
    draw: Any,  # PIL ImageDraw
    xy: tuple[int, int],
    text: str,
    font: Any,  # PIL FreeTypeFont
    fill: tuple[int, int, int],
    emoji_size: int = 0,
) -> None:
    """画一行文字：emoji 段用彩色字体贴上去，主字体缺的字用回退字体补。

    字体链见 :func:`_font_runs`。没有 emoji 字体、也没有回退字体时退化成普通的
    ``draw.text``（缺字会显示成方块）。

    Args:
        draw: ImageDraw 对象。
        xy: 起始坐标（左上角，锚点同 ``draw.text`` 的默认值）。
        text: 文本。
        font: 正文字体。
        fill: 颜色。
        emoji_size: emoji 字号；0 表示不做 emoji 处理。
    """
    x, top = xy
    baseline = top + font.getmetrics()[0]
    for chunk, slot in _font_runs(text, font, emoji_size):
        if slot is None:
            tile = _emoji_tile(chunk, emoji_size)
            if tile:
                image, baseline_in_tile, left, advance = tile
                image_draw = getattr(draw, "_image", None)
                if image_draw is None:  # pragma: no cover - 都有这个属性
                    image_draw = draw.im
                image_draw.paste(
                    image,
                    (int(x + left), int(baseline - baseline_in_tile)),
                    image,
                )
                x += advance
                continue
            slot = font  # 贴图失败就按普通文字画，至少不丢字
        draw.text((x, top), chunk, font=slot, fill=fill)
        x += slot.getlength(chunk)


def _flat(text: str) -> str:
    """去掉所有空白，用来比较有没有漏字。

    Args:
        text: 原始文本。

    Returns:
        去掉空白后的文本。
    """
    return "".join(text.split())


def wrap(
    text: str,
    font: Any,  # PIL FreeTypeFont
    max_w: int,
    max_lines: int,
    emoji_size: int = 0,
) -> list[str]:
    """按"词"折行，话题标签和网址不会被从中间劈开。

    Args:
        text: 原始文本。
        font: 字体对象。
        max_w: 每行最大像素宽度。
        max_lines: 最多行数，超出时最后一行加省略号。
        emoji_size: emoji / 回退字体用的字号；0 表示按主字体算宽度。

    Returns:
        折行后的文本列表。
    """
    import re

    def width(part: str) -> float:
        return _advance(part, font, emoji_size)

    lines: list[str] = []
    for para in text.split("\n"):
        if not para:
            continue
        cur = ""
        for token in re.findall(r"\S+\s*", para):
            if width(cur + token) <= max_w:
                cur += token
                continue
            if cur.strip():
                lines.append(cur.rstrip())
                cur = ""
                if len(lines) == max_lines:
                    break
            if width(token) > max_w:  # 单个词就超宽，只能按字符切
                piece = ""
                for char in token:
                    if width(piece + char) <= max_w:
                        piece += char
                    else:
                        lines.append(piece)
                        piece = char
                        if len(lines) == max_lines:
                            break
                cur = piece
            else:
                cur = token
            if len(lines) == max_lines:
                break
        if len(lines) == max_lines:
            break
        if cur.strip():
            lines.append(cur.rstrip())
    lines = lines[:max_lines]
    # 行尾空格会被 rstrip 掉，比较时先去空白，否则会误判成"被截断了"
    if lines and len(_flat("".join(lines))) < len(_flat(text)):
        lines[-1] = lines[-1][: max(1, len(lines[-1]) - 1)] + "…"
    return lines


def fit_title(
    title: str,
    font: Any,  # PIL FreeTypeFont
    max_w: int,
    max_lines: int,
    emoji_size: int = 0,
) -> list[str]:
    """标题折行；末尾的话题标签放不下就整条丢掉，不从中间截断。

    Args:
        title: 标题。
        font: 字体对象。
        max_w: 每行最大像素宽度。
        max_lines: 最多行数。
        emoji_size: emoji / 回退字体用的字号；0 表示按主字体算宽度。

    Returns:
        折行后的标题。
    """
    lines = wrap(title, font, max_w, max_lines, emoji_size)
    if len(_flat("".join(lines).rstrip("…"))) >= len(_flat(title)):
        return lines
    tokens = title.split(" ")
    while len(tokens) > 1 and tokens[-1].startswith("#"):
        tokens.pop()
        trial = " ".join(tokens)
        trial_lines = wrap(trial, font, max_w, max_lines, emoji_size)
        if len(_flat("".join(trial_lines).rstrip("…"))) >= len(_flat(trial)):
            return trial_lines
    return lines


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
    if is_ms or seconds > 10800:
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


def video_seconds(result: Any) -> int:
    """取结果里最长媒体的时长（秒）。

    上游单位不统一：毫秒（抖音 / 小红书）要除 1000；另外把明显过大的
    值（> 3 小时）也当毫秒处理，与 :func:`format_duration` 的显示规则一致。
    拿不到时长时返回 0，调用方据此跳过时长判断。

    Args:
        result: ParseHub 解析结果。

    Returns:
        最长媒体的时长（秒）；无法判断时返回 0。
    """
    platform_id = str(getattr(getattr(result, "platform", None), "id", ""))
    media = getattr(result, "media", None)
    items = [
        item
        for item in (
            list(media) if isinstance(media, (list, tuple)) else [media]
        )
        if item
    ]
    return duration_seconds(
        platform_id, (getattr(item, "duration", 0) for item in items)
    )


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

    Args:
        result: ParseHub 解析结果。
        media_count: 媒体数量。
        is_ms: 时长单位是不是毫秒。

    Returns:
        形如 ``视频 · 00:33`` 的一行文本。
    """
    post_type = getattr(getattr(result, "type", None), "value", "")
    parts = [TYPE_LABELS.get(str(post_type), "媒体")]
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


def _vertical_mask(
    size: tuple[int, int],
    start: int,
    end: int,
    *,
    peak: int = 255,
    floor: int = 0,
    curve: float = 1.0,
) -> Any:  # PIL Image
    """用 numpy 一次算出竖直渐变遮罩。

    逐行画线会长时间占着 GIL，放线程里也会把事件循环卡住，所以整张图一次性
    拼好再输出。

    Args:
        size: 遮罩尺寸 ``(宽, 高)``。
        start: 开始变深的位置。
        end: 达到 ``peak`` 的位置。
        peak: 最大不透明度。
        floor: 起始不透明度。
        curve: 曲线指数，越大起步越缓。

    Returns:
        ``L`` 模式的遮罩图。
    """
    import numpy as np
    from PIL import Image

    width, height = size
    ys = np.arange(height, dtype=np.float32)
    span = max(1, end - start)
    ramp = np.clip((ys - start) / span, 0.0, 1.0)
    alpha = np.where(
        ys < start, 0, floor + (peak - floor) * ramp**curve
    ).astype(np.uint8)
    return Image.fromarray(alpha.reshape(-1, 1), mode="L").resize(
        (width, height), Image.BILINEAR
    )


@lru_cache(maxsize=32)
def _logo(slug: str, height: int) -> Any:  # PIL Image
    """读 logo 并缩放（带缓存）。

    Args:
        slug: logo 文件名（不含扩展名）。
        height: 目标高度。

    Returns:
        RGBA 图像；文件不存在时用通用占位。
    """
    from PIL import Image

    path = ASSET_DIR / f"{slug}.png"
    if not path.exists():
        path = ASSET_DIR / "x.png"
    logo = Image.open(path).convert("RGBA")
    if logo.getchannel("A").getextrema() == (255, 255):
        # 抖音的 favicon 是黑底不透明 PNG，不抠掉黑底就是个白方块
        pixels = logo.load()
        for y in range(logo.height):
            for x in range(logo.width):
                red, green, blue, _ = pixels[x, y]
                pixels[x, y] = (
                    red,
                    green,
                    blue,
                    0 if max(red, green, blue) < 40 else 255,
                )
    box = logo.getbbox()
    if box:
        logo = logo.crop(box)
    width = max(1, int(logo.width * height / logo.height))
    return logo.resize((width, height), Image.LANCZOS)


def _is_dark(logo: Any) -> bool:  # PIL Image
    """判断 logo 是否要转成白色剪影。

    只有「偏暗且基本没有颜色」的标才转白：X、Threads、Medium 这类纯黑标在深色
    封面上会消失，必须转白；而抖音的青红、微博的红、YouTube 的红这些带品牌色的
    标，转白会把品牌色抹掉，所以只要颜色够多就保留原色。

    Args:
        logo: logo 图像。

    Returns:
        需要转白时为 ``True``。
    """
    pixels = logo.load()
    total = count = colorful = 0
    for y in range(0, logo.height, max(1, logo.height // 24)):
        for x in range(0, logo.width, max(1, logo.width // 24)):
            red, green, blue, alpha = pixels[x, y]
            if alpha > 128:
                total += 0.299 * red + 0.587 * green + 0.114 * blue
                if max(red, green, blue) - min(red, green, blue) > 40:
                    colorful += 1
                count += 1
    if count == 0:
        return False
    return total / count < 110 and colorful / count < 0.2


def _white(logo: Any) -> Any:  # PIL Image
    """把 logo 的 alpha 通道填成白色。

    Args:
        logo: logo 图像。

    Returns:
        白色剪影。
    """
    from PIL import Image

    white = Image.new("RGBA", logo.size, (255, 255, 255, 0))
    white.putalpha(logo.getchannel("A"))
    return white


@lru_cache(maxsize=32)
def _shadowed_logo(slug: str, height: int) -> Any:  # PIL Image
    """Logo 转白（必要时）并加投影，结果缓存。

    PIL 的 Image 不可哈希，所以按 ``slug + 高度`` 缓存。

    Args:
        slug: logo 文件名。
        height: 目标高度。

    Returns:
        带投影的 RGBA 图像。
    """
    from PIL import Image, ImageFilter

    logo = _logo(slug, height)
    if _is_dark(logo):
        logo = _white(logo)
    blur, offset, alpha = 7, 2, 205
    pad = blur * 2
    canvas = Image.new(
        "RGBA", (logo.width + pad * 2, logo.height + pad * 2), (0, 0, 0, 0)
    )
    shadow = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    shadow.putalpha(logo.getchannel("A").point(lambda v: int(v * alpha / 255)))
    canvas.alpha_composite(shadow, (pad + offset, pad + offset))
    canvas = canvas.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(logo, (pad, pad))
    return canvas


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
    top = int((big.height - card_h) * 0.28)
    pic = big.crop((left, top, left + card_w, top + card_h))

    title_font = _font(TITLE_SIZE, 700)
    meta_font = _font(META_SIZE, 400)
    kicker_font = _font(KICKER_SIZE, 600)
    title_lines = fit_title(
        getattr(result, "title", "") or "(无标题)",
        title_font,
        card_w - INSET * 2,
        3,
        emoji_size=TITLE_SIZE,
    )
    meta = meta_line(result, len(items), is_ms)

    # 渐变要保证文字所在的位置已经够暗：文字上方到 200/255，再往下到 252/255
    text_top = card_h - INSET - len(title_lines) * 70 - 8 - 34 - 50
    start = max(0, int(card_h * FADE_START))
    ramp_end = max(start + 1, text_top)
    upper = _vertical_mask(
        (card_w, ramp_end), start, ramp_end, peak=FADE_PEAK, curve=1.25
    )
    lower = _vertical_mask(
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
    logo = _shadowed_logo(slug, LOGO_HEIGHT)
    pad = logo.width - _logo(slug, LOGO_HEIGHT).width
    mark_w = logo.width - pad
    mark_h = logo.height - pad

    y = card_h - INSET - len(title_lines) * 70 - 8 - 34
    kicker_y = y - 44
    # 图标与「平台名」那一行垂直居中，左边缘贴 INSET
    canvas.alpha_composite(
        logo,
        (
            INSET - pad // 2,
            kicker_y + KICKER_SIZE // 2 - mark_h // 2 - pad // 2,
        ),
    )

    draw = ImageDraw.Draw(canvas)
    draw_text(
        draw,
        (INSET + mark_w + 18, kicker_y),
        name.upper(),
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
        y += 70
    draw_text(
        draw,
        (INSET, y + 8),
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
        _font(TITLE_SIZE, 700)
    except Exception:  # 缺 Pillow 或缺字体都算不可用
        return False
    return True
