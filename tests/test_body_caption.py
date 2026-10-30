# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""无标题正文帖：卡片只放摘要，正文要再补一条文字。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fake import make_group_event
from fakes import FakeBot


def _result(title: str = "", content: str = "正文内容") -> SimpleNamespace:
    return SimpleNamespace(
        platform=SimpleNamespace(id="coolapk", display_name="酷安"),
        title=title,
        content=content,
    )


@pytest.mark.asyncio
async def test_body_only_card_sends_caption(
    instance, service, tmp_media: Path, monkeypatch
) -> None:
    """卡片成功时，无标题帖仍补发正文。"""
    card_path = tmp_media / "card.jpg"
    card_path.write_bytes(b"card")

    async def fake_render(*args, **kwargs):
        return card_path

    monkeypatch.setattr(instance.cards, "render", fake_render)
    bot = FakeBot()

    info = await service.message.send_card_or_text(
        instance, bot, make_group_event("x"), _result(), []
    )

    assert info.card == card_path
    assert "正文内容" in info.caption
    assert len(bot.sent_images) == 1
    assert any("正文内容" in text for text in bot.sent_texts)


@pytest.mark.asyncio
async def test_titled_card_does_not_send_caption(
    instance, service, tmp_media: Path, monkeypatch
) -> None:
    """有标题时不补发，避免卡片和文字重复。"""
    card_path = tmp_media / "card.jpg"
    card_path.write_bytes(b"card")

    async def fake_render(*args, **kwargs):
        return card_path

    monkeypatch.setattr(instance.cards, "render", fake_render)
    bot = FakeBot()

    info = await service.message.send_card_or_text(
        instance, bot, make_group_event("x"), _result(title="标题"), []
    )

    assert info.card == card_path
    assert info.caption == ""
    assert not any("正文内容" in text for text in bot.sent_texts)


@pytest.mark.asyncio
async def test_missing_cover_logs_only_on_final_attempt(
    plugin, instance, service, tmp_media: Path, monkeypatch
) -> None:
    """下载前没封面只是暂态，不能在日志里说「卡片跳过」。"""
    image = tmp_media / "a.jpg"
    image.write_bytes(b"image")
    files = [
        plugin.media.MediaFile(path=image, kind=plugin.media.MediaKind.IMAGE)
    ]
    parsed = _result()
    seen: list[str] = []

    class FakeLogger:
        def info(self, message: str) -> None:
            seen.append(message)

        def debug(self, message: str) -> None:
            seen.append(message)

    async def no_cover(*args, **kwargs):
        return None

    monkeypatch.setattr(instance.cards, "render", no_cover)
    monkeypatch.setattr(service.message, "logger", FakeLogger())

    await service.message.send_card_or_text(
        instance,
        FakeBot(),
        make_group_event("x"),
        parsed,
        [],
        allow_text=False,
    )
    assert not any("卡片跳过" in message for message in seen)
    assert any("下载后重试" in message for message in seen)

    seen.clear()
    await service.message.send_card_or_text(
        instance,
        FakeBot(),
        make_group_event("x"),
        parsed,
        files,
    )
    assert any("卡片跳过" in message for message in seen)


@pytest.mark.asyncio
async def test_cached_body_caption_is_sent_after_card(
    instance, service, tmp_media: Path
) -> None:
    """缓存命中也要把正文帖的补发文字发出来。"""
    card_path = tmp_media / "card.jpg"
    card_path.write_bytes(b"card")
    media_path = tmp_media / "a.jpg"
    media_path.write_bytes(b"image")
    instance.cache.store(
        ["coolapk:1"],
        [media_path],
        card=card_path,
        caption="📦 酷安\n正文内容",
    )
    entry = instance.cache.get("coolapk:1")
    assert entry is not None

    bot = FakeBot()
    result = await service.works._deliver_cached(
        instance, bot, make_group_event("x"), entry, "coolapk:1"
    )

    assert result.outcome is service.Outcome.FROM_CACHE
    assert len(bot.sent_images) == 2  # 卡片 + 缓存里的图片
    assert any("正文内容" in text for text in bot.sent_texts)
