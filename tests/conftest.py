"""测试环境初始化：注册 OneBot v11 适配器，按 pyproject.toml 加载插件。"""

from __future__ import annotations

import os

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter
from pytest_asyncio import is_async_test

os.environ.setdefault("ENVIRONMENT", "test")


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
