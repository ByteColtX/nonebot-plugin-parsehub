# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""bm / ym 音频指令：目标提取、文件名清洗、key 隔离、编排与投递分支。

全部离线：B 站接口与 yt-dlp 子进程都换成假实现，只验证插件自身的行为。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fake import make_group_event
from nonebot.adapters.onebot.v11 import Bot, Message
from nonebot.adapters.onebot.v11.exception import ActionFailed


class FakeBot(Bot):
    """记录所有 API 调用的假 Bot。"""

    def __init__(self, *, fail_record: bool = False) -> None:
        super().__init__(adapter=MagicMock(), self_id="10000")
        self.calls: list[tuple[str, dict]] = []
        self.fail_record = fail_record

    async def call_api(self, api: str, **kwargs):  # type: ignore[override]
        self.calls.append((api, kwargs))
        if self.fail_record and api == "send_msg":
            message = kwargs.get("message")
            if isinstance(message, Message) and any(
                seg.type == "record" for seg in message
            ):
                raise ActionFailed(
                    status="failed",
                    retcode=100,
                    data=None,
                    message="record not supported",
                )
        return {"status": "ok", "retcode": 0, "data": None}

    @property
    def sent_records(self) -> list[str]:
        out = []
        for api, kwargs in self.calls:
            if api != "send_msg":
                continue
            message = kwargs.get("message")
            if isinstance(message, Message):
                out.extend(
                    seg.data.get("file", "")
                    for seg in message
                    if seg.type == "record"
                )
        return out

    @property
    def sent_texts(self) -> list[str]:
        out = []
        for api, kwargs in self.calls:
            if api != "send_msg":
                continue
            message = kwargs.get("message")
            if isinstance(message, Message):
                out.append(message.extract_plain_text())
        return out

    def uploaded_files(self) -> list[tuple[str, str]]:
        return [
            (kw.get("file", ""), kw.get("name", ""))
            for api, kw in self.calls
            if api == "upload_group_file"
        ]


_BILI_URL = "https://www.bilibili.com/video/BV1A2xL67EMP"
_BILI_CANONICAL = "https://www.bilibili.com/video/BV1A2xL67EMP"
_YT_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


def _bili_info(**overrides):
    from nonebot_plugin_parsehub.audio import BiliAudio

    fields = {
        "title": "据说煎鸡蛋会流量好🍳",
        "bvid": "BV1A2xL67EMP",
        "part": 1,
        "duration": 25,
        "audio_url": "https://upos.example.com/audio.m4s",
        "bandwidth": 139487,
    }
    fields.update(overrides)
    return BiliAudio(**fields)


def _yt_info(**overrides):
    from nonebot_plugin_parsehub.audio import YtAudio

    fields = {
        "title": "【Ref:rain】色づく世界の明日から【AMV/MAD】",
        "video_id": "ylLOn6KXzAc",
        "duration": 290,
        "webpage_url": "https://www.youtube.com/watch?v=ylLOn6KXzAc",
    }
    fields.update(overrides)
    return YtAudio(**fields)


def _patch_bili(monkeypatch, plugin, calls=None, **overrides) -> None:
    """把 B 站音频获取与下载换成假实现。"""
    info = _bili_info(**overrides)

    def fake_fetch(url, proxy="", cookie=""):
        if calls is not None:
            calls["fetch"] = calls.get("fetch", 0) + 1
        return info

    def fake_download(audio_url, dest: Path, proxy="", max_bytes=0):
        if calls is not None:
            calls["download"] = calls.get("download", 0) + 1
        dest.write_bytes(b"\x00\x00\x00 ftypiso5" + b"a" * 1024)
        return dest

    monkeypatch.setattr(plugin.audio, "bilibili_audio", fake_fetch)
    monkeypatch.setattr(plugin.audio, "download_bilibili", fake_download)


