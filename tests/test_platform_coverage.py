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
