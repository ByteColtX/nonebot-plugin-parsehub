# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""平台元数据与平台相关的单位换算。

这里是唯一的平台表：展示名、品牌色、logo 文件名、是否走代理、``duration`` 的
单位。``keys`` / ``config`` / ``card`` 都从这里取，避免同一个平台在三个地方各写
一份。

模块只含纯数据和纯函数，不依赖 nonebot / Pillow / httpx。
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

# 平台 id → (展示名, 品牌色, logo 文件名)。
# 别名（xhs / wechat / twitter / baidu）与主 id 指向同一套展示信息，方便白名单
# 里写常见写法。
PLATFORMS: dict[str, tuple[str, tuple[int, int, int], str]] = {
    "bilibili": ("bilibili", (251, 114, 153), "bilibili"),
    "douyin": ("抖音", (254, 44, 85), "douyin"),
    "xiaohongshu": ("小红书", (255, 36, 66), "xiaohongshu"),
    "xhs": ("小红书", (255, 36, 66), "xiaohongshu"),
    "weibo": ("微博", (230, 22, 45), "weibo"),
    "zhihu": ("知乎", (0, 132, 255), "zhihu"),
    "kuaishou": ("快手", (255, 73, 6), "kuaishou"),
    "pipix": ("皮皮虾", (255, 90, 45), "pipix"),
    "tieba": ("贴吧", (41, 50, 225), "baidu"),
    "baidu": ("百度", (41, 50, 225), "baidu"),
    "weixin": ("微信", (7, 193, 96), "wechat"),
    "wechat": ("微信", (7, 193, 96), "wechat"),
    "weixin_channels": ("微信视频号", (250, 157, 59), "wechat"),
    "toutiao": ("今日头条", (255, 55, 60), "toutiao"),
    "coolapk": ("酷安", (17, 178, 101), "coolapk"),
    "xiaoheihe": ("小黑盒", (26, 26, 32), "xiaoheihe"),
    "zuiyou": ("最右", (255, 196, 0), "zuiyou"),
    "x": ("X", (255, 255, 255), "x"),
    "twitter": ("X", (255, 255, 255), "x"),
    "youtube": ("YouTube", (255, 0, 0), "youtube"),
    "instagram": ("Instagram", (255, 0, 105), "instagram"),
    "tiktok": ("TikTok", (255, 255, 255), "tiktok"),
    "threads": ("Threads", (255, 255, 255), "threads"),
    "facebook": ("Facebook", (8, 102, 255), "facebook"),
    "douban": ("豆瓣", (45, 150, 61), "douban"),
}

# 插件支持的平台（parsehub 的 platform.id）。上游可能多加载了解析器，
# 但链接识别、`/解析平台` 与代理默认值只认这份白名单：新平台要显式加进来，
# 上游多出来的（bangumi / medium / snapchat）不会自动变成「支持」。
SUPPORTED_PLATFORMS: frozenset[str] = frozenset(
    {
        "bilibili",
        "coolapk",
        "douban",
        "douyin",
        "facebook",
        "instagram",
        "kuaishou",
        "pipix",
        "threads",
        "tieba",
        "tiktok",
        "toutiao",
        "twitter",
        "weibo",
        "weixin",
        "weixin_channels",
        "xhs",
        "xiaoheihe",
        "youtube",
        "zhihu",
        "zuiyou",
    }
)

# 平台白名单里允许写的别名（parsehub 的 id 是 xhs / weixin / twitter）。
PLATFORM_ALIASES: dict[str, str] = {
    "xiaohongshu": "xhs",
    "red": "xhs",
    "小红书": "xhs",
    "wechat": "weixin",
    "微信": "weixin",
    "x": "twitter",
    "哔哩哔哩": "bilibili",
    "b站": "bilibili",
    "抖音": "douyin",
}

# 需要走代理的海外平台。
OVERSEAS_PLATFORMS: tuple[str, ...] = (
    "twitter",
    "instagram",
    "threads",
    "youtube",
    "tiktok",
    "facebook",
)

# 视频过长提示里「去 xx 看吧」用的平台名：平台展示名不一定适合念出来。
WATCH_NAMES: dict[str, str] = {
    "bilibili": "B 站",
    "twitter": "X",
    "weixin": "微信",
    "wechat": "微信",
}

# 正文型平台：卡片只放得下标题摘要，正文必须跟资源一起进合并转发。
# 这里只列「正文本身是内容主体」的平台；抖音 / 快手 / 皮皮虾这类短文案平台
# 不列，避免每个视频都多一个转发块。富文本（richtext）无论平台都算长正文。
LONG_BODY_PLATFORMS: frozenset[str] = frozenset(
    {
        "coolapk",
        "douban",
        "facebook",
        "instagram",
        "threads",
        "tieba",
        "toutiao",
        "twitter",
        "weibo",
        "weixin",
        "xhs",
        "xiaohongshu",
        "xiaoheihe",
        "youtube",
        "zhihu",
        "zuiyou",
    }
)

