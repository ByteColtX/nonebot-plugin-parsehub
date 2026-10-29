# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""卡片彩色 emoji 的离线测试。

测试字体是仓库里的 1.8KB 子集（只含 🍧），不联网。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

# Twemoji 15.0.2 子集（图形 CC-BY 4.0，字体构建 JoeBlakeB/ttf-twemoji-aur），
# 只保留 🍧；重新生成：
# uv run --with fonttools pyftsubset Twemoji-15.0.2.ttf \
#   --unicodes=U+1F367 --output-file=tests/assets/test-emoji.ttf
EMOJI_FONT = Path(__file__).resolve().parent / "assets" / "test-emoji.ttf"


@pytest.fixture()
def emoji_font() -> Path:
    """测试字体随仓库提供，缺失就失败，不允许跳过。"""
    assert EMOJI_FONT.exists(), f"测试 emoji 字体缺失：{EMOJI_FONT}"
    return EMOJI_FONT


def test_split_emoji_keeps_zwj_sequence(card) -> None:
    """组合序列（ZWJ、变体符、区域指示符）不能被切开。"""
    chunks = card.layout._split_emoji("猫猫🐱和👨‍👩‍👧还有🇨🇳")
    emoji = [text for text, is_emoji in chunks if is_emoji]
    plain = [text for text, is_emoji in chunks if not is_emoji]
    assert "🐱" in emoji
    assert "👨‍👩‍👧" in emoji, "ZWJ 家庭序列必须整段保留"
    assert "🇨🇳" in emoji, "国旗是两个区域指示符，不能被切开"
    assert plain == ["猫猫", "和", "还有"]


def test_is_emoji_char(card) -> None:
    """常见 emoji 判定，以及中英文不会被误判。"""
    assert card.layout._is_emoji_char("🍧")
    assert card.layout._is_emoji_char("✨")
    assert not card.layout._is_emoji_char("猫")
    assert not card.layout._is_emoji_char("A")
    assert not card.layout._is_emoji_char("#")


def test_draw_text_without_emoji_font(card) -> None:
    """没有 emoji 字体时退化成普通绘制，不抛异常。"""
    card.fonts._EMOJI_FONT = None
    card.fonts._emoji_font_file.cache_clear()
    card.fonts._emoji_strike_font.cache_clear()
    card.layout._emoji_tile.cache_clear()
    canvas = Image.new("RGB", (400, 80), (20, 20, 20))
    before = canvas.tobytes()
    draw = ImageDraw.Draw(canvas)
    card.layout.draw_text(
        draw,
        (10, 10),
        "猫猫🐱",
        card.fonts._font(28, 400),
        (255, 255, 255),
        emoji_size=28,
    )
    assert canvas.tobytes() != before, "没有 emoji 字体时也应该画出普通文字"


def test_emoji_tile_scales_bitmap_strike(card, emoji_font: Path) -> None:
    """位图字体只有固定档位，贴图必须缩到「字号 × 比例」。"""
    card.fonts._EMOJI_FONT = emoji_font
    card.fonts._emoji_font_file.cache_clear()
    card.fonts._emoji_strike_font.cache_clear()
    card.layout._emoji_tile.cache_clear()
    try:
        tile = card.layout._emoji_tile("🍧", 58)
    finally:
        card.fonts._EMOJI_FONT = None
        card.fonts._emoji_font_file.cache_clear()
        card.fonts._emoji_strike_font.cache_clear()
        card.layout._emoji_tile.cache_clear()
    assert tile, "贴图不该为空"
    image = tile[0]
    assert image.height == round(58 * card.layout.EMOJI_RATIO), "没缩到目标高度"
    assert image.width > 0


def test_emoji_tile_is_colored(card, emoji_font: Path) -> None:
    """emoji 贴图必须真的带上颜色，而不是空白。"""
    card.fonts._EMOJI_FONT = emoji_font
    card.fonts._emoji_font_file.cache_clear()
    card.fonts._emoji_strike_font.cache_clear()
    card.layout._emoji_tile.cache_clear()
    try:
        tile = card.layout._emoji_tile("🍧", 32)
    finally:
        card.fonts._EMOJI_FONT = None
        card.fonts._emoji_font_file.cache_clear()
        card.fonts._emoji_strike_font.cache_clear()
        card.layout._emoji_tile.cache_clear()
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


def test_ensure_emoji_font_caches_existing_file(
    card, tmp_path, monkeypatch
) -> None:
    """缓存目录里已经有字体、且 sha256 对得上时直接用，不联网。"""
    import hashlib

    payload = b"\x00" * 300_000
    font = tmp_path / card.fonts.EMOJI_FONT_NAME
    font.write_bytes(payload)
    monkeypatch.setattr(
        card.fonts,
        "EMOJI_FONT_SOURCES",
        (
            card.fonts.FontSource(
                "https://example.test/Twemoji.Mozilla.ttf",
                hashlib.sha256(payload).hexdigest(),
            ),
        ),
    )
    card.fonts._EMOJI_FONT = None
    card.fonts._emoji_font_file.cache_clear()
    assert card.fonts.ensure_emoji_font(tmp_path) == font
    card.fonts._EMOJI_FONT = None
    card.fonts._emoji_font_file.cache_clear()
