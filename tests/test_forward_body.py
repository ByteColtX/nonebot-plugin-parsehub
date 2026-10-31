# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""长正文平台：卡片后只发一个「正文 + 资源」的合并转发块。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake import make_group_event
from fakes import FakeBot, FakeDownloadResult, FakeMediaRef


def _parsed(
    *,
    platform: str = "coolapk",
    post_type: str = "image",
    content: str = "酷安正文",
    media=None,
):
    """构造一个带正文的解析结果。"""
    return SimpleNamespace(
        title="标题",
        content=content,
        media=media,
        raw_url=f"https://example.com/{platform}/1",
        platform=SimpleNamespace(id=platform, display_name=platform),
        type=SimpleNamespace(value=post_type),
    )


def test_long_body_platforms(platforms) -> None:
    """酷安 / 微博 / 富文本 / 长正文要进转发；短文案平台保持原样。"""
    assert platforms.needs_body_forward("coolapk", "image", "正文")
    assert platforms.needs_body_forward("weibo", "video", "正文")
    assert platforms.needs_body_forward("zhihu", "richtext", "短")
    assert platforms.needs_body_forward("bilibili", "image", "动态正文")
    assert platforms.needs_body_forward("bilibili", "video", "长" * 200)
    assert not platforms.needs_body_forward("bilibili", "video", "简介")
    assert not platforms.needs_body_forward("douyin", "video", "正文")


def test_forward_body_uses_content_only(instance, service) -> None:
    """转发块里放正文，不重复卡片里的标题和平台名。"""
    body = service.message.forward_body(
        _parsed(content="这是一段正文内容"), instance.settings
    )
    assert body == "这是一段正文内容"
    assert "标题" not in body
    assert "酷安" not in body


@pytest.mark.asyncio
async def test_media_with_caption_uses_one_forward(
    plugin, instance, tmp_media: Path
) -> None:
    """有正文时单图也强制合并转发，正文在第一个节点。"""
    image = tmp_media / "a.jpg"
    image.write_bytes(b"image")
    files = [
        plugin.media.MediaFile(path=image, kind=plugin.media.MediaKind.IMAGE)
    ]

    bot = FakeBot()
    await instance.delivery.send_media(
        bot, make_group_event("x"), files, caption="酷安正文"
    )

    forwards = [
        kwargs for api, kwargs in bot.calls if api == "send_group_forward_msg"
    ]
    assert len(forwards) == 1
    nodes = forwards[0]["messages"]
    assert nodes[0]["data"]["content"] == [
        {"type": "text", "data": {"text": "酷安正文"}}
    ]
    assert nodes[1]["data"]["content"][0]["type"] == "image"
    assert bot.sent_texts == []


@pytest.mark.asyncio
async def test_caption_only_in_first_batch(
    plugin, instance, tmp_media: Path
) -> None:
    """超过一批时，正文只进第一批，后续批次只有媒体。"""
    chunk = plugin.media.chunk_size(instance.settings.forward_timeout)
    count = chunk + 5
    files = []
    for i in range(count):
        path = tmp_media / f"c{i}.jpg"
        path.write_bytes(b"x")
        files.append(
            plugin.media.MediaFile(path=path, kind=plugin.media.MediaKind.IMAGE)
        )

    bot = FakeBot()
    await instance.delivery.send_media(
        bot, make_group_event("x"), files, caption="酷安正文"
    )

    forwards = [
        kwargs for api, kwargs in bot.calls if api == "send_group_forward_msg"
    ]
    assert [len(fwd["messages"]) for fwd in forwards] == [
        chunk + 1,
        count - chunk,
    ]
    assert forwards[0]["messages"][0]["data"]["content"] == [
        {"type": "text", "data": {"text": "酷安正文"}}
    ]
    assert all(
        node["data"]["content"][0]["type"] == "image"
        for node in forwards[1]["messages"]
    )
    assert bot.sent_texts == []


