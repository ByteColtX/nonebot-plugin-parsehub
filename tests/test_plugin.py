"""插件逻辑测试：链接提取、触发规则、去重、代理分流、cookie、发送流程、媒体中转。

全部离线：parsehub 的解析/下载被替换成假实现，只验证插件自身的行为。
matcher 级别的端到端测试见 test_nonebug.py。

    uv run pytest -q
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fake import make_group_event
from fakes import (
    FakeBot,
    FakeDownloadResult,
    FakeMediaRef,
    FakeParseResult,
)
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed


@pytest.fixture(autouse=True)
def _reset_state(plugin, monkeypatch, instance):
    instance.dedupe._seen.clear()

    async def _fake_platform_id(url):
        return "bilibili"

    monkeypatch.setattr(instance.client, "platform_id", _fake_platform_id)
    yield
    instance.dedupe._seen.clear()


def test_proxy_only_applies_to_listed_platforms(
    plugin, monkeypatch, instance, update_settings
):
    """代理默认只给海外平台走，国内平台直连。"""
    update_settings(proxy="http://127.0.0.1:7890")

    # 默认列表 = 海外平台
    assert instance.settings.proxy_for("youtube") == "http://127.0.0.1:7890"
    assert instance.settings.proxy_for("twitter") == "http://127.0.0.1:7890"
    assert instance.settings.proxy_for("bilibili") == ""
    assert instance.settings.proxy_for("douyin") == ""

    # 留空列表 = 所有平台都走
    update_settings(proxy="http://127.0.0.1:7890", proxy_platforms=frozenset())
    assert instance.settings.proxy_for("bilibili") == "http://127.0.0.1:7890"

    # 没配代理 = 一律直连
    update_settings(proxy="")
    assert instance.settings.proxy_for("youtube") == ""


def test_platform_cookie_prefers_parsehub_cookie_file(
    plugin, monkeypatch, tmp_path, parsehub_client
):
    """cookies.toml（ph set cookie 写的）优先于配置里的兜底 cookie。"""
    import parsehub.cli_config as cc

    cookie_file = tmp_path / "cookies.toml"
    cookie_file.write_text(
        '[cookie]\nbilibili = "SESSDATA=from-file"\n', encoding="utf-8"
    )
    monkeypatch.setattr(cc, "default_cookie_path", lambda: cookie_file)

    assert (
        parsehub_client.platform_cookie("bilibili", "fallback")
        == "SESSDATA=from-file"
    )
    assert (
        parsehub_client.platform_cookie("zhihu", "fallback") == "fallback"
    )  # 文件里没有 → 用兜底
    assert (
        parsehub_client.platform_cookie("bilibili", "") == "SESSDATA=from-file"
    )

    monkeypatch.setattr(
        cc, "default_cookie_path", lambda: tmp_path / "missing.toml"
    )
    assert parsehub_client.platform_cookie("bilibili", "fallback") == "fallback"
    assert parsehub_client.platform_cookie("bilibili", "") == ""


# --------------------------------------------------------------------------- #
# 1. 链接提取
# --------------------------------------------------------------------------- #
def test_config_accepts_env_style_values():
    """回归：.env 里的值会被 pydantic 当成 int/bool/list，配置字段类型必须能接住。"""
    from nonebot_plugin_parsehub.config import Config

    cfg = Config(
        parsehub_max_file_mb=100,  # .env 里的数字是 int
        parsehub_auto=True,
        parsehub_proxy_platforms=["twitter", "youtube"],  # .env 写 JSON 列表
    )
    assert cfg.parsehub_max_file_mb == 100
    assert cfg.parsehub_proxy_platforms == ["twitter", "youtube"]
    assert (
        Config(parsehub_max_file_mb="100").parsehub_max_file_mb == 100
    )  # 字符串也接住


def test_extract_urls_filters_hosts(plugin, keys):
    text = (
        "看看这个 https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=333 "
        "和 https://example.com/not-supported 还有 https://b23.tv/abcd。"
    )
    urls = keys.extract_urls(text)
    assert urls == [
        "https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=333",
        "https://b23.tv/abcd",
    ]


def test_extract_urls_dedupes(plugin, keys):
    text = "https://b23.tv/x https://b23.tv/x"
    assert keys.extract_urls(text) == ["https://b23.tv/x"]


def test_extract_urls_empty(plugin, keys):
    assert keys.extract_urls("今天天气不错，没有链接") == []


def test_extract_urls_rejects_broad_homepages(plugin, keys):
    """大站的首页 / 个人主页不再触发解析。"""
    text = (
        "首页 https://www.bilibili.com/ "
        "微博 https://weibo.com/u/abc "
        "频道 https://www.youtube.com/@someone "
        "主页 https://www.tiktok.com/share/user/123"
    )
    assert keys.extract_urls(text) == []


def test_extract_urls_ignores_removed_hosts(plugin, keys):
    """上游不支持 / 只在特定子域支持的域名不再进白名单。"""
    text = (
        "https://fb.watch/abc "
        "https://ixigua.com/abc "
        "https://threads.net/abc "
        "https://zuiyou.com/abc "
        "https://pipix.com/abc "
        "https://xiaochuankeji.cn/abc"
    )
    assert keys.extract_urls(text) == []


def test_extract_urls_accepts_iesdouyin(plugin, keys):
    """iesdouyin 仍是上游支持的抖音分享域名。"""
    url = "https://www.iesdouyin.com/share/video/7695216850086505892"
    assert keys.extract_urls(url) == [url]


def test_extract_urls_accepts_new_supported_shapes(plugin, keys):
    """补齐的域名和形状都能触发解析。"""
    text = (
        "动态 https://t.bilibili.com/123456789012345678 "
        "专栏 https://www.bilibili.com/opus/123 "
        "Bangumi https://chii.in/subject/topic/123 "
        "皮皮虾 https://h5.pipix.com/s/abc "
        "最右 https://share.xiaochuankeji.cn/hybrid/share/post?pid=123 "
        "最右短链 https://s.izuiyou.com/s/23BpI"
    )
    urls = keys.extract_urls(text)
    assert urls == [
        "https://t.bilibili.com/123456789012345678",
        "https://www.bilibili.com/opus/123",
        "https://chii.in/subject/topic/123",
        "https://h5.pipix.com/s/abc",
        "https://share.xiaochuankeji.cn/hybrid/share/post?pid=123",
        "https://s.izuiyou.com/s/23BpI",
    ]


def test_content_key_bilibili_opus_and_dynamic(plugin, keys):
    """B 站专栏和动态也能归一成独立 key。"""
    assert (
        keys.content_key("https://www.bilibili.com/opus/123") == "bilibili:123"
    )
    assert (
        keys.content_key("https://t.bilibili.com/123456789012345678")
        == "bilibili:123456789012345678"
    )


def test_content_key_instagram_prefixed_paths(plugin, keys):
    """Instagram 的 share / 任意前缀路径也能归一成同一个作品。"""
    assert (
        keys.content_key("https://www.instagram.com/share/p/abc")
        == "instagram:abc"
    )
    assert (
        keys.content_key("https://www.instagram.com/user/p/abc")
        == "instagram:abc"
    )


# --------------------------------------------------------------------------- #
# 1b. 裸 BV 号
# --------------------------------------------------------------------------- #
BV = "BV1xx411c7mD"
BV_URL = "https://www.bilibili.com/video/BV1xx411c7mD"


def test_extract_targets_bare_bv(plugin, keys):
    """只发作品号也能触发，拼成标准链接再交给 parsehub。"""
    assert keys.extract_targets(BV) == [BV_URL]
    assert keys.extract_targets(f"看这个 {BV} 好笑") == [BV_URL]


def test_extract_targets_skips_bv_inside_url(plugin, keys):
    """完整链接里的 BV 号不重复取，只留用户发的那条链接。"""
    link = f"https://www.bilibili.com/video/{BV}?p=2"
    assert keys.extract_targets(link) == [link]
    # 不在白名单域名里的链接也不从中抠 BV 号
    assert keys.extract_targets(f"https://example.com/{BV}") == []


def test_extract_targets_bv_boundaries(plugin, keys):
    """前后紧贴字母数字的不是 BV 号，别误触发。"""
    assert keys.extract_targets(f"x{BV}") == []
    assert keys.extract_targets(f"{BV}2") == []


def test_extract_targets_keeps_message_order(plugin, keys):
    """链接和 BV 号混发时按出现顺序处理。"""
    text = f"{BV} 和 https://b23.tv/abcd"
    assert keys.extract_targets(text) == [BV_URL, "https://b23.tv/abcd"]


def test_bare_bv_shares_key_with_link(plugin, keys):
    """裸 BV 号与完整链接是同一个作品：去重和缓存都对齐。"""
    link = f"https://www.bilibili.com/video/{BV}?spm_id_from=333"
    assert keys.content_key(BV_URL) == keys.content_key(link)
    assert keys.content_key(BV_URL) == f"bilibili:{BV.lower()}"


# --------------------------------------------------------------------------- #
# 2. 自动触发规则
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_auto_rule_matches_group_link(plugin, instance, triggers):
    event = make_group_event("https://b23.tv/abcd")
    assert await triggers.should_auto(instance, event) is True


@pytest.mark.asyncio
async def test_auto_rule_matches_bare_bv(plugin, instance, triggers):
    """消息里只有 BV 号也自动解析。"""
    event = make_group_event(BV)
    assert await triggers.should_auto(instance, event) is True


@pytest.mark.asyncio
async def test_auto_rule_treats_bare_parse_as_link(plugin, instance, triggers):
    """裸写的「解析 <链接>」不是命令，必须退回自动解析。

    ``COMMAND_START`` 默认只有 ``/``：``/解析`` 由命令匹配器（priority 10 +
    block）先吃掉；裸写若也跳过自动解析，两个匹配器都不管，消息就被静默丢弃。
    """
    event = make_group_event("解析 https://b23.tv/abcd")
    assert await triggers.should_auto(instance, event) is True


@pytest.mark.asyncio
async def test_auto_rule_matches_parse_prefixed_sentence(
    plugin, instance, triggers
):
    """「解析一下 <链接>」同样不是命令，应当自动解析。"""
    event = make_group_event("解析一下 https://b23.tv/abcd")
    assert await triggers.should_auto(instance, event) is True


def test_commands_run_before_auto_and_block(plugin, matchers) -> None:
    """自动规则敢不管命令，靠的是命令优先级更高且 block。"""
    for command in (matchers.parse, matchers.platforms, matchers.switch):
        assert command.priority < matchers.auto.priority
        assert command.block is True


@pytest.mark.asyncio
async def test_auto_rule_respects_session_switch(
    plugin, instance, switches, triggers
):
    """群白名单已改成按会话的开关：关掉的群不解析，别的群不受影响。"""
    instance.switches.set(switches.group_key(123456), False)
    assert (
        await triggers.should_auto(
            instance, make_group_event("https://b23.tv/abcd", group_id=123456)
        )
        is False
    )
    assert (
        await triggers.should_auto(
            instance, make_group_event("https://b23.tv/abcd", group_id=999)
        )
        is True
    )


@pytest.mark.asyncio
async def test_auto_rule_off(
    plugin, monkeypatch, instance, triggers, update_settings
):
    update_settings(auto=False)
    assert (
        await triggers.should_auto(
            instance, make_group_event("https://b23.tv/abcd")
        )
        is False
    )


# --------------------------------------------------------------------------- #
# 3. 去重
# --------------------------------------------------------------------------- #
def test_dedupe_window(plugin, instance):
    """同一会话内、窗口内只处理一次；失败 release 之后能重试。"""
    url = "https://b23.tv/a"
    dedupe = instance.dedupe
    assert dedupe.claim("group:1", [url], 60) is True
    assert dedupe.claim("group:1", [url], 60) is False  # 处理中
    dedupe.release("group:1", [url])
    assert dedupe.claim("group:1", [url], 60) is True  # 失败后可以重试
    dedupe.commit("group:1", [url])
    assert dedupe.seen("group:1", url, 60) is True
    assert dedupe.claim("group:1", [url], 60) is False  # 窗口内
    assert dedupe.claim("group:1", ["https://b23.tv/b"], 60) is True
    assert dedupe.seen("group:1", url, 0) is False  # 0 = 不去重


def test_content_key_normalises_platform_ids(plugin, keys):
    """同一个作品的不同写法归一成同一个 key（下面几条是实测出来的真实地址）。"""
    # 抖音：短链展开后 ParseHub 给的 raw_url 就长这样
    share = "https://www.iesdouyin.com/share/video/7695216850086505892/"
    assert keys.content_key(share) == "douyin:7695216850086505892"
    assert (
        keys.content_key(share + "?from_ssr=1&did=MS4wLjABAAAA")
        == "douyin:7695216850086505892"
    )
    assert keys.content_key(share.rstrip("/")) == keys.content_key(share)
    # ParseHub 给 douyin 的 raw_url 两种形态都出现过，必须归一成同一个
    assert (
        keys.content_key("https://www.douyin.com/video/7695216850086505892")
        == "douyin:7695216850086505892"
    )

    # B 站：移动端 / 带一堆追踪参数 / 末尾斜杠，都是同一个视频
    bv = keys.content_key("https://www.bilibili.com/video/BV1yupv6YEuE/")
    assert bv == "bilibili:bv1yupv6yeue"
    assert keys.content_key("https://www.bilibili.com/video/BV1yupv6YEuE") == bv
    assert (
        keys.content_key("https://m.bilibili.com/video/BV1yupv6YEuE?p=1") == bv
    )
    # 不同分P不能撞缓存
    assert (
        keys.content_key("https://www.bilibili.com/video/BV1yupv6YEuE?p=2")
        == "bilibili:bv1yupv6yeue:p2"
    )
    assert (
        keys.content_key("https://b23.tv/3XdwepO") != bv
    )  # 短链只能等解析展开


def test_content_key_zuiyou_and_xiaoheihe_share_ids(plugin, keys):
    """最右 / 小黑盒的分享链接按作品 ID 归一，不能共用一个 key。"""
    zuiyou_a = (
        "https://share.xiaochuankeji.cn/hybrid/share/post"
        "?pid=422153952&app=zuiyou"
    )
    zuiyou_b = (
        "https://share.xiaochuankeji.cn/hybrid/share/post"
        "?pid=426182135&app=zuiyou"
    )
    assert keys.content_key(zuiyou_a) == "zuiyou:422153952"
    assert keys.content_key(zuiyou_b) == "zuiyou:426182135"
    assert keys.content_key(zuiyou_a) != keys.content_key(zuiyou_b)
    # 短链还没有 pid，至少要让两个短码各自成 key，不能都退化成同一个路径
    assert keys.content_key("https://s.izuiyou.com/s/23BpI") == (
        "s.izuiyou.com/s/23bpi"
    )
    assert keys.content_key("https://s.izuiyou.com/s/23BpI") != (
        keys.content_key("https://s.izuiyou.com/s/23Bos")
    )

    xhh_a = (
        "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=361771deb4c0"
    )
    xhh_b = (
        "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=c31872bbe6e4"
    )
    assert keys.content_key(xhh_a) == "xiaoheihe:361771deb4c0"
    assert keys.content_key(xhh_b) == "xiaoheihe:c31872bbe6e4"
    assert keys.content_key(
        "https://www.xiaoheihe.cn/app/bbs/link/361771deb4c0"
    ) == keys.content_key(xhh_a)


def test_content_key_douban_topics(plugin, keys):
    """豆瓣 app 跳转链接要按 topic id 区分，不能都落到 dispatch 路径。"""
    first = (
        "https://www.douban.com/doubanapp/dispatch"
        "?uri=%2Ftopic%2F503848885%3F_spm_id%3DMzgwNzg5OTQ"
        "&_i=93340208406402e&dt_dapp=1"
    )
    second = (
        "https://www.douban.com/doubanapp/dispatch"
        "?uri=%2Ftopic%2F503145092%3F_spm_id%3DMTgxODg1MjY4"
        "&_i=93340218406402e&dt_dapp=1"
    )
    assert keys.content_key(first) == "douban:503848885"
    assert keys.content_key(second) == "douban:503145092"
    assert keys.content_key(first) != keys.content_key(second)
    assert keys.content_key(
        "https://www.douban.com/group/topic/503848885/"
    ) == keys.content_key(first)
    assert keys.content_key(
        "https://m.douban.com/doubanapp/dispatch"
        "?uri=%2Fgroup%2Ftopic%2F503848885"
    ) == keys.content_key(first)


def test_content_key_falls_back_to_host_and_path(plugin, keys):
    """认不出 ID 的链接退化成「主机 + 路径」，丢掉查询串。"""
    key = keys.content_key("https://b23.tv/3XdwepO")
    assert key == "b23.tv/3xdwepo"
    assert keys.content_key("https://b23.tv/3XdwepO?share=1") == key
    assert keys.content_key("https://b23.tv/3XdwepO?p=2") == "b23.tv/3xdwepo:p2"


def test_content_key_fold_case_covers_fallback(plugin, keys):
    """fold_case 也要覆盖没有 ID pattern 的 fallback 路径。"""
    lower = "https://www.youtube.com/shorts/aqz-KE-bpKQ"
    upper = "https://www.youtube.com/shorts/AQZ-KE-BPKQ"
    assert keys.content_key(lower) == keys.content_key(upper)
    assert keys.content_key(lower, fold_case=False) != keys.content_key(
        upper, fold_case=False
    )
    assert keys.content_key(
        "https://b23.tv/AbCdEf", fold_case=False
    ) != keys.content_key("https://b23.tv/abcDEF", fold_case=False)


def test_dedupe_is_per_session(plugin, instance):
    """跨会话不去重：同一个链接换个群 / 私聊照样解析。"""
    url = "https://b23.tv/a"
    dedupe = instance.dedupe
    for session in ("group:1", "group:2", "private:20000"):
        assert dedupe.claim(session, [url], 60) is True
        dedupe.commit(session, [url])
        assert dedupe.seen(session, url, 60) is True
    assert dedupe.claim("group:1", [url], 60) is False


# --------------------------------------------------------------------------- #
# 4. 发送流程
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_handle_link_sends_text_and_image(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
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
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download", fake_download)
    monkeypatch.setattr(
        media_mod,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=1024),
    )

    bot = FakeBot()
    await service.handle(
        instance,
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
    )

    apis = [api for api, _ in bot.calls]
    assert apis.count("send_msg") == 2, apis  # 文字 + 图片
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert plugin.feedback.Reaction.START in emojis, emojis  # 开始时回应
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
    # 默认上传模式是 file：同机部署直接给协议端本机路径
    assert image_call[0].data["file"].startswith("file:///")
    assert image_call[0].data["file"].endswith("/pic.jpg")


@pytest.mark.asyncio
async def test_handle_link_sends_card_before_media(
    plugin, monkeypatch, tmp_media, card, instance, service, media_mod
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
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download", fake_download)
    monkeypatch.setattr(
        media_mod,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=1024),
    )
    monkeypatch.setattr(
        card.cover, "fetch_cover", lambda result, files, proxy="": b"cover"
    )
    monkeypatch.setattr(card.layout, "render", fake_render)

    bot = FakeBot()
    await service.handle(
        instance,
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
    )

    sent = [kw["message"] for api, kw in bot.calls if api == "send_msg"]
    assert len(sent) == 2, sent  # 卡片 + 媒体，没有第三条文字
    assert "card.jpg" in sent[0][0].data["file"]
    assert "pic.jpg" in sent[1][0].data["file"]
    assert bot.sent_texts == ["", ""], "出了卡就不该再发文字"


@pytest.mark.asyncio
async def test_same_video_other_url_form_is_skipped(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
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
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download", fake_download)
    monkeypatch.setattr(
        media_mod,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=1024),
    )

    # 这个用例只测去重，关掉媒体缓存免得缓存先命中
    monkeypatch.setattr(
        instance,
        "cache",
        plugin.cache.MediaCache(
            tmp_media / "nocache", ttl_seconds=0, max_bytes=0
        ),
    )

    short = "https://b23.tv/3XdwepO"
    full = "https://www.bilibili.com/video/BV1yupv6YEuE"

    first = FakeBot()
    await service.handle(instance, first, make_group_event(short), short)
    assert [api for api, _ in first.calls].count("send_msg") > 0

    second = FakeBot()
    await service.handle(instance, second, make_group_event(full), full)
    assert [api for api, _ in second.calls].count("send_msg") == 0, (
        "同一个作品的另一种写法不该再发一次"
    )
    # 去重在「收到」之前就拦下了：同会话同作品重复出现是静默的
    assert not [
        kw["emoji_id"]
        for api, kw in second.calls
        if api == "set_msg_emoji_like"
    ]

    third = FakeBot()
    await service.handle(
        instance, third, make_group_event(full, group_id=654321), full
    )
    assert [api for api, _ in third.calls].count("send_msg") > 0, (
        "换个会话就该照常解析"
    )


def _patch_cached_parsehub(
    instance, monkeypatch, tmp_media, calls, raw_url, media_mod
):
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
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download", fake_download)
    monkeypatch.setattr(
        media_mod,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=1024),
    )
    return img


@pytest.mark.asyncio
async def test_cache_hit_still_respects_dedupe(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
):
    """命中缓存也要遵守去重：同一个群不能靠缓存把同一个作品反复发。"""
    calls = {"parse": 0, "download": 0}
    _patch_cached_parsehub(
        instance,
        monkeypatch,
        tmp_media,
        calls,
        "https://www.bilibili.com/video/BV1yupv6YEuE/",
        media_mod=media_mod,
    )
    url = "https://b23.tv/3XdwepO"

    first = FakeBot()
    await service.handle(instance, first, make_group_event(url), url)
    assert calls == {"parse": 1, "download": 1}
    assert [api for api, _ in first.calls].count("send_msg") > 0

    # 同一个群再来一次：去重拦住，什么都不发（以前会被缓存重新发一遍）
    second = FakeBot()
    await service.handle(instance, second, make_group_event(url), url)
    assert calls == {"parse": 1, "download": 1}
    assert [api for api, _ in second.calls].count("send_msg") == 0

    # 换个群：命中缓存直接发，不解析也不下载
    third = FakeBot()
    await service.handle(
        instance, third, make_group_event(url, group_id=654321), url
    )
    assert calls == {"parse": 1, "download": 1}, "缓存命中不该再解析 / 下载"
    assert [api for api, _ in third.calls].count("send_msg") > 0


@pytest.mark.asyncio
async def test_same_work_other_url_form_in_same_session_is_silent(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
):
    """用户复现过的那对链接：先短链、再完整链，同一个群只发一次。"""
    calls = {"parse": 0, "download": 0}
    _patch_cached_parsehub(
        instance,
        monkeypatch,
        tmp_media,
        calls,
        "https://www.douyin.com/video/7694556586975504313",
        media_mod=media_mod,
    )
    short = "https://v.douyin.com/mSJ4_DyZ4go/"
    full = "https://www.iesdouyin.com/share/video/7694556586975504313/"

    first = FakeBot()
    await service.handle(instance, first, make_group_event(short), short)
    assert [api for api, _ in first.calls].count("send_msg") > 0

    second = FakeBot()
    await service.handle(instance, second, make_group_event(full), full)
    assert calls == {"parse": 1, "download": 1}, "第二次不该再解析 / 下载"
    assert [api for api, _ in second.calls].count("send_msg") == 0, (
        "同一个作品换个写法再发，同群应该静默"
    )


@pytest.mark.asyncio
async def test_other_short_link_skips_download_via_cache(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
):
    """另一个短链指向同一作品：短链里没 ID 所以还得解析，但不再下载。"""
    calls = {"parse": 0, "download": 0}
    _patch_cached_parsehub(
        instance,
        monkeypatch,
        tmp_media,
        calls,
        "https://www.bilibili.com/video/BV1yupv6YEuE/",
        media_mod=media_mod,
    )

    first_url = "https://b23.tv/3XdwepO"
    await service.handle(
        instance, FakeBot(), make_group_event(first_url), first_url
    )
    assert calls == {"parse": 1, "download": 1}

    # 换个群：同群 120 秒内会被去重拦掉，那样就测不到缓存这一层了
    other_url = "https://b23.tv/ZzZzZzZ"
    second = FakeBot()
    await service.handle(
        instance,
        second,
        make_group_event(other_url, group_id=654321),
        other_url,
    )
    assert calls == {"parse": 2, "download": 1}, "只该多一次解析，不该再下载"
    assert [api for api, _ in second.calls].count("send_msg") > 0


@pytest.mark.asyncio
async def test_cache_off_still_parses_and_downloads(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
):
    """关掉缓存后行为跟以前一样。"""
    calls = {"parse": 0, "download": 0}
    _patch_cached_parsehub(
        instance,
        monkeypatch,
        tmp_media,
        calls,
        "https://www.bilibili.com/video/BV1yupv6YEuE/",
        media_mod=media_mod,
    )
    monkeypatch.setattr(
        instance,
        "cache",
        plugin.cache.MediaCache(
            tmp_media / "media", ttl_seconds=0, max_bytes=0
        ),
    )
    url = "https://b23.tv/3XdwepO"
    await service.handle(instance, FakeBot(), make_group_event(url), url)
    await service.handle(
        instance, FakeBot(), make_group_event(url, group_id=654321), url
    )
    assert calls == {"parse": 2, "download": 2}


def test_short_error_strips_nested_prefixes(plugin, errors):
    """ParseHub 的错误信息套好几层，发给群之前要压平。"""
    exc = RuntimeError("下载错误: 下载失败: 下载错误: HTTP错误: 403")
    assert errors.short_error(exc) == "HTTP错误: 403"
    assert errors.short_error(RuntimeError("")) == "RuntimeError"


def test_short_error_hides_local_paths(plugin, errors):
    """PIL 的报错带缓存绝对路径，不能原样发到群里。"""
    exc = RuntimeError(
        "cannot identify image file '/home/macy/.cache/parsehub/x.gif'"
    )
    assert errors.short_error(exc) == "下载到的文件不是有效图片"

    generic = RuntimeError("打开失败: '/home/macy/.cache/parsehub/x.gif'")
    assert "/home/macy" not in errors.short_error(generic)


def test_short_error_flattens_fallback_error(plugin, errors):
    """2.3.10 的「所有解析方式均失败」是多行，QQ 里只能发一句话。"""
    exc = RuntimeError(
        "所有解析方式均失败:\n- 网页解析: HTTP错误: 403\n- 移动端: 作品不可用"
    )
    assert (
        errors.short_error(exc)
        == "所有解析方式均失败（网页解析: HTTP错误: 403）"
    )
    # 超长的也要截断，别在气泡里刷一屏
    long = RuntimeError("解析失败：" + "很长的原因" * 40)
    assert len(errors.short_error(long)) == errors.MAX_ERROR_CHARS


def test_content_unavailable_covers_fallback_variant(plugin, errors, feedback):
    """新的 ContentUnavailableFallbackError 也要被认成「作品不可用」。"""
    from parsehub.errors import ContentUnavailableFallbackError

    exc = ContentUnavailableFallbackError([])
    assert errors.translate(exc).kind is errors.ErrorKind.UNAVAILABLE
    assert (
        feedback.failure_notice(errors.translate(exc))
        == "这个作品已经不可用了（可能被删了）。"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://g.us.sinaimg.cn/o0/x.mp4?label=gif_mp4", "mp4"),
        ("https://wx1.sinaimg.cn/x.gif", "gif"),
        ("https://example.test/no-extension", "gif"),
    ],
)
def test_normalise_animated_ext(parsehub_client, url: str, expected: str):
    """只按 URL 后缀纠正 GIF/MP4 错配，没后缀的不猜。"""
    ref = FakeMediaRef(url, "gif")
    parsehub_client._normalise_animated_ext(SimpleNamespace(media=[ref]))
    assert ref.ext == expected


@pytest.mark.asyncio
async def test_download_uses_concurrency(
    tmp_media, monkeypatch, parsehub_client
):
    """下载要带上并发数，否则又退回逐张串行。"""
    seen: dict[str, object] = {}
    parsed: dict[str, object] = {}

    class FakeResult:
        media = [
            FakeMediaRef("http://g.us.sinaimg.cn/o0/x.mp4?label=gif_mp4", "gif")
        ]

        def download_sync(self, path, **kwargs):
            seen.update(kwargs)
            seen["ext"] = self.media[0].ext
            return "parsed-ok"

    class FakeHub:
        def parse_sync(self, url, **kwargs):
            parsed.update(kwargs)
            return FakeResult()

    # conftest 会把 CLIENT.download_parsed 换成一个委托，这里要考真实现，
    # 所以直接调类上的方法。
    client = parsehub_client.ParseHubClient()
    monkeypatch.setattr(
        client, "hub", lambda: asyncio.sleep(0, result=FakeHub())
    )

    assert (
        await parsehub_client.ParseHubClient.download_parsed(
            client, FakeResult(), str(tmp_media)
        )
        == "parsed-ok"
    )
    assert seen["concurrency"] == parsehub_client.DOWNLOAD_CONCURRENCY > 1
    assert seen["ext"] == "mp4", "微博 GIF 实际是 mp4，下载前要改后缀"

    seen.clear()
    assert await parsehub_client.ParseHubClient.download(
        client, "u", str(tmp_media)
    ) == ("parsed-ok")
    assert parsed["proxy"] is None
    assert seen["concurrency"] == parsehub_client.DOWNLOAD_CONCURRENCY
    assert seen["ext"] == "mp4"


def _patch_failing_download(
    instance, monkeypatch, tmp_media, calls, failures, media_mod
):
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
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download", fake_download)
    monkeypatch.setattr(
        media_mod,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=1024),
    )


@pytest.mark.asyncio
async def test_download_retries_transient_cdn_error(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
):
    """CDN 偶发 403：重试一次就成功，媒体照样发出去。"""
    calls = {"download": 0}
    _patch_failing_download(
        instance, monkeypatch, tmp_media, calls, failures=1, media_mod=media_mod
    )
    monkeypatch.setattr(plugin.service.download, "DOWNLOAD_RETRY_DELAY", 0)

    bot = FakeBot()
    url = "https://b23.tv/abcd"
    await service.handle(instance, bot, make_group_event(url), url)

    assert calls["download"] == 2, "第一次 403 之后应该重试"
    assert [api for api, _ in bot.calls].count("send_msg") > 0
    assert not any("403" in text for text in bot.sent_texts), bot.sent_texts


@pytest.mark.asyncio
async def test_download_failure_after_retries_reports_short_error(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
):
    """一直失败：重试到上限后只发一条压平过的错误。"""
    calls = {"download": 0}
    _patch_failing_download(
        instance,
        monkeypatch,
        tmp_media,
        calls,
        failures=99,
        media_mod=media_mod,
    )
    monkeypatch.setattr(plugin.service.download, "DOWNLOAD_RETRY_DELAY", 0)

    bot = FakeBot()
    url = "https://b23.tv/abcd"
    await service.handle(instance, bot, make_group_event(url), url)

    assert calls["download"] == service.download.DOWNLOAD_ATTEMPTS
    texts = [text for text in bot.sent_texts if text]
    assert texts == ["下载失败：HTTP错误: 403"], texts
    # 下载失败也要给终点状态：先点「收到」，失败时补失败表情
    assert plugin.feedback.Reaction.FAIL in _emojis(bot), bot.calls


@pytest.mark.asyncio
async def test_handle_link_live_photo_sends_image_and_video(
    plugin, monkeypatch, tmp_media, instance, service
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
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download", fake_download)

    bot = FakeBot()
    await service.handle(
        instance,
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
    )

    apis = [api for api, _ in bot.calls]
    # 两个媒体 → 合并转发，节点里一个是图片一个是视频
    assert "send_group_forward_msg" in apis, apis
    fwd = dict(bot.calls)["send_group_forward_msg"]
    kinds = [n["data"]["content"][0]["type"] for n in fwd["messages"]]
    assert kinds == ["image", "video"], kinds


@pytest.mark.asyncio
async def test_handle_link_multi_media_uses_forward(
    plugin, monkeypatch, tmp_media, delivery, instance, service
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
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download", fake_download)

    bot = FakeBot()
    await service.handle(
        instance,
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
    )

    apis = [api for api, _ in bot.calls]
    assert "send_group_forward_msg" in apis, apis
    assert apis.count("send_msg") == 1, apis  # 只有标题那条文字，图片不单独发
    fwd = dict(bot.calls)["send_group_forward_msg"]
    assert fwd["group_id"] == 123456
    # 协议端抓一遍 URL 再上传很慢，必须显式给一个宽限，否则 30s 就被掐断
    assert fwd["_timeout"] == instance.settings.forward_timeout
    assert len(fwd["messages"]) == 3
    # 节点名跟随 NICKNAME（测试环境没配，退回默认值）
    assert all(
        n["data"]["name"] == delivery.forward_node_name()
        for n in fwd["messages"]
    )
    assert all(
        n["type"] == "node" and n["data"]["content"][0]["type"] == "image"
        for n in fwd["messages"]
    )
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert plugin.feedback.Reaction.OK in emojis, emojis


@pytest.mark.asyncio
async def test_handle_link_multi_media_forward_failure_falls_back(
    plugin, monkeypatch, tmp_media, instance, service
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
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download", fake_download)

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
    await service.handle(
        instance,
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
    )

    apis = [api for api, _ in bot.calls]
    assert "send_group_forward_msg" in apis
    assert apis.count("send_msg") == 3, apis  # 标题 + 两张图逐条兜底


@pytest.mark.asyncio
async def test_handle_link_single_media_no_forward(
    plugin, monkeypatch, tmp_media, instance, service
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
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download", fake_download)

    bot = FakeBot()
    await service.handle(
        instance,
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
    )

    apis = [api for api, _ in bot.calls]
    assert "send_group_forward_msg" not in apis, apis  # 单个媒体不合并转发
    assert apis.count("send_msg") == 2, apis


@pytest.mark.asyncio
async def test_handle_link_video_failure_falls_back_to_file(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
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
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download", fake_download)
    monkeypatch.setattr(
        media_mod,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=2048),
    )

    bot = FakeBot(fail_video=True)  # video 消息段被协议端拒绝
    result = await service.handle(
        instance,
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
    )

    apis = [api for api, _ in bot.calls]
    assert "upload_group_file" in apis, "video 段被拒时应回退群文件"
    assert not any("视频发送失败" in t for t in bot.sent_texts), bot.sent_texts
    assert result.outcome is service.Outcome.DELIVERED
    assert result.report is not None and result.report.ok
    emojis = [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]
    assert plugin.feedback.Reaction.OK in emojis, emojis


@pytest.mark.asyncio
async def test_handle_link_skips_oversized_media(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
):
    video = tmp_media / "big.mp4"
    video.write_bytes(b"x" * 1024)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/big.mp4")
        )

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(
        media_mod,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(
            0, result=500 * 1024 * 1024
        ),
    )

    bot = FakeBot()
    await service.handle(
        instance,
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
    )

    assert any("超过上限" in t for t in bot.sent_texts), bot.sent_texts
    assert "upload_group_file" not in [api for api, _ in bot.calls]
    # 全被挡掉也要给终点状态：失败表情
    assert plugin.feedback.Reaction.FAIL in _emojis(bot), bot.calls


@pytest.mark.asyncio
async def test_handle_link_text_only_result(
    plugin, monkeypatch, tmp_media, instance, service
):
    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(media=None, content="一篇长文")

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)

    bot = FakeBot()
    await service.handle(
        instance,
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
    )
    assert [api for api, _ in bot.calls].count(
        "send_msg"
    ) == 1  # 只有文字，没有媒体
    assert "一篇长文" in bot.sent_texts[0]
    assert plugin.feedback.Reaction.OK in _emojis(bot), (
        bot.calls
    )  # 纯文字帖也算处理成功


@pytest.mark.asyncio
async def test_handle_link_unsupported_manual(
    plugin, monkeypatch, instance, service
):
    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=False),
    )
    bot = FakeBot()
    await service.handle(
        instance,
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
        manual=True,
    )
    assert "不支持" in bot.sent_texts[0]


@pytest.mark.asyncio
async def test_handle_link_parse_error_manual(
    plugin, monkeypatch, instance, service
):
    class Boom(Exception):
        pass

    async def fake_parse(url, proxy="", cookie=""):
        raise Boom("解析炸了")

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)

    bot = FakeBot()
    await service.handle(
        instance,
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
    assert plugin.feedback.Reaction.START in emojis, emojis


# --------------------------------------------------------------------------- #
# 5. 媒体中转服务
# --------------------------------------------------------------------------- #
def test_media_server_serves_file_and_hides_listing(
    plugin, tmp_media, monkeypatch, instance
):
    import urllib.error
    import urllib.request

    # 插件的启动钩子可能已经起过服务，这里强制重新绑定测试端口
    monkeypatch.setattr(instance.relay, "_server", None)

    sub = tmp_media / "abc123"
    sub.mkdir()
    (sub / "a.mp4").write_bytes(b"hello")

    port = instance.relay.start("127.0.0.1", 18899)
    assert port == 18899

    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/abc123/a.mp4", timeout=5
    ) as resp:
        assert resp.read() == b"hello"

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/abc123/", timeout=5)
    assert exc.value.code == 404


def test_public_url_mapping(plugin, tmp_media, instance):
    sub = tmp_media / "deadbeef"
    sub.mkdir()
    f = sub / "x.jpg"
    f.write_bytes(b"1")
    assert (
        instance.relay.url_for(f, "192.168.1.5", 8899)
        == "http://192.168.1.5:8899/deadbeef/x.jpg"
    )


def test_public_url_mapping_encodes_special_names(plugin, tmp_media, instance):
    """标题里的空格 / # / 中文必须做百分号编码，否则协议端取不到。"""
    from urllib.parse import quote

    sub = tmp_media / "deadbeef"
    sub.mkdir()
    f = sub / "2024 歌单#1.m4a"
    f.write_bytes(b"1")
    expected = "http://192.168.1.5:8899/" + quote(
        "deadbeef/2024 歌单#1.m4a", safe="/"
    )
    assert instance.relay.url_for(f, "192.168.1.5", 8899) == expected


