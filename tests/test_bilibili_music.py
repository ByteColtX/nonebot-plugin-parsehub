# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""B 站音乐（au 链接）：自动解析按文件发送，卡片标「音乐」。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake import make_group_event
from fakes import FakeBot

MUSIC_URL = "https://www.bilibili.com/audio/au2854883"
SHORT_URL = "https://b23.tv/w8J4aMY"


class FakeMusicRef:
    """parsehub 2.4.0 的 MusicRef 替身。"""

    def __init__(self, url: str = "https://cdn.example.com/song.m4a") -> None:
        self.url = url
        self.ext = "m4a"
        self.thumb_url = ""
        self.duration = 200
        self.backup_urls: list[str] = []


def _music_result():
    """构造一份 BiliMusicParseResult 形状的解析结果。"""
    return SimpleNamespace(
        title="夜航星",
        content="简介",
        media=FakeMusicRef(),
        raw_url=MUSIC_URL,
        platform=SimpleNamespace(id="bilibili", display_name="哔哩哔哩"),
    )


def _patch_parse(instance, monkeypatch, parsed, calls=None):
    """把支持性判断与解析换成假实现。"""

    async def fake_parse(url, proxy="", cookie=""):
        if calls is not None:
            calls["parse"] = calls.get("parse", 0) + 1
        return parsed

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)


def _patch_download(service, plugin, monkeypatch, path: Path, calls=None):
    """把作品下载换成一份 record 形态的 .m4a（模拟 kind_of 的推断结果）。"""

    async def fake_download(*args, **kwargs):
        if calls is not None:
            calls["download"] = calls.get("download", 0) + 1
        return [
            plugin.media.MediaFile(
                path=path,
                kind=plugin.media.MediaKind.RECORD,
                duration=200,
            )
        ], None

    monkeypatch.setattr(service.download, "download_media", fake_download)


@pytest.mark.asyncio
async def test_bilibili_music_sends_file(
    plugin, instance, service, monkeypatch, tmp_path: Path
) -> None:
    """自动解析音乐链接：下载结果按文件上传，不发 record。"""
    audio_path = tmp_path / "夜航星.m4a"
    audio_path.write_bytes(b"audio")
    parsed = _music_result()

    _patch_parse(instance, monkeypatch, parsed)
    _patch_download(service, plugin, monkeypatch, audio_path)

    bot = FakeBot()
    result = await service.handle(
        instance, bot, make_group_event(MUSIC_URL), MUSIC_URL
    )

    assert result.outcome is service.Outcome.DELIVERED
    assert parsed.music is True, "卡片要标「音乐」"
    assert bot.sent_records == []
    assert [name for _, name in bot.uploaded_files()] == ["夜航星.m4a"]


@pytest.mark.asyncio
async def test_bilibili_music_ignores_video_duration_limit(
    plugin, instance, service, monkeypatch, tmp_path: Path, update_settings
) -> None:
    """音乐不套视频时长上限：200s 的歌在 60s 上限下照常发。"""
    update_settings(max_seconds=60)
    audio_path = tmp_path / "夜航星.m4a"
    audio_path.write_bytes(b"audio")

    _patch_parse(instance, monkeypatch, _music_result())
    _patch_download(service, plugin, monkeypatch, audio_path)

    bot = FakeBot()
    result = await service.handle(
        instance, bot, make_group_event(MUSIC_URL), MUSIC_URL
    )

    assert result.outcome is service.Outcome.DELIVERED
    assert [name for _, name in bot.uploaded_files()] == ["夜航星.m4a"]


@pytest.mark.asyncio
async def test_bilibili_music_cache_hit_keeps_file(
    plugin, instance, service, monkeypatch, tmp_path: Path
) -> None:
    """从缓存重发时仍按文件上传，不按扩展名退回 record。"""
    audio_path = tmp_path / "夜航星.m4a"
    audio_path.write_bytes(b"audio")
    calls: dict[str, int] = {}

    _patch_parse(instance, monkeypatch, _music_result(), calls)
    _patch_download(service, plugin, monkeypatch, audio_path, calls)

    first = FakeBot()
    await service.handle(
        instance, first, make_group_event(MUSIC_URL), MUSIC_URL
    )

    second = FakeBot()
    result = await service.handle(
        instance,
        second,
        make_group_event(MUSIC_URL, group_id=654321),
        MUSIC_URL,
    )

    assert result.outcome is service.Outcome.FROM_CACHE
    assert calls == {"parse": 1, "download": 1}, calls
    assert second.sent_records == []
    assert [name for _, name in second.uploaded_files()] == ["夜航星.m4a"]


@pytest.mark.asyncio
async def test_bilibili_music_cache_hit_ignores_duration_limit(
    plugin, instance, service, monkeypatch, tmp_path: Path, update_settings
) -> None:
    """200s 的歌在 60s 上限下仍能命中缓存，不重新解析 / 下载。"""
    update_settings(max_seconds=60)
    audio_path = tmp_path / "夜航星.m4a"
    audio_path.write_bytes(b"audio")
    calls: dict[str, int] = {}

    _patch_parse(instance, monkeypatch, _music_result(), calls)
    _patch_download(service, plugin, monkeypatch, audio_path, calls)

    for group_id in (123456, 654321):
        bot = FakeBot()
        result = await service.handle(
            instance,
            bot,
            make_group_event(MUSIC_URL, group_id=group_id),
            MUSIC_URL,
        )
        assert [name for _, name in bot.uploaded_files()] == ["夜航星.m4a"]

    assert result.outcome is service.Outcome.FROM_CACHE
    assert calls == {"parse": 1, "download": 1}, calls


@pytest.mark.asyncio
async def test_bilibili_music_short_link_cache_hit_keeps_file(
    plugin, instance, service, monkeypatch, tmp_path: Path
) -> None:
    """短链进来的音乐，靠条目里的作品 key 认出类型，重发仍是文件。"""
    audio_path = tmp_path / "夜航星.m4a"
    audio_path.write_bytes(b"audio")
    calls: dict[str, int] = {}

    _patch_parse(instance, monkeypatch, _music_result(), calls)
    _patch_download(service, plugin, monkeypatch, audio_path, calls)

    first = FakeBot()
    await service.handle(
        instance, first, make_group_event(SHORT_URL), SHORT_URL
    )
    assert [name for _, name in first.uploaded_files()] == ["夜航星.m4a"]

    second = FakeBot()
    result = await service.handle(
        instance,
        second,
        make_group_event(SHORT_URL, group_id=654321),
        SHORT_URL,
    )

    assert result.outcome is service.Outcome.FROM_CACHE
    assert calls == {"parse": 1, "download": 1}, calls
    assert second.sent_records == []
    assert [name for _, name in second.uploaded_files()] == ["夜航星.m4a"]
