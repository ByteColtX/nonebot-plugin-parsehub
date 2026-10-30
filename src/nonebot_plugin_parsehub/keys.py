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
from collections.abc import Iterator
from urllib.parse import parse_qsl, urlsplit

from .bilibili import (
    BV_PATTERN as _BV_PATTERN,
)
from .bilibili import (
    SHORT_HOSTS as _BILIBILI_SHORT_HOSTS,
)
from .bilibili import (
    VIDEO_URL as _BILIBILI_VIDEO_URL,
)

# 域名 → parsehub 平台 id。白名单与平台识别共用这一份表：加平台时域名和 id
# 写在一起，避免白名单与解析器各维护一份。
HOST_PLATFORMS: dict[str, str] = {
    "b23.tv": "bilibili",
    "bili2233.cn": "bilibili",
    "bilibili.com": "bilibili",
    "chenzhongtech.com": "kuaishou",
    "coolapk.com": "coolapk",
    "douban.com": "douban",
    "douc.cc": "douban",
    "douyin.com": "douyin",
    "facebook.com": "facebook",
    "fixupx.com": "twitter",
    "h5.pipix.com": "pipix",
    "iesdouyin.com": "douyin",
    "instagram.com": "instagram",
    "izuiyou.com": "zuiyou",
    "kuaishou.com": "kuaishou",
    "threads.com": "threads",
    "tieba.baidu.com": "tieba",
    "tiktok.com": "tiktok",
    "toutiao.com": "toutiao",
    "twitter.com": "twitter",
    "weibo.cn": "weibo",
    "weibo.com": "weibo",
    "weixin.qq.com": "weixin",
    "x.com": "twitter",
    "xiaochuankeji.cn": "zuiyou",
    "xiaoheihe.cn": "xiaoheihe",
    "xiaohongshu.com": "xhs",
    "xhslink.cn": "xhs",
    "xhslink.com": "xhs",
    "youtu.be": "youtube",
    "youtube.com": "youtube",
    "zhihu.com": "zhihu",
}

# 预筛白名单：只认这些域名后缀（含子域名）。
ALLOWED_HOSTS: tuple[str, ...] = tuple(HOST_PLATFORMS)

# 短链域名：路径本身没有作品信息，展开后才知道指向什么，
# 所以只做域名过滤，不再套 URL 形状。B 站短链见 :mod:`bilibili`。
_SHORT_HOSTS = frozenset(
    {*_BILIBILI_SHORT_HOSTS, "douc.cc", "xhslink.com", "xhslink.cn"}
)

