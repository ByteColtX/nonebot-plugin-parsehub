"""重构回归测试：每个曾经的缺陷一条，防止再退回去。"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fake import make_group_event
from fakes import (
    FakeBot,
    FakeDownloadResult,
    FakeMediaFile,
    FakeMediaRef,
    FakeParseResult,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed

URL = "https://b23.tv/abcd"


def _patch_parse(instance, monkeypatch, *, parse=None, supported=True):
    """把解析换成假实现，并固定平台。"""
    monkeypatch.setattr(
        instance.client,
        "platform_id",
        lambda url: asyncio.sleep(0, result="bilibili"),
    )
    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=supported),
    )
    if parse is not None:
        monkeypatch.setattr(instance.client, "parse", parse)


async def test_cache_hit_with_card_disabled_still_sends_caption(
    instance, service, update_settings, tmp_path: Path
) -> None:
    """P1-1：关掉卡片后命中缓存，正文不能丢。"""
    media = tmp_path / "a.jpg"
    media.write_bytes(b"media")
    card = tmp_path / "card.jpg"
    card.write_bytes(b"card")
    instance.cache.store(["b23.tv/abcd"], [media], card=card, caption="标题")
    update_settings(card_enabled=False)

    bot = FakeBot()
    result = await service.handle(instance, bot, make_group_event(URL), URL)

    assert result.outcome is service.Outcome.FROM_CACHE
    assert "标题" in bot.sent_texts


async def test_failed_parse_does_not_poison_dedupe(
    instance, service, monkeypatch
) -> None:
    """P1-2：解析失败后，同会话再发同一个链接要重新尝试。"""
    calls = {"parse": 0}

    async def flaky(url, proxy="", cookie=""):
        calls["parse"] += 1
        if calls["parse"] == 1:
            raise RuntimeError("网络抖动")
        return FakeParseResult(media=None)

    _patch_parse(instance, monkeypatch, parse=flaky)
    bot = FakeBot()
    event = make_group_event(URL)

    first = await service.handle(instance, bot, event, URL)
    second = await service.handle(instance, bot, event, URL)

    assert first.outcome is service.Outcome.PARSE_FAILED
    assert second.outcome is service.Outcome.NO_MEDIA
    assert calls["parse"] == 2, "失败的那次不该把链接记进去重窗口"


async def test_duplicate_auto_message_is_silent(
    instance, service, monkeypatch
) -> None:
    """同一会话重复出现：不点表情、不发消息。"""
    _patch_parse(
        instance,
        monkeypatch,
        parse=lambda url, proxy="", cookie="": asyncio.sleep(
            0, result=FakeParseResult(media=None)
        ),
    )
    event = make_group_event(URL)
    await service.handle(instance, FakeBot(), event, URL)

    bot = FakeBot()
    result = await service.handle(instance, bot, event, URL)

    assert result.outcome is service.Outcome.DUPLICATE
    assert bot.calls == []


async def test_image_rejection_does_not_escape(
    instance, service, monkeypatch, tmp_path: Path
) -> None:
    """P1-3：图片被协议端拒绝时只记失败，异常不能穿到 matcher。"""

    class RejectImageBot(FakeBot):
        async def call_api(self, api, **kwargs):  # type: ignore[override]
            if api == "send_msg":
                message = kwargs.get("message")
                if message is not None and any(
                    seg.type == "image" for seg in message
                ):
                    raise ActionFailed(
                        status="failed",
                        retcode=1200,
                        data=None,
                        message="image rejected",
                    )
            return await super().call_api(api, **kwargs)

    img = tmp_path / "pic.jpg"
    img.write_bytes(b"x" * 512)
    _patch_parse(
        instance,
        monkeypatch,
        parse=lambda url, proxy="", cookie="": asyncio.sleep(
            0,
            result=FakeParseResult(
                media=FakeMediaRef("https://cdn.example.com/pic.jpg", "jpg")
            ),
        ),
    )
    monkeypatch.setattr(
        instance.client,
        "download_parsed",
        lambda result, path, proxy="", cookie="": asyncio.sleep(
            0, result=FakeDownloadResult([img])
        ),
    )

    bot = RejectImageBot()
    result = await service.handle(instance, bot, make_group_event(URL), URL)

    assert result.outcome is service.Outcome.DELIVERY_FAILED
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert "5" in emojis, emojis


async def test_platform_id_is_looked_up_once(
    instance, service, monkeypatch
) -> None:
    """P2-4：一个链接只查一次平台。"""
    calls = {"platform": 0}

    async def platform_id(url):
        calls["platform"] += 1
        return "bilibili"

    monkeypatch.setattr(instance.client, "platform_id", platform_id)
    monkeypatch.setattr(
        instance.client, "is_supported", lambda url: asyncio.sleep(0, True)
    )

    async def boom(url, proxy="", cookie=""):
        raise RuntimeError("炸了")

    monkeypatch.setattr(instance.client, "parse", boom)
    await service.handle(instance, FakeBot(), make_group_event(URL), URL)

    assert calls["platform"] == 1


async def test_download_timeout_is_not_retried(
    instance, service, monkeypatch
) -> None:
    """P2-6：超时不重试（线程取消不掉，重试只会多开一份下载）。"""
    calls = {"download": 0}

    async def slow(result, path, proxy="", cookie=""):
        calls["download"] += 1
        raise TimeoutError("too slow")

    _patch_parse(
        instance,
        monkeypatch,
        parse=lambda url, proxy="", cookie="": asyncio.sleep(
            0,
            result=FakeParseResult(
                media=FakeMediaRef("https://cdn.example.com/a.mp4", "mp4")
            ),
        ),
    )
    monkeypatch.setattr(instance.client, "download_parsed", slow)

    bot = FakeBot()
    result = await service.handle(instance, bot, make_group_event(URL), URL)

    assert result.outcome is service.Outcome.DOWNLOAD_FAILED
    assert calls["download"] == 1, "超时不该重试"
    assert any("下载失败" in text for text in bot.sent_texts), bot.sent_texts


async def test_relay_start_failure_is_fatal(
    instance, monkeypatch, update_settings
) -> None:
    """P2-8：http 模式起不来就明确报错，不能留下 port 0。"""
    update_settings(delivery_mode="http")

    def boom(host, port):
        raise OSError("address already in use")

    monkeypatch.setattr(instance.relay, "start", boom)

    with pytest.raises(RuntimeError, match="中转服务启动失败"):
        await instance.startup()


def test_cache_lives_outside_relay_root(instance) -> None:
    """P3-9：缓存不能被中转服务暴露。"""
    cache_root = instance.cache.root.resolve()
    relay_root = instance.relay.root.resolve()
    assert cache_root != relay_root
    assert not cache_root.is_relative_to(relay_root)
    assert not relay_root.is_relative_to(cache_root)


def test_plugin_dirs_come_from_localstore(plugin) -> None:
    """目录必须由 localstore 给出：不自己拼，也不退回系统临时目录。"""
    temp = Path(tempfile.gettempdir()).resolve()
    for path in (plugin.paths.cache_root(), plugin.paths.data_root()):
        resolved = path.resolve()
        assert resolved.name == "nonebot_plugin_parsehub", resolved
        assert not resolved.is_relative_to(temp), resolved


async def test_cache_stores_only_sent_media(
    instance, service, monkeypatch, update_settings, tmp_path: Path
) -> None:
    """P3-12：被体积上限挡掉的媒体不该进缓存。"""
    small = tmp_path / "small.jpg"
    small.write_bytes(b"x" * 10)
    big = tmp_path / "big.jpg"
    big.write_bytes(b"x" * 500)
    update_settings(max_file_bytes=100)

    refs = [
        FakeMediaRef("https://cdn.example.com/small.jpg", "jpg"),
        FakeMediaRef("https://cdn.example.com/big.jpg", "jpg"),
    ]
    _patch_parse(
        instance,
        monkeypatch,
        parse=lambda url, proxy="", cookie="": asyncio.sleep(
            0, result=FakeParseResult(media=refs)
        ),
    )
    monkeypatch.setattr(
        instance.client,
        "download_parsed",
        lambda result, path, proxy="", cookie="": asyncio.sleep(
            0,
            result=FakeDownloadResult(
                [small, big],
                media=[FakeMediaFile(small), FakeMediaFile(big)],
            ),
        ),
    )

    result = await service.handle(
        instance, FakeBot(), make_group_event(URL), URL
    )
    assert result.outcome is service.Outcome.DELIVERED

    entry = instance.cache.get("b23.tv/abcd")
    assert entry is not None
    assert [path.name for path in entry.files] == ["small.jpg"]


async def test_unexpected_error_still_gets_a_terminal_signal(
    instance, service, monkeypatch
) -> None:
    """兜底：内部异常也要有提示和失败表情，不能静默。"""
    _patch_parse(instance, monkeypatch)

    async def boom(*args, **kwargs):
        raise RuntimeError("内部炸了")

    monkeypatch.setattr(service.works, "deliver", boom)

    bot = FakeBot()
    result = await service.handle(instance, bot, make_group_event(URL), URL)

    assert result.outcome is service.Outcome.INTERNAL_ERROR
    assert any("出错" in text for text in bot.sent_texts), bot.sent_texts
    assert "5" in [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]


async def test_support_check_failure_falls_through_to_parse(
    instance, service, monkeypatch
) -> None:
    """is_supported 自己炸了不该把整条消息吞掉，交给解析去报错。"""
    monkeypatch.setattr(
        instance.client,
        "platform_id",
        lambda url: asyncio.sleep(0, result="bilibili"),
    )

    async def broken(url):
        raise RuntimeError("判断失败")

    monkeypatch.setattr(instance.client, "is_supported", broken)
    monkeypatch.setattr(
        instance.client,
        "parse",
        lambda url, proxy="", cookie="": asyncio.sleep(
            0, result=FakeParseResult(media=None)
        ),
    )

    result = await service.handle(
        instance, FakeBot(), make_group_event(URL), URL
    )
    assert result.outcome is service.Outcome.NO_MEDIA


async def test_dedupe_window_zero_does_not_grow(
    instance, service, monkeypatch, update_settings
) -> None:
    """PARSEHUB_DEDUPE_SECONDS=0 时不能把每个作品都记进 _seen。"""
    update_settings(dedupe_seconds=0)
    _patch_parse(
        instance,
        monkeypatch,
        parse=lambda url, proxy="", cookie="": asyncio.sleep(
            0, result=FakeParseResult(media=None)
        ),
    )
    event = make_group_event(URL)

    first = await service.handle(instance, FakeBot(), event, URL)
    second = await service.handle(instance, FakeBot(), event, URL)

    assert first.outcome is service.Outcome.NO_MEDIA
    assert second.outcome is service.Outcome.NO_MEDIA, "窗口为 0 不该去重"
    assert instance.dedupe._seen == {}


async def test_partial_failure_not_cached(
    instance, service, monkeypatch, tmp_path: Path
) -> None:
    """部分媒体发送失败的作品不能写进缓存：下次命中会缺文件。"""

    class RejectSecondBot(FakeBot):
        """合并转发挂掉，第二个文件的逐条发送也被拒。"""

        def __init__(self) -> None:
            super().__init__()
            self._media_sends = 0

        async def call_api(self, api, **kwargs):  # type: ignore[override]
            if api == "send_group_forward_msg":
                raise ActionFailed(
                    status="failed",
                    retcode=1200,
                    data=None,
                    message="no forward",
                )
            if api == "send_msg":
                message = kwargs.get("message")
                if message is not None and any(
                    seg.type == "image"
                    and str(seg.data.get("file", "")).endswith("two.jpg")
                    for seg in message
                ):
                    self._media_sends += 1
                    if self._media_sends >= 1:
                        raise ActionFailed(
                            status="failed",
                            retcode=1200,
                            data=None,
                            message="second rejected",
                        )
            return await super().call_api(api, **kwargs)

    first = tmp_path / "one.jpg"
    second = tmp_path / "two.jpg"
    first.write_bytes(b"x" * 512)
    second.write_bytes(b"y" * 512)
    refs = [
        FakeMediaRef("https://cdn.example.com/one.jpg", "jpg"),
        FakeMediaRef("https://cdn.example.com/two.jpg", "jpg"),
    ]
    _patch_parse(
        instance,
        monkeypatch,
        parse=lambda url, proxy="", cookie="": asyncio.sleep(
            0, result=FakeParseResult(media=refs)
        ),
    )
    monkeypatch.setattr(
        instance.client,
        "download_parsed",
        lambda result, path, proxy="", cookie="": asyncio.sleep(
            0,
            result=FakeDownloadResult(
                [first, second],
                media=[FakeMediaFile(first), FakeMediaFile(second)],
            ),
        ),
    )

    bot = RejectSecondBot()
    result = await service.handle(instance, bot, make_group_event(URL), URL)

    assert result.report is not None and result.report.failed, (
        "第二个文件的失败要记进 report.failed"
    )
    assert instance.cache.get("b23.tv/abcd") is None, (
        "部分失败的作品不能写进缓存"
    )


async def test_truncated_notice_via_result(
    instance, service, monkeypatch, tmp_path: Path
) -> None:
    """媒体数超上限的截断提示走 Result.notice，由 handle 统一发一次。"""
    files = []
    for i in range(20):
        path = tmp_path / f"m{i}.jpg"
        path.write_bytes(b"x" * 64)
        files.append(path)
    refs = [
        FakeMediaRef(f"https://cdn.example.com/m{i}.jpg", "jpg")
        for i in range(25)
    ]
    _patch_parse(
        instance,
        monkeypatch,
        parse=lambda url, proxy="", cookie="": asyncio.sleep(
            0, result=FakeParseResult(media=refs)
        ),
    )
    monkeypatch.setattr(
        instance.client,
        "download_parsed",
        lambda result, path, proxy="", cookie="": asyncio.sleep(
            0,
            result=FakeDownloadResult(
                files, media=[FakeMediaFile(p) for p in files]
            ),
        ),
    )

    bot = FakeBot()
    result = await service.handle(instance, bot, make_group_event(URL), URL)

    assert result.notice is not None, "截断提示要走 Result.notice"
    assert "作品共 25 项" in result.notice
    notice_texts = [text for text in bot.sent_texts if "作品共 25 项" in text]
    assert len(notice_texts) == 1, "截断提示只发一次"

    calls = bot.calls
    notice_idx = next(
        i
        for i, (api, kw) in enumerate(calls)
        if api == "send_msg"
        and "作品共 25 项" in kw["message"].extract_plain_text()
    )
    media_idx = next(
        i for i, (api, _) in enumerate(calls) if api == "send_group_forward_msg"
    )
    assert media_idx < notice_idx, "提示要晚于媒体"
    emoji_idx = max(
        i for i, (api, _) in enumerate(calls) if api == "set_msg_emoji_like"
    )
    assert notice_idx < emoji_idx, "提示要早于终态表情"
