# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""ParseHub 调用封装。

本模块承担三件事：

1. 用一份便宜的域名白名单从消息文本里预筛链接，命中了才值得把 parsehub
   拉起来（它的 import 要数秒）；
2. 懒加载 ParseHub 实例，并把所有同步阻塞的调用丢进线程执行，避免阻塞
   NoneBot 的事件循环；
3. 按平台决定是否走代理、用哪个 cookie。

模块级单例 ``CLIENT`` 是进程内共享的解析客户端，持有懒加载的 ParseHub
实例。

典型用法：

    urls = extract_urls(message_text)
    if urls:
        result = await CLIENT.parse(urls[0])
"""

from __future__ import annotations

import asyncio
import re
import time
import tomllib
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import plugin_config

# 预筛白名单：只认这些域名后缀，避免为无关链接付出 parsehub 的 import 代价。
_ALLOWED_HOSTS: tuple[str, ...] = (
    "b23.tv",
    "bangumi.tv",
    "bgm.tv",
    "bilibili.com",
    "channels.weixin.qq.com",
    "coolapk.com",
    "douban.com",
    "douyin.com",
    "facebook.com",
    "fb.watch",
    "iesdouyin.com",
    "instagram.com",
    "ixigua.com",
    "kuaishou.com",
    "medium.com",
    "mp.weixin.qq.com",
    "pipix.com",
    "snapchat.com",
    "threads.com",
    "threads.net",
    "tieba.baidu.com",
    "tiktok.com",
    "toutiao.com",
    "twitter.com",
    "weibo.cn",
    "weibo.com",
    "x.com",
    "xiaoheihe.cn",
    "xiaohongshu.com",
    "xhslink.com",
    "youtu.be",
    "youtube.com",
    "zhihu.com",
    "zuiyou.com",
)

# 中文标点和引号不会出现在 URL 里，直接当作边界。
_URL_PATTERN = re.compile(
    r"https?://[^\s\u4e00-\u9fff\"'<>()\[\]{}，。、；：！？]+",
    re.IGNORECASE,
)
_TRAILING_CHARS = "。，、；：！？)）】》\"'"

# 探测文件体积的超时（秒）。
_PROBE_TIMEOUT_SECONDS = 15.0


def extract_urls(text: str) -> list[str]:
    """从消息文本里挑出可能受支持的链接。

    只做域名层面的粗筛，是否真的支持交给 ParseHub 判断。

    Args:
        text: 消息纯文本，可以是分享文案。

    Returns:
        去重且保持原顺序的链接列表；没有命中时返回空列表。
    """
    found: list[str] = []
    for raw_url in _URL_PATTERN.findall(text or ""):
        url = raw_url.rstrip(_TRAILING_CHARS)
        host = (urlsplit(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[len("www.") :]
        if not _host_allowed(host):
            continue
        if url not in found:
            found.append(url)
    return found


def _host_allowed(host: str) -> bool:
    """判断域名是否落在预筛白名单里（含子域名）。

    Args:
        host: 已小写、已去掉 ``www.`` 的域名。

    Returns:
        命中白名单返回 True。
    """
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in _ALLOWED_HOSTS
    )


def proxy_for(platform: str) -> str:
    """按平台决定代理地址。

    配置了 ``PARSEHUB_PROXY`` 时，只对 ``PARSEHUB_PROXY_PLATFORMS`` 里的平台
    启用；该列表为空表示所有平台都走代理。

    Args:
        platform: ParseHub 的平台 id，例如 ``youtube``。

    Returns:
        代理地址；不需要代理时返回空字符串。
    """
    proxy = plugin_config.parsehub_proxy
    if not proxy:
        return ""
    allowed = plugin_config.parsehub_proxy_platforms
    if allowed and platform not in allowed:
        return ""
    return proxy


def platform_cookie(platform: str, fallback: str) -> str:
    """取该平台要用的 cookie。

    优先读 ParseHub 自己的 ``cookies.toml``（由 ``ph set cookie <平台>``
    写入），没有再退回配置里的兜底 cookie。

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
    except (ImportError, OSError, ValueError):
        return ""
    value = (data.get("cookie") or {}).get(platform)
    return str(value) if value else ""


async def probe_size(
    url: str,
    timeout: float = _PROBE_TIMEOUT_SECONDS,
    proxy: str = "",
) -> int | None:
    """在下载前尽量问出文件体积。

    先试 HEAD，部分 CDN 不支持，再用 ``Range: bytes=0-0`` 从 Content-Range
    里取总长度。任何失败都视为未知，不影响主流程。

    Args:
        url: 媒体直链。
        timeout: 单次请求超时（秒）。
        proxy: 需要走代理时传入代理地址。

    Returns:
        文件字节数；探测不出来时返回 None。
    """
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy or None,
        ) as client:
            response = await client.head(url)
            length = response.headers.get("content-length", "")
            if response.status_code < 400 and length.isdigit():
                return int(length)

            response = await client.get(url, headers={"Range": "bytes=0-0"})
            content_range = response.headers.get("content-range", "")
    except (httpx.HTTPError, httpx.InvalidURL):
        return None

    tail = content_range.rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


class DedupeCache:
    """同一链接在时间窗口内只处理一次。

    记录每个链接最近一次被处理的时间戳，避免群里同一张卡片被反复解析。

    Attributes:
        _seen: 链接到最近处理时间戳的映射。
    """

    def __init__(self) -> None:
        """初始化空缓存。"""
        self._seen: dict[str, float] = {}

    def should_skip(self, url: str, window: int) -> bool:
        """判断该链接是否在去重窗口内出现过。

        无论是否跳过，都会刷新该链接的时间戳并顺手清理过期记录。

        Args:
            url: 待检查的链接。
            window: 去重窗口（秒），小于等于 0 表示不去重。

        Returns:
            窗口内重复出现返回 True。
        """
        now = time.time()
        for key, timestamp in list(self._seen.items()):
            if now - timestamp > window:
                self._seen.pop(key, None)

        last_seen = self._seen.get(url)
        self._seen[url] = now
        if window <= 0 or last_seen is None:
            return False
        return now - last_seen < window


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
    ) -> Any:
        """解析并下载媒体到指定目录。

        Args:
            url: 链接或分享文案。
            path: 下载目录。
            proxy: 解析与下载共用的代理。
            cookie: 登录态 cookie。

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
        )


def _import_parsehub() -> Any:
    """在线程里导入 parsehub 并构造实例。

    Returns:
        新的 ParseHub 实例。
    """
    from parsehub import ParseHub

    return ParseHub()


# 进程内共享的解析客户端。
CLIENT = ParseHubClient()