# 内容域名的 URL 形状：对齐 ParseHub 各解析器的 __match__。
# 只有形状也对得上才触发解析，避免“见到链接就触发”。
_URL_SHAPES: tuple[re.Pattern[str], ...] = (
    # B 站视频 / 专栏 / 动态
    re.compile(
        r"^https?://(www\.|m\.|t\.)?bilibili\.com/"
        r"(video/|opus/|\d{18,19})",
        re.IGNORECASE,
    ),
    # B 站直播间（插件自己处理，上游不支持）
    re.compile(r"^https?://live\.bilibili\.com/\d+", re.IGNORECASE),
    # 酷安动态 / 图片
    re.compile(r"^https?://www\.coolapk\.com/(feed|picture)/", re.IGNORECASE),
    # 豆瓣话题 / 分享跳转
    re.compile(
        r"^https?://(www\.|m\.)?douban\.com/"
        r"((group/)?topic/\d+|doubanapp/dispatch)",
        re.IGNORECASE,
    ),
    # 抖音：排除个人主页与汽水音乐
    re.compile(
        r"^https?://[^/]*douyin\.com/(?!share/user|qishui).+",
        re.IGNORECASE,
    ),
    # Facebook
    re.compile(
        r"^https?://[^/]*facebook\.com/"
        r"(watch\?v|share/[vr]|[^/]+/videos/|reel/)",
        re.IGNORECASE,
    ),
    # Instagram
    re.compile(
        r"^https?://(www\.)?instagram\.com/"
        r"(p|reel|reels|share|[^/]+/(p|reel))/",
        re.IGNORECASE,
    ),
    # 快手
    re.compile(
        r"^https?://(www|v|live|v\.m|[a-z0-9]+\.m)\."
        r"(kuaishou|chenzhongtech)\.com/.+",
        re.IGNORECASE,
    ),
    # 皮皮虾
    re.compile(r"^https?://h5\.pipix\.com/(s|ppx/item)/.+", re.IGNORECASE),
    # Threads
    re.compile(
        r"^https?://[^/]*threads\.com/(@[\w.]+/post|share)/",
        re.IGNORECASE,
    ),
    # 贴吧
    re.compile(r"^https?://[^/]*tieba\.baidu\.com/p/\d+", re.IGNORECASE),
    # TikTok：排除个人主页与汽水音乐
    re.compile(
        r"^https?://[^/]*tiktok\.com/(?!share/user|qishui).+",
        re.IGNORECASE,
    ),
    # 今日头条
    re.compile(
        r"^https?://(www|m)\.toutiao\.com/"
        r"(article/\d+|video/\d+|w/\d+|group/\d+|item/\d+|a\d+|i\d+|is/\w+)",
        re.IGNORECASE,
    ),
    # X / Twitter / fixupx
    re.compile(
        r"^https?://[^/]*(twitter|fixupx|x)\.com/.*/status(es)?/\d+",
        re.IGNORECASE,
    ),
    # 微博：排除个人主页，保留 mapp API 形态
    re.compile(
        r"^https?://((m\.|video\.)?weibo\.(com|cn)/(?!u/).+"
        r"|mapp\.api\.weibo\.cn/fx/.+)",
        re.IGNORECASE,
    ),
    # 微信公众号文章
    re.compile(r"^https?://mp\.weixin\.qq\.com/s/", re.IGNORECASE),
    # 微信视频号
    re.compile(
        r"^https?://channels\.weixin\.qq\.com/"
        r"finder-preview/pages/sph\?.*\bid=\w+",
        re.IGNORECASE,
    ),
    # 微信 sph 分享
    re.compile(r"^https?://weixin\.qq\.com/sph/\w+", re.IGNORECASE),
    # 小黑盒
    re.compile(
        r"^https?://[^/]*xiaoheihe\.cn/(v3|app)/bbs/(app|link).+",
        re.IGNORECASE,
    ),
    # 小红书
    re.compile(r"^https?://[^/]*xiaohongshu\.com/.+", re.IGNORECASE),
    # YouTube：排除直播 / 帖子 / 频道页（music 子域也算）
    re.compile(
        r"^https?://(www\.|m\.|music\.)?(youtube\.com/(?!live/|post/|@).+"
        r"|youtu\.be/[0-9A-Za-z_-]{11})",
        re.IGNORECASE,
    ),
    # 知乎
    re.compile(
        r"^https?://(www|zhuanlan|daily)\.zhihu\.com/"
        r"(pin|question|p|story|appview/p)/",
        re.IGNORECASE,
    ),
    # 最右：分享长链与 s.izuiyou.com 短链
    re.compile(
        r"^https?://(share\.xiaochuankeji\.cn/hybrid/share/post\?pid=\d+"
        r"|s\.izuiyou\.com/s/\w+)",
        re.IGNORECASE,
    ),
)

# 中文标点和引号不会出现在 URL 里，直接当作边界。
_URL_PATTERN = re.compile(
    r"https?://[^\s\u4e00-\u9fff\"'<>()\[\]{}，。、；：！？]+",
    re.IGNORECASE,
)
_TRAILING_CHARS = "。，、；：！？)）】》\"'"

