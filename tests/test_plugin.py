"""插件逻辑测试：链接提取、触发规则、去重、代理分流、cookie、发送流程、媒体中转。

全部离线：parsehub 的解析/下载被替换成假实现，只验证插件自身的行为。
matcher 级别的端到端测试见 test_nonebug.py。

    uv run pytest -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fake import make_group_event
from nonebot.adapters.onebot.v11 import (
    Bot,
    Message,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed


class FakeBot(Bot):
    """记录所有 API 调用的假 Bot。"""

    def __init__(self, *, fail_video: bool = False) -> None:
        super().__init__(adapter=MagicMock(), self_id="10000")
        self.calls: list[tuple[str, dict]] = []
        self.fail_video = fail_video

    async def call_api(self, api: str, **kwargs):  # type: ignore[override]
        self.calls.append((api, kwargs))
        if self.fail_video and api == "send_msg":
            message = kwargs.get("message")
            if isinstance(message, Message) and any(
                seg.type == "video" for seg in message
            ):
                raise ActionFailed(
                    status="failed",
                    retcode=100,
                    data=None,
                    message="video not supported",
                )
        return {"status": "ok", "retcode": 0, "data": None}

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


class FakeMediaRef:
    def __init__(self, url: str, ext: str = "mp4") -> None:
        self.url = url
        self.ext = ext
        self.width = 1920
        self.height = 1080
        self.duration = 10


class FakeParseResult:
    def __init__(
        self,
        *,
        title="标题",
        content="正文",
        media=None,
        raw_url="https://b23.tv/abcd",
    ) -> None:
        self.title = title
        self.content = content
        self.media = media
        self.raw_url = raw_url
        self.platform = SimpleNamespace(id="bilibili", display_name="哔哩哔哩")


class FakeMediaFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.width = 1920
        self.height = 1080
        self.duration = 10


class FakeDownloadResult:
    def __init__(self, files: list[Path], media=None) -> None:
        self.media = (
            media if media is not None else [FakeMediaFile(p) for p in files]
        )
        self.output_dir = files[0].parent if files else Path("/tmp")


@pytest.fixture()
def tmp_media(tmp_path, monkeypatch, plugin, server):
    """把中转服务的根目录指到临时目录。"""
    monkeypatch.setattr(server.RELAY, "root", tmp_path)
    monkeypatch.setattr(plugin._ENDPOINT, "host", "127.0.0.1")
    monkeypatch.setattr(plugin._ENDPOINT, "port", 18899)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_state(plugin, core, monkeypatch):
    plugin.DEDUPE._seen.clear()

    async def _fake_platform_id(url):
        return "bilibili"

    monkeypatch.setattr(core.CLIENT, "platform_id", _fake_platform_id)
    yield
    plugin.DEDUPE._seen.clear()


def test_proxy_only_applies_to_listed_platforms(plugin, monkeypatch, core):
    """代理默认只给海外平台走，国内平台直连。"""
    cfg = plugin.plugin_config
    monkeypatch.setattr(cfg, "parsehub_proxy", "http://192.168.12.25:6152")

    # 默认列表 = 海外平台
    assert core.proxy_for("youtube") == "http://192.168.12.25:6152"
    assert core.proxy_for("twitter") == "http://192.168.12.25:6152"
    assert core.proxy_for("bilibili") == ""
    assert core.proxy_for("douyin") == ""

    # 留空列表 = 所有平台都走
    monkeypatch.setattr(cfg, "parsehub_proxy_platforms", [])
    assert core.proxy_for("bilibili") == "http://192.168.12.25:6152"

    # 没配代理 = 一律直连
    monkeypatch.setattr(cfg, "parsehub_proxy", "")
    assert core.proxy_for("youtube") == ""


def test_platform_cookie_prefers_parsehub_cookie_file(
    plugin, monkeypatch, tmp_path, core
):
    """cookies.toml（ph set cookie 写的）优先于配置里的兜底 cookie。"""
    import parsehub.cli_config as cc

    cookie_file = tmp_path / "cookies.toml"
    cookie_file.write_text(
        '[cookie]\nbilibili = "SESSDATA=from-file"\n', encoding="utf-8"
    )
    monkeypatch.setattr(cc, "default_cookie_path", lambda: cookie_file)

    assert core.platform_cookie("bilibili", "fallback") == "SESSDATA=from-file"
    assert (
        core.platform_cookie("zhihu", "fallback") == "fallback"
    )  # 文件里没有 → 用兜底
    assert core.platform_cookie("bilibili", "") == "SESSDATA=from-file"

    monkeypatch.setattr(
        cc, "default_cookie_path", lambda: tmp_path / "missing.toml"
    )
    assert core.platform_cookie("bilibili", "fallback") == "fallback"
    assert core.platform_cookie("bilibili", "") == ""


# --------------------------------------------------------------------------- #
# 1. 链接提取
# --------------------------------------------------------------------------- #
def test_config_accepts_env_style_values():
    """回归：.env 里的值会被 pydantic 当成 int/bool/list，配置字段类型必须能接住。"""
    from nonebot_plugin_parsehub.config import Config

    cfg = Config(
        parsehub_groups=[941285969],  # .env 写 [941285969]
        parsehub_video_send="video",
        parsehub_max_mb=100,  # .env 里的数字是 int
        parsehub_auto=True,
    )
    assert cfg.parsehub_groups == [941285969]
    assert cfg.parsehub_max_mb == 100
    assert Config(parsehub_max_mb="100").parsehub_max_mb == 100  # 字符串也接住


def test_extract_urls_filters_hosts(plugin, core):
    text = (
        "看看这个 https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=333 "
        "和 https://example.com/not-supported 还有 https://b23.tv/abcd。"
    )
    urls = core.extract_urls(text)
    assert urls == [
        "https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=333",
        "https://b23.tv/abcd",
    ]


def test_extract_urls_dedupes(plugin, core):
    text = "https://b23.tv/x https://b23.tv/x"
    assert core.extract_urls(text) == ["https://b23.tv/x"]


def test_extract_urls_empty(plugin, core):
    assert core.extract_urls("今天天气不错，没有链接") == []


# --------------------------------------------------------------------------- #
# 2. 自动触发规则
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_auto_rule_matches_group_link(plugin):
    event = make_group_event("https://b23.tv/abcd")
    assert await plugin._auto_rule(event) is True


@pytest.mark.asyncio
async def test_auto_rule_ignores_command(plugin):
    event = make_group_event("解析 https://b23.tv/abcd")
    assert await plugin._auto_rule(event) is False


@pytest.mark.asyncio
async def test_auto_rule_respects_group_filter(plugin, monkeypatch):
    monkeypatch.setattr(plugin.plugin_config, "parsehub_groups", [999])
    assert (
        await plugin._auto_rule(
            make_group_event("https://b23.tv/abcd", group_id=123456)
        )
        is False
    )
    assert (
        await plugin._auto_rule(
            make_group_event("https://b23.tv/abcd", group_id=999)
        )
        is True
    )


@pytest.mark.asyncio
async def test_auto_rule_off(plugin, monkeypatch):
    monkeypatch.setattr(plugin.plugin_config, "parsehub_auto", False)
    assert (
        await plugin._auto_rule(make_group_event("https://b23.tv/abcd"))
        is False
    )


# --------------------------------------------------------------------------- #
# 3. 去重
# --------------------------------------------------------------------------- #
def test_dedupe_window(plugin):
    """同一链接在窗口内只处理一次，不同链接互不影响。"""
    assert plugin.DEDUPE.should_skip("https://b23.tv/a", 60) is False
    assert plugin.DEDUPE.should_skip("https://b23.tv/a", 60) is True
    assert plugin.DEDUPE.should_skip("https://b23.tv/b", 60) is False
    assert plugin.DEDUPE.should_skip("https://b23.tv/a", 0) is False


# --------------------------------------------------------------------------- #
# 4. 发送流程
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_handle_link_sends_text_and_image(
    plugin, monkeypatch, tmp_media, core
):
    img = tmp_media / "pic.jpg"
    img.write_bytes(b"x" * 1024)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/pic.jpg", "jpg")
        )

    async def fake_download(url, path, proxy="", cookie=""):
        return FakeDownloadResult([img])

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

    bot = FakeBot()
    await plugin.handle_link(
        bot, make_group_event("https://b23.tv/abcd"), "https://b23.tv/abcd"
    )

    apis = [api for api, _ in bot.calls]
    assert apis.count("send_msg") == 2, apis  # 文字 + 图片
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert plugin.REACT_START in emojis, emojis  # 开始时回应
    text = bot.sent_texts[0]
    assert "哔哩哔哩" in text and "标题" in text
    assert "https://b23.tv" not in text, "回复里不应该再带原始链接"
    assert not any(
        seg.type == "reply"
        for api, kw in bot.calls
        if api == "send_msg"
        for seg in kw["message"]
    )
    image_call = next(
        kw["message"]
        for api, kw in bot.calls
        if api == "send_msg" and kw["message"][0].type == "image"
    )
    assert image_call[0].data["file"].startswith("http://127.0.0.1:18899/")


@pytest.mark.asyncio
async def test_handle_link_live_photo_sends_image_and_video(
    plugin, monkeypatch, tmp_media, core
):
    """实况照片 = 图片 + 一小段视频，两个都要发出去。"""
    img = tmp_media / "live.jpg"
    img.write_bytes(b"x" * 512)
    vid = tmp_media / "live_video.mp4"
    vid.write_bytes(b"y" * 1024)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/live.jpg", "jpg")
        )

    async def fake_download(url, path, proxy="", cookie=""):
        class LivePhotoFile:
            def __init__(self):
                self.path = img
                self.video_path = vid
                self.width = 1080
                self.height = 1440
                self.duration = 3

        return FakeDownloadResult([], media=[LivePhotoFile()])

    monkeypatch.setattr(
        core.CLIENT, "is_supported", lambda url: asyncio.sleep(0, result=True)
    )
    monkeypatch.setattr(core.CLIENT, "parse", fake_parse)
    monkeypatch.setattr(core.CLIENT, "download", fake_download)

    bot = FakeBot()
    await plugin.handle_link(
        bot, make_group_event("https://b23.tv/abcd"), "https://b23.tv/abcd"
    )

    apis = [api for api, _ in bot.calls]
    # 两个媒体 → 合并转发，节点里一个是图片一个是视频
    assert "send_group_forward_msg" in apis, apis
    fwd = dict(bot.calls)["send_group_forward_msg"]
    kinds = [n["data"]["content"][0]["type"] for n in fwd["messages"]]
    assert kinds == ["image", "video"], kinds


@pytest.mark.asyncio
async def test_handle_link_multi_media_uses_forward(
    plugin, monkeypatch, tmp_media, core
):
    pics = []
    for i in range(3):
        p = tmp_media / f"p{i}.jpg"
        p.write_bytes(b"x" * 512)
        pics.append(p)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=[
                FakeMediaRef(f"https://cdn.example.com/p{i}.jpg", "jpg")
                for i in range(3)
            ]
        )

    async def fake_download(url, path, proxy="", cookie=""):
        return FakeDownloadResult(pics)

    monkeypatch.setattr(
        core.CLIENT, "is_supported", lambda url: asyncio.sleep(0, result=True)
    )
    monkeypatch.setattr(core.CLIENT, "parse", fake_parse)
    monkeypatch.setattr(core.CLIENT, "download", fake_download)

    bot = FakeBot()
    await plugin.handle_link(
        bot, make_group_event("https://b23.tv/abcd"), "https://b23.tv/abcd"
    )

    apis = [api for api, _ in bot.calls]
    assert "send_group_forward_msg" in apis, apis
    assert apis.count("send_msg") == 1, apis  # 只有标题那条文字，图片不单独发
    fwd = dict(bot.calls)["send_group_forward_msg"]
    assert fwd["group_id"] == 123456
    assert len(fwd["messages"]) == 3
    assert all(
        n["type"] == "node" and n["data"]["content"][0]["type"] == "image"
        for n in fwd["messages"]
    )
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert plugin.REACT_OK in emojis, emojis


@pytest.mark.asyncio
async def test_handle_link_multi_media_forward_failure_falls_back(
    plugin, monkeypatch, tmp_media, core
):
    pics = []
    for i in range(2):
        p = tmp_media / f"p{i}.jpg"
        p.write_bytes(b"x" * 512)
        pics.append(p)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=[
                FakeMediaRef(f"https://cdn.example.com/p{i}.jpg", "jpg")
                for i in range(2)
            ]
        )

    async def fake_download(url, path, proxy="", cookie=""):
        return FakeDownloadResult(pics)

    monkeypatch.setattr(
        core.CLIENT, "is_supported", lambda url: asyncio.sleep(0, result=True)
    )
    monkeypatch.setattr(core.CLIENT, "parse", fake_parse)
    monkeypatch.setattr(core.CLIENT, "download", fake_download)

    class NoForwardBot(FakeBot):
        async def call_api(self, api, **kwargs):
            if api.endswith("forward_msg"):
                self.calls.append((api, kwargs))
                raise ActionFailed(
                    status="failed",
                    retcode=100,
                    data=None,
                    message="no forward",
                )
            return await super().call_api(api, **kwargs)

    bot = NoForwardBot()
    await plugin.handle_link(
        bot, make_group_event("https://b23.tv/abcd"), "https://b23.tv/abcd"
    )

    apis = [api for api, _ in bot.calls]
    assert "send_group_forward_msg" in apis
    assert apis.count("send_msg") == 3, apis  # 标题 + 两张图逐条兜底


@pytest.mark.asyncio
async def test_handle_link_single_media_no_forward(
    plugin, monkeypatch, tmp_media, core
):
    img = tmp_media / "one.jpg"
    img.write_bytes(b"x" * 512)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/one.jpg", "jpg")
        )

    async def fake_download(url, path, proxy="", cookie=""):
        return FakeDownloadResult([img])

    monkeypatch.setattr(
        core.CLIENT, "is_supported", lambda url: asyncio.sleep(0, result=True)
    )
    monkeypatch.setattr(core.CLIENT, "parse", fake_parse)
    monkeypatch.setattr(core.CLIENT, "download", fake_download)

    bot = FakeBot()
    await plugin.handle_link(
        bot, make_group_event("https://b23.tv/abcd"), "https://b23.tv/abcd"
    )

    apis = [api for api, _ in bot.calls]
    assert "send_group_forward_msg" not in apis, apis  # 单个媒体不合并转发
    assert apis.count("send_msg") == 2, apis


@pytest.mark.asyncio
async def test_handle_link_video_failure_does_not_fallback(
    plugin, monkeypatch, tmp_media, core
):
    video = tmp_media / "v.mp4"
    video.write_bytes(b"x" * 2048)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/v.mp4")
        )

    async def fake_download(url, path, proxy="", cookie=""):
        return FakeDownloadResult([video])

    monkeypatch.setattr(
        core.CLIENT, "is_supported", lambda url: asyncio.sleep(0, result=True)
    )
    monkeypatch.setattr(core.CLIENT, "parse", fake_parse)
    monkeypatch.setattr(core.CLIENT, "download", fake_download)
    monkeypatch.setattr(
        core,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=2048),
    )

    bot = FakeBot(fail_video=True)  # video 消息段被协议端拒绝
    await plugin.handle_link(
        bot, make_group_event("https://b23.tv/abcd"), "https://b23.tv/abcd"
    )

    apis = [api for api, _ in bot.calls]
    assert "upload_group_file" not in apis, "不应该回退群文件"
    assert any("视频发送失败" in t for t in bot.sent_texts), bot.sent_texts
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert plugin.REACT_FAIL in emojis, emojis  # 失败要回应失败表情


@pytest.mark.asyncio
async def test_handle_link_video_mode_file(
    plugin, monkeypatch, tmp_media, core
):
    video = tmp_media / "v.mp4"
    video.write_bytes(b"x" * 2048)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/v.mp4")
        )

    async def fake_download(url, path, proxy="", cookie=""):
        return FakeDownloadResult([video])

    monkeypatch.setattr(
        core.CLIENT, "is_supported", lambda url: asyncio.sleep(0, result=True)
    )
    monkeypatch.setattr(core.CLIENT, "parse", fake_parse)
    monkeypatch.setattr(core.CLIENT, "download", fake_download)
    monkeypatch.setattr(
        core,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=2048),
    )
    monkeypatch.setattr(plugin.plugin_config, "parsehub_video_send", "file")

    bot = FakeBot()
    await plugin.handle_link(
        bot, make_group_event("https://b23.tv/abcd"), "https://b23.tv/abcd"
    )

    apis = [api for api, _ in bot.calls]
    assert "upload_group_file" in apis, apis
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert plugin.REACT_OK in emojis, emojis  # 成功要回应成功表情
    upload = dict(bot.calls)["upload_group_file"]
    assert upload["group_id"] == 123456
    assert upload["file"].startswith("http://127.0.0.1:18899/")


@pytest.mark.asyncio
async def test_handle_link_skips_oversized_media(
    plugin, monkeypatch, tmp_media, core
):
    video = tmp_media / "big.mp4"
    video.write_bytes(b"x" * 1024)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/big.mp4")
        )

    monkeypatch.setattr(
        core.CLIENT, "is_supported", lambda url: asyncio.sleep(0, result=True)
    )
    monkeypatch.setattr(core.CLIENT, "parse", fake_parse)
    monkeypatch.setattr(
        core,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(
            0, result=500 * 1024 * 1024
        ),
    )

    bot = FakeBot()
    await plugin.handle_link(
        bot, make_group_event("https://b23.tv/abcd"), "https://b23.tv/abcd"
    )

    assert any("超过上限" in t for t in bot.sent_texts), bot.sent_texts
    assert "upload_group_file" not in [api for api, _ in bot.calls]


@pytest.mark.asyncio
async def test_handle_link_text_only_result(
    plugin, monkeypatch, tmp_media, core
):
    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(media=None, content="一篇长文")

    monkeypatch.setattr(
        core.CLIENT, "is_supported", lambda url: asyncio.sleep(0, result=True)
    )
    monkeypatch.setattr(core.CLIENT, "parse", fake_parse)

    bot = FakeBot()
    await plugin.handle_link(
        bot, make_group_event("https://b23.tv/abcd"), "https://b23.tv/abcd"
    )
    assert [api for api, _ in bot.calls].count(
        "send_msg"
    ) == 1  # 只有文字，没有媒体
    assert "一篇长文" in bot.sent_texts[0]


@pytest.mark.asyncio
async def test_handle_link_unsupported_manual(plugin, monkeypatch, core):
    monkeypatch.setattr(
        core.CLIENT, "is_supported", lambda url: asyncio.sleep(0, result=False)
    )
    bot = FakeBot()
    await plugin.handle_link(
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
        manual=True,
    )
    assert "不支持" in bot.sent_texts[0]


@pytest.mark.asyncio
async def test_handle_link_parse_error_manual(plugin, monkeypatch, core):
    class Boom(Exception):
        pass

    async def fake_parse(url, proxy="", cookie=""):
        raise Boom("解析炸了")

    monkeypatch.setattr(
        core.CLIENT, "is_supported", lambda url: asyncio.sleep(0, result=True)
    )
    monkeypatch.setattr(core.CLIENT, "parse", fake_parse)

    bot = FakeBot()
    await plugin.handle_link(
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
        manual=True,
    )
    assert any("解析失败" in t for t in bot.sent_texts), bot.sent_texts
    # 「收到」的回应要在解析之前就发出去，解析失败也留着
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert plugin.REACT_START in emojis, emojis


# --------------------------------------------------------------------------- #
# 5. 媒体中转服务
# --------------------------------------------------------------------------- #
def test_media_server_serves_file_and_hides_listing(
    plugin, tmp_media, monkeypatch, server
):
    import urllib.error
    import urllib.request

    # 插件的启动钩子可能已经起过服务，这里强制重新绑定测试端口
    monkeypatch.setattr(server.RELAY, "_server", None)

    sub = tmp_media / "abc123"
    sub.mkdir()
    (sub / "a.mp4").write_bytes(b"hello")

    port = server.RELAY.start("127.0.0.1", 18899)
    assert port == 18899

    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/abc123/a.mp4", timeout=5
    ) as resp:
        assert resp.read() == b"hello"

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/abc123/", timeout=5)
    assert exc.value.code == 404


def test_public_url_mapping(plugin, tmp_media, server):
    sub = tmp_media / "deadbeef"
    sub.mkdir()
    f = sub / "x.jpg"
    f.write_bytes(b"1")
    assert (
        server.RELAY.url_for(f, "192.168.1.5", 8899)
        == "http://192.168.1.5:8899/deadbeef/x.jpg"
    )


def test_cleanup_removes_old_dirs(plugin, tmp_media, server):
    old = tmp_media / "old"
    old.mkdir()
    (old / "a.txt").write_bytes(b"1")
    import os
    import time

    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert server.RELAY.cleanup(60) == 1
    assert not old.exists()
