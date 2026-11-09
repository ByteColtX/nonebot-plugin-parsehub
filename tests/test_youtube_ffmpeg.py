# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""没有系统 ffmpeg 时，yt-dlp 也要能合并音视频。"""

from __future__ import annotations

from types import SimpleNamespace

from parsehub.parsers.parser.instagram import InstagramYtVideoParseResult
from parsehub.parsers.parser.youtube import YtbVideoParseResult


def test_ensure_ffmpeg_injects_bundled_location(
    parsehub_client, monkeypatch
) -> None:
    """系统 PATH 里没有 ffmpeg 时，把内置二进制的路径传给 yt-dlp。"""
    monkeypatch.setattr(parsehub_client.shutil, "which", lambda name: None)

    exe = parsehub_client._ensure_ffmpeg_available()

    assert exe
    result = YtbVideoParseResult(dl=SimpleNamespace(info_json={}))
    assert "--ffmpeg-location" in result.cli_args
    assert exe in result.cli_args


def test_ffmpeg_injection_survives_instagram_codec_override(
    parsehub_client, monkeypatch
) -> None:
    """2.4.1 的 IG 结果类重写了 cli_args，注入的 ffmpeg 路径不能丢。"""
    monkeypatch.setattr(parsehub_client.shutil, "which", lambda name: None)

    exe = parsehub_client._ensure_ffmpeg_available()

    assert exe
    args = InstagramYtVideoParseResult(
        dl=SimpleNamespace(info_json={})
    ).cli_args
    assert "--ffmpeg-location" in args
    assert exe in args
    assert args[args.index("-S") + 1] == "vcodec:h264"