# 平台内容的 ID 特征：命中就能把同一个作品的各种写法归一成同一个 key。
# 顺序有意义，更具体的排前面（直播间 > 动态 > opus > BV > av）。
_ID_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 直播间：live.bilibili.com/<room_id>，与视频共用 bilibili 平台但独立成 key
    ("bilibili_live", re.compile(r"live\.bilibili\.com/(\d+)")),
    # B 站动态：18–19 位数字
    ("bilibili", re.compile(r"bilibili\.com/(\d{18,19})(?:[/?#]|$)")),
    # B 站专栏
    ("bilibili", re.compile(r"/opus/(\d+)")),
    ("bilibili", re.compile(r"(BV[0-9A-Za-z]{10})")),
    ("bilibili", re.compile(r"/av(\d+)")),
    ("douyin", re.compile(r"/video/(\d{6,})")),
    ("douyin", re.compile(r"/note/(\d{6,})")),
    ("douyin", re.compile(r"[?&]modal_id=(\d{6,})")),
    ("kuaishou", re.compile(r"/(?:short-video|fw/photo)/([0-9A-Za-z_-]+)")),
    # 最右长链的 pid / 小黑盒分享链的 link_id 都在查询串里；不取出来会让
    # 所有分享链接共用一个 key，后续链接被误判成重复 / 直接命中上一份缓存。
    (
        "zuiyou",
        re.compile(r"xiaochuankeji\.cn/[^?#]*[?&]pid=(\d+)"),
    ),
    ("xiaoheihe", re.compile(r"[?&]link_id=([0-9A-Za-z]+)")),
    ("xiaoheihe", re.compile(r"/bbs/link/([0-9A-Za-z]+)")),
    # 豆瓣：普通话题链接，以及 doubanapp 里被 URL 编码的 uri 参数
    (
        "douban",
        re.compile(
            r"douban\.com/(?:group/)?topic/(\d+)",
            re.IGNORECASE,
        ),
    ),
    (
        "douban",
        re.compile(
            r"douban\.com/doubanapp/dispatch\?[^#]*?uri="
            r"(?:%2f|/)(?:group(?:%2f|/))?topic(?:%2f|/)(\d+)",
            re.IGNORECASE,
        ),
    ),
    ("xiaohongshu", re.compile(r"/(?:explore|discovery/item)/([0-9A-Za-z]+)")),
    ("weibo", re.compile(r"/(?:detail|status)/([0-9A-Za-z]+)")),
    ("youtube", re.compile(r"youtu\.be/([0-9A-Za-z_-]{11})")),
    ("youtube", re.compile(r"[?&]v=([0-9A-Za-z_-]{11})")),
    ("twitter", re.compile(r"/status(?:es)?/(\d+)")),
    # Instagram：p / reel / tv / share，以及任意前缀下的 p / reel
    (
        "instagram",
        re.compile(
            r"/(?:p|reel|tv|share/(?:p|reel)"
            r"|[^/]+/(?:p|reel))/([0-9A-Za-z_-]+)"
        ),
    ),
)

# 裸 BV 号：见 :mod:`bilibili`（与 audio 共用同一份模式）。

# 归一化时要去掉的主机名前缀（m.bilibili.com 与 www.bilibili.com 是同一个站）。
_HOST_PREFIXES = ("www.", "m.", "mobile.")


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


def _shape_allowed(url: str) -> bool:
    """判断链接是否落在已知的 URL 形状里。

    短链域名直接放行；内容域名必须命中 :data:`_URL_SHAPES` 中的一条，
    这样 ``bilibili.com``、``weibo.com`` 这类大站的首页 / 个人主页
    不会再触发解析。

    Args:
        url: 链接。

    Returns:
        值得交给 ParseHub / 直播分支处理为 ``True``。
    """
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[len("www.") :]
    if host in _SHORT_HOSTS:
        return True
    return any(pattern.match(url) for pattern in _URL_SHAPES)


