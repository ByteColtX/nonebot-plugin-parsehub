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
        parsehub_max_mb=100,  # .env 里的数字是 int
        parsehub_auto=True,
        parsehub_proxy_platforms=["twitter", "youtube"],  # .env 写 JSON 列表
    )
    assert cfg.parsehub_max_mb == 100
    assert cfg.parsehub_proxy_platforms == ["twitter", "youtube"]
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
async def test_auto_rule_respects_session_switch(plugin):
    """群白名单已改成按会话的开关：关掉的群不解析，别的群不受影响。"""
    plugin.SWITCHES.set(plugin.switches.group_key(123456), False)
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
    """同一会话内、窗口内只处理一次；不同链接互不影响。"""
    url = "https://b23.tv/a"
    assert plugin.DEDUPE.should_skip("group:1", url, 60) is False
    assert plugin.DEDUPE.should_skip("group:1", url, 60) is True
    assert plugin.DEDUPE.should_skip("group:1", "https://b23.tv/b", 60) is False
    assert plugin.DEDUPE.should_skip("group:1", url, 0) is False  # 0 = 不去重


def test_content_key_normalises_platform_ids(plugin, core):
    """同一个作品的不同写法归一成同一个 key（下面几条是实测出来的真实地址）。"""
    # 抖音：短链展开后 ParseHub 给的 raw_url 就长这样
    share = "https://www.iesdouyin.com/share/video/7695216850086505892/"
    assert core.content_key(share) == "douyin:7695216850086505892"
    assert (
        core.content_key(share + "?from_ssr=1&did=MS4wLjABAAAA")
        == "douyin:7695216850086505892"
    )
    assert core.content_key(share.rstrip("/")) == core.content_key(share)
    # ParseHub 给 douyin 的 raw_url 两种形态都出现过，必须归一成同一个
    assert (
        core.content_key("https://www.douyin.com/video/7695216850086505892")
        == "douyin:7695216850086505892"
    )

    # B 站：移动端 / 带一堆追踪参数 / 末尾斜杠，都是同一个视频
    bv = core.content_key("https://www.bilibili.com/video/BV1yupv6YEuE/")
    assert bv == "bilibili:bv1yupv6yeue"
    assert core.content_key("https://www.bilibili.com/video/BV1yupv6YEuE") == bv
    assert (
        core.content_key("https://m.bilibili.com/video/BV1yupv6YEuE?p=1") == bv
    )
    assert (
        core.content_key("https://b23.tv/3XdwepO") != bv
    )  # 短链只能等解析展开


def test_content_key_falls_back_to_host_and_path(plugin, core):
    """认不出 ID 的链接退化成「主机 + 路径」，丢掉查询串。"""
    key = core.content_key("https://b23.tv/3XdwepO")
    assert key == "b23.tv/3xdwepo"
    assert core.content_key("https://b23.tv/3XdwepO?share=1") == key


def test_dedupe_is_per_session(plugin):
    """跨会话不去重：同一个链接换个群 / 私聊照样解析。"""
    url = "https://b23.tv/a"
    assert plugin.DEDUPE.should_skip("group:1", url, 60) is False
    assert plugin.DEDUPE.should_skip("group:1", url, 60) is True
    assert plugin.DEDUPE.should_skip("group:2", url, 60) is False
    assert plugin.DEDUPE.should_skip("private:20000", url, 60) is False


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
async def test_handle_link_sends_card_before_media(
    plugin, monkeypatch, tmp_media, core, card
):
    """能出卡时：卡片先发、媒体随后，且不再补一条文字。"""
    img = tmp_media / "pic.jpg"
    img.write_bytes(b"x" * 1024)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/pic.jpg", "jpg")
        )

    async def fake_download(url, path, proxy="", cookie=""):
        return FakeDownloadResult([img])

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
    monkeypatch.setattr(card, "fetch_cover", lambda result, files: b"cover")
    monkeypatch.setattr(card, "render", fake_render)

    bot = FakeBot()
    await plugin.handle_link(
        bot, make_group_event("https://b23.tv/abcd"), "https://b23.tv/abcd"
    )

    sent = [kw["message"] for api, kw in bot.calls if api == "send_msg"]
    assert len(sent) == 2, sent  # 卡片 + 媒体，没有第三条文字
    assert "card.jpg" in sent[0][0].data["file"]
    assert "pic.jpg" in sent[1][0].data["file"]
    assert bot.sent_texts == ["", ""], "出了卡就不该再发文字"


