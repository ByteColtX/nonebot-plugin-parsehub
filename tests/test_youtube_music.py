# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""YouTube Music：卡片显示音乐，媒体发 record。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake import make_group_event
from fakes import FakeBot, FakeMediaRef, FakeParseResult

MUSIC_URL = (
    "https://music.youtube.com/watch?v=pAFgy6hLjqc&list=RDAMVMnWaZ9vBijc0"
)


def test_is_youtube_music(platforms) -> None:
    """只认 music 子域，普通 YouTube 视频仍走视频。"""
    assert platforms.is_youtube_music(MUSIC_URL)
    assert not platforms.is_youtube_music(
        "https://www.youtube.com/watch?v=pAFgy6hLjqc"
    )


def test_music_meta_line(card) -> None:
    """音乐卡片的类型是「音乐」，不是「视频」。"""
    result = SimpleNamespace(
        music=True,
        type=SimpleNamespace(value="video"),
        media=SimpleNamespace(duration=155),
    )
    assert card.layout.meta_line(result, 1, False) == "音乐 · 02:35"


@pytest.mark.asyncio
async def test_download_music_returns_record(
    plugin, instance, service, monkeypatch, tmp_path: Path
) -> None:
    """下载侧只取音频，并按清洗后的标题落盘。"""
    audio = plugin.audio

    def fake_info(*args, **kwargs):
        return audio.YtAudio(
            title="Song/Title",
            video_id="abc",
            duration=155,
            webpage_url=MUSIC_URL,
        )

    def fake_download(url, dest_dir, *args, **kwargs):
        path = Path(dest_dir) / "abc.webm"
        path.write_bytes(b"audio")
        return path

    monkeypatch.setattr(service.download.audio, "youtube_audio", fake_info)
    monkeypatch.setattr(
        service.download.audio, "download_youtube", fake_download
    )

    files, error = await service.download.download_music(
        instance, MUSIC_URL, "", "", plugin.budget.Budget(10)
    )

    assert error is None
    assert files is not None and len(files) == 1
    assert files[0].kind is plugin.media.MediaKind.RECORD
    assert files[0].duration == 155
    assert files[0].path.name == "Song Title.webm"
    assert files[0].path.read_bytes() == b"audio"


@pytest.mark.asyncio
async def test_youtube_music_sends_record(
    plugin, instance, service, monkeypatch, tmp_path: Path
) -> None:
    """完整流水线：不下载视频，发一条 record。"""
    audio_path = tmp_path / "song.m4a"
    audio_path.write_bytes(b"audio")

    async def fake_parse(url, proxy="", cookie=""):
        result = FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/song.webm", "webm"),
            raw_url=MUSIC_URL,
        )
        result.platform = SimpleNamespace(id="youtube", display_name="YouTube")
        return result

    monkeypatch.setattr(
        instance.client,
        "platform_id",
        lambda url: asyncio.sleep(0, result="youtube"),
    )
    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)

    called = {"music": 0}

    async def fake_music(*args, **kwargs):
        called["music"] += 1
        return [
            plugin.media.MediaFile(
                path=audio_path,
                kind=plugin.media.MediaKind.RECORD,
                duration=155,
            )
        ], None

    async def fake_video(*args, **kwargs):
        raise AssertionError("YouTube Music 不该走视频下载")

    monkeypatch.setattr(service.download, "download_music", fake_music)
    monkeypatch.setattr(service.download, "download_media", fake_video)

    bot = FakeBot()
    result = await service.handle(
        instance, bot, make_group_event(MUSIC_URL), MUSIC_URL
    )

    assert called["music"] == 1
    assert result.outcome is service.Outcome.DELIVERED
    assert len(bot.sent_records) == 1
    assert bot.sent_videos == []