def test_cleanup_removes_old_dirs(plugin, tmp_media, instance):
    old = tmp_media / "old"
    old.mkdir()
    (old / "a.txt").write_bytes(b"1")
    import os
    import time

    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert instance.relay.cleanup(60) == 1
    assert not old.exists()


def test_sweep_tmp_removes_stale_cookie_files(tmp_path):
    """崩溃残留的旧 cookie 文件会被清掉，新的保留。"""
    from nonebot_plugin_parsehub.app import _sweep_tmp

    old = tmp_path / "parsehub-yt-old.cookies"
    new = tmp_path / "parsehub-yt-new.cookies"
    old.write_text("secret", encoding="utf-8")
    new.write_text("secret", encoding="utf-8")

    import os
    import time

    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert _sweep_tmp(tmp_path) == 1
    assert not old.exists()
    assert new.exists()


@pytest.mark.asyncio
async def test_send_swallows_protocol_failure(plugin, monkeypatch, instance):
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
    await instance.delivery.send_text(bot, make_group_event("test"), "hi")


def _emojis(bot: FakeBot) -> list[str]:
    """取这次调用里点过的表情 ID。"""
    return [
        kw["emoji_id"] for api, kw in bot.calls if api == "set_msg_emoji_like"
    ]


@pytest.mark.asyncio
async def test_warmup_preloads_parsehub(plugin, monkeypatch, instance):
    """启动预热：后台调用 hub，且失败也不抛异常（不阻塞启动）。"""
    called = {"hub": 0}

    async def fake_hub():
        called["hub"] += 1
        return object()

    monkeypatch.setattr(instance.client, "hub", fake_hub)
    await instance._warm()
    await asyncio.sleep(0.05)  # 让 create_task 跑完
    assert called["hub"] == 1

    async def boom():
        raise RuntimeError("预热炸了")

    monkeypatch.setattr(instance.client, "hub", boom)
    await instance._warm()  # 不该抛出去
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_parse_failure_reacts_fail_without_text(
    plugin, monkeypatch, instance, service
):
    """解析失败（非「作品不可用」）：点失败表情就够，不刷屏。"""

    class Boom(Exception):
        pass

    async def fake_parse(url, proxy="", cookie=""):
        raise Boom("解析炸了")

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)

    bot = FakeBot()
    await service.handle(
        instance,
        bot,
        make_group_event("https://b23.tv/abcd"),
        "https://b23.tv/abcd",
    )

    assert plugin.feedback.Reaction.FAIL in _emojis(bot), bot.calls
    assert [api for api, _ in bot.calls if api == "send_msg"] == [], (
        bot.sent_texts
    )


