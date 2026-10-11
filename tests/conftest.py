"""测试环境初始化：注册 OneBot v11 适配器，按 pyproject.toml 加载插件。"""

from __future__ import annotations

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
    """nonebug 初始化完成后：注册适配器 + 加载本插件。"""
    driver = nonebot.get_driver()
    driver.register_adapter(OnebotV11Adapter)
    nonebot.load_from_toml("pyproject.toml")


@pytest.fixture(scope="session")
def plugin():
    """被加载的插件模块对象（用于 monkeypatch 插件内的名字）。"""
    loaded = nonebot.get_plugin("nonebot_plugin_parsehub")
    assert loaded is not None, "插件加载失败"
    return loaded.module


@pytest.fixture(scope="session")
def core(plugin):
    """插件的 core 子模块（延迟导入，此时 nonebot 已初始化）。"""
    return plugin.core


@pytest.fixture(scope="session")
def server(plugin):
    """插件的 server 子模块。"""
    return plugin.server


@pytest.fixture(scope="session")
def card(plugin):
    """插件的 card 子模块（必须在 nonebot 初始化之后才 import）。"""
    return plugin.card


@pytest.fixture(scope="session")
def switches(plugin):
    """插件的 switches 子模块（同样要等 nonebot 初始化）。"""
    return plugin.switches


@pytest.fixture(scope="session")
def mediacache(plugin):
    """插件的 mediacache 子模块。"""
    return plugin.mediacache


@pytest.fixture(autouse=True)
def _clean_media_cache(plugin, monkeypatch, tmp_path):
    """每个测试一份独立的媒体缓存，别写进仓库目录、也别互相命中。"""
    monkeypatch.setattr(
        plugin,
        "MEDIA_CACHE",
        plugin.mediacache.MediaCache(
            tmp_path / "media", ttl_seconds=24 * 3600, max_bytes=1024**3
        ),
    )


@pytest.fixture(autouse=True)
def _clean_dedupe(plugin, monkeypatch):
    """每个测试用一份干净的去重缓存，免得用例之间互相跳过。"""
    monkeypatch.setattr(plugin, "DEDUPE", plugin.core.DedupeCache())


@pytest.fixture(autouse=True)
def _clean_switches(plugin, monkeypatch, tmp_path):
    """每个测试用一份独立的解析开关，避免用例之间互相影响。"""
    monkeypatch.setattr(
        plugin,
        "SWITCHES",
        plugin.switches.SwitchStore(tmp_path / "sessions.json"),
    )


@pytest.fixture(autouse=True)
def _offline_card(card, monkeypatch):
    """卡片模块在测试里一律不联网。

    字体换成仓库里的子集，封面一律当作取不到（否则会真去请求封面 CDN）。
    需要真出卡的测试自己再 monkeypatch ``card.render`` / ``card.fetch_cover``。
    """
    card.set_font(TEST_FONT)
    monkeypatch.setattr(card, "fetch_cover", lambda *a, **kw: None)