# 名单外的平台，正文超过这个长度也按长正文处理（B 站动态、未来新平台）。
LONG_BODY_MIN_CHARS = 140


def needs_body_forward(
    platform_id: str,
    post_type: str,
    content: str,
) -> bool:
    """正文是否要和媒体一起走合并转发。

    酷安、微博、知乎这类平台的正文可能长到卡片放不下；只把正文放在卡片里会
    截断，单独发一条文字又和媒体分成两条消息。命中这些平台、富文本结果，或
    正文本身够长时，调用方应把正文放进合并转发。

    Args:
        platform_id: ParseHub 的平台 id。
        post_type: ParseHub 的帖子类型（video / image / richtext 等）。
        content: 解析出的正文。

    Returns:
        需要把正文放进合并转发时返回 True。
    """
    text = (content or "").strip()
    if not text:
        return False
    if post_type == "richtext":
        return True
    platform = normalise_platform(platform_id or "")
    if platform in LONG_BODY_PLATFORMS:
        return True
    # B 站动态是 image 类型、正文在 content 里；普通视频简介不强行塞转发块。
    if platform == "bilibili" and post_type == "image":
        return True
    return len(text) > LONG_BODY_MIN_CHARS


# duration 单位上游各平台不统一：抖音 / 小红书 / 快手给的是毫秒，其它是秒。
# 新平台要实测：下载媒体用 cv2 / ffprobe 量真实时长核对（TikTok 就是这样确认
# 是秒的——报告 12，量出 12.20s，早先当毫秒会显示成 00:00；快手报告 11933，
# 实际 11.93s）。
MS_PLATFORMS = frozenset({"douyin", "xhs", "kuaishou"})


def normalise_platform(name: str) -> str:
    """把白名单里写的平台名归一成 parsehub 的平台 id。

    先查手写别名表（小红书 → xhs 这种），再查展示名唯一匹配的平台。

    Args:
        name: 白名单里写的一项，如 ``小红书`` / ``xhs`` / ``bilibili``。

    Returns:
        归一后的平台 id。
    """
    key = name.strip().lower()
    if key in PLATFORM_ALIASES:
        return PLATFORM_ALIASES[key]
    if key in PLATFORMS:
        return key
    matches = {
        platform_id
        for platform_id, (display, _, _) in PLATFORMS.items()
        if display.lower() == key
    }
    return matches.pop() if len(matches) == 1 else key


def is_youtube_music(url: str) -> bool:
    """链接是不是 YouTube Music。

    Args:
        url: 原始链接或解析结果里的链接。

    Returns:
        ``music.youtube.com`` 返回 True。
    """
    return (urlsplit(url).hostname or "").lower() == "music.youtube.com"


def display_name(platform_id: str) -> str:
    """平台展示名。

    Args:
        platform_id: parsehub 的平台 id。

    Returns:
        中文/品牌展示名；未知平台原样返回 id。
    """
    return PLATFORMS.get(platform_id, (platform_id or "未知平台",))[0]


def watch_name(platform_id: str, fallback: str = "") -> str:
    """拼「还是去 xx 看吧」这句提示用的平台名。

    Args:
        platform_id: parsehub 的平台 id。
        fallback: 平台 id 不认识时的兜底名字。

    Returns:
        适合念出来的平台名，例如 ``B 站``。
    """
    key = platform_id.lower()
    return WATCH_NAMES.get(key) or fallback or display_name(key)


def uses_milliseconds(platform_id: str) -> bool:
    """该平台的 ``duration`` 是不是毫秒。

    Args:
        platform_id: parsehub 的平台 id。

    Returns:
        抖音 / 小红书返回 True。
    """
    return platform_id.lower() in MS_PLATFORMS


def duration_seconds(platform_id: str, durations: Iterable[int]) -> int:
    """取一组媒体时长里最长的那条，并换算成秒。

    Args:
        platform_id: parsehub 的平台 id，决定单位。
        durations: 各媒体上报的 duration 原始值。

    Returns:
        最长时长（秒）；都拿不到时返回 0。
    """
    longest = 0
    for value in durations:
        try:
            longest = max(longest, int(value or 0))
        except (TypeError, ValueError):
            continue
    if longest <= 0:
        return 0
    if uses_milliseconds(platform_id):
        return longest // 1000
    return longest
