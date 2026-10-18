"""上传模式：http 走中转地址，base64 把文件内容内联进消息段。

uv run pytest -q tests/test_upload_mode.py
"""

from __future__ import annotations

import base64
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
def tmp_media(tmp_path, monkeypatch, plugin, server):
    """把中转服务的根目录指到临时目录。"""
    monkeypatch.setattr(server.RELAY, "root", tmp_path)
    monkeypatch.setattr(plugin._ENDPOINT, "host", "127.0.0.1")
    monkeypatch.setattr(plugin._ENDPOINT, "port", 18899)
    return tmp_path


@pytest.fixture()
def upload_mode(plugin, monkeypatch):
    """把上传模式切到指定值。"""

    def _set(mode: str) -> None:
        monkeypatch.setattr(plugin.plugin_config, "parsehub_upload_mode", mode)

    return _set


def _inline_bytes(ref: str) -> bytes:
    """从 ``base64://`` 引用里取回原始字节。"""
    assert ref.startswith("base64://"), ref[:32]
    return base64.b64decode(ref[len("base64://") :])


def _sent_segments(bot: RecordingBot, api: str = "send_msg") -> list:
    """取出指定 API 收到的消息段（按调用顺序）。"""
    return [
        segment
        for called, kwargs in bot.calls
        if called == api
        for segment in kwargs.get("message", [])
    ]


@pytest.mark.asyncio
async def test_media_ref_http_mode_points_at_relay(
    plugin, tmp_media, upload_mode
):
    """http 模式仍然给协议端中转地址，行为和改动前一致。"""
    upload_mode("http")
    sub = tmp_media / "abc123"
    sub.mkdir()
    media = sub / "a.jpg"
    media.write_bytes(b"hello")

    ref = await plugin._media_ref(media)
    assert ref == "http://127.0.0.1:18899/abc123/a.jpg"


@pytest.mark.asyncio
async def test_media_ref_base64_mode_inlines_bytes(
    plugin, tmp_media, upload_mode
):
    """base64 模式把文件内容内联，协议端不需要能访问本机。"""
    upload_mode("base64")
    sub = tmp_media / "abc123"
    sub.mkdir()
    media = sub / "a.jpg"
    media.write_bytes(b"hello")

    assert _inline_bytes(await plugin._media_ref(media)) == b"hello"


@pytest.mark.asyncio
async def test_single_image_uses_inline_base64(plugin, tmp_media, upload_mode):
    """单张图片走消息段时，file 是 base64:// 而不是 http 地址。"""
    upload_mode("base64")
    image = tmp_media / "a.jpg"
    image.write_bytes(b"img")

    bot = RecordingBot()
    await plugin._send_media_files(bot, make_group_event("x"), [image])

    segments = _sent_segments(bot)
    assert [segment.type for segment in segments] == ["image"]
    assert _inline_bytes(segments[0].data["file"]) == b"img"


@pytest.mark.asyncio
async def test_single_video_uses_inline_base64(plugin, tmp_media, upload_mode):
    """单个视频走消息段时同样内联。"""
    upload_mode("base64")
    video = tmp_media / "b.mp4"
    video.write_bytes(b"vid")

    bot = RecordingBot()
    await plugin._send_media_files(bot, make_group_event("x"), [video])

    segments = _sent_segments(bot)
    assert [segment.type for segment in segments] == ["video"]
    assert _inline_bytes(segments[0].data["file"]) == b"vid"


@pytest.mark.parametrize("mode", ["http", "base64"])
@pytest.mark.asyncio
async def test_unknown_extension_goes_through_group_file(
    plugin, tmp_media, upload_mode, mode
):
    """群文件上传同样跟着模式走。"""
    upload_mode(mode)
    media = tmp_media / "a.bin"
    media.write_bytes(b"bin")

    bot = RecordingBot()
    await plugin._send_media_files(bot, make_group_event("x"), [media])

    uploads = [kw for api, kw in bot.calls if api == "upload_group_file"]
    assert len(uploads) == 1
    if mode == "base64":
        assert _inline_bytes(uploads[0]["file"]) == b"bin"
    else:
        assert uploads[0]["file"] == "http://127.0.0.1:18899/a.bin"


