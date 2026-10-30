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
    """把解析换成假实现。"""
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


async def test_start_reaction_does_not_gate_parse(
    instance, service, feedback, monkeypatch
) -> None:
    """START 只是反馈：协议端没回包时，解析不能跟着停。"""
    gate = asyncio.Event()
    parse_done = asyncio.Event()
    emojis: list[str] = []

    async def slow_call_api(api, **kwargs):
        if api == "set_msg_emoji_like":
            emojis.append(kwargs["emoji_id"])
            if len(emojis) == 1:
                await gate.wait()
        return {"status": "ok", "retcode": 0, "data": None}

    async def fake_parse(url, proxy="", cookie=""):
        parse_done.set()
        return FakeParseResult(media=None)

    _patch_parse(instance, monkeypatch, parse=fake_parse)
    bot = FakeBot()
    monkeypatch.setattr(bot, "call_api", slow_call_api)

    task = asyncio.create_task(
        service.handle(instance, bot, make_group_event(URL), URL)
    )
    await asyncio.wait_for(parse_done.wait(), 1)
    assert not task.done(), "START 还没回包，解析完成后 handle 不该结束"

    gate.set()
    result = await asyncio.wait_for(task, 1)
    assert result.outcome is service.Outcome.NO_MEDIA
    assert emojis == [feedback.Reaction.START, feedback.Reaction.OK]


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


async def test_post_cache_hit_releases_pre_key(
    instance, service, update_settings, monkeypatch, tmp_path: Path
) -> None:
    """解析后命中缓存，也要把当前短链的 key 交给去重收尾。"""
    media = tmp_path / "a.mp4"
    media.write_bytes(b"media")
    instance.cache.store(["bilibili:bv1a2xl67emp"], [media], seconds=10)
    # 0 表示不裁剪 inflight，漏掉的 key 永远不会自己过期。
    update_settings(dedupe_seconds=0)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            raw_url="https://www.bilibili.com/video/BV1A2xL67EMP"
        )

    _patch_parse(instance, monkeypatch, parse=fake_parse)
    result = await service.handle(
        instance, FakeBot(), make_group_event(URL), URL
    )

    assert result.outcome is service.Outcome.FROM_CACHE
    assert instance.dedupe.claim("group:123456", ["b23.tv/abcd"], 0), (
        "短链 key 还留在 inflight 里，下一次同链接会被静默丢弃"
    )


class _DeadSendBot(FakeBot):
    """send_msg 全部失败的协议端。"""

    async def call_api(self, api, **kwargs):
        if api == "send_msg":
            raise RuntimeError("send failed")
        return await super().call_api(api, **kwargs)


async def test_too_long_send_failure_is_delivery_failed(
    instance, service, feedback, update_settings, monkeypatch
) -> None:
    """超时长只发封面时，卡片和文字都失败不能算成功。"""
    update_settings(max_seconds=300)
    ref = FakeMediaRef("https://cdn.example.com/a.mp4")
    ref.duration = 400

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(media=ref)

    _patch_parse(instance, monkeypatch, parse=fake_parse)
    bot = _DeadSendBot()
    result = await service.handle(instance, bot, make_group_event(URL), URL)

    assert result.outcome is service.Outcome.DELIVERY_FAILED
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert emojis[-1] == feedback.Reaction.FAIL


async def test_no_media_send_failure_is_delivery_failed(
    instance, service, monkeypatch
) -> None:
    """纯文字帖卡片和正文都发不出去时，结局不能是 NO_MEDIA。"""

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(media=None)

    _patch_parse(instance, monkeypatch, parse=fake_parse)
    bot = _DeadSendBot()
    result = await service.handle(instance, bot, make_group_event(URL), URL)

    assert result.outcome is service.Outcome.DELIVERY_FAILED


async def test_post_parse_duplicate_gets_terminal_reaction(
    instance, service, feedback, monkeypatch
) -> None:
    """解析后才发现的重复也要收尾，不能把消息永远留在 START。"""
    instance.dedupe.commit("group:123456", ["bilibili:bv1a2xl67emp"])

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            raw_url="https://www.bilibili.com/video/BV1A2xL67EMP"
        )

    _patch_parse(instance, monkeypatch, parse=fake_parse)
    bot = FakeBot()
    result = await service.handle(instance, bot, make_group_event(URL), URL)

    assert result.outcome is service.Outcome.DUPLICATE
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert emojis == [feedback.Reaction.START, feedback.Reaction.OK]
