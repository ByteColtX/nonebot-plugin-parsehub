# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""ParseHub 调用封装。

ParseHub 全程同步阻塞（内部还用了 requests），所以所有调用都通过
``asyncio.to_thread`` 执行，避免阻塞 NoneBot 的事件循环。首次 import 会拉起
numpy / opencv（数秒），因此实例是懒加载的，并由 ``App`` 在启动时预热。

登录态 cookie 优先读 ParseHub 自己的 ``cookies.toml``（由
``ph set cookie <平台>`` 写入），没有再退回配置里的兜底值。
"""

from __future__ import annotations

import asyncio
import tomllib
from typing import Any

# 多文件并发下载数（parsehub 2.3.10 起支持）。下载以网络等待为主，并发比串行快
# 很多；但内存只有 1G 上下的机器别开太大。
DOWNLOAD_CONCURRENCY = 8


def platform_cookie(platform: str, fallback: str) -> str:
    """取该平台要用的 cookie。

    Args:
        platform: ParseHub 的平台 id。
        fallback: 配置里提供的兜底 cookie。

    Returns:
        cookie 字符串；都没有时返回空字符串。
    """
    if platform:
        cookie = _read_cookie_file(platform)
        if cookie:
            return cookie
    return fallback or ""


def _read_cookie_file(platform: str) -> str:
    """从 ParseHub 的 cookies.toml 读取指定平台的 cookie。

    parsehub 的 import 代价很高，因此这里延迟导入，避免插件加载时就把它拉起来。

    Args:
        platform: ParseHub 的平台 id。

    Returns:
        文件里记录的 cookie；没读到返回空字符串。
    """
    try:
        from parsehub.cli_config import default_cookie_path

        path = default_cookie_path()
        if not path.exists():
            return ""
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, ValueError):
        return ""
    value = (data.get("cookie") or {}).get(platform)
    return str(value) if value else ""


class ParseHubClient:
    """ParseHub 的懒加载封装。

    首次调用 ``hub`` 时会在线程里导入 parsehub（numpy + opencv，数秒开销），
    之后复用同一个实例。

    Attributes:
        _hub: 缓存的 ParseHub 实例。
        _lock: 保护懒加载过程的锁。
    """

    def __init__(self) -> None:
        """初始化空的客户端。"""
        self._hub: Any | None = None
        self._lock = asyncio.Lock()

    async def hub(self) -> Any:
        """返回 ParseHub 实例，必要时先加载。

        Returns:
            已就绪的 ParseHub 实例。
        """
        if self._hub is None:
            async with self._lock:
                if self._hub is None:
                    self._hub = await asyncio.to_thread(_import_parsehub)
        return self._hub

    async def platforms(self) -> list[dict]:
        """列出 ParseHub 支持的平台。

        Returns:
            平台信息字典的列表，每项含 ``id`` / ``name`` / ``supported_types``。
        """
        hub = await self.hub()
        return await asyncio.to_thread(hub.get_platforms)

    async def is_supported(self, url: str) -> bool:
        """判断链接是否有对应的解析器。

        Args:
            url: 待检查的链接。

        Returns:
            有解析器返回 True。
        """
        hub = await self.hub()
        return await asyncio.to_thread(lambda: hub.get_parser(url) is not None)

    async def platform_id(self, url: str) -> str | None:
        """返回链接所属的平台 id。

        Args:
            url: 待检查的链接。

        Returns:
            平台 id，例如 ``bilibili``；无对应平台时返回 None。
        """
        hub = await self.hub()
        platform = await asyncio.to_thread(hub.get_platform, url)
        return platform.id if platform else None

    async def parse(
        self,
        url: str,
        proxy: str = "",
        cookie: str = "",
    ) -> Any:
        """解析链接或分享文案。

        Args:
            url: 链接或分享文案。
            proxy: 解析代理，空字符串表示直连。
            cookie: 登录态 cookie，空字符串表示不带。

        Returns:
            ParseHub 的解析结果对象。

        Raises:
            Exception: ParseHub 抛出的解析异常，由调用方决定如何提示用户。
        """
        hub = await self.hub()
        return await asyncio.to_thread(
            hub.parse_sync,
            url,
            proxy=proxy or None,
            cookie=cookie or None,
        )

    async def download(
        self,
        url: str,
        path: str,
        proxy: str = "",
        cookie: str = "",
        concurrency: int = DOWNLOAD_CONCURRENCY,
    ) -> Any:
        """解析并下载媒体到指定目录。

        Args:
            url: 链接或分享文案。
            path: 下载目录。
            proxy: 解析与下载共用的代理。
            cookie: 登录态 cookie。
            concurrency: 多文件并发下载数。

        Returns:
            ParseHub 的下载结果对象，``media`` 里是本地文件。

        Raises:
            Exception: 下载失败时由 ParseHub 抛出的异常。
        """
        hub = await self.hub()
        return await asyncio.to_thread(
            hub.download_sync,
            url,
            path,
            proxy=proxy or None,
            parse_proxy=proxy or None,
            parse_cookie=cookie or None,
            concurrency=concurrency,
        )

    async def download_parsed(
        self,
        result: Any,
        path: str,
        proxy: str = "",
        concurrency: int = DOWNLOAD_CONCURRENCY,
    ) -> Any:
        """用已经解析好的结果下载媒体，不再解析一遍。

        ParseHub 的 ``download(url)`` 内部会重新解析一次（库源码：
        ``result = await self.parse(url, ...)`` 再 ``result.download(...)``）；
        手里已经有解析结果时走这里，省掉那次重复解析。

        注意：媒体直链的签名每次都变（实测同一链接解析两次，路径相同、签名不同），
        所以**重试**必须回到 ``download(url)`` 重新解析拿新签名。

        Args:
            result: ``parse()`` 返回的解析结果。
            path: 下载目录。
            proxy: 下载代理。
            concurrency: 多文件并发下载数。

        Returns:
            ParseHub 的下载结果对象，``media`` 里是本地文件。

        Raises:
            Exception: 下载失败时由 ParseHub 抛出的异常。
        """
        return await asyncio.to_thread(
            result.download_sync,
            path,
            proxy=proxy or None,
            concurrency=concurrency,
        )


def _import_parsehub() -> Any:
    """在线程里导入 parsehub 并构造实例。

    Returns:
        新的 ParseHub 实例。
    """
    from parsehub import ParseHub

    return ParseHub()