def _patch_yt(monkeypatch, plugin, calls=None, **overrides) -> None:
    """把 yt-dlp 元信息与下载换成假实现。"""
    info = _yt_info(**overrides)

    def fake_fetch(url, proxy="", cookie=""):
        if calls is not None:
            calls["fetch"] = calls.get("fetch", 0) + 1
        return info

    def fake_download(
        url, dest_dir: Path, proxy="", timeout=0, max_bytes=0, cookie=""
    ):
        if calls is not None:
            calls["download"] = calls.get("download", 0) + 1
        out = dest_dir / f"{info.video_id}.m4a"
        out.write_bytes(b"\x00\x00\x00 ftypiso5" + b"a" * 1024)
        return out

    monkeypatch.setattr(plugin.audio, "youtube_audio", fake_fetch)
    monkeypatch.setattr(plugin.audio, "youtube_download", fake_download)


# --------------------------------------------------------------------------- #
# 1. 命令参数与目标提取
# --------------------------------------------------------------------------- #
def test_parse_command_strips_file_option(audio):
    """-f / --file 只影响当次，剩余文本原样返回。"""
    opts, rest = audio.parse_command("BV1A2xL67EMP")
    assert opts.as_file is False
    assert rest == "BV1A2xL67EMP"

    opts, rest = audio.parse_command("-f BV1A2xL67EMP")
    assert opts.as_file is True
    assert rest == "BV1A2xL67EMP"

    opts, rest = audio.parse_command("--file BV1A2xL67EMP")
    assert opts.as_file is True
    assert rest == "BV1A2xL67EMP"

    # 选项不在最前面就当普通文本
    opts, rest = audio.parse_command("BV1A2xL67EMP -f")
    assert opts.as_file is False
    assert rest == "BV1A2xL67EMP -f"


def test_parse_command_only_option(audio):
    """只有选项没有目标：选项照常解析，剩余为空。"""
    opts, rest = audio.parse_command("-f")
    assert opts.as_file is True
    assert rest == ""


def test_extract_bilibili_bare_bv(audio):
    """裸 BV 号拼成标准链接。"""
    assert audio.extract_bilibili("BV1A2xL67EMP") == _BILI_CANONICAL


def test_extract_bilibili_full_url_keeps_part(audio):
    """完整链接原样保留，分P参数跟着走。"""
    url = "https://www.bilibili.com/video/BV1pTap6UE1w/?p=2&"
    assert audio.extract_bilibili(url) == url


def test_extract_bilibili_short_link(audio):
    """b23 短链原样返回，展开推迟到获取阶段。"""
    assert audio.extract_bilibili("https://b23.tv/w8J4aMY") == (
        "https://b23.tv/w8J4aMY"
    )


def test_extract_bilibili_from_share_text(audio):
    """分享文案里抠出链接，标题文字不碍事。"""
    text = "【「山巅雪国的记忆」】 https://b23.tv/w8J4aMY 好听！"
    assert audio.extract_bilibili(text) == "https://b23.tv/w8J4aMY"


def test_extract_bilibili_none_for_plain_text(audio):
    """普通聊天文本提不出目标。"""
    assert audio.extract_bilibili("今天天气不错") is None
    assert audio.extract_bilibili("") is None


def test_extract_bilibili_prefers_first(audio):
    """多个目标时取最先出现的那个。"""
    text = (
        "https://b23.tv/w8J4aMY 和 https://www.bilibili.com/video/BV1pTap6UE1w/"
    )
    assert audio.extract_bilibili(text) == "https://b23.tv/w8J4aMY"


def test_has_av_number(audio):
    """av 号（大小写都算）能被识别，好给出专门提示。"""
    assert audio.has_av_number("av170001") is True
    assert audio.has_av_number("AV170001") is True
    assert (
        audio.has_av_number("https://www.bilibili.com/video/av170001") is True
    )
    # nav123 的 av 前贴着字母，不算
    assert audio.has_av_number("nav123") is False
    assert audio.has_av_number("BV1A2xL67EMP") is False