@pytest.mark.asyncio
async def test_forward_nodes_use_inline_base64(plugin, tmp_media, upload_mode):
    """合并转发的每个节点也内联 base64。"""
    upload_mode("base64")
    first = tmp_media / "a.jpg"
    first.write_bytes(b"one")
    second = tmp_media / "b.jpg"
    second.write_bytes(b"two")

    bot = RecordingBot()
    assert await plugin._send_forward(
        bot, make_group_event("x"), [first, second]
    )

    calls = [kw for api, kw in bot.calls if api == "send_group_forward_msg"]
    assert len(calls) == 1
    refs = [
        node["data"]["content"][0]["data"]["file"]
        for node in calls[0]["messages"]
    ]
    assert [_inline_bytes(ref) for ref in refs] == [b"one", b"two"]


@pytest.mark.asyncio
async def test_card_image_uses_inline_base64(
    plugin, tmp_media, upload_mode, monkeypatch
):
    """卡片图也内联，走的是同一个入口。"""
    upload_mode("base64")
    card_path = tmp_media / "card.jpg"
    card_path.write_bytes(b"card")

    async def fake_render(*args, **kwargs):
        return card_path

    monkeypatch.setattr(plugin, "_render_card", fake_render)

    bot = RecordingBot()
    info = await plugin._send_card_or_text(
        bot,
        make_group_event("x"),
        object(),
        "哔哩哔哩",
        [],
        tmp_media,
        plugin.plugin_config,
    )

    assert info.sent
    segments = _sent_segments(bot)
    assert _inline_bytes(segments[0].data["file"]) == b"card"


@pytest.mark.asyncio
async def test_cached_card_uses_inline_base64(plugin, tmp_media, upload_mode):
    """命中缓存时发的卡片图同样跟着模式走。"""
    upload_mode("base64")
    entry = tmp_media / "entry"
    (entry / "files").mkdir(parents=True)
    (entry / "files" / "a.jpg").write_bytes(b"media")
    (entry / "card.jpg").write_bytes(b"cached-card")

    bot = RecordingBot()
    assert await plugin._send_cached(
        bot, make_group_event("x"), entry, plugin.plugin_config
    )

    segments = _sent_segments(bot)
    assert _inline_bytes(segments[0].data["file"]) == b"cached-card"


@pytest.mark.asyncio
async def test_base64_mode_does_not_start_relay(
    plugin, monkeypatch, upload_mode
):
    """base64 模式不占端口：启动钩子不应该启动中转服务。"""
    upload_mode("base64")

    def boom(*args, **kwargs):
        raise AssertionError("base64 模式不应该启动中转服务")

    monkeypatch.setattr(plugin.server.RELAY, "start", boom)

    async def noop() -> None:
        return

    monkeypatch.setattr(plugin, "_sweep_expired_media", noop)

    await plugin._on_startup()


@pytest.mark.asyncio
async def test_http_mode_starts_relay(plugin, monkeypatch, upload_mode):
    """http 模式仍然启动中转服务（回归保护）。"""
    upload_mode("http")
    started: list[tuple[str, int]] = []

    def fake_start(host, port):
        started.append((host, port))
        return 18899

    monkeypatch.setattr(plugin.server.RELAY, "start", fake_start)
    monkeypatch.setattr(plugin.server, "guess_lan_ip", lambda: "10.0.0.2")

    async def noop() -> None:
        return

    monkeypatch.setattr(plugin, "_sweep_expired_media", noop)

    await plugin._on_startup()
    assert started == [("0.0.0.0", plugin.plugin_config.parsehub_files_port)]
    assert plugin._ENDPOINT.host == "10.0.0.2"