@pytest.mark.asyncio
async def test_content_unavailable_gets_notice(
    plugin, monkeypatch, instance, service
):
    """作品被删 / 下架：失败表情 + 一句人话（自动解析也要说）。"""
    from parsehub.errors import ContentUnavailableError

    async def fake_parse(url, proxy="", cookie=""):
        raise ContentUnavailableError("作品不可用: 抖音 status=0")

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)

    bot = FakeBot()
    url = "https://v.douyin.com/kE-zqatAP7M/"
    await service.handle(instance, bot, make_group_event(url), url)

    assert plugin.feedback.Reaction.FAIL in _emojis(bot), bot.calls
    assert any("不可用" in text for text in bot.sent_texts), bot.sent_texts


def _wire_download_paths(
    plugin,
    monkeypatch,
    tmp_media,
    instance,
    *,
    parsed_fails: bool = False,
    media_mod,
) -> dict:
    """装好解析 + 两条下载路径，返回调用计数器。"""
    img = tmp_media / "pic.jpg"
    img.write_bytes(b"x" * 1024)
    calls = {"parsed": 0, "by_url": 0}

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/pic.jpg", "jpg")
        )

    async def fake_parsed(result, path, proxy=""):
        calls["parsed"] += 1
        if parsed_fails:
            raise RuntimeError("下载错误: HTTP错误: 403")
        return FakeDownloadResult([img])

    async def fake_by_url(url, path, proxy="", cookie=""):
        calls["by_url"] += 1
        return FakeDownloadResult([img])

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download_parsed", fake_parsed)
    monkeypatch.setattr(instance.client, "download", fake_by_url)
    monkeypatch.setattr(
        media_mod,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=1024),
    )
    monkeypatch.setattr(plugin.service.download, "DOWNLOAD_RETRY_DELAY", 0)
    return calls


