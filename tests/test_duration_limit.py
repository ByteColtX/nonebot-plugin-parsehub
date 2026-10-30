"""视频时长上限：超过就只发封面 + 一句提示，不下载。

默认 5 分钟（``PARSEHUB_MAX_SECONDS=300``），设 0 表示不限。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fake import make_group_event
from fakes import (
    FakeBot,
    FakeDownloadResult,
    FakeMediaRef,
    FakeParseResult,
)

URL = "https://b23.tv/abcd"


def _wire(
    monkeypatch,
    instance,
    card,
    tmp_media,
    *,
    duration: int = 0,
    durations: list[int] | None = None,
    platform_id: str = "bilibili",
    display_name: str = "哔哩哔哩",
    ext: str = "mp4",
    media_mod,
) -> dict[str, int]:
    """装好假的 parse / download / probe_size，返回调用计数。

    ``durations`` 用来造多视频作品；不给时按单个 ``duration`` 造。
    """
    media_file = tmp_media / f"media.{ext}"
    media_file.write_bytes(b"x" * 1024)
    calls = {"download": 0}

    async def fake_parse(url, proxy="", cookie=""):
        values = durations if durations is not None else [duration]
        refs = []
        for index, value in enumerate(values):
            ref = FakeMediaRef(
                f"https://cdn.example.com/media{index}.{ext}", ext
            )
            ref.duration = value
            refs.append(ref)
        result = FakeParseResult(media=refs if len(refs) > 1 else refs[0])
        result.platform = SimpleNamespace(
            id=platform_id, display_name=display_name
        )
        return result

    async def fake_download(url, path, proxy="", cookie=""):
        calls["download"] += 1
        return FakeDownloadResult([media_file])

    def fake_render(result, cover, out):
        out.write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download", fake_download)
    monkeypatch.setattr(
        media_mod,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=1024),
    )
    # 有封面 → 走真卡片分支（渲染换成假的，测试不依赖 Pillow 排版）
    monkeypatch.setattr(
        card.cover, "fetch_cover", lambda result, files, proxy="": b"cover"
    )
    monkeypatch.setattr(card.layout, "render", fake_render)
    return calls


@pytest.mark.asyncio
async def test_long_video_sends_cover_and_notice(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
):
    """超过上限：发封面 + 提示，且完全不下载。"""
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=600,
        media_mod=media_mod,
    )
    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(URL), URL)

    texts = bot.sent_texts
    assert "视频太长啦，还是去B 站看吧" in texts, texts
    assert calls["download"] == 0, "长视频不该下载"
    assert "upload_group_file" not in [api for api, _ in bot.calls]
    # 封面（卡片）仍然要发
    assert any(
        api == "send_msg" and kw["message"][0].type == "image"
        for api, kw in bot.calls
    ), bot.calls


@pytest.mark.asyncio
async def test_short_video_downloads_normally(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
):
    """没超上限：照常下载发送，没有提示。"""
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=120,
        media_mod=media_mod,
    )
    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(URL), URL)

    assert calls["download"] == 1, "正常视频应该下载"
    assert not any("太长" in t for t in bot.sent_texts), bot.sent_texts


@pytest.mark.asyncio
async def test_limit_zero_means_unlimited(
    plugin,
    monkeypatch,
    tmp_media,
    card,
    instance,
    service,
    media_mod,
    update_settings,
):
    """``PARSEHUB_MAX_SECONDS=0`` 时不做时长判断。"""
    update_settings(max_seconds=0)
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=99999,
        media_mod=media_mod,
    )
    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(URL), URL)

    assert calls["download"] == 1, "设 0 应该不限时长"
    assert not any("太长" in t for t in bot.sent_texts), bot.sent_texts


@pytest.mark.asyncio
async def test_cached_long_video_is_still_gated(
    plugin,
    monkeypatch,
    tmp_media,
    card,
    instance,
    service,
    media_mod,
    update_settings,
):
    """缓存里存着加限制之前的旧条目，也要被时长上限拦住。"""
    update_settings(max_seconds=0)
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=600,
        media_mod=media_mod,
    )
    first = FakeBot()
    await service.handle(instance, first, make_group_event(URL), URL)
    assert calls["download"] == 1, "先按不限时长跑一次，把媒体写进缓存"

    # 打开限制、换个群再来一次：缓存里有，但时长判断在缓存查询之前
    update_settings(max_seconds=300)
    second = FakeBot()
    await service.handle(
        instance, second, make_group_event(URL, group_id=654321), URL
    )
    assert "视频太长啦，还是去B 站看吧" in second.sent_texts, second.sent_texts
    assert not any(
        seg.type == "video"
        for api, kw in second.calls
        if api == "send_msg"
        for seg in kw["message"]
    ), "长视频不该从缓存里发出来"


@pytest.mark.asyncio
async def test_millisecond_platform_converts_before_checking(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
):
    """小红书给的是毫秒：400 秒的视频要按 400 秒判，不能当成 400000 秒。"""
    _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=400_000,
        platform_id="xhs",
        display_name="小红书",
        media_mod=media_mod,
    )
    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(URL), URL)
    assert "视频太长啦，还是去小红书看吧" in bot.sent_texts, bot.sent_texts

    # 同一平台 8 秒的视频不该被拦
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=7967,
        platform_id="xhs",
        display_name="小红书",
        media_mod=media_mod,
    )
    instance.dedupe._seen.clear()
    bot2 = FakeBot()
    await service.handle(
        instance, bot2, make_group_event("https://xhslink.cn/o/abc"), URL
    )
    assert calls["download"] == 1, "8 秒的小红书视频不该被时长上限拦下"


@pytest.mark.asyncio
async def test_kuaishou_milliseconds_are_not_treated_as_long(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
):
    """快手报告 11933，实际 11.93 秒，不能按 11933 秒拦下载。"""
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=11933,
        platform_id="kuaishou",
        display_name="快手",
        media_mod=media_mod,
    )
    bot = FakeBot()
    await service.handle(
        instance, bot, make_group_event("https://v.kuaishou.com/abc"), URL
    )
    assert calls["download"] == 1, "12 秒的快手视频不该被时长上限拦下"
    assert not any("太长" in t for t in bot.sent_texts), bot.sent_texts


@pytest.mark.asyncio
async def test_long_seconds_video_is_blocked(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
):
    """YouTube 这类秒单位的 4 小时视频不能被当成 14 秒放过去。"""
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=14400,
        platform_id="youtube",
        display_name="YouTube",
        media_mod=media_mod,
    )
    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(URL), URL)
    assert "视频太长啦，还是去YouTube看吧" in bot.sent_texts, bot.sent_texts
    assert calls["download"] == 0, "4 小时视频不该下载"


def test_long_seconds_are_not_milliseconds(card, plugin) -> None:
    """秒单位的 4 小时仍是 4 小时；只有明确的毫秒平台才除以 1000。"""
    assert plugin.platforms.duration_seconds("youtube", [14400]) == 14400
    assert card.layout.format_duration(14400) == "4:00:00"
    assert plugin.platforms.duration_seconds("douyin", [14400]) == 14
    assert card.layout.format_duration(14400, is_ms=True) == "00:14"


def _fake_probe(plugin, monkeypatch, seconds: int) -> dict[str, int]:
    """把视频号时长探测换成固定返回值，记录调用次数。"""
    calls = {"probe": 0}

    def probe(url, *, timeout=8.0, proxy=""):
        calls["probe"] += 1
        return seconds

    monkeypatch.setattr(plugin.service.duration, "probe", probe)
    return calls


@pytest.mark.asyncio
async def test_weixin_channels_probes_duration_before_download(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
):
    """视频号上游 duration 恒为 0：下载前从容器头探出真实时长并拦截。"""
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=0,
        platform_id="weixin_channels",
        display_name="微信视频号",
        media_mod=media_mod,
    )
    probed = _fake_probe(plugin, monkeypatch, 600)

    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(URL), URL)

    assert probed["probe"] == 1, "视频号必须在下载前探测时长"
    assert "视频太长啦，还是去微信视频号看吧" in bot.sent_texts, bot.sent_texts
    assert calls["download"] == 0, "超长视频号视频不该下载"


@pytest.mark.asyncio
async def test_weixin_channels_probe_failure_falls_through(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
):
    """探测失败按未知处理：照常下载，不因为探测失败就丢掉作品。"""
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=0,
        platform_id="weixin_channels",
        display_name="微信视频号",
        media_mod=media_mod,
    )
    probed = _fake_probe(plugin, monkeypatch, 0)

    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(URL), URL)

    assert probed["probe"] == 1
    assert calls["download"] == 1, "探测失败时应按未知时长继续"
    assert not any("太长" in t for t in bot.sent_texts), bot.sent_texts


@pytest.mark.asyncio
async def test_probe_skipped_when_limit_disabled(
    plugin,
    monkeypatch,
    tmp_media,
    card,
    instance,
    service,
    media_mod,
    update_settings,
):
    """``PARSEHUB_MAX_SECONDS=0`` 时没有上限要判，不该多花一次探测。"""
    update_settings(max_seconds=0)
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=0,
        platform_id="weixin_channels",
        display_name="微信视频号",
        media_mod=media_mod,
    )
    probed = _fake_probe(plugin, monkeypatch, 600)

    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(URL), URL)

    assert probed["probe"] == 0, "不限时长时不需要探测"
    assert calls["download"] == 1


@pytest.mark.asyncio
async def test_probe_skipped_when_duration_is_known(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
):
    """解析结果已经给了时长：不再多花一次探测。"""
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=120,
        platform_id="zuiyou",
        display_name="最右",
        media_mod=media_mod,
    )
    probed = _fake_probe(plugin, monkeypatch, 600)

    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(URL), URL)

    assert probed["probe"] == 0, "已知时长不该再探测"
    assert calls["download"] == 1


@pytest.mark.asyncio
async def test_any_platform_with_unknown_duration_is_probed(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
):
    """不只视频号：任何平台解析结果为 0 的视频都走同一套兜底。"""
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=0,
        platform_id="zuiyou",
        display_name="最右",
        media_mod=media_mod,
    )
    probed = _fake_probe(plugin, monkeypatch, 600)

    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(URL), URL)

    assert probed["probe"] == 1, "非视频号平台的 0 时长也要探测"
    assert "视频太长啦，还是去最右看吧" in bot.sent_texts, bot.sent_texts
    assert calls["download"] == 0


@pytest.mark.asyncio
async def test_multiple_unknown_videos_probe_takes_longest(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
):
    """多视频作品逐个探测，任意一个超限就停，取最长值判断。"""
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        durations=[0, 0],
        media_mod=media_mod,
    )
    values = iter([100, 600])
    probed = {"probe": 0}

    def probe(url, *, timeout=8.0, proxy=""):
        probed["probe"] += 1
        return next(values)

    monkeypatch.setattr(plugin.service.duration, "probe", probe)

    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(URL), URL)

    assert probed["probe"] == 2, "两个视频都要探测"
    assert "视频太长啦，还是去B 站看吧" in bot.sent_texts, bot.sent_texts
    assert calls["download"] == 0


@pytest.mark.asyncio
async def test_image_post_does_not_probe(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
):
    """图片帖没有视频引用，不该为「时长为 0」白探一次。"""
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=0,
        platform_id="weixin_channels",
        display_name="微信视频号",
        ext="jpg",
        media_mod=media_mod,
    )
    probed = _fake_probe(plugin, monkeypatch, 600)

    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(URL), URL)

    assert probed["probe"] == 0, "图片不该探测时长"
    assert calls["download"] == 1


def test_parse_duration_from_ffmpeg_stderr(plugin) -> None:
    """ffmpeg 的 Duration 行要能转成秒；N/A 视为未知。"""
    duration = plugin.service.duration
    stderr = (
        "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'https://example.com/a.mp4':\n"
        "  Duration: 00:01:07.34, start: 0.000000, bitrate: 1048 kb/s\n"
    )
    assert duration.parse_duration(stderr) == 67
    assert duration.parse_duration("Duration: 01:02:03.00, start: 0") == 3723
    assert duration.parse_duration("  Duration: N/A, start: 0.000000") == 0
    assert duration.parse_duration("") == 0