def test_extract_youtube_variants(audio):
    """watch / youtu.be / shorts / music 域都认。"""
    assert (
        audio.extract_youtube(
            "https://www.youtube.com/watch?v=9fpazsv5S5k&list=RDMM&start_radio=1"
        )
        == "https://www.youtube.com/watch?v=9fpazsv5S5k&list=RDMM&start_radio=1"
    )
    assert audio.extract_youtube("https://youtu.be/ylLOn6KXzAc?si=abc") == (
        "https://youtu.be/ylLOn6KXzAc?si=abc"
    )
    assert audio.extract_youtube(
        "https://www.youtube.com/shorts/abcdEFGHijK"
    ) == ("https://www.youtube.com/shorts/abcdEFGHijK")
    assert (
        audio.extract_youtube("https://music.youtube.com/watch?v=aqz-KE-bpKQ")
        == "https://music.youtube.com/watch?v=aqz-KE-bpKQ"
    )


def test_extract_youtube_bare_id(audio):
    """单独一个 11 位视频 ID 也当目标，拼成 youtu.be 链接。"""
    assert (
        audio.extract_youtube("aqz-KE-bpKQ") == "https://youtu.be/aqz-KE-bpKQ"
    )


def test_extract_youtube_bare_id_needs_own_token(audio):
    """混在文案里的 11 位串不是视频 ID，不能瞎猜。"""
    assert audio.extract_youtube("这首歌 aqz-KE-bpKQ 真好听") is None


def test_extract_youtube_none_for_other_links(audio):
    """别家平台的链接提不出来。"""
    assert audio.extract_youtube(_BILI_URL) is None
    assert audio.extract_youtube("今天天气不错") is None


# --------------------------------------------------------------------------- #
# 2. 文件名清洗
# --------------------------------------------------------------------------- #
def test_file_stem_keeps_emoji_and_cjk(audio):
    """emoji 与中日标点都不是文件系统非法字符，原样保留。"""
    assert audio.file_stem("据说煎鸡蛋会流量好🍳", "BV1A2xL67EMP") == (
        "据说煎鸡蛋会流量好🍳"
    )
    assert audio.file_stem("「山巅雪国的记忆」：《原神》OST", "BV") == (
        "「山巅雪国的记忆」：《原神》OST"
    )


def test_file_stem_replaces_illegal_chars(audio):
    """各平台文件系统非法字符换成空格，空白折叠。"""
    assert (
        audio.file_stem('a/b\\c:d*e?f"g<h>i|j', "BV") == "a b c d e f g h i j"
    )
    assert audio.file_stem("换行\n制表\t空格  多", "BV") == "换行 制表 空格 多"


def test_file_stem_truncates(audio):
    """超长标题截断到 80 字符。"""
    assert audio.file_stem("标" * 100, "BV") == "标" * 80


def test_file_stem_falls_back_when_empty(audio):
    """清洗后为空就回退到作品号。"""
    assert audio.file_stem("", "BV1A2xL67EMP") == "BV1A2xL67EMP"
    assert audio.file_stem("///:::", "BV1A2xL67EMP") == "BV1A2xL67EMP"
    assert audio.file_stem("  .  ", "BV1A2xL67EMP") == "BV1A2xL67EMP"


def test_file_stem_strips_trailing_dots(audio):
    """首尾的点是 Windows 非法结尾，去掉。"""
    assert audio.file_stem("..标题...", "BV") == "标题"


def test_file_stem_part_suffix(audio):
    """分P大于 1 时加上 P 标签，且不会被截断切掉。"""
    assert audio.file_stem("标题", "BV", part=1) == "标题"
    assert audio.file_stem("标题", "BV", part=2) == "标题 - P2"
    assert audio.file_stem("标" * 80, "BV", part=3) == "标" * 80 + " - P3"


# --------------------------------------------------------------------------- #
# 3. key 隔离与归一
# --------------------------------------------------------------------------- #
def test_cache_key_isolated_from_video(audio, keys):
    """音频 key 带 audio: 前缀，不与视频缓存撞。"""
    assert audio.cache_key(_BILI_URL) == "audio:bilibili:bv1a2xl67emp"
    assert keys.content_key(_BILI_URL) == "bilibili:bv1a2xl67emp"


def test_cache_key_includes_part(audio):
    """分P进 key，不同 P 不撞缓存。"""
    assert (
        audio.cache_key("https://www.bilibili.com/video/BV1pTap6UE1w/?p=2")
        == "audio:bilibili:bv1ptap6ue1w:p2"
    )
    assert (
        audio.cache_key("https://www.bilibili.com/video/BV1pTap6UE1w/")
        == "audio:bilibili:bv1ptap6ue1w"
    )


