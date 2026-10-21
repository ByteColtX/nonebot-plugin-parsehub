"""B 站分P：链接带 p 就解析该 P，不带时用默认P，时长按对应 P 计算。"""

from __future__ import annotations

import pytest
from fake import make_group_event
from test_duration_limit import _wire
from test_plugin import FakeBot


@pytest.fixture()
def tmp_media(tmp_path, monkeypatch, instance):
    """中转根目录指到临时目录，避免真写到仓库里。"""
    monkeypatch.setattr(instance.relay, "root", tmp_path)
    monkeypatch.setattr(instance.endpoint, "host", "127.0.0.1")
    monkeypatch.setattr(instance.endpoint, "port", 18899)
    return tmp_path


def test_requested_part(plugin) -> None:
    """没写 / 非法 / 0 都按默认P，正常数字按原值。"""
    assert plugin.bilibili.requested_part("https://b23.tv/abcd") == 1
    assert plugin.bilibili.requested_part("https://b23.tv/abcd?p=3") == 3
    assert plugin.bilibili.requested_part("https://b23.tv/abcd?p=abc") == 1
    assert plugin.bilibili.requested_part("https://b23.tv/abcd?p=0") == 1


def test_part_url_keeps_requested_part(plugin) -> None:
    """带 p 就保留，p=1 / 非法 p 归一成默认P；其它参数保留。"""
    assert (
        plugin.bilibili.part_url(
            "https://www.bilibili.com/video/BV1yupv6YEuE?p=2&spm_id_from=333"
        )
        == "https://www.bilibili.com/video/BV1yupv6YEuE?spm_id_from=333&p=2"
    )
    assert (
        plugin.bilibili.part_url("https://m.bilibili.com/video/av170001?p=3")
        == "https://m.bilibili.com/video/av170001?p=3"
    )
    assert (
        plugin.bilibili.part_url(
            "https://www.bilibili.com/video/BV1yupv6YEuE?p=1"
        )
        == "https://www.bilibili.com/video/BV1yupv6YEuE"
    )
    assert (
        plugin.bilibili.part_url(
            "https://www.bilibili.com/video/BV1yupv6YEuE?p=abc"
        )
        == "https://www.bilibili.com/video/BV1yupv6YEuE"
    )
    assert (
        plugin.bilibili.part_url("https://b23.tv/abcd?p=2")
        == "https://b23.tv/abcd?p=2"
    )


def test_part_url_leaves_other_urls_alone(plugin) -> None:
    """非 B 站视频链接（动态、其它平台）不动。"""
    dynamic = "https://t.bilibili.com/123456789012345678"
    assert plugin.bilibili.part_url(dynamic) == dynamic
    xhs = "https://www.xiaohongshu.com/explore/abc123?p=2"
    assert plugin.bilibili.part_url(xhs) == xhs


def test_page_seconds_uses_requested_page(plugin) -> None:
    """B 站 view 的 duration 是所有分P总和，必须取指定 pages 项。"""
    data = {
        "duration": 168,
        "pages": [
            {"page": 1, "duration": 51},
            {"page": 2, "duration": 57},
            {"page": 3, "duration": 60},
        ],
    }
    assert plugin.bilibili.page_seconds(data) == 51
    assert plugin.bilibili.page_seconds(data, 2) == 57
    assert plugin.bilibili.page_seconds(data, 99) == 51
    assert plugin.bilibili.page_seconds({"duration": 51}) == 51
    assert plugin.bilibili.page_seconds({}) == 0
    assert plugin.bilibili.page_seconds(None) == 0


@pytest.mark.asyncio
async def test_resolve_short_link_keeps_p(plugin, monkeypatch) -> None:
    """短链展开后带 p，保留该分P。"""

    class FakeResponse:
        url = "https://www.bilibili.com/video/BV1yupv6YEuE?p=3"

        def raise_for_status(self) -> None:
            pass

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers, follow_redirects):
            return FakeResponse()

    monkeypatch.setattr(plugin.bilibili, "_client", lambda proxy: FakeClient())
    resolved = await plugin.bilibili.resolve_part_url("https://b23.tv/abcd")
    assert resolved == "https://www.bilibili.com/video/BV1yupv6YEuE?p=3"