@pytest.mark.asyncio
async def test_forward_failure_falls_back_to_separate_caption(
    plugin, instance, tmp_media: Path, monkeypatch
) -> None:
    """协议端不支持合并转发时，正文和媒体仍要发出去。"""
    image = tmp_media / "a.jpg"
    image.write_bytes(b"image")
    files = [
        plugin.media.MediaFile(path=image, kind=plugin.media.MediaKind.IMAGE)
    ]

    async def no_forward(*args, **kwargs):
        return False

    monkeypatch.setattr(instance.delivery, "send_forward", no_forward)
    bot = FakeBot()
    await instance.delivery.send_media(
        bot, make_group_event("x"), files, caption="酷安正文"
    )

    assert "酷安正文" in bot.sent_texts
    assert len(bot.sent_images) == 1


@pytest.mark.asyncio
async def test_pipeline_card_then_body_forward(
    plugin,
    instance,
    service,
    media_mod,
    monkeypatch,
    tmp_media: Path,
) -> None:
    """完整流水线：先卡片，再「正文 + 资源」转发，不单独发正文。"""
    card_path = tmp_media / "card.jpg"
    card_path.write_bytes(b"card")
    image = tmp_media / "a.jpg"
    image.write_bytes(b"image")

    async def fake_render(*args, **kwargs):
        return card_path

    async def fake_parse(url, proxy="", cookie=""):
        return _parsed(
            content="酷安正文内容",
            media=[FakeMediaRef("https://cdn.example.com/a.jpg", "jpg")],
        )

    async def fake_download(result, path, proxy="", cookie=""):
        return FakeDownloadResult([image])

    monkeypatch.setattr(instance.cards, "render", fake_render)
    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download_parsed", fake_download)
    monkeypatch.setattr(
        media_mod,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=1024),
    )

    bot = FakeBot()
    url = "https://www.coolapk.com/feed/1"
    result = await service.handle(instance, bot, make_group_event(url), url)

    assert result.outcome is service.Outcome.DELIVERED
    assert len(bot.sent_images) == 1  # 卡片
    forwards = [
        kwargs for api, kwargs in bot.calls if api == "send_group_forward_msg"
    ]
    assert len(forwards) == 1
    nodes = forwards[0]["messages"]
    assert nodes[0]["data"]["content"] == [
        {"type": "text", "data": {"text": "酷安正文内容"}}
    ]
    assert nodes[1]["data"]["content"][0]["type"] == "image"
    assert "酷安正文内容" not in bot.sent_texts


@pytest.mark.asyncio
async def test_cached_forward_caption_replays_as_forward(
    plugin, instance, service, tmp_media: Path
) -> None:
    """缓存命中也按转发块重放，不把正文退化成单独文字。"""
    card_path = tmp_media / "card.jpg"
    card_path.write_bytes(b"card")
    image = tmp_media / "a.jpg"
    image.write_bytes(b"image")
    instance.cache.store(
        ["coolapk:1"],
        [image],
        card=card_path,
        caption="酷安正文内容",
        caption_in_forward=True,
    )
    entry = instance.cache.get("coolapk:1")
    assert entry is not None
    assert entry.caption_in_forward is True

    bot = FakeBot()
    result = await service.works._deliver_cached(
        instance, bot, make_group_event("x"), entry, "coolapk:1"
    )

    assert result.outcome is service.Outcome.FROM_CACHE
    assert len(bot.sent_images) == 1  # 卡片
    forwards = [
        kwargs for api, kwargs in bot.calls if api == "send_group_forward_msg"
    ]
    assert len(forwards) == 1
    nodes = forwards[0]["messages"]
    assert nodes[0]["data"]["content"] == [
        {"type": "text", "data": {"text": "酷安正文内容"}}
    ]
    assert nodes[1]["data"]["content"][0]["type"] == "image"
    assert "酷安正文内容" not in bot.sent_texts