@pytest.mark.asyncio
async def test_same_video_other_url_form_is_skipped(
    plugin, monkeypatch, tmp_media, core
):
    """短链和完整链是同一个作品：同一会话里只发一次，换个会话照发。"""
    img = tmp_media / "pic.jpg"
    img.write_bytes(b"x" * 1024)

    async def fake_parse(url, proxy="", cookie=""):
        # 两种写法解析出来是同一个作品，raw_url 也归一成同一个
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/pic.jpg", "jpg"),
            raw_url="https://www.bilibili.com/video/BV1yupv6YEuE/",
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

    # 这个用例只测去重，关掉媒体缓存免得缓存先命中
    monkeypatch.setattr(
        plugin,
        "MEDIA_CACHE",
        plugin.mediacache.MediaCache(
            tmp_media / "nocache", ttl_seconds=0, max_bytes=0
        ),
    )

    short = "https://b23.tv/3XdwepO"
    full = "https://www.bilibili.com/video/BV1yupv6YEuE"

    first = FakeBot()
    await plugin.handle_link(first, make_group_event(short), short)
    assert [api for api, _ in first.calls].count("send_msg") > 0

    second = FakeBot()
    await plugin.handle_link(second, make_group_event(full), full)
    assert [api for api, _ in second.calls].count("send_msg") == 0, (
        "同一个作品的另一种写法不该再发一次"
    )
    # 仍然给「收到」的回应，免得看起来像没反应
    assert plugin.REACT_START in [
        kw["emoji_id"]
        for api, kw in second.calls
        if api == "set_msg_emoji_like"
    ]

    third = FakeBot()
    await plugin.handle_link(
        third, make_group_event(full, group_id=654321), full
    )
    assert [api for api, _ in third.calls].count("send_msg") > 0, (
        "换个会话就该照常解析"
    )


def _patch_cached_parsehub(core, monkeypatch, tmp_media, calls, raw_url):
    """把解析 / 下载换成计数用的假实现。"""
    img = tmp_media / "pic.jpg"
    img.write_bytes(b"x" * 1024)

    async def fake_parse(url, proxy="", cookie=""):
        calls["parse"] += 1
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/pic.jpg", "jpg"),
            raw_url=raw_url,
        )

    async def fake_download(url, path, proxy="", cookie=""):
        calls["download"] += 1
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
    return img


@pytest.mark.asyncio
async def test_cache_hit_still_respects_dedupe(
    plugin, monkeypatch, tmp_media, core
):
    """命中缓存也要遵守去重：同一个群不能靠缓存把同一个作品反复发。"""
    calls = {"parse": 0, "download": 0}
    _patch_cached_parsehub(
        core,
        monkeypatch,
        tmp_media,
        calls,
        "https://www.bilibili.com/video/BV1yupv6YEuE/",
    )
    url = "https://b23.tv/3XdwepO"

    first = FakeBot()
    await plugin.handle_link(first, make_group_event(url), url)
    assert calls == {"parse": 1, "download": 1}
    assert [api for api, _ in first.calls].count("send_msg") > 0

    # 同一个群再来一次：去重拦住，什么都不发（以前会被缓存重新发一遍）
    second = FakeBot()
    await plugin.handle_link(second, make_group_event(url), url)
    assert calls == {"parse": 1, "download": 1}
    assert [api for api, _ in second.calls].count("send_msg") == 0

    # 换个群：命中缓存直接发，不解析也不下载
    third = FakeBot()
    await plugin.handle_link(third, make_group_event(url, group_id=654321), url)
    assert calls == {"parse": 1, "download": 1}, "缓存命中不该再解析 / 下载"
    assert [api for api, _ in third.calls].count("send_msg") > 0


