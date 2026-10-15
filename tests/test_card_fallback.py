# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""主字体缺字时的回退字体链。

离线跑：用替身字体和查表式的字形判定，不需要真的回退字体文件。
"""

from __future__ import annotations

import pytest


class _StubFont:
    """够用的字体替身。"""

    def __init__(self, name: str, size: int = 40, advance: float = 10.0) -> None:
        self.name = name
        self.size = size
        self.advance = advance

    def getlength(self, _text: str) -> float:
        return self.advance

    def getmetrics(self) -> tuple[int, int]:
        return (30, 10)

    def getmask(self, _text: str, mode: str = "L") -> bytes:
        raise AssertionError("测试里不该真去取字形")


@pytest.fixture
def chain(card, monkeypatch):
    """装一条假的字体链：主字体 + 两个回退字体，字形有无由表决定。"""
    main = _StubFont("main")
    fallbacks = (_StubFont("fb1"), _StubFont("fb2"))
    monkeypatch.setattr(card, "_GLYPH_CACHE", {})
    monkeypatch.setattr(card, "_fallback_fonts", lambda _size: fallbacks)

    table: dict[tuple[str, str], bool] = {}

    def has_glyph(font, text):
        return table.get((font.name, text), False)

    monkeypatch.setattr(card, "_has_glyph", has_glyph)
    return main, fallbacks, table


def test_font_runs_picks_first_font_with_glyph(card, chain) -> None:
    """主字体缺的字按顺序往下找，先找到谁就用谁。"""
    main, (fb1, fb2), table = chain
    table[("main", "汉")] = True
    table[("fb1", "˶")] = True
    table[("fb2", "⚈")] = True
    runs = card._font_runs("汉˶⚈汉", main)
    assert runs == [("汉", main), ("˶", fb1), ("⚈", fb2), ("汉", main)]


def test_font_runs_merges_adjacent_same_font(card, chain) -> None:
    """同一字体的相邻片段要合并，别拆成一堆小段。"""
    main, (fb1, _fb2), table = chain
    for ch in "汉字":
        table[("main", ch)] = True
    table[("fb1", "˶")] = True
    table[("fb1", "˵")] = True
    runs = card._font_runs("汉字˶˵汉字", main)
    assert runs == [("汉字", main), ("˶˵", fb1), ("汉字", main)]


def test_font_runs_keeps_blank_with_main_font(card, chain) -> None:
    """空格不参与回退查找，否则每个空格都会换字体。"""
    main, (fb1, _fb2), table = chain
    table[("main", "汉")] = True
    table[("fb1", " ")] = True  # 就算回退字体"有"空格，也不该被选中
    runs = card._font_runs("汉 汉", main)
    assert runs == [("汉 汉", main)]


def test_font_runs_without_fallbacks_stays_main(card, monkeypatch) -> None:
    """没有回退字体时，缺字仍然交回主字体（会画成方块）。"""
    main = _StubFont("main")
    monkeypatch.setattr(card, "_GLYPH_CACHE", {})
    monkeypatch.setattr(card, "_fallback_fonts", lambda _size: ())
    monkeypatch.setattr(card, "_has_glyph", lambda _font, _text: False)
    assert card._font_runs("⚈", main) == [("⚈", main)]


def test_font_runs_emoji_font_without_glyph_falls_back(card, monkeypatch) -> None:
    """被当成 emoji 但 emoji 字体里没有的字，要落到回退字体上，不能直接消失。"""
    main = _StubFont("main")
    fb = _StubFont("fb")
    emoji = _StubFont("emoji")
    monkeypatch.setattr(card, "_GLYPH_CACHE", {})
    monkeypatch.setattr(card, "_fallback_fonts", lambda _size: (fb,))
    monkeypatch.setattr(card, "_emoji_font", lambda _size: emoji)
    # ⚈ 落在 emoji 码点区间里，但 Twemoji 没有这个字形
    monkeypatch.setattr(
        card,
        "_has_glyph",
        lambda font, text: (font.name, text) == ("fb", "⚈"),
    )
    assert card._font_runs("⚈", main, emoji_size=40) == [("⚈", fb)]


def test_font_runs_emoji_font_with_glyph_wins(card, monkeypatch) -> None:
    """emoji 字体有字形时优先走彩色贴图（字体为 None）。"""
    main = _StubFont("main")
    emoji = _StubFont("emoji")
    monkeypatch.setattr(card, "_GLYPH_CACHE", {})
    monkeypatch.setattr(card, "_fallback_fonts", lambda _size: ())
    monkeypatch.setattr(card, "_emoji_font", lambda _size: emoji)
    monkeypatch.setattr(
        card, "_has_glyph", lambda font, text: font.name == "emoji"
    )
    assert card._font_runs("🍧", main, emoji_size=40) == [("🍧", None)]


def test_advance_uses_the_font_that_draws(card, chain) -> None:
    """量宽度要用真正画那段字的字体，否则折行会算错。"""
    main, (fb1, _fb2), table = chain
    main.advance = 10.0
    fb1.advance = 4.0
    table[("main", "汉")] = True
    table[("fb1", "˶")] = True
    assert card._advance("汉˶汉", main) == 24.0


def test_ensure_fallback_fonts_skips_failed_download(card, tmp_path, monkeypatch) -> None:
    """回退字体下不到就当它不存在，不影响出卡。"""
    monkeypatch.setattr(card, "_FALLBACK_FILES", [])
    monkeypatch.setattr(card, "_fetch_font", lambda *_a, **_k: False)
    assert card.ensure_fallback_fonts(tmp_path) == []
    assert card._FALLBACK_FILES == []


def test_ensure_fallback_fonts_accepts_verified_cache(card, tmp_path, monkeypatch) -> None:
    """缓存里已有且 sha256 对得上时不再下载。"""
    payload = b"f" * 200_000
    digest = card.hashlib.sha256(payload).hexdigest()
    name = "Stub.ttf"
    local = tmp_path / name
    local.write_bytes(payload)
    monkeypatch.setattr(
        card,
        "FALLBACK_FONTS",
        ((name, (card.FontSource("https://example.test/stub.ttf", digest),)),),
    )
    called = []
    monkeypatch.setattr(card, "_fetch_font", lambda *_a, **_k: called.append(1) or True)
    assert card.ensure_fallback_fonts(tmp_path) == [local]
    assert called == [], "校验通过就不该再下载"


def test_ensure_fallback_fonts_drops_tampered_cache(card, tmp_path, monkeypatch) -> None:
    """缓存里的回退字体被改过就删掉重下。"""
    monkeypatch.setattr(card, "_FALLBACK_FILES", [])
    name = "Stub.ttf"
    local = tmp_path / name
    local.write_bytes(b"tampered" + b"\0" * 200_000)
    monkeypatch.setattr(
        card,
        "FALLBACK_FONTS",
        ((name, (card.FontSource("https://example.test/stub.ttf", "0" * 64),)),),
    )
    monkeypatch.setattr(card, "_fetch_font", lambda *_a, **_k: False)
    assert card.ensure_fallback_fonts(tmp_path) == []
    assert not local.exists(), "校验不过的文件必须删掉，不能留着用"


def test_fallback_sources_are_pinned(card) -> None:
    """回退字体的下载源同样得钉版本 + 带 sha256。"""
    for name, sources in card.FALLBACK_FONTS:
        assert sources, name
        for source in sources:
            assert len(source.sha256) == 64, source.url
            assert "/main/" not in source.url, f"还跟着分支走：{source.url}"
            assert "@master" not in source.url, f"还跟着分支走：{source.url}"