@pytest.mark.asyncio
async def test_first_download_uses_parsed_result(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
):
    """首次下载走已经解析好的结果，不让库再解析一遍。"""
    calls = _wire_download_paths(
        plugin, monkeypatch, tmp_media, instance, media_mod=media_mod
    )
    bot = FakeBot()
    url = "https://b23.tv/abcd"
    await service.handle(instance, bot, make_group_event(url), url)

    assert calls == {"parsed": 1, "by_url": 0}, calls
    assert any(
        api == "send_msg" and kw["message"][0].type == "image"
        for api, kw in bot.calls
    ), bot.calls


@pytest.mark.asyncio
async def test_download_retry_reparses_with_url(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
):
    """重试必须重新解析拿新签名，所以回到 download(url)。"""
    calls = _wire_download_paths(
        plugin,
        monkeypatch,
        tmp_media,
        instance,
        parsed_fails=True,
        media_mod=media_mod,
    )
    bot = FakeBot()
    url = "https://b23.tv/abcd"
    await service.handle(instance, bot, make_group_event(url), url)

    assert calls == {"parsed": 1, "by_url": 1}, calls
    assert any(
        api == "send_msg" and kw["message"][0].type == "image"
        for api, kw in bot.calls
    ), bot.calls


@pytest.mark.asyncio
async def test_card_and_download_run_in_parallel(
    plugin, monkeypatch, tmp_media, instance, service, media_mod
):
    """卡片和下载并行：两边互相等对方开始，串行的话必然超时。"""
    card_started = asyncio.Event()
    download_started = asyncio.Event()
    img = tmp_media / "pic.jpg"
    img.write_bytes(b"x" * 1024)
    card_path = tmp_media / "card.jpg"
    card_path.write_bytes(b"c" * 512)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/pic.jpg", "jpg")
        )

    async def fake_render(result, files, task_dir, cfg, proxy=""):
        card_started.set()
        await asyncio.wait_for(download_started.wait(), 1)
        return card_path

    async def fake_parsed(result, path, proxy=""):
        download_started.set()
        await asyncio.wait_for(card_started.wait(), 1)
        return FakeDownloadResult([img])

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download_parsed", fake_parsed)
    monkeypatch.setattr(instance.cards, "render", fake_render)
    monkeypatch.setattr(
        media_mod,
        "probe_size",
        lambda url, timeout=15, proxy="": asyncio.sleep(0, result=1024),
    )

    bot = FakeBot()
    url = "https://b23.tv/abcd"
    await service.handle(instance, bot, make_group_event(url), url)

    sends = [str(kw["message"]) for api, kw in bot.calls if api == "send_msg"]
    assert any("card.jpg" in text for text in sends), sends
    assert any("pic.jpg" in text for text in sends), sends