def _iter_urls(text: str) -> Iterator[tuple[int, str]]:
    """扫出文本里既在白名单域名、又符合已知形状的链接。

    Args:
        text: 消息纯文本，可以是分享文案。

    Yields:
        ``(在原文里的起始下标, 链接)``，保持出现顺序，不去重。
    """
    for match in _URL_PATTERN.finditer(text or ""):
        url = match.group().rstrip(_TRAILING_CHARS)
        host = (urlsplit(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[len("www.") :]
        if host_allowed(host) and _shape_allowed(url):
            yield match.start(), url


def extract_urls(text: str) -> list[str]:
    """从消息文本里挑出可能受支持的链接。

    先按域名白名单粗筛，再按 URL 形状精筛；短链域名只做域名判断。

    Args:
        text: 消息纯文本，可以是分享文案。

    Returns:
        去重且保持原顺序的链接列表；没有命中时返回空列表。
    """
    found: list[str] = []
    for _, url in _iter_urls(text):
        if url not in found:
            found.append(url)
    return found


def extract_targets(text: str) -> list[str]:
    """从消息文本里挑出所有要解析的目标：链接和裸 BV 号。

    裸 BV 号（``BV1xx411c7mD``）拼成 ``https://www.bilibili.com/video/...`` 再
    往下走，这样它和完整链接归一成同一个作品 key，缓存也共用。已经写在链接里的
    BV 号不重复取，两个都发时只留链接本身。

    Args:
        text: 消息纯文本，可以是分享文案。

    Returns:
        去重且保持出现顺序的链接列表；没有命中时返回空列表。
    """
    text = text or ""
    # 链接的原始范围（含不在白名单里的）：里面的 BV 号归 extract_urls 管，
    # 别从 example.com/BV... 这种链接里再抠一个 B 站视频出来。
    spans = [match.span() for match in _URL_PATTERN.finditer(text)]
    found: list[tuple[int, str]] = list(_iter_urls(text))
    seen = {url for _, url in found}

    for match in _BV_PATTERN.finditer(text):
        start, end = match.span()
        if any(
            start < span_end and span_start < end
            for span_start, span_end in spans
        ):
            continue
        url = _BILIBILI_VIDEO_URL.format(match.group(1))
        if url in seen:
            continue
        seen.add(url)
        found.append((start, url))

    found.sort(key=lambda item: item[0])
    return [url for _, url in found]


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


def platform_of(url: str) -> str:
    """从链接的域名判断 parsehub 平台 id（纯字符串处理，不发请求）。

    ``weixin.qq.com`` 同时承载公众号文章和视频号分享：``/sph/`` 路径与
    ``channels.`` 子域归到 ``weixin_channels``，其余归 ``weixin``。

    Args:
        url: 链接。

    Returns:
        平台 id；域名不在白名单里时为空串。
    """
    parsed = urlsplit(url or "")
    host = (parsed.hostname or "").lower()
    for allowed, platform in HOST_PLATFORMS.items():
        if host == allowed or host.endswith("." + allowed):
            if platform == "weixin" and (
                host.startswith("channels.") or parsed.path.startswith("/sph/")
            ):
                return "weixin_channels"
            return platform
    return ""


def content_key(url: str, *, fold_case: bool = True) -> str:
    """把链接归一化成去重/缓存用的 key（纯字符串处理，不发请求）。

    认得出作品 ID 的平台直接用 ``平台:id``，这样同一个作品的两种写法能对上——比如
    抖音的 ``v.douyin.com/xxx`` 和 ``iesdouyin.com/share/video/<id>``、B 站的
    ``b23.tv/xxx`` 和 ``bilibili.com/video/BVxxxx``（解析结果里的 ``raw_url``
    已经是短链展开后的地址）、B 站动态的 18–19 位 ID、专栏的 ``opus/<id>``。
    B 站带 ``p`` 时把分P也写进 key，避免不同 P 撞缓存。认不出来的退化成
    「主机 + 路径」，丢掉查询串和末尾斜杠。

    Args:
        url: 链接，可以是用户发的原始链接，也可以是 ParseHub 给的 ``raw_url``。
        fold_case: 是否把作品 ID 统一成小写；音频缓存传 ``False``，保留
            YouTube / B 站 ID 的大小写。

    Returns:
        归一化后的 key。
    """
    for name, pattern in _ID_PATTERNS:
        match = pattern.search(url)
        if match:
            ident = match.group(1)
            key = f"{name}:{ident.lower() if fold_case else ident}"
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
    key = f"{host}{parsed.path.rstrip('/')}"
    if fold_case:
        key = key.lower()
    if _is_bilibili_short_host(host):
        part = _bilibili_part(url)
        if part > 1:
            key += f":p{part}"
    return key
