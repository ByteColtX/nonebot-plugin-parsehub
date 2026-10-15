# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""卡片彩色 emoji 的离线测试。

emoji 字体是 1.5MB 的 Twemoji Mozilla，不进仓库，所以真正渲染的用例在没有字体时跳过。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

# 本机手动放一份 emoji 字体时，这个用例会真的跑起来；CI 上跳过。
_EMOJI_FONT_HINT = Path(
    os.environ.get("PARSEHUB_EMOJI_FONT", "~/.cache/nonebot2/parsehub/emoji-test.ttf")
).expanduser()


def test_split_emoji_keeps_zwj_sequence(card) -> None:
    """组合序列（ZWJ、变体符、区域指示符）不能被切开。"""
    chunks = card._split_emoji("猫猫🐱和👨‍👩‍👧还有🇨🇳")
    emoji = [text for text, is_emoji in chunks if is_emoji]
    plain = [text for text, is_emoji in chunks if not is_emoji]
    assert "🐱" in emoji
    assert "👨‍👩‍👧" in emoji, "ZWJ 家庭序列必须整段保留"
    assert "🇨🇳" in emoji, "国旗是两个区域指示符，不能被切开"
    assert plain == ["猫猫", "和", "还有"]


def test_is_emoji_char(card) -> None:
    """常见 emoji 判定，以及中英文不会被误判。"""
    assert card._is_emoji_char("🍧")
    assert card._is_emoji_char("✨")
    assert not card._is_emoji_char("猫")
    assert not card._is_emoji_char("A")
    assert not card._is_emoji_char("#")


def test_draw_text_without_emoji_font(card) -> None:
    """没有 emoji 字体时退化成普通绘制，不抛异常。"""
    card._EMOJI_FONT = None
    card._emoji_font_file.cache_clear()
    card._emoji_strike_font.cache_clear()
    card._emoji_tile.cache_clear()
    canvas = Image.new("RGB", (400, 80), (20, 20, 20))
    draw = ImageDraw.Draw(canvas)
    card.draw_text(
        draw,
        (10, 10),
        "猫猫🐱",
        card._font(28, 400),
        (255, 255, 255),
        emoji_size=28,
    )


@pytest.mark.skipif(
    not _EMOJI_FONT_HINT.exists(), reason="本地没有 emoji 字体（部署时会自动下载）"
)
def test_emoji_tile_scales_bitmap_strike(card) -> None:
    """位图字体只有固定档位，贴图必须缩到「字号 × 比例」。"""
    card._EMOJI_FONT = _EMOJI_FONT_HINT
    card._emoji_font_file.cache_clear()
    card._emoji_strike_font.cache_clear()
    card._emoji_tile.cache_clear()
    try:
        tile = card._emoji_tile("🍧", 58)
    finally:
        card._EMOJI_FONT = None
        card._emoji_font_file.cache_clear()
        card._emoji_strike_font.cache_clear()
        card._emoji_tile.cache_clear()
    assert tile, "贴图不该为空"
    image = tile[0]
    assert image.height == round(58 * card.EMOJI_RATIO), "没缩到目标高度"
    assert image.width > 0


@pytest.mark.skipif(
    not _EMOJI_FONT_HINT.exists(), reason="本地没有 emoji 字体（部署时会自动下载）"
)
def test_emoji_tile_is_colored(card) -> None:
    """emoji 贴图必须真的带上颜色，而不是空白。"""
    card._EMOJI_FONT = _EMOJI_FONT_HINT
    card._emoji_font_file.cache_clear()
    card._emoji_strike_font.cache_clear()
    card._emoji_tile.cache_clear()
    try:
        tile = card._emoji_tile("🍧", 32)
    finally:
        card._EMOJI_FONT = None
        card._emoji_font_file.cache_clear()
        card._emoji_strike_font.cache_clear()
        card._emoji_tile.cache_clear()
    assert tile, "贴图不该为空"
    image = tile[0]
    pixels = image.convert("RGB").load()
    colorful = sum(
        1
        for x in range(image.width)
        for y in range(image.height)
        if max(pixels[x, y]) - min(pixels[x, y]) > 40
    )
    assert colorful > 50, f"贴图里彩色像素太少（{colorful}）"


def test_ensure_emoji_font_caches_existing_file(card, tmp_path, monkeypatch) -> None:
    """缓存目录里已经有字体、且 sha256 对得上时直接用，不联网。"""
    import hashlib

    payload = b"\x00" * 300_000
    font = tmp_path / card.EMOJI_FONT_NAME
    font.write_bytes(payload)
    monkeypatch.setattr(
        card,
        "EMOJI_FONT_SOURCES",
        (
            card.FontSource(
                "https://example.test/Twemoji.Mozilla.ttf",
                hashlib.sha256(payload).hexdigest(),
            ),
        ),
    )
    card._EMOJI_FONT = None
    card._emoji_font_file.cache_clear()
    assert card.ensure_emoji_font(tmp_path) == font
    card._EMOJI_FONT = None
    card._emoji_font_file.cache_clear()