def test_cache_key_youtube_variants(audio):
    """YouTube 的各种写法归一成同一个音频 key。"""
    assert audio.cache_key("https://youtu.be/aqz-KE-bpKQ") == (
        "audio:youtube:aqz-ke-bpkq"
    )
    assert (
        audio.cache_key("https://www.youtube.com/watch?v=aqz-KE-bpKQ&list=RDMM")
        == "audio:youtube:aqz-ke-bpkq"
    )
    assert audio.cache_key("https://youtu.be/aqz-KE-bpKQ?si=x") == (
        "audio:youtube:aqz-ke-bpkq"
    )


def test_music_youtube_shape_now_allowed(keys):
    """music.youtube.com 也是 YouTube，链接识别不再漏掉它。"""
    assert keys.extract_urls(
        "https://music.youtube.com/watch?v=aqz-KE-bpKQ"
    ) == ["https://music.youtube.com/watch?v=aqz-KE-bpKQ"]


# --------------------------------------------------------------------------- #
# 4. 媒体类型
# --------------------------------------------------------------------------- #
def test_audio_extensions_map_to_record(media_mod):
    """音频扩展名按语音段投递，不影响图片 / 视频的判定。"""
    assert media_mod.kind_of(Path("a.m4a")) is media_mod.MediaKind.RECORD
    assert media_mod.kind_of(Path("a.mp3")) is media_mod.MediaKind.RECORD
    assert media_mod.kind_of(Path("a.opus")) is media_mod.MediaKind.RECORD
    assert media_mod.kind_of(Path("a.weba")) is media_mod.MediaKind.RECORD
    assert media_mod.kind_of(Path("a.mp4")) is media_mod.MediaKind.VIDEO
    assert media_mod.kind_of(Path("a.jpg")) is media_mod.MediaKind.IMAGE
    assert media_mod.kind_of(Path("a.zip")) is media_mod.MediaKind.FILE


# --------------------------------------------------------------------------- #
# 5. 编排：bm
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_handle_audio_bm_sends_record(
    plugin, monkeypatch, instance, service
):
    """bm 默认发 record 语音段，不带任何文字。"""
    _patch_bili(monkeypatch, plugin)

    bot = FakeBot()
    result = await service.handle_audio(
        instance,
        bot,
        make_group_event(f"bm {_BILI_URL}"),
        _BILI_URL,
        "bilibili",
    )

    assert result.outcome is service.Outcome.DELIVERED
    assert len(bot.sent_records) == 1, "只发一条语音"
    assert bot.sent_records[0].startswith("file://"), bot.sent_records
    assert bot.sent_records[0].endswith(".m4a")
    assert bot.sent_texts == [""], "语音之外不该有文字"
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert emojis == [
        plugin.feedback.Reaction.START,
        plugin.feedback.Reaction.OK,
    ], emojis
    assert "audio:bilibili:bv1a2xl67emp" in result.keys


@pytest.mark.asyncio
async def test_handle_audio_bm_file_option_names_by_title(
    plugin, monkeypatch, instance, service
):
    """-f 以群文件发送，文件名是清洗后的作品标题。"""
    _patch_bili(monkeypatch, plugin)

    bot = FakeBot()
    result = await service.handle_audio(
        instance,
        bot,
        make_group_event(f"bm -f {_BILI_URL}"),
        _BILI_URL,
        "bilibili",
        as_file=True,
    )

    assert result.outcome is service.Outcome.DELIVERED
    uploads = bot.uploaded_files()
    assert len(uploads) == 1, uploads
    ref, name = uploads[0]
    assert ref.startswith("file://") and ref.endswith(".m4a")
    assert name == "据说煎鸡蛋会流量好🍳.m4a"
    assert bot.sent_records == [], "-f 模式不该再发语音"


