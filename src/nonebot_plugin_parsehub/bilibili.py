# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""B 站相关规则的单一来源。

keys / audio / bilibili_live 三个模块都认同一套域名、BV 号与 AV 号规则，
规则只在这里写一份，改短链域名之类的调整不需要再对三处。

AV 号（``av170001``）是 B 站的老编号，和 BV 号指向同一个作品。两者之间的
换算是纯位运算（见 :func:`av_to_bvid`），所以插件内统一先把 AV 号换成 BV
号，再去请求、去重和缓存：同一条视频不管用户发的是哪种编号，都落到同一个
作品 key 上。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from urllib.parse import urlsplit

# 直播间链接的主机名。
LIVE_HOST = "live.bilibili.com"

# 短链域名：路径没有作品信息，展开后才知道指向什么。
SHORT_HOSTS: tuple[str, ...] = ("b23.tv", "bili2233.cn")

# B 站相关主机（含子域名），短链排在末尾。
HOSTS: tuple[str, ...] = ("bilibili.com", *SHORT_HOSTS)

# 裸 BV / AV 号拼成的标准链接，和用户直接发完整链接走同一套 key / 缓存。
VIDEO_URL = "https://www.bilibili.com/video/{}"

# 裸 BV 号：前后不能紧贴字母数字，免得把 ``xBV...`` 或更长的串切一段出来。
BV_PATTERN: re.Pattern[str] = re.compile(
    r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])"
)

# 裸 AV 号（老编号）：同样卡前后边界；分组 1 是数字部分。
AV_PATTERN: re.Pattern[str] = re.compile(
    r"(?<![0-9A-Za-z])[aA][vV](\d+)(?![0-9A-Za-z])"
)

# AV → BV 的位运算常量，取自 B 站的公开换算方式。
_XOR_CODE = 23442827791579
_MASK_CODE = 2251799813685247
_MAX_AID = 1 << 51
_ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
_ENCODE_MAP = (8, 7, 0, 5, 1, 3, 2, 4, 6)
_BV_PREFIX = "BV1"


def av_to_bvid(aid: int) -> str:
    """把 AV 号换算成等价的 BV 号（纯位运算，不发请求）。

    Args:
        aid: 数字形式的 AV 号（不带 ``av`` 前缀）。

    Returns:
        对应的 BV 号。

    Raises:
        ValueError: 号段超出 BV 号能表示的范围。
    """
    if not 0 <= aid <= _MASK_CODE:
        raise ValueError(f"av 号超出范围: {aid}")
    chars = [""] * len(_ENCODE_MAP)
    tmp = (_MAX_AID | aid) ^ _XOR_CODE
    for index in _ENCODE_MAP:
        chars[index] = _ALPHABET[tmp % len(_ALPHABET)]
        tmp //= len(_ALPHABET)
    return _BV_PREFIX + "".join(chars)


def av_to_bvid_safe(aid: int) -> str | None:
    """AV → BV，号段非法时返回 ``None`` 而不抛异常。"""
    try:
        return av_to_bvid(aid)
    except ValueError:
        return None


def bvid_of(text: str) -> str | None:
    """取文本里的视频编号，AV 号换算成等价的 BV 号。

    Args:
        text: 链接、裸编号或分享文案。

    Returns:
        BV 号；没有可识别的编号（或 AV 号超出范围）时为 ``None``。
    """
    match = BV_PATTERN.search(text)
    if match:
        return match.group(1)
    match = AV_PATTERN.search(text)
    if match:
        return av_to_bvid_safe(int(match.group(1)))
    return None


def video_url(bvid: str, source: str = "") -> str:
    """用 BV 号拼标准链接，保留源链接里的查询串（分P参数靠它）。

    Args:
        bvid: BV 号。
        source: 原链接；空串表示没有查询串要保留。

    Returns:
        标准链接，形如 ``https://www.bilibili.com/video/BV...?p=2``。
    """
    query = urlsplit(source).query if source else ""
    url = VIDEO_URL.format(bvid)
    return f"{url}?{query}" if query else url


def bare_video_urls(
    text: str, spans: Iterable[tuple[int, int]]
) -> Iterator[tuple[int, str]]:
    """扫出不在已有链接范围内的裸 BV / AV 号。

    编号归一成标准链接（AV 换算成 BV），调用方不用再关心用户发的是哪种。

    Args:
        text: 消息文本。
        spans: 已经被当作链接的字符范围；落在里面的编号不重复取。

    Yields:
        ``(在原文里的起始下标, 标准链接)``。
    """
    spans = list(spans)

    def inside(start: int, end: int) -> bool:
        return any(
            start < span_end and span_start < end
            for span_start, span_end in spans
        )

    for match in BV_PATTERN.finditer(text):
        if inside(*match.span()):
            continue
        yield match.start(), VIDEO_URL.format(match.group(1))
    for match in AV_PATTERN.finditer(text):
        if inside(*match.span()):
            continue
        bvid = av_to_bvid_safe(int(match.group(1)))
        if bvid is not None:
            yield match.start(), VIDEO_URL.format(bvid)
