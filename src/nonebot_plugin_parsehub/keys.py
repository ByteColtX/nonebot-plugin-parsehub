# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""链接识别与作品 key 归一化（纯字符串处理，不发请求）。

两层过滤：

1. 域名白名单粗筛，避免为无关链接付出 parsehub 的 import 代价；
2. 平台作品 ID 归一，让同一个作品的不同写法（短链 / 完整链）落到同一个 key。

模块不依赖 nonebot，方便单独测试。
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

# 预筛白名单：只认这些域名后缀（含子域名）。
ALLOWED_HOSTS: tuple[str, ...] = (
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

# B 站短链没有作品 ID，但分P要进 key，否则同一个短链的不同 P 会撞缓存。
_BILIBILI_SHORT_HOSTS = ("b23.tv", "bili2233.cn")


def _bilibili_part(url: str) -> int:
    """取 B 站链接里的分P序号。

    Args:
        url: 链接。

    Returns:
        大于 0 的分P序号；没写或非法时为默认P（1）。
    """
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key.lower() != "p":
            continue
        try:
            part = int(value)
        except (TypeError, ValueError):
            return 1
        return part if part > 0 else 1
    return 1


def _is_bilibili_short_host(host: str) -> bool:
    """判断主机是不是 B 站短链站。

    Args:
        host: 已小写的主机名。

    Returns:
        命中返回 True。
    """
    return any(
        host == item or host.endswith("." + item)
        for item in _BILIBILI_SHORT_HOSTS
    )


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
        if not host_allowed(host):
            continue
        if url not in found:
            found.append(url)
    return found


def host_allowed(host: str) -> bool:
    """判断域名是否落在预筛白名单里（含子域名）。

    Args:
        host: 已小写、已去掉 ``www.`` 的域名。

    Returns:
        命中白名单返回 True。
    """
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in ALLOWED_HOSTS
    )


def content_key(url: str) -> str:
    """把链接归一化成去重/缓存用的 key（纯字符串处理，不发请求）。

    认得出作品 ID 的平台直接用 ``平台:id``，这样同一个作品的两种写法能对上——比如
    抖音的 ``v.douyin.com/xxx`` 和 ``iesdouyin.com/share/video/<id>``、B 站的
    ``b23.tv/xxx`` 和 ``bilibili.com/video/BVxxxx``（解析结果里的 ``raw_url``
    已经是短链展开后的地址）。B 站带 ``p`` 时把分P也写进 key，避免不同 P
    撞缓存。认不出来的退化成「主机 + 路径」，丢掉查询串和末尾斜杠。

    Args:
        url: 链接，可以是用户发的原始链接，也可以是 ParseHub 给的 ``raw_url``。

    Returns:
        归一化后的 key。
    """
    for name, pattern in _ID_PATTERNS:
        match = pattern.search(url)
        if match:
            key = f"{name}:{match.group(1).lower()}"
            if name == "bilibili":
                part = _bilibili_part(url)
                if part > 1:
                    key += f":p{part}"
            return key

    parsed = urlsplit(url)
    host = parsed.netloc.lower()
    for prefix in _HOST_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    key = f"{host}{parsed.path.rstrip('/')}".lower()
    if _is_bilibili_short_host(host):
        part = _bilibili_part(url)
        if part > 1:
            key += f":p{part}"
    return key