@pytest.mark.asyncio
async def test_handle_audio_bm_part_suffix_in_filename(
    plugin, monkeypatch, instance, service
):
    """多分P作品下载 P2 时文件名带 P 标签。"""
    _patch_bili(
        monkeypatch,
        plugin,
        title="《原神》过场动画-「遗赠」",
        bvid="BV1pTap6UE1w",
        part=2,
    )
    url = "https://www.bilibili.com/video/BV1pTap6UE1w/?p=2"

    bot = FakeBot()
    await service.handle_audio(
        instance,
        bot,
        make_group_event(f"bm -f {url}"),
        url,
        "bilibili",
        as_file=True,
    )

    _, name = bot.uploaded_files()[0]
    assert name == "《原神》过场动画-「遗赠」 - P2.m4a"


@pytest.mark.asyncio
async def test_handle_audio_second_call_hits_cache(
    plugin, monkeypatch, instance, service
):
    """同一作品第二次直接发缓存，不再请求 B 站。"""
    calls: dict = {}
    _patch_bili(monkeypatch, plugin, calls=calls)

    first = FakeBot()
    result = await service.handle_audio(
        instance,
        first,
        make_group_event(f"bm {_BILI_URL}"),
        _BILI_URL,
        "bilibili",
    )
    assert result.outcome is service.Outcome.DELIVERED
    assert first.sent_records

    second = FakeBot()
    result = await service.handle_audio(
        instance,
        second,
        make_group_event(f"bm {_BILI_URL}"),
        _BILI_URL,
        "bilibili",
    )
    assert result.outcome is service.Outcome.FROM_CACHE
    assert second.sent_records, "缓存命中也要发语音"

    assert calls == {"fetch": 1, "download": 1}, calls


@pytest.mark.asyncio
async def test_handle_audio_fetch_failure_notice(
    plugin, monkeypatch, instance, service
):
    """获取元信息失败：失败表情 + 一句人话。"""

    def fake_fetch(url, proxy=""):
        raise RuntimeError("这个视频不存在哦")

    monkeypatch.setattr(plugin.audio, "bilibili_audio", fake_fetch)

    bot = FakeBot()
    result = await service.handle_audio(
        instance,
        bot,
        make_group_event(f"bm {_BILI_URL}"),
        _BILI_URL,
        "bilibili",
    )

    assert result.outcome is service.Outcome.PARSE_FAILED
    assert "音频" in bot.sent_texts[0] or "失败" in bot.sent_texts[0]
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert emojis[-1] == plugin.feedback.Reaction.FAIL


@pytest.mark.asyncio
async def test_handle_audio_too_large_by_estimate(
    plugin, monkeypatch, instance, service, update_settings
):
    """码率 × 时长预估超体积上限：不下载，直接提示。"""
    calls: dict = {}
    _patch_bili(monkeypatch, plugin, calls=calls)
    update_settings(max_file_bytes=1024)  # 预估 ~426KB，必然超

    bot = FakeBot()
    result = await service.handle_audio(
        instance,
        bot,
        make_group_event(f"bm {_BILI_URL}"),
        _BILI_URL,
        "bilibili",
    )

    assert result.outcome is service.Outcome.TOO_LARGE
    assert "上限" in bot.sent_texts[0]
    assert calls.get("download", 0) == 0, "超限就不该下载"


@pytest.mark.asyncio
async def test_handle_audio_too_large_during_download(
    plugin, monkeypatch, instance, service, update_settings
):
    """下载中发现超限（yt-dlp --max-filesize 语义）：同样拦下。"""

    def fake_download(
        url, dest_dir: Path, proxy="", timeout=0, max_bytes=0, cookie=""
    ):
        raise plugin.audio.AudioTooLarge()

    _patch_yt(monkeypatch, plugin)
    monkeypatch.setattr(plugin.audio, "youtube_download", fake_download)

    bot = FakeBot()
    result = await service.handle_audio(
        instance, bot, make_group_event(f"ym {_YT_URL}"), _YT_URL, "youtube"
    )

    assert result.outcome is service.Outcome.TOO_LARGE
    assert "上限" in bot.sent_texts[0]


