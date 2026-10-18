# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""插件目录约定。

三类文件各有各的家，互不重叠：

- ``cache/media``：作品级媒体缓存（可重建、有 TTL）；
- ``cache/relay``：待投递的任务目录，中转服务**只**服务这一层；
- ``data``：需要长期保留的东西（会话开关）。

媒体目录按 NoneBot 官方要求交给 ``nonebot-plugin-localstore`` 管理；没装时退回
系统临时目录，保证插件仍能加载。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from nonebot import logger, require

# 缓存根目录下的子目录名。
MEDIA_DIR_NAME = "media"
RELAY_DIR_NAME = "relay"

# 没装 localstore 时的兜底目录名。
_FALLBACK_DIR_NAME = "nonebot-plugin-parsehub"


def _store() -> object | None:
    """尽量拿到 localstore 模块。

    Returns:
        ``nonebot_plugin_localstore`` 模块；没装时为 None。
    """
    try:
        require("nonebot_plugin_localstore")

        import nonebot_plugin_localstore as store
    except Exception:  # noqa: BLE001 - 缺少依赖时降级，不能让插件整体挂掉
        return None
    return store


def cache_root() -> Path:
    """插件缓存根目录（媒体缓存与字体都放这儿）。

    Returns:
        缓存根目录。
    """
    store = _store()
    if store is None:
        logger.warning(
            "[parsehub] 未安装 nonebot-plugin-localstore，"
            "媒体临时文件改用系统临时目录"
        )
        return Path(tempfile.gettempdir()) / _FALLBACK_DIR_NAME
    return store.get_plugin_cache_dir()


def data_root() -> Path:
    """插件数据目录（需要长期保留的东西放这儿）。

    Returns:
        数据目录。
    """
    store = _store()
    if store is None:
        logger.warning(
            "[parsehub] 未安装 nonebot-plugin-localstore，"
            "插件数据改用系统临时目录（重启会丢）"
        )
        return Path(tempfile.gettempdir()) / f"{_FALLBACK_DIR_NAME}-data"
    return store.get_plugin_data_dir()


def media_root() -> Path:
    """作品级媒体缓存目录。

    Returns:
        缓存根目录下的 ``media``。
    """
    return cache_root() / MEDIA_DIR_NAME


def relay_root() -> Path:
    """中转服务根目录（只放待投递的任务目录）。

    Returns:
        缓存根目录下的 ``relay``。
    """
    return cache_root() / RELAY_DIR_NAME