def _card_event(payload: str):
    """造一条只含 json 卡片段的消息（QQ 小程序分享就是这个形态）。"""

    event = make_group_event("")
    event.message = Message(
        [MessageSegment(type="json", data={"data": payload})]
    )
    return event


# QQ 小程序卡片：链接在 meta.detail_1.qqdocurl 里，且 JSON 把斜杠转义了
BILI_CARD = (
    '{"app":"com.tencent.miniapp_01","prompt":"[QQ小程序]【测试】视频",'
    '"meta":{"detail_1":{"appid":"1109937557","title":"测试视频",'
    '"qqdocurl":"https:\\/\\/b23.tv\\/abcd",'
    '"url":"m.q.qq.com\\/a\\/s?appid=1109937557"}}}'
)
QQ_ONLY_CARD = '{"url":"https:\\/\\/m.q.qq.com\\/a\\/s?appid=1109937557"}'


def test_link_sources_reads_miniprogram_card(plugin, triggers):
    """json 卡片里的链接要能取出来（JSON 把斜杠转义了）。"""
    event = _card_event(BILI_CARD)
    assert plugin.keys.extract_urls(triggers.link_sources(event)) == [
        "https://b23.tv/abcd"
    ]


def test_link_sources_skips_unrelated_card(plugin, triggers):
    """卡片里 QQ 自己的链接不该被当成解析目标。"""
    event = _card_event(QQ_ONLY_CARD)
    assert plugin.keys.extract_urls(triggers.link_sources(event)) == []


