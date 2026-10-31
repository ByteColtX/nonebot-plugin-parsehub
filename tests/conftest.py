"""测试环境初始化：注册 OneBot v11 适配器，按 pyproject.toml 加载插件。

每个测试拿到一个独立的 ``App``（临时缓存 / 中转 / 数据目录 + 假下载），不再
monkeypatch 模块全局。
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter
from pytest_asyncio import is_async_test

os.environ.setdefault("ENVIRONMENT", "test")

# 子集字体：测试绝不联网去下 18MB 的完整字体
TEST_FONT = Path(__file__).resolve().parent / "assets" / "test-font.ttf"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """让所有异步测试共用一个 session 级事件循环。"""
    for item in items:
        if is_async_test(item):
            item.add_marker(
                pytest.mark.asyncio(loop_scope="session"), append=False
            )


@pytest.fixture(scope="session", autouse=True)
async def after_nonebot_init(after_nonebot_init: None) -> None:  # noqa: PT004
    """nonebot 初始化完成后：注册适配器 + 加载本插件。"""
    driver = nonebot.get_driver()
    driver.register_adapter(OnebotV11Adapter)
    nonebot.load_from_toml("pyproject.toml")


@pytest.fixture(scope="session")
def plugin():
    """被加载的插件模块对象。"""
    loaded = nonebot.get_plugin("nonebot_plugin_parsehub")
    assert loaded is not None, "插件加载失败"
    return loaded.module


@pytest.fixture(scope="session")
def config_mod(plugin):
    """配置类。"""
    return plugin.Config


@pytest.fixture(scope="session")
def card(plugin):
    """卡片包。"""
    return plugin.card


@pytest.fixture(scope="session")
def cache(plugin):
    """缓存模块。"""
    return plugin.cache


@pytest.fixture(scope="session")
def service(plugin):
    """编排模块。"""
    return plugin.service


@pytest.fixture(scope="session")
def triggers(plugin):
    """触发层模块。"""
    return plugin.triggers


@pytest.fixture(scope="session")
def delivery(plugin):
    """投递模块。"""
    return plugin.delivery


@pytest.fixture(scope="session")
def feedback(plugin):
    """反馈模块。"""
    return plugin.feedback


@pytest.fixture(scope="session")
def keys(plugin):
    """链接识别模块。"""
    return plugin.keys


@pytest.fixture(scope="session")
def errors(plugin):
    """错误分类模块。"""
    return plugin.errors


@pytest.fixture(scope="session")
def media_mod(plugin):
    """媒体模块。"""
    return plugin.media


@pytest.fixture(scope="session")
def platforms(plugin):
    """平台元数据模块。"""
    return plugin.platforms


@pytest.fixture(scope="session")
def live_mod(plugin):
    """B 站直播间封面模块。"""
    return plugin.bilibili_live


@pytest.fixture(scope="session")
def parsehub_client(plugin):
    """ParseHub 客户端模块。"""
    return plugin.parsehub_client


@pytest.fixture(scope="session")
def switches(plugin):
    """会话开关模块。"""
    return plugin.switches


@pytest.fixture(scope="session")
def audio(plugin):
    """音频能力模块。"""
    return plugin.audio


@pytest.fixture()
def instance(plugin, tmp_path, monkeypatch):
    """每个测试一个独立实例：临时目录 + 假下载。"""
    settings = plugin.Settings.from_config(plugin.Config())
    instance = plugin.App.build(
        settings,
        cache_dir=tmp_path / "cache",
        relay_root=tmp_path / "relay",
        data_dir=tmp_path / "data",
    )

    async def delegate(result, path, proxy="", cookie=""):
        return await instance.client.download("<parsed>", path, proxy, cookie)

    # 测试用的 FakeParseResult 没有 download_sync；委托给 download 之后，
    # 只 patch CLIENT.download 的用例仍然覆盖首次下载这条路。
    monkeypatch.setattr(instance.client, "download_parsed", delegate)
    return instance


@pytest.fixture()
def update_settings(instance, monkeypatch):
    """改一项设置（Settings 是 frozen，整体替换）。"""

    def _update(**changes):
        settings = dataclasses.replace(instance.settings, **changes)
        monkeypatch.setattr(instance, "settings", settings)
        # Delivery 持有设置引用，一起换掉
        monkeypatch.setattr(instance.delivery, "settings", settings)

    return _update


@pytest.fixture()
def matchers(instance, triggers):
    """按当前实例注册一套 matcher（测试用）。"""
    return triggers.create_matchers(instance)


@pytest.fixture()
def tmp_media(tmp_path, monkeypatch, instance):
    """把中转服务的根目录指到临时目录。"""
    monkeypatch.setattr(instance.relay, "root", tmp_path)
    monkeypatch.setattr(instance.endpoint, "host", "127.0.0.1")
    monkeypatch.setattr(instance.endpoint, "port", 18899)
    return tmp_path


@pytest.fixture(autouse=True)
def _offline_card(card, monkeypatch):
    """卡片模块在测试里一律不联网。

    字体换成仓库里的子集，封面一律当作取不到（否则会真去请求封面 CDN）。
    需要真出卡的测试自己再 monkeypatch ``card.layout.render`` /
    ``card.cover.fetch_cover``。
    """
    card.fonts.set_font(TEST_FONT)
    monkeypatch.setattr(card.cover, "fetch_cover", lambda *a, **kw: None)
    # 测试绝不联网：emoji / 回退字体没有下载源，直接跳过
    monkeypatch.setattr(card.fonts, "EMOJI_FONT_SOURCES", ())
    monkeypatch.setattr(card.fonts, "FALLBACK_FONTS", ())