@pytest.mark.asyncio
async def test_same_work_other_url_form_in_same_session_is_silent(
    plugin, monkeypatch, tmp_media, core
):
    """用户复现过的那对链接：先短链、再完整链，同一个群只发一次。"""
    calls = {"parse": 0, "download": 0}
    _patch_cached_parsehub(
        core,
        monkeypatch,
        tmp_media,
        calls,
        "https://www.douyin.com/video/7694556586975504313",
    )
    short = "https://v.douyin.com/mSJ4_DyZ4go/"
    full = "https://www.iesdouyin.com/share/video/7694556586975504313/"

    first = FakeBot()
    await plugin.handle_link(first, make_group_event(short), short)
    assert [api for api, _ in first.calls].count("send_msg") > 0

    second = FakeBot()
    await plugin.handle_link(second, make_group_event(full), full)
    assert calls == {"parse": 1, "download": 1}, "第二次不该再解析 / 下载"
    assert [api for api, _ in second.calls].count("send_msg") == 0, (
        "同一个作品换个写法再发，同群应该静默"
    )


@pytest.mark.asyncio
async def test_other_short_link_skips_download_via_cache(
    plugin, monkeypatch, tmp_media, core
):
    """另一个短链指向同一作品：短链里没 ID 所以还得解析，但不再下载。"""
    calls = {"parse": 0, "download": 0}
    _patch_cached_parsehub(
        core,
        monkeypatch,
        tmp_media,
        calls,
        "https://www.bilibili.com/video/BV1yupv6YEuE/",
    )

    first_url = "https://b23.tv/3XdwepO"
    await plugin.handle_link(FakeBot(), make_group_event(first_url), first_url)
    assert calls == {"parse": 1, "download": 1}

    # 换个群：同群 120 秒内会被去重拦掉，那样就测不到缓存这一层了
    other_url = "https://b23.tv/ZzZzZzZ"
    second = FakeBot()
    await plugin.handle_link(
        second, make_group_event(other_url, group_id=654321), other_url
    )
    assert calls == {"parse": 2, "download": 1}, "只该多一次解析，不该再下载"
    assert [api for api, _ in second.calls].count("send_msg") > 0


@pytest.mark.asyncio
async def test_cache_off_still_parses_and_downloads(
    plugin, monkeypatch, tmp_media, core
):
    """关掉缓存后行为跟以前一样。"""
    calls = {"parse": 0, "download": 0}
    _patch_cached_parsehub(
        core,
        monkeypatch,
        tmp_media,
        calls,
        "https://www.bilibili.com/video/BV1yupv6YEuE/",
    )
    monkeypatch.setattr(
        plugin,
        "MEDIA_CACHE",
        plugin.mediacache.MediaCache(
            tmp_media / "media", ttl_seconds=0, max_bytes=0
        ),
    )
    url = "https://b23.tv/3XdwepO"
    await plugin.handle_link(FakeBot(), make_group_event(url), url)
    await plugin.handle_link(
        FakeBot(), make_group_event(url, group_id=654321), url
    )
    assert calls == {"parse": 2, "download": 2}


def test_short_error_strips_nested_prefixes(plugin):
    """ParseHub 的错误信息套好几层，发给群之前要压平。"""
    exc = RuntimeError("下载错误: 下载失败: 下载错误: HTTP错误: 403")
    assert plugin._short_error(exc) == "HTTP错误: 403"
    assert plugin._short_error(RuntimeError("")) == "RuntimeError"


def _patch_failing_download(core, monkeypatch, tmp_media, calls, failures):
    """造一个「前 N 次失败、之后成功」的下载。"""
    img = tmp_media / "pic.jpg"
    img.write_bytes(b"x" * 1024)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/pic.jpg", "jpg")
        )

    async def fake_download(url, path, proxy="", cookie=""):
        calls["download"] += 1
        if calls["download"] <= failures:
            raise RuntimeError("下载错误: 下载失败: 下载错误: HTTP错误: 403")
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


@pytest.mark.asyncio
async def test_download_retries_transient_cdn_error(
    plugin, monkeypatch, tmp_media, core
):
    """CDN 偶发 403：重试一次就成功，媒体照样发出去。"""
    calls = {"download": 0}
    _patch_failing_download(core, monkeypatch, tmp_media, calls, failures=1)
    monkeypatch.setattr(plugin, "_DOWNLOAD_RETRY_DELAY", 0)

    bot = FakeBot()
    url = "https://b23.tv/abcd"
    await plugin.handle_link(bot, make_group_event(url), url)

    assert calls["download"] == 2, "第一次 403 之后应该重试"
    assert [api for api, _ in bot.calls].count("send_msg") > 0
    assert not any("403" in text for text in bot.sent_texts), bot.sent_texts


