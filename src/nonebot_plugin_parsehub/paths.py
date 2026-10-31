# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""插件目录约定。

四类文件各有各的家，互不重叠：

- ``cache/media``：作品级媒体缓存（可重建、有 TTL）；
- ``cache/relay``：待投递的任务目录，中转服务**只**服务这一层；
- ``cache/tmp``：一次性的临时文件（如 yt-dlp cookie），不参与媒体缓存，
  也不由中转服务提供；
- ``data``：需要长期保留的东西（会话开关）。

媒体与数据目录按 NoneBot 官方要求交给 ``nonebot-plugin-localstore`` 管理：它负责
Windows / macOS / Linux 上的正确路径，插件不自己拼目录、也不往系统临时目录里写。
"""

from __future__ import annotations

from pathlib import Path

from nonebot import require

# 官方写法：先 require 再 import，否则插件可能还没被加载。
require("nonebot_plugin_localstore")

import nonebot_plugin_localstore as store  # noqa: E402

# 缓存根目录下的子目录名。
MEDIA_DIR_NAME = "media"
RELAY_DIR_NAME = "relay"
TMP_DIR_NAME = "tmp"


def cache_root() -> Path:
    """插件缓存根目录（媒体缓存与字体都放这儿）。

    Returns:
        缓存根目录。
    """
    return store.get_plugin_cache_dir()


def data_root() -> Path:
    """插件数据目录（需要长期保留的东西放这儿）。

    Returns:
        数据目录。
    """
    return store.get_plugin_data_dir()


def relay_root(cache_dir: Path | None = None) -> Path:
    """中转服务根目录（只放待投递的任务目录）。

    Args:
        cache_dir: 缓存根目录；``None`` 时走 localstore 的默认。

    Returns:
        缓存根目录下的 ``relay``。
    """
    return (cache_dir or cache_root()) / RELAY_DIR_NAME


def tmp_root(cache_dir: Path | None = None) -> Path:
    """插件临时目录（一次性文件，不放系统临时目录）。

    Args:
        cache_dir: 缓存根目录；``None`` 时走 localstore 的默认。

    Returns:
        缓存根目录下的 ``tmp``。
    """
    return (cache_dir or cache_root()) / TMP_DIR_NAME
