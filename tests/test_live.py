# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""直播间封面卡片：识别、合成结果、元信息行与投递分支。

全部离线：直播接口、短链展开、封面下载都换成假实现，只验证插件自身的行为。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fake import make_group_event
from nonebot.adapters.onebot.v11 import Bot, Message


class FakeBot(Bot):
    """记录所有 API 调用的假 Bot。"""

    def __init__(self) -> None:
        super().__init__(adapter=MagicMock(), self_id="10000")
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, api: str, **kwargs):  # type: ignore[override]
        self.calls.append((api, kwargs))
        return {"status": "ok", "retcode": 0, "data": None}

    @property
    def sent_images(self) -> list[str]:
        out = []
        for api, kwargs in self.calls:
            if api != "send_msg":
                continue
            message = kwargs.get("message")
            if isinstance(message, Message):
                out.extend(
                    seg.data.get("file", "")
                    for seg in message
                    if seg.type == "image"
                )
        return out


_LIVE_URL = "https://live.bilibili.com/22603245"
_SHORT_URL = "https://b23.tv/o9LYdgy"


def _info(**overrides):
    from nonebot_plugin_parsehub.live import LiveInfo

    fields = {
        "room_id": 22603245,
        "title": "夜猫子起床影之刃零逆天",
        "uname": "永雏塔菲",
        "user_cover": "https://i0.hdslb.com/bfs/live/new_room_cover/a.jpg",
        "keyframe": "https://i0.hdslb.com/bfs/live-key-frame/b.jpg",
        "live_status": 1,
    }
    fields.update(overrides)
    return LiveInfo(**fields)


def test_content_key_live_url(keys):
    """直播间链接归一成独立 key，不跟视频撞缓存。"""
    assert keys.content_key(_LIVE_URL) == "bilibili_live:22603245"
    assert keys.content_key(f"{_LIVE_URL}?live_from=85001") == (
        "bilibili_live:22603245"
    )


def test_is_live_url(live_mod):
    """只认 live.bilibili.com 下的纯数字路径。"""
    assert live_mod.is_live_url(_LIVE_URL) is True
    assert live_mod.is_live_url("https://live.bilibili.com/510") is True
    assert live_mod.is_live_url("https://www.bilibili.com/video/BV1xx") is False
    assert live_mod.is_live_url("https://b23.tv/o9LYdgy") is False


def test_is_short_link(live_mod):
    """B 站短链域名才需要展开。"""
    assert live_mod.is_short_link(_SHORT_URL) is True
    assert live_mod.is_short_link("https://bili2233.cn/abc") is True
    assert live_mod.is_short_link(_LIVE_URL) is False


def test_room_id_from(live_mod):
    """路径里的房间号能取出来，别的形态取不到。"""
    assert live_mod.room_id_from(_LIVE_URL) == 22603245
    assert live_mod.room_id_from("https://live.bilibili.com/510/") == 510
    assert live_mod.room_id_from(_SHORT_URL) is None


def test_build_result_live_prefers_keyframe(live_mod):
    """直播中先试当前画面帧，失败才回退静态封面。"""
    result = live_mod.build_result(_info())
    assert [item.thumb_url for item in result.media] == [
        "https://i0.hdslb.com/bfs/live-key-frame/b.jpg",
        "https://i0.hdslb.com/bfs/live/new_room_cover/a.jpg",
    ]
    assert result.type.value == "live"
    assert result.author == "永雏塔菲"
    assert result.room_id == 22603245


def test_build_result_offline_only_user_cover(live_mod):
    """未开播直接用静态封面，没有 keyframe。"""
    result = live_mod.build_result(_info(live_status=0, keyframe=""))
    assert [item.thumb_url for item in result.media] == [
        "https://i0.hdslb.com/bfs/live/new_room_cover/a.jpg",
    ]
    assert result.type.value == "offline"


def test_meta_line_live(card, live_mod):
    """直播中的元信息行是 LIVE · 主播 · 房间。"""
    result = live_mod.build_result(_info())
    assert card.layout.meta_line(result, 0, False) == (
        "LIVE · 永雏塔菲 · 房间 22603245"
    )


def test_meta_line_offline(card, live_mod):
    """未开播的元信息行是 OFFLINE · 主播 · 房间。"""
    result = live_mod.build_result(_info(live_status=0))
    assert card.layout.meta_line(result, 0, False) == (
        "OFFLINE · 永雏塔菲 · 房间 22603245"
    )


@pytest.mark.asyncio
async def test_live_url_sends_card_only(
    monkeypatch, card, instance, service, live_mod
):
    """完整直播间链接只发一张封面卡片，不走 ParseHub、不发媒体。"""

    def fake_fetch_live(url, proxy=""):
        return _info()

    def fake_render(result, cover, out):
        out.write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(live_mod, "fetch_live", fake_fetch_live)
    monkeypatch.setattr(
        card.cover, "fetch_cover", lambda result, files, proxy="": b"cover"
    )
    monkeypatch.setattr(card.layout, "render", fake_render)

    bot = FakeBot()
    result = await service.handle(
        instance, bot, make_group_event(_LIVE_URL), _LIVE_URL
    )

    assert result.outcome is service.Outcome.DELIVERED
    assert "bilibili_live:22603245" in result.keys
    assert bot.sent_images, "应该发出一张卡片图"
    assert len(bot.sent_images) == 1, "直播间只发卡片，不发媒体"
    assert all(api != "upload_group_file" for api, _ in bot.calls), (
        "直播间不该传群文件"
    )


@pytest.mark.asyncio
async def test_short_link_to_live_after_parse_failure(
    monkeypatch, card, instance, service, live_mod
):
    """短链指向直播间时：ParseHub 解析失败后展开短链，改发封面卡片。"""

    async def fake_parse(url, proxy="", cookie=""):
        raise RuntimeError("所有解析方式均失败")

    def fake_expand_short(url, proxy=""):
        return _LIVE_URL

    def fake_fetch_live(url, proxy=""):
        return _info()

    def fake_render(result, cover, out):
        out.write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(live_mod, "expand_short", fake_expand_short)
    monkeypatch.setattr(live_mod, "fetch_live", fake_fetch_live)
    monkeypatch.setattr(
        card.cover, "fetch_cover", lambda result, files, proxy="": b"cover"
    )
    monkeypatch.setattr(card.layout, "render", fake_render)

    bot = FakeBot()
    result = await service.handle(
        instance, bot, make_group_event(_SHORT_URL), _SHORT_URL
    )

    assert result.outcome is service.Outcome.DELIVERED
    assert "bilibili_live:22603245" in result.keys
    assert bot.sent_images, "短链指向直播间时也应发出卡片"