@pytest.mark.asyncio
async def test_cover_fetch_uses_platform_proxy(
    plugin,
    monkeypatch,
    tmp_media,
    card,
    instance,
    service,
    media_mod,
    update_settings,
):
    """海外平台的封面也要走代理：直连会超时，卡片就被跳过了。"""
    seen = {}

    def fake_fetch_cover(result, files, proxy=""):
        seen["proxy"] = proxy
        return None

    img = tmp_media / "pic.jpg"
    img.write_bytes(b"x" * 1024)

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            media=FakeMediaRef("https://cdn.example.com/pic.jpg", "jpg")
        )

    async def fake_download(result, path, proxy=""):
        return FakeDownloadResult([img])

    update_settings(proxy="http://proxy.test:1", proxy_platforms=frozenset())
    monkeypatch.setattr(card.cover, "fetch_cover", fake_fetch_cover)
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
    url = "https://b23.tv/abcd"
    await service.handle(instance, bot, make_group_event(url), url)

    assert seen.get("proxy") == "http://proxy.test:1", seen


@pytest.mark.asyncio
async def test_auto_rule_accepts_miniprogram_card(plugin, instance, triggers):
    """只有 json 卡片、没有文字的消息也要能触发自动解析。"""
    assert await triggers.should_auto(instance, _card_event(BILI_CARD)) is True
    assert (
        await triggers.should_auto(instance, _card_event(QQ_ONLY_CARD)) is False
    )


