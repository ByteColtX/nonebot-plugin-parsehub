"""上传模式：file 给本机路径，http 给中转地址，base64 把内容内联进消息段。

uv run pytest -q tests/test_upload_mode.py
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fake import make_group_event
from nonebot.adapters.onebot.v11 import Bot


class RecordingBot(Bot):
    """记录所有 API 调用的假 Bot。"""

    def __init__(self) -> None:
        super().__init__(adapter=MagicMock(), self_id="10000")
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, api: str, **kwargs):  # type: ignore[override]
        self.calls.append((api, kwargs))
        return {"status": "ok", "retcode": 0, "data": None}


@pytest.fixture()
def tmp_media(tmp_path, monkeypatch, plugin, instance):
    """把中转服务的根目录指到临时目录。"""
    monkeypatch.setattr(instance.relay, "root", tmp_path)
    monkeypatch.setattr(instance.endpoint, "host", "127.0.0.1")
    monkeypatch.setattr(instance.endpoint, "port", 18899)
    return tmp_path


@pytest.fixture()
def upload_mode(plugin, monkeypatch, instance, update_settings):
    """把上传模式切到指定值。"""

    def _set(mode: str) -> None:
        update_settings(delivery_mode=mode)

    return _set


def _as_media(path: Path):
    """把本地路径包成带类型的媒体。"""
    from nonebot_plugin_parsehub import media

    return media.MediaFile(path=path, kind=media.kind_of(path))


def _expected_ref(path: Path, mode: str) -> str:
    """非 http 模式下协议端应该收到的引用。"""
    if mode == "base64":
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"base64://{data}"
    return path.resolve().as_uri()


def _sent_segments(bot: RecordingBot, api: str = "send_msg") -> list:
    """取出指定 API 收到的消息段（按调用顺序）。"""
    return [
        segment
        for called, kwargs in bot.calls
        if called == api
        for segment in kwargs.get("message", [])
    ]


def test_default_upload_mode_is_file(plugin, config_mod):
    """默认值就是 file：同机部署零配置即可用，不占端口。"""
    assert config_mod().parsehub_delivery == "file"


@pytest.mark.asyncio
async def test_media_ref_http_mode_points_at_relay(
    plugin, tmp_media, upload_mode, instance
):
    """http 模式仍然给协议端中转地址。"""
    upload_mode("http")
    sub = tmp_media / "abc123"
    sub.mkdir()
    media = sub / "a.jpg"
    media.write_bytes(b"hello")

    ref = await instance.delivery.ref(media)
    assert ref == "http://127.0.0.1:18899/abc123/a.jpg"


@pytest.mark.parametrize("mode", ["file", "base64"])
@pytest.mark.asyncio
async def test_media_ref_follows_mode(
    plugin, tmp_media, upload_mode, mode, instance
):
    """file 给本机路径，base64 给内联内容。"""
    upload_mode(mode)
    sub = tmp_media / "abc123"
    sub.mkdir()
    media = sub / "a.jpg"
    media.write_bytes(b"hello")

    ref = await instance.delivery.ref(media)
    assert ref == _expected_ref(media, mode)
    if mode == "file":
        assert ref.startswith("file:///")


@pytest.mark.parametrize("mode", ["file", "base64"])
@pytest.mark.asyncio
async def test_single_image_follows_mode(
    plugin, tmp_media, upload_mode, mode, instance
):
    """单张图片走消息段时按模式给引用。"""
    upload_mode(mode)
    image = tmp_media / "a.jpg"
    image.write_bytes(b"img")

    bot = RecordingBot()
    await instance.delivery.send_media(
        bot, make_group_event("x"), [_as_media(image)]
    )

    segments = _sent_segments(bot)
    assert [segment.type for segment in segments] == ["image"]
    assert segments[0].data["file"] == _expected_ref(image, mode)


@pytest.mark.parametrize("mode", ["file", "base64"])
@pytest.mark.asyncio
async def test_single_video_follows_mode(
    plugin, tmp_media, upload_mode, mode, instance
):
    """单个视频走消息段时同样按模式给引用。"""
    upload_mode(mode)
    video = tmp_media / "b.mp4"
    video.write_bytes(b"vid")

    bot = RecordingBot()
    await instance.delivery.send_media(
        bot, make_group_event("x"), [_as_media(video)]
    )

    segments = _sent_segments(bot)
    assert [segment.type for segment in segments] == ["video"]
    assert segments[0].data["file"] == _expected_ref(video, mode)


@pytest.mark.parametrize("mode", ["file", "http", "base64"])
@pytest.mark.asyncio
async def test_unknown_extension_goes_through_group_file(
    plugin, tmp_media, upload_mode, mode, instance
):
    """群文件上传同样跟着模式走。"""
    upload_mode(mode)
    media = tmp_media / "a.bin"
    media.write_bytes(b"bin")

    bot = RecordingBot()
    await instance.delivery.send_media(
        bot, make_group_event("x"), [_as_media(media)]
    )

    uploads = [kw for api, kw in bot.calls if api == "upload_group_file"]
    assert len(uploads) == 1
    if mode == "http":
        assert uploads[0]["file"] == "http://127.0.0.1:18899/a.bin"
    else:
        assert uploads[0]["file"] == _expected_ref(media, mode)


@pytest.mark.parametrize("mode", ["file", "base64"])
@pytest.mark.asyncio
async def test_forward_nodes_follow_mode(
    plugin, tmp_media, upload_mode, mode, instance
):
    """合并转发的每个节点也按模式给引用。"""
    upload_mode(mode)
    first = tmp_media / "a.jpg"
    first.write_bytes(b"one")
    second = tmp_media / "b.jpg"
    second.write_bytes(b"two")

    bot = RecordingBot()
    assert await instance.delivery.send_forward(
        bot,
        make_group_event("x"),
        [_as_media(first), _as_media(second)],
    )

    calls = [kw for api, kw in bot.calls if api == "send_group_forward_msg"]
    assert len(calls) == 1
    refs = [
        node["data"]["content"][0]["data"]["file"]
        for node in calls[0]["messages"]
    ]
    assert refs == [_expected_ref(first, mode), _expected_ref(second, mode)]


@pytest.mark.parametrize("mode", ["file", "base64"])
@pytest.mark.asyncio
async def test_card_image_follows_mode(
    plugin, tmp_media, upload_mode, monkeypatch, mode, instance, service
):
    """卡片图也走同一个入口。"""
    upload_mode(mode)
    card_path = tmp_media / "card.jpg"
    card_path.write_bytes(b"card")

    async def fake_render(*args, **kwargs):
        return card_path

    monkeypatch.setattr(instance.cards, "render", fake_render)

    bot = RecordingBot()
    info = await service._send_card_or_text(
        instance,
        bot,
        make_group_event("x"),
        object(),
        [],
        tmp_media,
    )

    assert info.sent
    segments = _sent_segments(bot)
    assert segments[0].data["file"] == _expected_ref(card_path, mode)


@pytest.mark.parametrize("mode", ["file", "base64"])
@pytest.mark.asyncio
async def test_cached_card_follows_mode(
    plugin, tmp_path, upload_mode, mode, card, instance, service
):
    """命中缓存时发的卡片图同样跟着模式走。"""
    upload_mode(mode)
    store = plugin.cache.MediaCache(
        tmp_path / "cache", ttl_seconds=3600, max_bytes=0
    )
    media = tmp_path / "a.jpg"
    media.write_bytes(b"media")
    card = tmp_path / "card.jpg"
    card.write_bytes(b"cached-card")
    store.store(["k"], [media], card=card)
    entry = store.get("k")
    assert entry is not None

    bot = RecordingBot()
    assert await service._deliver_cached(
        instance, bot, make_group_event("x"), entry, "k"
    )

    segments = _sent_segments(bot)
    sent = segments[0].data["file"]
    if mode == "base64":
        assert sent == _expected_ref(entry.card, mode)
    else:
        # 缓存里的卡片会先硬链接进任务目录，路径变了但内容一致
        from urllib.parse import unquote, urlsplit

        assert sent.startswith("file://")
        assert Path(unquote(urlsplit(sent).path)).read_bytes() == b"cached-card"


@pytest.mark.parametrize("mode", ["file", "base64"])
@pytest.mark.asyncio
async def test_non_http_mode_does_not_start_relay(
    plugin, monkeypatch, upload_mode, mode, instance
):
    """file / base64 都不占端口：启动钩子不应该启动中转服务。"""
    upload_mode(mode)

    def boom(*args, **kwargs):
        raise AssertionError(f"{mode} 模式不应该启动中转服务")

    monkeypatch.setattr(instance.relay, "start", boom)

    async def noop() -> None:
        return

    monkeypatch.setattr(instance, "_sweep_forever", noop)

    await instance.startup()


@pytest.mark.asyncio
async def test_http_mode_starts_relay(
    plugin, monkeypatch, upload_mode, instance
):
    """http 模式仍然启动中转服务（回归保护）。"""
    upload_mode("http")
    started: list[tuple[str, int]] = []

    def fake_start(host, port):
        started.append((host, port))
        return 18899

    monkeypatch.setattr(instance.relay, "start", fake_start)
    monkeypatch.setattr(plugin.app, "guess_lan_ip", lambda: "10.0.0.2")

    async def noop() -> None:
        return

    monkeypatch.setattr(instance, "_sweep_forever", noop)

    await instance.startup()
    assert started == [("0.0.0.0", instance.settings.relay_port)]
    assert instance.endpoint.host == "10.0.0.2"