@pytest.mark.asyncio
async def test_handle_audio_download_failure(
    plugin, monkeypatch, instance, service
):
    """下载炸了：失败表情 + 提示，不发半成品。"""

    def fake_download(audio_url, dest: Path, proxy="", max_bytes=0):
        raise RuntimeError("CDN 403")

    _patch_bili(monkeypatch, plugin)
    monkeypatch.setattr(plugin.audio, "download_bilibili", fake_download)

    bot = FakeBot()
    result = await service.handle_audio(
        instance,
        bot,
        make_group_event(f"bm {_BILI_URL}"),
        _BILI_URL,
        "bilibili",
    )

    assert result.outcome is service.Outcome.DOWNLOAD_FAILED
    assert bot.sent_records == []
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert emojis[-1] == plugin.feedback.Reaction.FAIL


@pytest.mark.asyncio
async def test_handle_audio_record_send_failure(
    plugin, monkeypatch, instance, service
):
    """协议端拒绝 record 段：无回退，失败表情 + 提示。"""
    _patch_bili(monkeypatch, plugin)

    bot = FakeBot(fail_record=True)
    result = await service.handle_audio(
        instance,
        bot,
        make_group_event(f"bm {_BILI_URL}"),
        _BILI_URL,
        "bilibili",
    )

    assert result.outcome is service.Outcome.DELIVERY_FAILED
    assert not any(api == "upload_group_file" for api, _ in bot.calls), (
        "语音失败不许回退成文件"
    )
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert emojis[-1] == plugin.feedback.Reaction.FAIL


@pytest.mark.asyncio
async def test_handle_audio_platform_off(
    plugin, monkeypatch, instance, service, update_settings
):
    """平台白名单关掉 bilibili：提示「没开解析」。"""
    update_settings(allowed_platforms=frozenset({"douyin"}))
    _patch_bili(monkeypatch, plugin)

    bot = FakeBot()
    result = await service.handle_audio(
        instance,
        bot,
        make_group_event(f"bm {_BILI_URL}"),
        _BILI_URL,
        "bilibili",
    )

    assert result.outcome is service.Outcome.PLATFORM_OFF
    assert "bilibili" in bot.sent_texts[0]


@pytest.mark.asyncio
async def test_handle_audio_live_youtube_rejected(
    plugin, monkeypatch, instance, service
):
    """直播 / 预告不给提取音频，提示原因。"""

    def fake_fetch(url, proxy="", cookie=""):
        raise RuntimeError("不支持直播")

    monkeypatch.setattr(plugin.audio, "youtube_audio", fake_fetch)

    bot = FakeBot()
    result = await service.handle_audio(
        instance, bot, make_group_event(f"ym {_YT_URL}"), _YT_URL, "youtube"
    )

    assert result.outcome is service.Outcome.PARSE_FAILED
    assert "直播" in bot.sent_texts[0]


# --------------------------------------------------------------------------- #
# 6. 编排：ym
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_handle_audio_ym_sends_record(
    plugin, monkeypatch, instance, service
):
    """ym 默认发 record，缓存 key 用 youtube id。"""
    _patch_yt(monkeypatch, plugin)

    bot = FakeBot()
    result = await service.handle_audio(
        instance, bot, make_group_event(f"ym {_YT_URL}"), _YT_URL, "youtube"
    )

    assert result.outcome is service.Outcome.DELIVERED
    assert len(bot.sent_records) == 1
    assert bot.sent_records[0].endswith(".m4a")
    assert "audio:youtube:aqz-ke-bpkq" in result.keys


@pytest.mark.asyncio
async def test_handle_audio_ym_file_option(
    plugin, monkeypatch, instance, service
):
    """ym -f 群文件，文件名是视频标题。"""
    _patch_yt(monkeypatch, plugin)

    bot = FakeBot()
    result = await service.handle_audio(
        instance,
        bot,
        make_group_event(f"ym -f {_YT_URL}"),
        _YT_URL,
        "youtube",
        as_file=True,
    )

    assert result.outcome is service.Outcome.DELIVERED
    _, name = bot.uploaded_files()[0]
    assert name == "【Ref rain】色づく世界の明日から【AMV MAD】.m4a"


# --------------------------------------------------------------------------- #
# 7. matcher 分发
# --------------------------------------------------------------------------- #
def _adapter():
    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    return nonebot.get_adapter(OnebotV11Adapter)


