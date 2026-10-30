"""平台覆盖度回归测试。

起因：小红书新短链域名 ``xhslink.cn`` 不在白名单里，整条消息被静默丢掉；
另外 ParseHub 的 platform.id 是 ``xhs``，而卡片平台表里只有 ``xiaohongshu``，
即使解析成功也会用错图标。这类"上游多一个域名 / 换一个 id"的漂移必须被测试挡住。
"""

from __future__ import annotations

import importlib
import pkgutil

import parsehub.parsers.parser as parser_pkg
import pytest

# 上游支持、但白名单一度漏掉的短链域名（每条都对应一个 __match__ 模式）
SHORT_LINK_HOSTS = (
    "xhslink.cn",
    "bili2233.cn",
    "douc.cc",
    "fixupx.com",
    "chenzhongtech.com",
    "xiaochuankeji.cn",
    "weixin.qq.com",
)

# 每个平台取一条上游解析器认得的链接，用来核对本地 host→platform 表。
PLATFORM_SAMPLES = (
    "https://www.bilibili.com/video/BV1xx411c7mD",
    "https://b23.tv/abcd",
    "https://v.douyin.com/abc/",
    "https://www.iesdouyin.com/share/video/1",
    "https://www.douban.com/topic/123",
    "https://douc.cc/abc",
    "https://xhslink.com/a/abc",
    "https://www.coolapk.com/feed/1",
    "https://www.zhihu.com/question/1",
    "https://weibo.com/123/abc",
    "https://h5.pipix.com/s/abc",
    "https://s.izuiyou.com/s/abc",
    "https://share.xiaochuankeji.cn/hybrid/share/post?pid=1",
    "https://www.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=1",
    "https://twitter.com/a/status/1",
    "https://fixupx.com/a/status/1",
    "https://x.com/a/status/1",
    "https://www.youtube.com/watch?v=abc",
    "https://youtu.be/abcdefghijk",
    "https://www.instagram.com/p/abc/",
    "https://www.tiktok.com/@a/video/1",
    "https://www.threads.com/@a/post/1",
    "https://www.facebook.com/watch?v=1",
    "https://mp.weixin.qq.com/s/abc",
    "https://weixin.qq.com/sph/abc",
    "https://channels.weixin.qq.com/finder-preview/pages/sph?id=abc",
    "https://www.tieba.baidu.com/p/123",
    "https://www.toutiao.com/article/123",
    "https://www.kuaishou.com/short-video/1",
)


def upstream_platform_ids() -> set[str]:
    """枚举 ParseHub 里所有解析器的 platform.id。"""
    found: set[str] = set()
    for module_info in pkgutil.iter_modules(parser_pkg.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(
            f"{parser_pkg.__name__}.{module_info.name}"
        )
        for attribute in dir(module):
            value = getattr(module, attribute)
            if (
                isinstance(value, type)
                and hasattr(value, "__platform__")
                and hasattr(value, "__match__")
            ):
                platform_id = getattr(value.__platform__, "id", None)
                if platform_id:
                    found.add(platform_id)
    return found


@pytest.fixture(scope="session")
def upstream_hub():
    """本地 ParseHub 实例：parser 选择只做正则，不发请求。"""
    from parsehub import ParseHub

    return ParseHub()


def test_every_supported_platform_has_card_entry(platforms) -> None:
    """白名单里的每个平台都要能在卡片里查到，否则图标会退回默认的 X。"""
    missing = sorted(platforms.SUPPORTED_PLATFORMS - platforms.PLATFORMS.keys())
    assert not missing, f"卡片平台表缺少这些 id：{missing}"


def test_supported_platforms_exist_upstream(platforms) -> None:
    """白名单里的 id 必须仍存在于上游；上游改名 / 删除时要显式处理。"""
    found = upstream_platform_ids()
    assert found, "没有发现任何上游 parser，枚举逻辑失效"
    assert "bilibili" in found, f"上游平台枚举不完整：{sorted(found)}"
    missing = sorted(platforms.SUPPORTED_PLATFORMS - found)
    assert not missing, f"白名单里这些 id 上游已没有：{missing}"


def test_host_platforms_are_supported(keys, platforms) -> None:
    """``HOST_PLATFORMS`` 里的 id 必须都是插件支持的平台。"""
    unknown = sorted(
        set(keys.HOST_PLATFORMS.values()) - platforms.SUPPORTED_PLATFORMS
    )
    assert not unknown, f"host 表里有不支持的平台 id：{unknown}"


@pytest.mark.parametrize("url", PLATFORM_SAMPLES)
def test_platform_of_matches_upstream(url: str, upstream_hub, keys) -> None:
    """本地域名表必须和 ParseHub 的 parser 选择结果一致。"""
    upstream = upstream_hub.get_platform(url)
    assert upstream is not None, f"上游不认这条链接：{url}"
    assert keys.platform_of(url) == upstream.id, url


def test_every_platform_logo_exists(platforms, card) -> None:
    """平台表里引用的 logo 文件必须真实存在。"""
    assert platforms.PLATFORMS, "卡片平台表为空"
    missing = sorted(
        {
            logo
            for _, _, logo in platforms.PLATFORMS.values()
            if not (card.compose.ASSET_DIR / f"{logo}.png").exists()
        }
    )
    assert not missing, f"缺少 logo 文件：{missing}"


def test_platform_logo_is_loadable(platforms, card) -> None:
    """每个平台表里的 logo 都要能按尺寸画出来（防尺寸为 0 / 坏文件）。"""
    assert platforms.PLATFORMS, "卡片平台表为空"
    for platform_id, (_, _, logo) in platforms.PLATFORMS.items():
        image = card.compose.logo(platform_id, 54)
        assert image.width > 0 and image.height == 54, (
            f"{platform_id} → {logo} 画不出来"
        )


@pytest.mark.parametrize("host", SHORT_LINK_HOSTS)
def test_short_link_host_is_allowed(host: str, keys) -> None:
    """上游支持的短链域名必须能过预筛白名单。"""
    assert keys.host_allowed(host), f"{host} 不在白名单里"
    assert keys.host_allowed(f"www.{host}"), f"{host} 的子域名没被放行"


def test_extract_urls_keeps_xhslink_cn(keys) -> None:
    """小红书分享文案里的 xhslink.cn 链接要能被抓出来。"""
    text = "不负圣女之名 https://xhslink.cn/o/7mQiQjVqUtO \n【小红书】里有答案，快去围观~"
    assert keys.extract_urls(text) == ["https://xhslink.cn/o/7mQiQjVqUtO"]


def test_xhs_platform_maps_to_xiaohongshu_logo(card) -> None:
    """parsehub 的小红书 id 是 xhs，必须映射到小红书标和名字。"""

    class _Platform:
        id = "xhs"

    class _Result:
        platform = _Platform()

    name, _, logo = card.layout.platform_of(_Result())
    assert name == "小红书"
    assert logo == "xiaohongshu"


def test_xhs_duration_is_milliseconds(card, plugin) -> None:
    """小红书给的 duration 是毫秒：实测 7967 对应真视频 7.97 秒。"""
    assert "xhs" in plugin.platforms.MS_PLATFORMS
    assert card.layout.format_duration(7967, is_ms=True) == "00:07"
    assert card.layout.format_duration(7967) == "2:12:47"  # 不换算就会成这样