@pytest.mark.asyncio
async def test_handle_link_caps_media_items(
    plugin, monkeypatch, tmp_media, instance, media_mod, service
):
    """媒体超过上限时只下载并发送前 20 个，并回一句提示。"""
    total = media_mod.MAX_MEDIA_ITEMS + 10
    pics = []
    for i in range(total):
        p = tmp_media / f"p{i}.jpg"
        p.write_bytes(b"x" * 512)
        pics.append(p)

    result = FakeParseResult(
        media=[
            FakeMediaRef(f"https://cdn.example.com/p{i}.jpg", "jpg")
            for i in range(total)
        ]
    )
    seen: dict[str, int] = {}

    async def fake_parse(url, proxy="", cookie=""):
        return result

    async def fake_download(res, path, proxy=""):
        # 下载拿到的 result 必须已经截断过，否则 30 张图会全被拉下来
        seen["media"] = len(res.media)
        return FakeDownloadResult(pics[: len(res.media)])

    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance.client, "parse", fake_parse)
    monkeypatch.setattr(instance.client, "download_parsed", fake_download)

    bot = FakeBot()
    url = "https://v.douyin.com/abcd/"
    await service.handle(instance, bot, make_group_event(url), url)

    assert seen["media"] == media_mod.MAX_MEDIA_ITEMS, seen
    fwd = dict(bot.calls)["send_group_forward_msg"]
    assert len(fwd["messages"]) == media_mod.MAX_MEDIA_ITEMS
    texts = [
        str(kw.get("message", "")) for api, kw in bot.calls if api == "send_msg"
    ]
    assert any("只发前 20 项" in t for t in texts), texts


@pytest.mark.asyncio
async def test_send_media_files_caps_items(
    plugin, tmp_media, instance, media_mod
):
    """兜底：直接调用发送函数时也不会超过上限（缓存命中走的就是这条路）。"""
    files = []
    for i in range(media_mod.MAX_MEDIA_ITEMS + 5):
        p = tmp_media / f"m{i}.jpg"
        p.write_bytes(b"x" * 512)
        files.append(
            media_mod.MediaFile(path=p, kind=media_mod.MediaKind.IMAGE)
        )

    bot = FakeBot()
    await instance.delivery.send_media(bot, make_group_event("x"), files)

    fwd = dict(bot.calls)["send_group_forward_msg"]
    assert len(fwd["messages"]) == media_mod.MAX_MEDIA_ITEMS