def _recorder(plugin, monkeypatch, seen: list) -> None:
    """把 handle_audio 换成记录参数的假实现。"""

    async def fake(app, sender, event, target, platform, *, as_file=False):
        seen.append((target, platform, as_file))

    monkeypatch.setattr(plugin.triggers, "handle_audio", fake)


@pytest.mark.asyncio
async def test_bm_matcher_dispatches(
    app, plugin, monkeypatch, instance, matchers
):
    """bm 命令把 BV 号拼成标准链接交给音频链路。"""
    seen: list = []
    _recorder(plugin, monkeypatch, seen)

    event = make_group_event("bm BV1A2xL67EMP", message_id=21)
    async with app.test_matcher(matchers.bm) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=_adapter())
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_finished()

    assert seen == [(_BILI_CANONICAL, "bilibili", False)]


@pytest.mark.asyncio
async def test_bm_matcher_file_option(
    app, plugin, monkeypatch, instance, matchers
):
    """-f 选项透传到音频链路。"""
    seen: list = []
    _recorder(plugin, monkeypatch, seen)

    event = make_group_event("bm -f BV1A2xL67EMP", message_id=22)
    async with app.test_matcher(matchers.bm) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=_adapter())
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_finished()

    assert seen == [(_BILI_CANONICAL, "bilibili", True)]


@pytest.mark.asyncio
async def test_bm_matcher_no_target_shows_usage(
    app, plugin, monkeypatch, instance, matchers
):
    """没有目标就回用法，不去碰音频链路。"""
    seen: list = []
    _recorder(plugin, monkeypatch, seen)

    event = make_group_event("bm 今天天气不错", message_id=23)
    async with app.test_matcher(matchers.bm) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=_adapter())
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            "用法：bm <BV 号或 B 站链接>（-f 以文件发送）",
            result=None,
            bot=bot,
        )
        ctx.should_finished()

    assert seen == []


@pytest.mark.asyncio
async def test_bm_matcher_av_hint(app, plugin, monkeypatch, instance, matchers):
    """av 号专门提示不支持。"""
    seen: list = []
    _recorder(plugin, monkeypatch, seen)

    event = make_group_event("bm av170001", message_id=24)
    async with app.test_matcher(matchers.bm) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=_adapter())
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            "不支持 AV 号，请发 BV 号或视频链接。",
            result=None,
            bot=bot,
        )
        ctx.should_finished()

    assert seen == []


@pytest.mark.asyncio
async def test_ym_matcher_dispatches(
    app, plugin, monkeypatch, instance, matchers
):
    """ym 命令把裸视频 ID 拼成链接交给音频链路。"""
    seen: list = []
    _recorder(plugin, monkeypatch, seen)

    event = make_group_event("ym aqz-KE-bpKQ", message_id=25)
    async with app.test_matcher(matchers.ym) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=_adapter())
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_finished()

    assert seen == [("https://youtu.be/aqz-KE-bpKQ", "youtube", False)]


@pytest.mark.asyncio
async def test_ym_matcher_no_target_shows_usage(
    app, plugin, monkeypatch, instance, matchers
):
    seen: list = []
    _recorder(plugin, monkeypatch, seen)

    event = make_group_event("ym 今天天气不错", message_id=26)
    async with app.test_matcher(matchers.ym) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=_adapter())
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            "用法：ym <YouTube 链接或视频 ID>（-f 以文件发送）",
            result=None,
            bot=bot,
        )
        ctx.should_finished()

    assert seen == []


@pytest.mark.asyncio
async def test_bm_denied_when_closed(
    app, plugin, monkeypatch, instance, switches, matchers
):
    """关掉解析后普通成员不能用 bm。"""
    seen: list = []
    _recorder(plugin, monkeypatch, seen)
    instance.switches.set(switches.group_key(123456), False)

    event = make_group_event("bm BV1A2xL67EMP", user_id=20000, message_id=27)
    async with app.test_matcher(matchers.bm) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=_adapter())
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            "本群的解析已关闭，只有超管或群管理员能手动解析。",
            result=None,
            bot=bot,
        )
        ctx.should_finished()

    assert seen == []
