# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""B 站分P兼容：只解析默认P，并按默认P的时长判断。

ParseHub 的 B 站解析器在 ``p=1`` 时直接取 ``View.duration``，而 B 站这个字段
是**所有分P的时长总和**；分P越多，插件越容易误判成超长视频。这里在解析前后各
补一刀：

1. 解析前去掉 ``p`` 参数，让 ParseHub 始终解析默认P；
2. 解析后向 B 站查一次 ``pages[0].duration``，覆盖结果里的总时长。

第 2 步失败时保持原样（退化成旧行为），不影响解析和下载。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from nonebot import logger

# B 站视频 ID。BV 号大小写敏感，av 号只在路径或参数里认。
_BV_PATTERN = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_PATTERN = re.compile(r"(?:/|=)av(\d+)", re.IGNORECASE)
_AID_PATTERN = re.compile(r"[?&]aid=(\d+)")

# 需要做默认P归一的主机。短链没有 ID，但同样可能带 p 参数。
_VIDEO_HOSTS = ("bilibili.com", "b23.tv", "bili2233.cn")
_SHORT_HOSTS = ("b23.tv", "bili2233.cn")

_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)
_TIMEOUT_SECONDS = 5.0


def _video_params(url: str) -> dict[str, str] | None:
    """从链接里取出 B 站视频 ID。

    Args:
        url: 可能是完整链或短链展开后的链接。

    Returns:
        ``{"bvid": ...}`` 或 ``{"aid": ...}``；认不出来时返回 None。
    """
    match = _BV_PATTERN.search(url)
    if match:
        return {"bvid": match.group(1)}
    match = _AV_PATTERN.search(url)
    if match:
        return {"aid": match.group(1)}
    match = _AID_PATTERN.search(url)
    if match:
        return {"aid": match.group(1)}
    return None


def _is_video_host(host: str) -> bool:
    """判断主机是不是 B 站视频站（含短链站）。

    Args:
        host: 已小写的主机名。

    Returns:
        命中返回 True。
    """
    return any(
        host == item or host.endswith("." + item) for item in _VIDEO_HOSTS
    )


def _is_short_host(host: str) -> bool:
    """判断主机是不是 B 站短链站。

    Args:
        host: 已小写的主机名。

    Returns:
        命中返回 True。
    """
    return any(
        host == item or host.endswith("." + item) for item in _SHORT_HOSTS
    )


def default_part_url(url: str) -> str:
    """去掉 B 站视频链接里的 ``p`` 参数。

    去掉后 ParseHub 会解析默认P（P1）；对短链也先去掉，展开后即使带 ``p``
    也不会被保留。

    Args:
        url: 待解析的链接。

    Returns:
        去掉 ``p`` 后的链接；非 B 站视频链接原样返回。
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if not _is_video_host(host):
        return url
    if not _is_short_host(host) and _video_params(url) is None:
        return url
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "p"
    ]
    return urlunsplit(parsed._replace(query=urlencode(query)))


async def resolve_default_part_url(url: str, proxy: str = "") -> str:
    """展开短链后再去掉 ``p``，确保短链也只解析默认P。

    短链本身没有 ID，ParseHub 展开时会把重定向目标里的 ``p`` 保留下来；这里先
    自己展开一次，拿到完整链后统一去掉 ``p``。展开失败就退回原链接。

    Args:
        url: 用户发的链接。
        proxy: 展开短链用的代理。

    Returns:
        指向默认P的完整链；非 B 站短链原样返回。
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if not _is_short_host(host):
        return default_part_url(url)
    try:
        async with _client(proxy) as client:
            response = await client.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            )
            response.raise_for_status()
            resolved = str(response.url)
    except Exception as exc:  # noqa: BLE001 - 展开失败交给 ParseHub 自己处理
        logger.debug(
            f"[parsehub] 展开 B 站短链失败: {type(exc).__name__}: {exc}"
        )
        return default_part_url(url)
    # 重定向到非视频页时把短链还给 ParseHub，让它按原链接选解析器。
    if _video_params(resolved) is None:
        return default_part_url(url)
    return default_part_url(resolved)


def default_page_seconds(data: Any) -> int:  # noqa: ANN401 - B 站接口 JSON
    """从 B 站 ``view`` 接口的 ``data`` 里取默认P时长。

    Args:
        data: 接口返回的 ``data`` 字段。

    Returns:
        默认P（``pages[0]``）的秒数；拿不到时退回总时长，再拿不到返回 0。
    """
    if not isinstance(data, dict):
        return 0
    pages = data.get("pages")
    if isinstance(pages, list) and pages and isinstance(pages[0], dict):
        try:
            return max(int(pages[0].get("duration") or 0), 0)
        except (TypeError, ValueError):
            pass
    try:
        return max(int(data.get("duration") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _client(proxy: str) -> httpx.AsyncClient:
    """构造查接口用的 HTTP 客户端（测试里替换成不联网的假实现）。

    Args:
        proxy: 代理地址，空字符串表示直连。

    Returns:
        异步 HTTP 客户端。
    """
    return httpx.AsyncClient(
        proxy=proxy or None,
        timeout=_TIMEOUT_SECONDS,
        follow_redirects=True,
    )


async def default_part_seconds(
    url: str,
    proxy: str = "",
    cookie: str = "",
) -> int:
    """查询 B 站视频默认P的时长。

    Args:
        url: 视频链接（完整链或已展开的短链）。
        proxy: 解析代理，空字符串表示直连。
        cookie: 登录态 cookie，空字符串表示不带。

    Returns:
        默认P的秒数；不是 B 站视频或请求失败时返回 0。
    """
    params = _video_params(url)
    if params is None:
        return 0
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": "https://www.bilibili.com/",
    }
    if cookie:
        headers["Cookie"] = cookie
    try:
        async with _client(proxy) as client:
            response = await client.get(
                _VIEW_API, params=params, headers=headers
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 - 查时长失败不能影响解析
        logger.debug(
            f"[parsehub] 查询 B 站默认P时长失败: {type(exc).__name__}: {exc}"
        )
        return 0
    data = payload.get("data") if isinstance(payload, dict) else None
    return default_page_seconds(data)


def apply_duration(result: Any, seconds: int) -> None:  # noqa: ANN401 - parsehub 结果
    """把默认P时长写回解析结果里的第一个媒体。

    卡片和时长上限都从 ``result.media[].duration`` 读，改这里两边一起生效。

    Args:
        result: ParseHub 的解析结果。
        seconds: 默认P的秒数。
    """
    if seconds <= 0:
        return
    media = getattr(result, "media", None)
    items = list(media) if isinstance(media, (list, tuple)) else [media]
    for item in items:
        if item is not None and hasattr(item, "duration"):
            try:
                item.duration = seconds
            except (AttributeError, TypeError):
                pass
            return