@pytest.mark.asyncio
async def test_download_failure_after_retries_reports_short_error(
    plugin, monkeypatch, tmp_media, core
):
    """一直失败：重试到上限后只发一条压平过的错误。"""
    calls = {"download": 0}
    _patch_failing_download(core, monkeypatch, tmp_media, calls, failures=99)
    monkeypatch.setattr(plugin, "_DOWNLOAD_RETRY_DELAY", 0)

    bot = FakeBot()
    url = "https://b23.tv/abcd"
    await plugin.handle_link(bot, make_group_event(url), url)

    assert calls["download"] == plugin._DOWNLOAD_ATTEMPTS
    texts = [text for text in bot.sent_texts if text]
    assert texts == ["下载失败：HTTP错误: 403"], texts


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
    # 协议端抓一遍 URL 再上传很慢，必须显式给一个宽限，否则 30s 就被掐断
    assert fwd["_timeout"] == plugin.plugin_config.parsehub_forward_timeout
    assert len(fwd["messages"]) == 3
    # 节点名跟随 NICKNAME（测试环境没配，退回默认值）
    assert all(
        n["data"]["name"] == plugin._forward_node_name()
        for n in fwd["messages"]
    )
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


@pytest.mark.asyncio
async def test_send_swallows_protocol_failure(plugin, monkeypatch):
    """协议端拒绝发消息时只记警告，不能把异常抛给 matcher。"""
    bot = FakeBot()

    async def refuse(*args, **kwargs):
        raise ActionFailed(
            status="failed",
            retcode=1200,
            data=None,
            message="发送消息失败 (code=120)",
        )

    monkeypatch.setattr(bot, "send", refuse)
    # 不抛异常就算通过（修复前这里会抛 ActionFailed，日志里多一整段堆栈）
    await plugin._send(bot, make_group_event("test"), Message("hi"))


def _emojis(bot: FakeBot) -> list[str]:
    """取这次调用里点过的表情 ID。"""
    return [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]


@pytest.mark.asyncio
async def test_warmup_preloads_parsehub(plugin, monkeypatch):
    """启动预热：后台调用 hub，且失败也不抛异常（不阻塞启动）。"""
    called = {"hub": 0}

    async def fake_hub():
        called["hub"] += 1
        return object()

    monkeypatch.setattr(plugin.core.CLIENT, "hub", fake_hub)
    await plugin._warm_parsehub()
    await asyncio.sleep(0.05)  # 让 create_task 跑完
    assert called["hub"] == 1

    async def boom():
        raise RuntimeError("预热炸了")

    monkeypatch.setattr(plugin.core.CLIENT, "hub", boom)
    await plugin._warm_parsehub()  # 不该抛出去
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_parse_failure_reacts_fail_without_text(
    plugin, monkeypatch, core
):
    """解析失败（非「作品不可用」）：点失败表情就够，不刷屏。"""

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
        bot, make_group_event("https://b23.tv/abcd"), "https://b23.tv/abcd"
    )

    assert plugin.REACT_FAIL in _emojis(bot), bot.calls
    assert [api for api, _ in bot.calls if api == "send_msg"] == [], (
        bot.sent_texts
    )


@pytest.mark.asyncio
async def test_content_unavailable_gets_notice(plugin, monkeypatch, core):
    """作品被删 / 下架：失败表情 + 一句人话（自动解析也要说）。"""
    from parsehub.errors import ContentUnavailableError

    async def fake_parse(url, proxy="", cookie=""):
        raise ContentUnavailableError("作品不可用: 抖音 status=0")

    monkeypatch.setattr(
        core.CLIENT, "is_supported", lambda url: asyncio.sleep(0, result=True)
    )
    monkeypatch.setattr(core.CLIENT, "parse", fake_parse)

    bot = FakeBot()
    url = "https://v.douyin.com/kE-zqatAP7M/"
    await plugin.handle_link(bot, make_group_event(url), url)

    assert plugin.REACT_FAIL in _emojis(bot), bot.calls
    assert any("不可用" in text for text in bot.sent_texts), bot.sent_texts