@pytest.mark.asyncio
async def test_resolve_short_link_preserves_its_own_p(
    plugin, monkeypatch
) -> None:
    """短链自己带 p、重定向目标没带时，保留短链的 p。"""

    class FakeResponse:
        url = "https://www.bilibili.com/video/BV1yupv6YEuE"

        def raise_for_status(self) -> None:
            pass

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers, follow_redirects):
            return FakeResponse()

    monkeypatch.setattr(plugin.bilibili, "_client", lambda proxy: FakeClient())
    resolved = await plugin.bilibili.resolve_part_url("https://b23.tv/abcd?p=2")
    assert resolved == "https://www.bilibili.com/video/BV1yupv6YEuE?p=2"


@pytest.mark.asyncio
async def test_part_seconds_queries_view_api(plugin, monkeypatch) -> None:
    """按 BV 号查 view 接口，返回指定分P时长。"""
    seen = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {
                "code": 0,
                "data": {
                    "duration": 168,
                    "pages": [
                        {"page": 1, "duration": 51},
                        {"page": 2, "duration": 57},
                    ],
                },
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, params, headers):
            seen["url"] = url
            seen["params"] = params
            return FakeResponse()

    monkeypatch.setattr(plugin.bilibili, "_client", lambda proxy: FakeClient())
    seconds = await plugin.bilibili.part_seconds(
        "https://www.bilibili.com/video/BV1yupv6YEuE?p=2"
    )
    assert seconds == 57
    assert seen["url"] == "https://api.bilibili.com/x/web-interface/view"
    assert seen["params"] == {"bvid": "BV1yupv6YEuE"}


def test_content_key_includes_part(plugin) -> None:
    """不同分P不能撞缓存；p=1 与默认P是同一个 key。"""
    default = plugin.keys.content_key(
        "https://www.bilibili.com/video/BV1yupv6YEuE"
    )
    assert (
        plugin.keys.content_key(
            "https://www.bilibili.com/video/BV1yupv6YEuE?p=1"
        )
        == default
    )
    assert (
        plugin.keys.content_key(
            "https://www.bilibili.com/video/BV1yupv6YEuE?p=2"
        )
        != default
    )
    assert plugin.keys.content_key("https://b23.tv/abcd?p=2").endswith(":p2")


@pytest.mark.asyncio
async def test_multipart_uses_requested_page_duration(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
):
    """带 p=2 时解析第 2 P，并用第 2 P 的时长判断上限。"""
    calls = _wire(
        monkeypatch,
        instance,
        card,
        tmp_media,
        duration=600,
        media_mod=media_mod,
    )
    seen = {}
    original_parse = instance.client.parse

    async def capture_parse(url, proxy="", cookie=""):
        seen["parse_url"] = url
        result = await original_parse(url, proxy, cookie)
        seen["result"] = result
        return result

    async def fake_seconds(url, proxy="", cookie=""):
        seen["seconds_url"] = url
        return 120

    monkeypatch.setattr(instance.client, "parse", capture_parse)
    monkeypatch.setattr(plugin.bilibili, "part_seconds", fake_seconds)

    url = "https://www.bilibili.com/video/BV1yupv6YEuE?p=2"
    bot = FakeBot()
    await service.handle(instance, bot, make_group_event(url), url)

    assert seen["parse_url"] == (
        "https://www.bilibili.com/video/BV1yupv6YEuE?p=2"
    )
    assert seen["seconds_url"] == "https://b23.tv/abcd"
    assert seen["result"].media.duration == 120
    assert calls["download"] == 1, "第 2 P 只有 120s，不该被总时长拦下"
    assert not any("太长" in text for text in bot.sent_texts), bot.sent_texts
