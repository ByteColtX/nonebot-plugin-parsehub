"""视频时长上限：超过就只发封面 + 一句提示，不下载。

默认 5 分钟（``PARSEHUB_MAX_SECONDS=300``），设 0 表示不限。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fake import make_group_event
from test_plugin import (
    FakeBot,
    FakeDownloadResult,
    FakeMediaRef,
    FakeParseResult,
)

URL = "https://b23.tv/abcd"


@pytest.fixture()
def tmp_media(tmp_path, monkeypatch, plugin, server):
    """中转根目录指到临时目录，避免真写到仓库里。"""
    monkeypatch.setattr(server.RELAY, "root", tmp_path)
    monkeypatch.setattr(plugin._ENDPOINT, "host", "127.0.0.1")
    monkeypatch.setattr(plugin._ENDPOINT, "port", 18899)
    return tmp_path


def _wire(
    monkeypatch,
    core,
    card,
    tmp_media,
    *,
    duration: int,
    platform_id: str = "bilibili",
    display_name: str = "哔哩哔哩",
    ext: str = "mp4",
) -> dict[str, int]:
    """装好假的 parse / download / probe_size，返回调用计数。"""
    media_file = tmp_media / f"media.{ext}"
    media_file.write_bytes(b"x" * 1024)
    calls = {"download": 0}

    async def fake_parse(url, proxy="", cookie=""):
        ref = FakeMediaRef(f"https://cdn.example.com/media.{ext}", ext)
        ref.duration = duration
        result = FakeParseResult(media=ref)
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
        core.CLIENT, "is_supported", lambda url: asyncio.sleep(0, result=True)
    )
    monkeypatch.setattr(core.CLIENT, "parse", fake_parse)
    monkeypatch.setattr(core.CLIENT, "download", fake_download)
    monkeypatch.setattr(
        core,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=1024),
    )
    # 有封面 → 走真卡片分支（渲染换成假的，测试不依赖 Pillow 排版）
    monkeypatch.setattr(
        card, "fetch_cover", lambda result, files, proxy="": b"cover"
    )
    monkeypatch.setattr(card, "render", fake_render)
    return calls


@pytest.mark.asyncio
async def test_long_video_sends_cover_and_notice(
    plugin, monkeypatch, tmp_media, core, card
):
    """超过上限：发封面 + 提示，且完全不下载。"""
    calls = _wire(monkeypatch, core, card, tmp_media, duration=600)
    bot = FakeBot()
    await plugin.handle_link(bot, make_group_event(URL), URL)

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
    plugin, monkeypatch, tmp_media, core, card
):
    """没超上限：照常下载发送，没有提示。"""
    calls = _wire(monkeypatch, core, card, tmp_media, duration=120)
    bot = FakeBot()
    await plugin.handle_link(bot, make_group_event(URL), URL)

    assert calls["download"] == 1, "正常视频应该下载"
    assert not any("太长" in t for t in bot.sent_texts), bot.sent_texts


@pytest.mark.asyncio
async def test_limit_zero_means_unlimited(
    plugin, monkeypatch, tmp_media, core, card
):
    """``PARSEHUB_MAX_SECONDS=0`` 时不做时长判断。"""
    monkeypatch.setattr(plugin.plugin_config, "parsehub_max_seconds", 0)
    calls = _wire(monkeypatch, core, card, tmp_media, duration=99999)
    bot = FakeBot()
    await plugin.handle_link(bot, make_group_event(URL), URL)

    assert calls["download"] == 1, "设 0 应该不限时长"
    assert not any("太长" in t for t in bot.sent_texts), bot.sent_texts


@pytest.mark.asyncio
async def test_cached_long_video_is_still_gated(
    plugin, monkeypatch, tmp_media, core, card
):
    """缓存里存着加限制之前的旧条目，也要被时长上限拦住。"""
    monkeypatch.setattr(plugin.plugin_config, "parsehub_max_seconds", 0)
    calls = _wire(monkeypatch, core, card, tmp_media, duration=600)
    first = FakeBot()
    await plugin.handle_link(first, make_group_event(URL), URL)
    assert calls["download"] == 1, "先按不限时长跑一次，把媒体写进缓存"

    # 打开限制、换个群再来一次：缓存里有，但时长判断在缓存查询之前
    monkeypatch.setattr(plugin.plugin_config, "parsehub_max_seconds", 300)
    second = FakeBot()
    await plugin.handle_link(
        second, make_group_event(URL, group_id=654321), URL
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
    plugin, monkeypatch, tmp_media, core, card
):
    """小红书给的是毫秒：400 秒的视频要按 400 秒判，不能当成 400000 秒。"""
    _wire(
        monkeypatch,
        core,
        card,
        tmp_media,
        duration=400_000,
        platform_id="xhs",
        display_name="小红书",
    )
    bot = FakeBot()
    await plugin.handle_link(bot, make_group_event(URL), URL)
    assert "视频太长啦，还是去小红书看吧" in bot.sent_texts, bot.sent_texts

    # 同一平台 8 秒的视频不该被拦
    calls = _wire(
        monkeypatch,
        core,
        card,
        tmp_media,
        duration=7967,
        platform_id="xhs",
        display_name="小红书",
    )
    plugin.DEDUPE._seen.clear()
    bot2 = FakeBot()
    await plugin.handle_link(
        bot2, make_group_event("https://xhslink.cn/o/abc"), URL
    )
    assert calls["download"] == 1, "8 秒的小红书视频不该被时长上限拦下"
