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
    "bili2233.cn",
    "bilibili.com",
    "channels.weixin.qq.com",
    "chenzhongtech.com",
    "coolapk.com",
    "douban.com",
    "douc.cc",
    "douyin.com",
    "facebook.com",
    "fb.watch",
    "fixupx.com",
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
    "weixin.qq.com",
    "x.com",
    "xiaochuankeji.cn",
    "xiaoheihe.cn",
    "xiaohongshu.com",
    "xhslink.cn",
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


# 平台内容的 ID 特征：命中就能把同一个作品的各种写法归一成同一个 key。
# 顺序有意义，更具体的排前面（BV 号优先于 /video/ 里的数字）。
_ID_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bilibili", re.compile(r"(BV[0-9A-Za-z]{10})")),
    ("bilibili", re.compile(r"/av(\d+)")),
    ("douyin", re.compile(r"/video/(\d{6,})")),
    ("douyin", re.compile(r"/note/(\d{6,})")),
    ("douyin", re.compile(r"[?&]modal_id=(\d{6,})")),
    ("kuaishou", re.compile(r"/(?:short-video|fw/photo)/([0-9A-Za-z_-]+)")),
    ("xiaohongshu", re.compile(r"/(?:explore|discovery/item)/([0-9A-Za-z]+)")),
    ("weibo", re.compile(r"/(?:detail|status)/([0-9A-Za-z]+)")),
    ("youtube", re.compile(r"youtu\.be/([0-9A-Za-z_-]{11})")),
    ("youtube", re.compile(r"[?&]v=([0-9A-Za-z_-]{11})")),
    ("twitter", re.compile(r"/status(?:es)?/(\d+)")),
    ("instagram", re.compile(r"/(?:p|reel|tv)/([0-9A-Za-z_-]+)")),
)

# 归一化时要去掉的主机名前缀（m.bilibili.com 与 www.bilibili.com 是同一个站）。
_HOST_PREFIXES = ("www.", "m.", "mobile.")


def content_key(url: str) -> str:
    """把链接归一化成去重用的 key（纯字符串处理，不发请求）。

    认得出作品 ID 的平台直接用 ``平台:id``，这样同一个作品的两种写法能对上——比如
    抖音的 ``v.douyin.com/xxx`` 和 ``iesdouyin.com/share/video/<id>``、B 站的
    ``b23.tv/xxx`` 和 ``bilibili.com/video/BVxxxx``（解析结果里的 ``raw_url``
    已经是短链展开后的地址）。认不出来的退化成「主机 + 路径」，丢掉查询串和末尾
    斜杠。

    Args:
        url: 链接，可以是用户发的原始链接，也可以是 ParseHub 给的 ``raw_url``。

    Returns:
        归一化后的 key。
    """
    for name, pattern in _ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return f"{name}:{match.group(1).lower()}"

    parsed = urlsplit(url)
    host = parsed.netloc.lower()
    for prefix in _HOST_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    return f"{host}{parsed.path.rstrip('/')}".lower()


class DedupeCache:
    """同一链接在同一个会话里、时间窗口内只处理一次。

    记录 ``(会话, 链接)`` 最近一次被处理的时间戳，避免同一个群里同一张卡片被反复
    解析。**跨会话不去重**：同一个链接在 A 群发过，B 群再发照样解析。

    Attributes:
        _seen: ``(会话, 链接)`` 到最近处理时间戳的映射。
    """

    def __init__(self) -> None:
        """初始化空缓存。"""
        self._seen: dict[tuple[str, str], float] = {}

    def seen(self, session: str, key: str, window: int) -> bool:
        """这个会话里该 key 是否还在窗口内（只看不记）。

        Args:
            session: 会话标识（群 / 私聊）。
            key: 作品 key。
            window: 去重窗口（秒），小于等于 0 表示不去重。

        Returns:
            窗口内出现过返回 True。
        """
        if window <= 0:
            return False
        self._prune(window)
        last_seen = self._seen.get((session, key))
        return last_seen is not None and time.time() - last_seen < window

    def mark(self, session: str, key: str) -> None:
        """记下这个会话里该 key 刚被处理过。

        Args:
            session: 会话标识（群 / 私聊）。
            key: 作品 key。
        """
        self._seen[(session, key)] = time.time()

    def should_skip(self, session: str, key: str, window: int) -> bool:
        """查一下并顺手记上（「处理过了」的语义）。

        Args:
            session: 会话标识（群 / 私聊）。
            key: 作品 key。
            window: 去重窗口（秒），小于等于 0 表示不去重。

        Returns:
            窗口内重复出现返回 True。
        """
        repeated = self.seen(session, key, window)
        self.mark(session, key)
        return repeated

    def _prune(self, window: int) -> None:
        """清掉窗口外的旧记录。

        Args:
            window: 去重窗口（秒）。
        """
        now = time.time()
        for key, timestamp in list(self._seen.items()):
            if now - timestamp > window:
                self._seen.pop(key, None)


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

    async def download_parsed(
        self,
        result: Any,
        path: str,
        proxy: str = "",
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

        Returns:
            ParseHub 的下载结果对象，``media`` 里是本地文件。

        Raises:
            Exception: 下载失败时由 ParseHub 抛出的异常。
        """
        return await asyncio.to_thread(
            result.download_sync,
            path,
            proxy=proxy or None,
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
