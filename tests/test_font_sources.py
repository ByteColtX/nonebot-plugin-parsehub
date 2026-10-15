# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""字体下载源的版本固定与 sha256 校验。"""

from __future__ import annotations

import hashlib
from pathlib import Path


class _FakeResponse:
    """httpx.stream 的最小替身。"""

    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.status_code = status
        self._payload = payload

    def iter_bytes(self, _size: int):
        yield self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _patch_stream(card, monkeypatch, payload: bytes, status: int = 200) -> None:
    """把 httpx.stream 换成返回固定内容的替身。"""
    monkeypatch.setattr(
        card.httpx, "stream", lambda *_a, **_k: _FakeResponse(payload, status)
    )


def test_sources_are_pinned_and_hashed(card) -> None:
    """每个下载源都得钉到具体版本、带 sha256，不能跟分支走。"""
    for source in card.FONT_SOURCES + card.EMOJI_FONT_SOURCES:
        assert len(source.sha256) == 64, source.url
        assert "@main/" not in source.url, f"还跟着分支走：{source.url}"
        assert "/main/" not in source.url, f"还跟着分支走：{source.url}"


def test_matches_sources_detects_tampering(card, tmp_path: Path) -> None:
    """文件被改过就该认出来。"""
    good = tmp_path / "good.bin"
    good.write_bytes(b"hello")
    digest = hashlib.sha256(b"hello").hexdigest()
    source = card.FontSource("https://example.test/good.bin", digest)
    assert card._matches_sources(good, (source,))

    good.write_bytes(b"hello!")
    assert not card._matches_sources(good, (source,))


def test_matches_sources_without_hash_is_permissive(card, tmp_path: Path) -> None:
    """没给 sha256（自定义地址）时不校验。"""
    path = tmp_path / "any.bin"
    path.write_bytes(b"whatever")
    assert card._matches_sources(path, (card.FontSource("https://x/y", ""),))
    assert card._matches_sources(path, ())


def test_fetch_font_keeps_matching_download(card, tmp_path, monkeypatch) -> None:
    """sha256 对得上才落盘。"""
    payload = b"x" * 300_000
    digest = hashlib.sha256(payload).hexdigest()
    _patch_stream(card, monkeypatch, payload)
    target = tmp_path / "font.ttf"
    source = card.FontSource("https://example.test/font.ttf", digest)
    assert card._fetch_font(source, target, 200_000) is True
    assert target.read_bytes() == payload


def test_fetch_font_drops_mismatched_download(card, tmp_path, monkeypatch) -> None:
    """sha256 对不上：不落盘、不留 .part。"""
    payload = b"x" * 300_000
    _patch_stream(card, monkeypatch, payload)
    target = tmp_path / "font.ttf"
    source = card.FontSource("https://example.test/font.ttf", "0" * 64)
    assert card._fetch_font(source, target, 200_000) is False
    assert not target.exists()
    assert not (tmp_path / "font.part").exists()


def test_fetch_font_rejects_truncated_download(card, tmp_path, monkeypatch) -> None:
    """下了一半的文件不算数。"""
    payload = b"x" * 100
    digest = hashlib.sha256(payload).hexdigest()
    _patch_stream(card, monkeypatch, payload)
    target = tmp_path / "font.ttf"
    source = card.FontSource("https://example.test/font.ttf", digest)
    assert card._fetch_font(source, target, 200_000) is False
    assert not target.exists()


def test_ensure_font_drops_tampered_cache(card, tmp_path, monkeypatch) -> None:
    """缓存里的字体校验不过时删掉重下；下不到就宁可不给字体。"""
    local = tmp_path / "NotoSansSC.ttf"
    local.write_bytes(b"tampered" + b"\x00" * 2_000_000)
    monkeypatch.setattr(card, "_font_file", lambda: None)
    monkeypatch.setattr(card, "_fetch_font", lambda *_a, **_k: False)
    card._CUSTOM_FONT = None
    assert card.ensure_font(tmp_path) is None
    assert not local.exists()
