# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""文字层：emoji 切分、字形链、折行与绘制。

折行、贴图、字体链都只管「这段文字怎么画」，不知道卡片长什么样；版式与合成
在 :mod:`layout` / :mod:`compose`。全部同步阻塞，调用方放进线程池。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .fonts import emoji_strike_font, fallback_fonts, has_glyph

# emoji 底边落在基线下多少（占字号），跟文字重心对齐
EMOJI_DROP = 0.06

# 算作 emoji 的码点区间（含 ZWJ、变体符、区域指示符，组合序列不会被切开）。
EMOJI_RANGES: tuple[tuple[int, int], ...] = (
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

# emoji 贴图高度占字号多少（中文墨迹大约 0.9 个字号高）
EMOJI_RATIO = 0.92


def is_emoji_char(ch: str) -> bool:
    """这个字符是不是 emoji 码点。

    Args:
        ch: 单个字符。

    Returns:
        落在 emoji 区间里为 ``True``。
    """
    code = ord(ch)
    return any(low <= code <= high for low, high in EMOJI_RANGES)


def font_runs(
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
    emoji = emoji_strike_font() if emoji_size else None
    fallbacks = fallback_fonts(size) if size else ()
    runs: list[tuple[str, Any]] = []
    for chunk, is_emoji in split_emoji(text):
        if is_emoji:
            # 变体选择符只是"按图形呈现"的提示，emoji 字体不需要它；有些
            # FreeType 会把它当整字宽的可见字形，把 emoji 之间的间距撑得很大。
            chunk = strip_variation(chunk)
        if is_emoji and emoji is not None and has_glyph(emoji, chunk):
            runs.append((chunk, None))
            continue
        for ch in chunk:
            slot = font
            if not ch.isspace() and not has_glyph(font, ch):
                for candidate in fallbacks:
                    if has_glyph(candidate, ch):
                        slot = candidate
                        break
            if runs and runs[-1][1] is slot:
                runs[-1] = (runs[-1][0] + ch, slot)
            else:
                runs.append((ch, slot))
    return runs


def advance(text: str, font: Any, emoji_size: int = 0) -> float:
    """算一段文字实际画出来有多宽（跟 :func:`draw_text` 的推进保持一致）。

    Args:
        text: 文本。
        font: 主字体对象。
        emoji_size: emoji 字号；0 表示不处理 emoji。

    Returns:
        宽度（像素）。
    """
    total = 0.0
    for chunk, slot in font_runs(text, font, emoji_size):
        if slot is None:
            tile = emoji_tile(chunk, emoji_size)
            if tile:
                total += tile[3]
            else:
                total += font.getlength(chunk)
        else:
            total += slot.getlength(chunk)
    return total


def strip_variation(text: str) -> str:
    """去掉变体选择符（U+FE0E / U+FE0F）。

    它们只影响"按文字还是按图形呈现"，画 emoji 时不需要；留着反而会被某些
    FreeType 当成整字宽的可见字形，把间距撑开。

    Args:
        text: 文本。

    Returns:
        去掉变体选择符的文本。
    """
    return text.replace("\ufe0f", "").replace("\ufe0e", "")


def split_emoji(text: str) -> list[tuple[str, bool]]:
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
        cur = is_emoji_char(ch)
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
def emoji_tile(chunk: str, size: int) -> tuple[Any, int, int, float]:
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
    font = emoji_strike_font()
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

    字体链见 :func:`font_runs`。没有 emoji 字体、也没有回退字体时退化成普通的
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
    for chunk, slot in font_runs(text, font, emoji_size):
        if slot is None:
            tile = emoji_tile(chunk, emoji_size)
            if tile:
                image, baseline_in_tile, left, step = tile
                image_draw = getattr(draw, "_image", None)
                if image_draw is None:  # pragma: no cover - 都有这个属性
                    image_draw = draw.im
                image_draw.paste(
                    image,
                    (int(x + left), int(baseline - baseline_in_tile)),
                    image,
                )
                x += step
                continue
            slot = font  # 贴图失败就按普通文字画，至少不丢字
        draw.text((x, top), chunk, font=slot, fill=fill)
        x += slot.getlength(chunk)


def flat(text: str) -> str:
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
        return advance(part, font, emoji_size)

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
    if lines and len(flat("".join(lines))) < len(flat(text)):
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
    if len(flat("".join(lines).rstrip("…"))) >= len(flat(title)):
        return lines
    tokens = title.split(" ")
    while len(tokens) > 1 and tokens[-1].startswith("#"):
        tokens.pop()
        trial = " ".join(tokens)
        trial_lines = wrap(trial, font, max_w, max_lines, emoji_size)
        if len(flat("".join(trial_lines).rstrip("…"))) >= len(flat(trial)):
            return trial_lines
    return lines
