"""平台白名单（``PARSEHUB_PLATFORMS``）：留空 = 全部，填了就只有这些平台解析。

判断必须挡在媒体缓存前面，否则被关掉的平台还能靠缓存把旧作品发出来。
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

BILI = "https://b23.tv/abcd"
DOUYIN = "https://v.douyin.com/abcdef/"
XHS = "https://xhslink.cn/o/7mQiQjVqUtO"


@pytest.fixture()
def tmp_media(tmp_path, monkeypatch, plugin, server):
    """中转根目录指到临时目录。"""
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
    platform_id: str,
    display_name: str = "测试平台",
) -> dict[str, int]:
    """装好假的解析链路，返回调用计数。"""
    media_file = tmp_media / "media.mp4"
    media_file.write_bytes(b"x" * 1024)
    calls = {"parse": 0, "download": 0}

    async def fake_parse(url, proxy="", cookie=""):
        calls["parse"] += 1
        ref = FakeMediaRef("https://cdn.example.com/media.mp4")
        ref.duration = 10
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
        core.CLIENT,
        "platform_id",
        lambda url: asyncio.sleep(0, result=platform_id),
    )
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
    monkeypatch.setattr(card, "fetch_cover", lambda result, files: b"cover")
    monkeypatch.setattr(card, "render", fake_render)
    return calls


@pytest.mark.asyncio
async def test_platform_outside_whitelist_is_skipped(
    plugin, monkeypatch, tmp_media, core, card
):
    """白名单里只有 bilibili 时，抖音链接不解析、不下载、不发言。"""
    monkeypatch.setattr(
        plugin.plugin_config, "parsehub_platforms", ["bilibili"]
    )
    calls = _wire(monkeypatch, core, card, tmp_media, platform_id="douyin")
    bot = FakeBot()
    await plugin.handle_link(bot, make_group_event(DOUYIN), DOUYIN)

    assert calls == {"parse": 0, "download": 0}, calls
    assert [api for api, _ in bot.calls if api == "send_msg"] == []


@pytest.mark.asyncio
async def test_platform_inside_whitelist_parses(
    plugin, monkeypatch, tmp_media, core, card
):
    """白名单里的平台照常解析发送。"""
    monkeypatch.setattr(
        plugin.plugin_config, "parsehub_platforms", ["bilibili"]
    )
    calls = _wire(monkeypatch, core, card, tmp_media, platform_id="bilibili")
    bot = FakeBot()
    await plugin.handle_link(bot, make_group_event(BILI), BILI)

    assert calls == {"parse": 1, "download": 1}, calls


@pytest.mark.asyncio
async def test_empty_whitelist_allows_everything(
    plugin, monkeypatch, tmp_media, core, card
):
    """留空 = 全部平台（默认行为不变）。"""
    monkeypatch.setattr(plugin.plugin_config, "parsehub_platforms", [])
    calls = _wire(monkeypatch, core, card, tmp_media, platform_id="douyin")
    bot = FakeBot()
    await plugin.handle_link(bot, make_group_event(DOUYIN), DOUYIN)

    assert calls["download"] == 1, calls


@pytest.mark.asyncio
async def test_chinese_alias_matches_real_id(
    plugin, monkeypatch, tmp_media, core, card
):
    """白名单写「小红书」也能匹配 parsehub 的 id xhs。"""
    monkeypatch.setattr(
        plugin.plugin_config, "parsehub_platforms", ["哔哩哔哩", "小红书"]
    )
    calls = _wire(monkeypatch, core, card, tmp_media, platform_id="xhs")
    bot = FakeBot()
    await plugin.handle_link(bot, make_group_event(XHS), XHS)

    assert calls["download"] == 1, calls


@pytest.mark.asyncio
async def test_manual_command_tells_user_platform_is_off(
    plugin, monkeypatch, tmp_media, core, card
):
    """命令触发时明确回一句，别让用户以为坏了。"""
    monkeypatch.setattr(
        plugin.plugin_config, "parsehub_platforms", ["bilibili"]
    )
    _wire(monkeypatch, core, card, tmp_media, platform_id="weibo")
    bot = FakeBot()
    await plugin.handle_link(
        bot, make_group_event("/解析 " + DOUYIN), DOUYIN, manual=True
    )

    assert any("暂时没开解析" in text for text in bot.sent_texts), (
        bot.sent_texts
    )


@pytest.mark.asyncio
async def test_blocked_platform_is_not_served_from_cache(
    plugin, monkeypatch, tmp_media, core, card
):
    """缓存里存着被关平台的作品，也不能靠缓存发出来。"""
    # 先不限平台，把作品写进缓存
    monkeypatch.setattr(plugin.plugin_config, "parsehub_platforms", [])
    calls = _wire(monkeypatch, core, card, tmp_media, platform_id="douyin")
    first = FakeBot()
    await plugin.handle_link(first, make_group_event(DOUYIN), DOUYIN)
    assert calls["download"] == 1

    # 再关掉抖音，换个群发同一个作品
    monkeypatch.setattr(
        plugin.plugin_config, "parsehub_platforms", ["bilibili"]
    )
    plugin.DEDUPE._seen.clear()
    second = FakeBot()
    await plugin.handle_link(
        second, make_group_event(DOUYIN, group_id=654321), DOUYIN
    )
    assert calls["parse"] == 1, "被关的平台不该再解析"
    assert [api for api, _ in second.calls if api == "send_msg"] == [], (
        "缓存里的作品也不该发"
    )
