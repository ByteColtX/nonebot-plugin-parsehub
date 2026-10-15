# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""把解析结果画成一张卡片图。

版式是 App Store「Today」那种：图片铺满整张卡片，卡片底部一段很长的渐变把画面
压暗，标题等信息压在渐变上。没有外框、没有页面底色，所以不存在"图片结束、背景
开始"那条边界。

渲染全部是同步阻塞的 CPU 活（Pillow），调用方必须放进 ``asyncio.to_thread``，
否则事件循环会被卡住几百毫秒。

字体需要一份中文字体：优先用配置指定的路径，其次找插件数据目录里已下载的，
再其次找系统字体，都没有就尝试下载；全都失败时 :func:`render` 返回 ``False``，
由调用方退回纯文字。
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("nonebot_plugin_parsehub.card")

ASSET_DIR = Path(__file__).resolve().parent / "assets" / "logos"

# duration 单位上游各平台不统一：抖音 / 小红书给的是毫秒，其它是秒。
# 新平台要实测：下载媒体用 cv2 量真实时长核对（TikTok 就是这样确认是秒的——
# 报告 12，cv2 量出 30fps × 366 帧 = 12.20s，早先当毫秒会显示成 00:00）。
_MS_PLATFORMS = frozenset({"douyin", "xhs"})

@dataclass(frozen=True)
class FontSource:
    """一个字体下载源。

    URL 必须钉在具体版本上（提交 SHA / release tag），不能跟着分支走；下载后按
    ``sha256`` 校验，不匹配就丢掉换下一个源。

    Attributes:
        url: 下载地址。
        sha256: 该文件的小写十六进制 sha256。
    """

    url: str
    sha256: str


# 中文字体（HarmonyOS Sans SC，静态单字重）。钉在镜像仓库的这次提交上，
# 不跟分支走：上游一改文件，缓存里那份和线上就不是同一个东西了。
# 华为官方只发 zip 包、没有稳定直链，所以用镜像仓库 + 提交 SHA + sha256 锁字节。
HARMONY_COMMIT = "33172a5f6b9122f2310a6a26c64f7f64e38a8456"
HARMONY_REGULAR_SHA256 = (
    "297b088424be212207df2ce8b98e335468b782aa6b96832af0b8b773d711e2b1"
)
HARMONY_REGULAR_PATH = "HarmonyOS_Sans_SC/HarmonyOS_Sans_SC_Regular.ttf"

# 缓存目录里的文件名。
FONT_NAME = "HarmonyOS_Sans_SC_Regular.ttf"

FONT_SOURCES: tuple[FontSource, ...] = (
    FontSource(
        "https://raw.githubusercontent.com/ajacocks/harmonyos-sans-font/"
        f"{HARMONY_COMMIT}/{HARMONY_REGULAR_PATH}",
        HARMONY_REGULAR_SHA256,
    ),
    FontSource(
        "https://github.com/ajacocks/harmonyos-sans-font/raw/"
        f"{HARMONY_COMMIT}/{HARMONY_REGULAR_PATH}",
        HARMONY_REGULAR_SHA256,
    ),
    FontSource(
        "https://cdn.jsdelivr.net/gh/ajacocks/harmonyos-sans-font@"
        f"{HARMONY_COMMIT}/{HARMONY_REGULAR_PATH}",
        HARMONY_REGULAR_SHA256,
    ),
)
FONT_URLS: tuple[str, ...] = tuple(source.url for source in FONT_SOURCES)

# 系统里可能已经有这些中文字体，有就直接用，不必下载。
SYSTEM_FONTS: tuple[str, ...] = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)


def _sha256_of(path: Path) -> str:
    """算文件的 sha256。

    Args:
        path: 文件路径。

    Returns:
        小写十六进制摘要。
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matches_sources(path: Path, sources: tuple[FontSource, ...]) -> bool:
    """文件是不是等于某个已知源（按 sha256）。

    Args:
        path: 待校验文件。
        sources: 允许的来源列表。

    Returns:
        命中任一来源的 sha256 为 ``True``；
        ``sources`` 为空（或都没写 sha256）时不做校验，直接 ``True``。
    """
    expected = [source.sha256 for source in sources if source.sha256]
    if not expected:
        return True
    actual = _sha256_of(path)
    if actual in expected:
        return True
    logger.warning(
        f"[parsehub] 字体 sha256 对不上，已丢弃 {path}（实际 {actual[:12]}…）"
    )
    return False


def _fetch_font(
    source: FontSource,
    target: Path,
    min_size: int,
) -> bool:
    """下载一个字体源并校验 sha256。

    校验不通过的文件会直接丢掉，绝不落到 ``target``。

    Args:
        source: 字体源（地址 + 期望 sha256）。
        target: 目标路径。
        min_size: 小于这个字节数就当作没下全。

    Returns:
        下载并校验成功为 ``True``。
    """
    tmp = target.with_suffix(".part")
    landed = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream(
            "GET", source.url, timeout=180, follow_redirects=True
        ) as resp:
            if resp.status_code != 200:
                return False
            with tmp.open("wb") as handle:
                for chunk in resp.iter_bytes(1 << 16):
                    handle.write(chunk)
        if tmp.stat().st_size < min_size:
            return False
        if source.sha256 and not _matches_sources(tmp, (source,)):
            return False
        tmp.replace(target)
        landed = True
    except Exception as exc:  # noqa: BLE001 - 网络问题都算下载失败
        logger.warning(f"[parsehub] 字体下载失败 {source.url}: {exc}")
        return False
    finally:
        if not landed and tmp.exists():
            tmp.unlink(missing_ok=True)
    logger.info(f"[parsehub] 字体已下载并校验 {target}")
    return True

# 平台 → (展示名, 品牌色, logo 文件名)
# key 用 parsehub 的 platform.id（xhs/coolapk/… 都是它的原始 id）。
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
    "snapchat": ("Snapchat", (255, 252, 0), "snapchat"),
    "medium": ("Medium", (255, 255, 255), "medium"),
    "douban": ("豆瓣", (45, 150, 61), "douban"),
    "bangumi": ("Bangumi", (245, 145, 180), "bilibili"),
}

# 结果类型 → 中文标签
TYPE_LABELS: dict[str, str] = {
    "video": "视频",
    "image": "图集",
    "multimedia": "图文",
    "richtext": "图文",
}

# 版面参数
WIDTH = 1080
INSET = 48
TITLE_SIZE = 58
META_SIZE = 27
KICKER_SIZE = 27
LOGO_HEIGHT = 54
MIN_RATIO = 0.62
MAX_RATIO = 1.25
FADE_START = 0.34
FADE_PEAK = 200
FADE_BOTTOM = 252
FADE_COLOR = (6, 6, 10)


def _pil() -> Any:  # noqa: ANN401 - 返回 PIL.Image 模块，避免顶层 import 失败
    """延迟导入 Pillow，缺失时由调用方处理。

    Returns:
        ``PIL.Image`` 模块。

    Raises:
        ImportError: 没装 Pillow。
    """
    from PIL import Image

    return Image


# 配置指定的字体（``set_font`` 注入），优先于系统字体。
_CUSTOM_FONT: Path | None = None


@lru_cache(maxsize=1)
def _font_file() -> Path | None:
    """找一份可用的中文字体文件（不下载）。

    Returns:
        字体文件路径；都没找到时为 ``None``。
    """
    if _CUSTOM_FONT is not None and _CUSTOM_FONT.exists():
        return _CUSTOM_FONT
    for path in SYSTEM_FONTS:
        if Path(path).exists():
            logger.info(f"[parsehub] 使用系统字体 {path}")
            return Path(path)
    return None


@lru_cache(maxsize=32)
def _font(size: int, weight: int = 400) -> Any:  # noqa: ANN401 - PIL FreeTypeFont
    """按字号和字重取字体对象（带缓存）。

    Args:
        size: 字号（像素）。
        weight: 字重，可变字体支持 100-900。

    Returns:
        Pillow 的字体对象。

    Raises:
        RuntimeError: 没有任何可用字体。
    """
    from PIL import ImageFont

    path = _font_file()
    if path is None:
        raise RuntimeError("没有可用的中文字体")
    font = ImageFont.truetype(str(path), size)
    try:
        font.set_variation_by_axes([weight])
    except Exception:  # noqa: BLE001 - 非可变字体不支持设置字重
        pass
    return font


# 彩色 emoji 字体：OpenMoji Color（CBDT 位图，6.8MB，OFL）。
# 没用 Twemoji Mozilla：那是 COLR 彩色字体，FreeType 按基字形的 bbox 去裁彩色
# 图层，超出部分被切掉（Pillow issue #9146），实测 emoji 下半截连嘴巴一起不见。
# CBDT 是位图字形，没有这个问题；代价是只有一档位图，别的字号得先画再缩。
OPENMOJI_COMMIT = "aeb8bb3a59e2de39c754ac79180c8131c906acea"
OPENMOJI_SHA256 = (
    "c1bb01e2802eed4e7aab21f76af7e508b2da3e7c6a6acd222f16ad2cb9614788"
)
OPENMOJI_PATH = "font/OpenMoji-color-cbdt/OpenMoji-color-cbdt.ttf"
EMOJI_FONT_SOURCES: tuple[FontSource, ...] = (
    FontSource(
        "https://raw.githubusercontent.com/hfg-gmuend/openmoji/"
        f"{OPENMOJI_COMMIT}/{OPENMOJI_PATH}",
        OPENMOJI_SHA256,
    ),
    FontSource(
        "https://github.com/hfg-gmuend/openmoji/raw/"
        f"{OPENMOJI_COMMIT}/{OPENMOJI_PATH}",
        OPENMOJI_SHA256,
    ),
    FontSource(
        "https://cdn.jsdelivr.net/gh/hfg-gmuend/openmoji@"
        f"{OPENMOJI_COMMIT}/{OPENMOJI_PATH}",
        OPENMOJI_SHA256,
    ),
)
EMOJI_FONT_URLS: tuple[str, ...] = tuple(
    source.url for source in EMOJI_FONT_SOURCES
)
EMOJI_FONT_NAME = "OpenMoji-color-cbdt.ttf"
# CBDT 位图档位：这个字体只认 99px，画别的字号必须先按它画再缩
EMOJI_STRIKE = 99
# emoji 贴图高度占字号多少（中文墨迹大约 0.9 个字号高）
EMOJI_RATIO = 0.92
# emoji 底边落在基线下多少（占字号），跟文字重心对齐
EMOJI_DROP = 0.06

# 算作 emoji 的码点区间（含 ZWJ、变体符、区域指示符，组合序列不会被切开）。
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF), (0x1F1E6, 0x1F1FF), (0x2600, 0x27BF), (0x2300, 0x23FF),
    (0x25A0, 0x25FF), (0x2B00, 0x2BFF), (0x2190, 0x21FF), (0x200D, 0x200D),
    (0xFE00, 0xFE0F), (0x20E3, 0x20E3), (0x203C, 0x203C), (0x2049, 0x2049),
    (0x2122, 0x2122), (0x2139, 0x2139), (0x24C2, 0x24C2), (0x3030, 0x3030),
    (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299),
)

_EMOJI_FONT: Path | None = None
_GLYPH_CACHE: dict[tuple[int, str], bool] = {}


def _is_emoji_char(ch: str) -> bool:
    """这个字符是不是 emoji 码点。

    Args:
        ch: 单个字符。

    Returns:
        落在 emoji 区间里为 ``True``。
    """
    code = ord(ch)
    return any(low <= code <= high for low, high in _EMOJI_RANGES)


@lru_cache(maxsize=1)
def _emoji_font_file() -> Path | None:
    """彩色 emoji 字体文件（不下载）。

    Returns:
        字体路径；没有时为 ``None``。
    """
    if _EMOJI_FONT is not None and _EMOJI_FONT.exists():
        return _EMOJI_FONT
    return None


@lru_cache(maxsize=1)
def _emoji_strike_font() -> Any:  # noqa: ANN401 - PIL FreeTypeFont
    """取 emoji 字体对象（位图字体只有固定档位）。

    CBDT 位图字体按档位给字号，随便传一个数字会报 ``invalid pixel size``，所以先
    试 :data:`EMOJI_STRIKE`，再退几个常见档位。

    Returns:
        Pillow 字体对象；不可用时为 ``None``。
    """
    from PIL import ImageFont

    path = _emoji_font_file()
    if path is None:
        return None
    for size in (EMOJI_STRIKE, 109, 136, 128, 64, 32):
        try:
            return ImageFont.truetype(str(path), size)
        except Exception:  # noqa: BLE001 - 这个档位没有就试下一个
            continue
    return None


def ensure_emoji_font(cache_dir: Path, url: str = "") -> Path | None:
    """确保有一份彩色 emoji 字体，必要时下载到缓存目录。

    失败不影响卡片渲染，只是标题里的 emoji 会退回原来的方块。

    Args:
        cache_dir: 插件缓存目录，下载的字体放在这里。
        url: 自定义字体地址，留空则依次尝试 :data:`EMOJI_FONT_URLS`。

    Returns:
        字体文件路径；不可用时为 ``None``。
    """
    global _EMOJI_FONT  # noqa: PLW0603 - 单例式配置

    if _EMOJI_FONT is not None and _EMOJI_FONT.exists():
        return _EMOJI_FONT

    local = cache_dir / EMOJI_FONT_NAME
    if local.exists() and local.stat().st_size > 200_000:
        if _matches_sources(local, EMOJI_FONT_SOURCES):
            _EMOJI_FONT = local
            _emoji_font_file.cache_clear()
            _emoji_strike_font.cache_clear()
            return local
        local.unlink(missing_ok=True)  # 校验不过就重下

    if url and _fetch_font(FontSource(url, ""), local, 200_000):
        _EMOJI_FONT = local
        _emoji_font_file.cache_clear()
        _emoji_strike_font.cache_clear()
        return local
    for source in EMOJI_FONT_SOURCES:
        if _fetch_font(source, local, 200_000):
            _EMOJI_FONT = local
            _emoji_font_file.cache_clear()
            _emoji_strike_font.cache_clear()
            return local

    logger.warning("[parsehub] 没有可用的 emoji 字体，标题里的 emoji 会是方块")
    return None


# 回退字体：主字体缺字时按顺序往下找（主字体 → 彩色 emoji → DejaVu →
# Noto Sans）。
# 颜文字那类字符（˶ ⚈ Ɛ ˵ ʕ ɞ ฅ）主字体里没有，不补就是豆腐框或空白。
# DejaVu Sans 补符号和 IPA，Noto Sans 补修饰符字母。
DEJAVU_TAG = "v3.9.2"
DEJAVU_SHA256 = (
    "3fdf69cabf06049ea70a00b5919340e2ce1e6d02b0cc3c4b44fb6801bd1e0d22"
)
DEJAVU_PATH = "lib/matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf"
FALLBACK_NOTO_COMMIT = "a85815a42757630ce188fdad368c2dfc444d4773"
FALLBACK_NOTO_SHA256 = (
    "bfb7bb691513f12e734dc346c03a03f784912432d7e3fa8e56efcf906fe86b3d"
)
FALLBACK_NOTO_PATH = "ofl/notosans/NotoSans%5Bwdth,wght%5D.ttf"

# (缓存文件名, 下载源)，顺序就是查找顺序。
FALLBACK_FONTS: tuple[tuple[str, tuple[FontSource, ...]], ...] = (
    (
        "DejaVuSans.ttf",
        (
            FontSource(
                "https://raw.githubusercontent.com/matplotlib/matplotlib/"
                f"{DEJAVU_TAG}/{DEJAVU_PATH}",
                DEJAVU_SHA256,
            ),
            FontSource(
                "https://github.com/matplotlib/matplotlib/raw/"
                f"{DEJAVU_TAG}/{DEJAVU_PATH}",
                DEJAVU_SHA256,
            ),
            FontSource(
                "https://cdn.jsdelivr.net/gh/matplotlib/matplotlib@"
                f"{DEJAVU_TAG}/{DEJAVU_PATH}",
                DEJAVU_SHA256,
            ),
        ),
    ),
    (
        "NotoSans.ttf",
        (
            FontSource(
                "https://raw.githubusercontent.com/google/fonts/"
                f"{FALLBACK_NOTO_COMMIT}/{FALLBACK_NOTO_PATH}",
                FALLBACK_NOTO_SHA256,
            ),
            FontSource(
                "https://github.com/google/fonts/raw/"
                f"{FALLBACK_NOTO_COMMIT}/{FALLBACK_NOTO_PATH}",
                FALLBACK_NOTO_SHA256,
            ),
            FontSource(
                "https://cdn.jsdelivr.net/gh/google/fonts@"
                f"{FALLBACK_NOTO_COMMIT}/{FALLBACK_NOTO_PATH}",
                FALLBACK_NOTO_SHA256,
            ),
        ),
    ),
)

_FALLBACK_FILES: list[Path] = []


@lru_cache(maxsize=16)
def _fallback_fonts(size: int) -> tuple[Any, ...]:
    """按字号取回退字体对象，顺序即查找顺序。

    Args:
        size: 字号（像素）。

    Returns:
        可用的字体对象元组；一个都没有时为空元组。
    """
    from PIL import ImageFont

    fonts: list[Any] = []
    for path in _FALLBACK_FILES:
        try:
            fonts.append(ImageFont.truetype(str(path), size))
        except Exception:  # noqa: BLE001 - 字体坏掉就跳过，不影响出卡
            continue
    return tuple(fonts)


def ensure_fallback_fonts(cache_dir: Path) -> list[Path]:
    """确保回退字体都在缓存里，缺的按 :data:`FALLBACK_FONTS` 下载并校验。

    Args:
        cache_dir: 插件缓存目录。

    Returns:
        可用的字体文件列表；某个下不到就当它不存在（缺字退回方块），不影响出卡。
    """
    global _FALLBACK_FILES  # noqa: PLW0603 - 单例式配置

    # 已经装好了就直接返回，别每出一张卡就把两个字体重新哈希一遍
    if _FALLBACK_FILES and all(path.exists() for path in _FALLBACK_FILES):
        return list(_FALLBACK_FILES)

    files: list[Path] = []
    for name, sources in FALLBACK_FONTS:
        local = cache_dir / name
        if local.exists() and local.stat().st_size > 100_000:
            if not _matches_sources(local, sources):
                local.unlink(missing_ok=True)  # 校验不过就重下，不留着用
        if not (local.exists() and local.stat().st_size > 100_000):
            for source in sources:
                if _fetch_font(source, local, 100_000):
                    break
        if local.exists() and local.stat().st_size > 100_000:
            files.append(local)
        else:
            logger.warning(f"[parsehub] 回退字体 {name} 不可用，缺字会画成方块")
    _FALLBACK_FILES = files
    _fallback_fonts.cache_clear()
    return files


def _has_glyph(font: Any, text: str) -> bool:  # noqa: ANN401 - PIL FreeTypeFont
    """查字体里有没有这段文字的字形。

    用私用区字符当"必然缺字"的参照，跟 ``.notdef`` 的 mask 比"不相等"——不能比
    "非空"：COLR 彩色字体里有字形反而拿到空 mask，而有的普通字体 ``.notdef``
    本身就是空白。

    Args:
        font: 字体对象。
        text: 待查片段。

    Returns:
        有字形为 ``True``。
    """
    key = (id(font), text)
    if key not in _GLYPH_CACHE:
        try:
            missing = bytes(font.getmask("\ue123", mode="L"))
            _GLYPH_CACHE[key] = bytes(font.getmask(text, mode="L")) != missing
        except Exception:  # noqa: BLE001 - 取不到字形就当画不了
            _GLYPH_CACHE[key] = False
    return _GLYPH_CACHE[key]


def _font_runs(
    text: str,
    font: Any,  # noqa: ANN401 - PIL FreeTypeFont
    emoji_size: int = 0,
) -> list[tuple[str, Any]]:
    """把文本切成 (片段, 用哪个字体) 的序列。

    字体为 ``None`` 表示这一段要用彩色 emoji 贴图。逐字符挑第一个有字形的字体：
    主字体 → 彩色 emoji（emoji 码点）→ 回退字体；都没有就还回主字体
    （会画成方块）。

    Args:
        text: 文本。
        font: 主字体对象。
        emoji_size: emoji 字号；0 表示不处理 emoji。

    Returns:
        ``(片段, 字体对象或 None)`` 的列表，相邻同字体的片段已合并。
    """
    size = int(getattr(font, "size", 0) or emoji_size or 0)
    emoji = _emoji_strike_font() if emoji_size else None
    fallbacks = _fallback_fonts(size) if size else ()
    runs: list[tuple[str, Any]] = []
    for chunk, is_emoji in _split_emoji(text):
        if is_emoji and emoji is not None and _has_glyph(emoji, chunk):
            runs.append((chunk, None))
            continue
        for ch in chunk:
            slot = font
            if not ch.isspace() and not _has_glyph(font, ch):
                for candidate in fallbacks:
                    if _has_glyph(candidate, ch):
                        slot = candidate
                        break
            if runs and runs[-1][1] is slot:
                runs[-1] = (runs[-1][0] + ch, slot)
            else:
                runs.append((ch, slot))
    return runs


def _advance(text: str, font: Any, emoji_size: int = 0) -> float:  # noqa: ANN401
    """算一段文字实际画出来有多宽（跟 :func:`draw_text` 的推进保持一致）。

    Args:
        text: 文本。
        font: 主字体对象。
        emoji_size: emoji 字号；0 表示不处理 emoji。

    Returns:
        宽度（像素）。
    """
    total = 0.0
    for chunk, slot in _font_runs(text, font, emoji_size):
        if slot is None:
            tile = _emoji_tile(chunk, emoji_size)
            if tile:
                total += tile[0].width
            else:
                total += font.getlength(chunk)
        else:
            total += slot.getlength(chunk)
    return total


def _can_draw_emoji(font: Any, text: str) -> bool:  # noqa: ANN401 - PIL FreeTypeFont
    """查 emoji 字体里有没有这段文字的字形。

    等价于 :func:`_has_glyph`，保留这个名字是因为 emoji 那条路径一直在用它。

    Args:
        font: emoji 字体对象。
        text: 待画片段。

    Returns:
        有字形为 ``True``。
    """
    return _has_glyph(font, text)


def _split_emoji(text: str) -> list[tuple[str, bool]]:
    """把文本切成 (片段, 是否 emoji) 的序列。

    Args:
        text: 原始文本。

    Returns:
        片段列表，相邻同类字符合并，组合序列（ZWJ、变体符）不会被切开。
    """
    out: list[tuple[str, bool]] = []
    buf = ""
    flag: bool | None = None
    for ch in text:
        cur = _is_emoji_char(ch)
        if flag is None or cur == flag:
            buf += ch
        else:
            out.append((buf, flag))
            buf = ch
        flag = cur
    if buf and flag is not None:
        out.append((buf, flag))
    return out


@lru_cache(maxsize=256)
@lru_cache(maxsize=256)
def _emoji_tile(chunk: str, size: int) -> tuple[Any, int, int]:  # noqa: ANN401
    """把一段 emoji 画在透明小图上，缩到目标大小。

    单独画一个 emoji 时 Pillow 不会上色（只有整串都是 emoji 时才有这个毛病），
    所以左边垫一个空格当"陪衬"，再把它画到裁剪区外面去。

    Args:
        chunk: emoji 片段。
        size: 正文字号。

    Returns:
        ``(透明小图, 基线在图中 y, 墨迹左偏移)``；字体不可用时为空元组。
    """
    font = _emoji_strike_font()
    if font is None:
        return ()
    from PIL import Image, ImageDraw

    advance = max(1, int(round(font.getlength(chunk))))
    ascent, descent = font.getmetrics()
    baseline = ascent + 2
    pad = font.getlength(" ") or advance
    tile = Image.new("RGBA", (advance + 8, ascent + descent + 8), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text(
        (-pad, baseline), " " + chunk, font=font, fill=(255, 255, 255),
        anchor="ls", embedded_color=True,
    )
    box = tile.getbbox()
    if box is None:
        return ()
    art = tile.crop(box)
    # 位图只有一档，缩到目标高度（宽度按比例）
    target_h = max(1, int(round(size * EMOJI_RATIO)))
    scale = target_h / art.height
    art = art.resize(
        (max(1, int(round(art.width * scale))), target_h), Image.LANCZOS
    )
    return art, target_h - int(round(size * EMOJI_DROP)), 0


def draw_text(
    draw: Any,  # noqa: ANN401 - PIL ImageDraw
    xy: tuple[int, int],
    text: str,
    font: Any,  # noqa: ANN401 - PIL FreeTypeFont
    fill: tuple[int, int, int],
    emoji_size: int = 0,
) -> None:
    """画一行文字：emoji 段用彩色字体贴上去，主字体缺的字用回退字体补。

    字体链见 :func:`_font_runs`。没有 emoji 字体、也没有回退字体时退化成普通的
    ``draw.text``（缺字会显示成方块）。

    Args:
        draw: ImageDraw 对象。
        xy: 起始坐标（左上角，锚点同 ``draw.text`` 的默认值）。
        text: 文本。
        font: 正文字体。
        fill: 颜色。
        emoji_size: emoji 字号；0 表示不做 emoji 处理。
    """
    x, top = xy
    baseline = top + font.getmetrics()[0]
    for chunk, slot in _font_runs(text, font, emoji_size):
        if slot is None:
            tile = _emoji_tile(chunk, emoji_size)
            if tile:
                image, baseline_in_tile, left = tile
                image_draw = getattr(draw, "_image", None)
                if image_draw is None:  # pragma: no cover - ImageDraw 都有
                    image_draw = draw.im
                image_draw.paste(
                    image,
                    (int(x - left), int(baseline - baseline_in_tile)),
                    image,
                )
                x += _advance(chunk, font, emoji_size)
                continue
            slot = font  # 贴图失败就按普通文字画，至少不丢字
        draw.text((x, top), chunk, font=slot, fill=fill)
        x += slot.getlength(chunk)


def set_font(path: Path) -> None:
    """指定字体文件并清掉字体缓存。

    Args:
        path: 字体文件路径。
    """
    global _CUSTOM_FONT  # noqa: PLW0603 - 单例式配置
    _CUSTOM_FONT = path
    _font_file.cache_clear()
    _font.cache_clear()


def ensure_font(cache_dir: Path, url: str = "") -> Path | None:
    """确保有一份可用字体，必要时下载到缓存目录。

    Args:
        cache_dir: 插件缓存目录，下载的字体放在这里。
        url: 自定义字体地址，留空则依次尝试 :data:`FONT_URLS`。

    Returns:
        字体文件路径；不可用时为 ``None``。
    """
    global _CUSTOM_FONT  # noqa: PLW0603 - 单例式配置

    if _CUSTOM_FONT is not None and _CUSTOM_FONT.exists():
        return _CUSTOM_FONT

    local = cache_dir / FONT_NAME
    if local.exists() and local.stat().st_size > 1_000_000:
        if _matches_sources(local, FONT_SOURCES):
            _CUSTOM_FONT = local
            _font_file.cache_clear()
            _font.cache_clear()
            return local
        local.unlink(missing_ok=True)  # 校验不过就重下，不留着用

    system = _font_file()
    if system is not None:
        return system

    if url:
        # 自定义地址没法预先知道 sha256，只能下下来就用；出问题由调用方负责。
        if _fetch_font(FontSource(url, ""), local, 1_000_000):
            _CUSTOM_FONT = local
            _font_file.cache_clear()
            _font.cache_clear()
            return local
    for source in FONT_SOURCES:
        if _fetch_font(source, local, 1_000_000):
            _CUSTOM_FONT = local
            _font_file.cache_clear()
            _font.cache_clear()
            return local
    logger.warning("[parsehub] 没有可用字体，卡片功能关闭")
    return None


def _flat(text: str) -> str:
    """去掉所有空白，用来比较有没有漏字。

    Args:
        text: 原始文本。

    Returns:
        去掉空白后的文本。
    """
    return "".join(text.split())


def wrap(
    text: str,
    font: Any,  # noqa: ANN401 - PIL FreeTypeFont
    max_w: int,
    max_lines: int,
    emoji_size: int = 0,
) -> list[str]:
    """按"词"折行，话题标签和网址不会被从中间劈开。

    Args:
        text: 原始文本。
        font: 字体对象。
        max_w: 每行最大像素宽度。
        max_lines: 最多行数，超出时最后一行加省略号。
        emoji_size: emoji / 回退字体用的字号；0 表示按主字体算宽度。

    Returns:
        折行后的文本列表。
    """
    import re

    def width(part: str) -> float:
        return _advance(part, font, emoji_size)

    lines: list[str] = []
    for para in text.split("\n"):
        if not para:
            continue
        cur = ""
        for token in re.findall(r"\S+\s*", para):
            if width(cur + token) <= max_w:
                cur += token
                continue
            if cur.strip():
                lines.append(cur.rstrip())
                cur = ""
                if len(lines) == max_lines:
                    break
            if width(token) > max_w:  # 单个词就超宽，只能按字符切
                piece = ""
                for char in token:
                    if width(piece + char) <= max_w:
                        piece += char
                    else:
                        lines.append(piece)
                        piece = char
                        if len(lines) == max_lines:
                            break
                cur = piece
            else:
                cur = token
            if len(lines) == max_lines:
                break
        if len(lines) == max_lines:
            break
        if cur.strip():
            lines.append(cur.rstrip())
    lines = lines[:max_lines]
    # 行尾空格会被 rstrip 掉，比较时先去空白，否则会误判成"被截断了"
    if lines and len(_flat("".join(lines))) < len(_flat(text)):
        lines[-1] = lines[-1][: max(1, len(lines[-1]) - 1)] + "…"
    return lines


def fit_title(
    title: str,
    font: Any,  # noqa: ANN401 - PIL FreeTypeFont
    max_w: int,
    max_lines: int,
    emoji_size: int = 0,
) -> list[str]:
    """标题折行；末尾的话题标签放不下就整条丢掉，不从中间截断。

    Args:
        title: 标题。
        font: 字体对象。
        max_w: 每行最大像素宽度。
        max_lines: 最多行数。
        emoji_size: emoji / 回退字体用的字号；0 表示按主字体算宽度。

    Returns:
        折行后的标题。
    """
    lines = wrap(title, font, max_w, max_lines, emoji_size)
    if len(_flat("".join(lines).rstrip("…"))) >= len(_flat(title)):
        return lines
    tokens = title.split(" ")
    while len(tokens) > 1 and tokens[-1].startswith("#"):
        tokens.pop()
        trial = " ".join(tokens)
        trial_lines = wrap(trial, font, max_w, max_lines, emoji_size)
        if len(_flat("".join(trial_lines).rstrip("…"))) >= len(_flat(trial)):
            return trial_lines
    return lines


def format_duration(seconds: int, *, is_ms: bool = False) -> str:
    """把时长格式化成 ``mm:ss`` / ``h:mm:ss``。

    Args:
        seconds: 时长。
        is_ms: 传进来的值是不是毫秒（抖音、TikTok 是毫秒）。

    Returns:
        格式化后的时长；无法解析时返回空串。
    """
    if seconds <= 0:
        return ""
    if is_ms or seconds > 10800:
        seconds //= 1000
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def platform_uses_milliseconds(result: Any) -> bool:  # noqa: ANN401
    """该平台的 ``duration`` 是不是毫秒。

    Args:
        result: ParseHub 解析结果。

    Returns:
        抖音 / 小红书返回 True。
    """
    platform_id = str(getattr(getattr(result, "platform", None), "id", ""))
    return platform_id.lower() in _MS_PLATFORMS


def video_seconds(result: Any) -> int:  # noqa: ANN401
    """取结果里最长媒体的时长（秒）。

    上游单位不统一：毫秒（抖音 / 小红书）要除 1000；另外把明显过大的
    值（> 3 小时）也当毫秒处理，与 :func:`format_duration` 的显示规则一致。
    拿不到时长时返回 0，调用方据此跳过时长判断。

    Args:
        result: ParseHub 解析结果。

    Returns:
        最长媒体的时长（秒）；无法判断时返回 0。
    """
    media = getattr(result, "media", None)
    items = [
        item
        for item in (
            list(media) if isinstance(media, (list, tuple)) else [media]
        )
        if item
    ]
    longest = 0
    for item in items:
        try:
            value = int(getattr(item, "duration", 0) or 0)
        except (TypeError, ValueError):
            continue
        longest = max(longest, value)
    if longest <= 0:
        return 0
    if platform_uses_milliseconds(result) or longest > 10800:
        return longest // 1000
    return longest


def platform_of(result: Any) -> tuple[str, tuple[int, int, int], str]:  # noqa: ANN401
    """取平台展示名、品牌色和 logo 名。

    Args:
        result: ParseHub 解析结果。

    Returns:
        ``(展示名, 品牌色, logo 文件名)``。
    """
    platform = getattr(result, "platform", None)
    key = str(getattr(platform, "id", "") or "").lower()
    if key in PLATFORMS:
        return PLATFORMS[key]
    name = str(getattr(platform, "display_name", "") or key or "未知平台")
    return name, (150, 150, 150), "x"


def meta_line(result: Any, media_count: int, is_ms: bool) -> str:  # noqa: ANN401
    """拼装元信息行（类型 · 时长 · 媒体数）。

    Args:
        result: ParseHub 解析结果。
        media_count: 媒体数量。
        is_ms: 时长单位是不是毫秒。

    Returns:
        形如 ``视频 · 00:33`` 的一行文本。
    """
    post_type = getattr(getattr(result, "type", None), "value", "")
    parts = [TYPE_LABELS.get(str(post_type), "媒体")]
    media = getattr(result, "media", None)
    items = list(media) if isinstance(media, (list, tuple)) else [media]
    first = next((item for item in items if item), None)
    duration = format_duration(
        int(getattr(first, "duration", 0) or 0), is_ms=is_ms
    )
    if duration:
        parts.append(duration)
    if media_count > 1:
        parts.append(f"{media_count} 个媒体")
    return " · ".join(parts)


def _is_placeholder(data: bytes, image: Any) -> bool:  # noqa: ANN401 - PIL Image
    """判断封面是不是纯色占位图。

    Args:
        data: 原始图片字节。
        image: 已解码的 RGB 图像。

    Returns:
        像占位图时为 ``True``。
    """
    if len(data) < 24_000:
        return True
    colors = image.resize((16, 16)).getcolors(16 * 16) or []
    return bool(colors) and max(count for count, _ in colors) > 16 * 16 * 0.9


def _frame_from_video(path: Path) -> bytes | None:
    """从本地视频里抽第一帧当封面。

    Args:
        path: 本地视频路径。

    Returns:
        JPEG 字节；抽不出来时为 ``None``。
    """
    try:
        import cv2

        Image = _pil()  # noqa: N806 - 与 PIL 模块名保持一致
    except ImportError:
        return None

    cap = cv2.VideoCapture(str(path))
    try:
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok:
        return None
    buffer = io.BytesIO()
    Image.fromarray(frame[:, :, ::-1]).save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def fetch_cover(  # noqa: ANN401
    result: Any,
    local_files: list[Path],
    proxy: str = "",
) -> bytes | None:
    """取封面图，三级兜底。

    顺序：``thumb_url`` → 本身就是图片的媒体 → 本地视频抽首帧。

    Args:
        result: ParseHub 解析结果。
        local_files: 已下载到本地的媒体文件。
        proxy: 取封面用的代理；海外平台（如 TikTok）的封面 CDN 直连会超时。

    Returns:
        JPEG/PNG 字节；都拿不到时为 ``None``。
    """
    Image = _pil()  # noqa: N806 - 与 PIL 模块名保持一致
    media = getattr(result, "media", None)
    items = [
        item
        for item in (
            list(media) if isinstance(media, (list, tuple)) else [media]
        )
        if item
    ]
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        with httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers=headers,
            proxy=proxy or None,
        ) as client:
            for item in items:
                url = getattr(item, "thumb_url", None)
                if not url:
                    continue
                resp = client.get(url)
                if resp.status_code != 200 or len(resp.content) < 1024:
                    continue
                try:
                    image = Image.open(io.BytesIO(resp.content)).convert("RGB")
                except Exception:  # noqa: BLE001 - 不是图片就换下一个来源
                    continue
                if not _is_placeholder(resp.content, image):
                    return resp.content
            for item in items:
                if getattr(item, "ext", "") not in (
                    "jpg",
                    "jpeg",
                    "png",
                    "webp",
                ):
                    continue
                resp = client.get(getattr(item, "url", ""))
                if resp.status_code == 200 and len(resp.content) > 24_000:
                    return resp.content
    except Exception as exc:  # noqa: BLE001 - 网络问题都算取封面失败
        logger.warning(f"[parsehub] 取封面失败: {type(exc).__name__}: {exc}")

    for path in local_files:
        if path.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm"):
            frame = _frame_from_video(path)
            if frame:
                return frame
        elif path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            try:
                return path.read_bytes()
            except OSError:
                continue
    return None


def _vertical_mask(
    size: tuple[int, int],
    start: int,
    end: int,
    *,
    peak: int = 255,
    floor: int = 0,
    curve: float = 1.0,
) -> Any:  # noqa: ANN401 - PIL Image
    """用 numpy 一次算出竖直渐变遮罩。

    逐行画线在 armv7 上会长时间占着 GIL，放线程里也会把事件循环卡住。

    Args:
        size: 遮罩尺寸 ``(宽, 高)``。
        start: 开始变深的位置。
        end: 达到 ``peak`` 的位置。
        peak: 最大不透明度。
        floor: 起始不透明度。
        curve: 曲线指数，越大起步越缓。

    Returns:
        ``L`` 模式的遮罩图。
    """
    import numpy as np
    from PIL import Image

    width, height = size
    ys = np.arange(height, dtype=np.float32)
    span = max(1, end - start)
    ramp = np.clip((ys - start) / span, 0.0, 1.0)
    alpha = np.where(
        ys < start, 0, floor + (peak - floor) * ramp**curve
    ).astype(np.uint8)
    return Image.fromarray(alpha.reshape(-1, 1), mode="L").resize(
        (width, height), Image.BILINEAR
    )


@lru_cache(maxsize=32)
def _logo(slug: str, height: int) -> Any:  # noqa: ANN401 - PIL Image
    """读 logo 并缩放（带缓存）。

    Args:
        slug: logo 文件名（不含扩展名）。
        height: 目标高度。

    Returns:
        RGBA 图像；文件不存在时用通用占位。
    """
    from PIL import Image

    path = ASSET_DIR / f"{slug}.png"
    if not path.exists():
        path = ASSET_DIR / "x.png"
    logo = Image.open(path).convert("RGBA")
    if logo.getchannel("A").getextrema() == (255, 255):
        # 抖音的 favicon 是黑底不透明 PNG，不抠掉黑底就是个白方块
        pixels = logo.load()
        for y in range(logo.height):
            for x in range(logo.width):
                red, green, blue, _ = pixels[x, y]
                pixels[x, y] = (
                    red,
                    green,
                    blue,
                    0 if max(red, green, blue) < 40 else 255,
                )
    box = logo.getbbox()
    if box:
        logo = logo.crop(box)
    width = max(1, int(logo.width * height / logo.height))
    return logo.resize((width, height), Image.LANCZOS)


def _is_dark(logo: Any) -> bool:  # noqa: ANN401 - PIL Image
    """判断 logo 是否要转成白色剪影。

    只有「偏暗且基本没有颜色」的标才转白：X、Threads、Medium 这类纯黑标在深色
    封面上会消失，必须转白；而抖音的青红、微博的红、YouTube 的红这些带品牌色的
    标，转白会把品牌色抹掉，所以只要颜色够多就保留原色。

    Args:
        logo: logo 图像。

    Returns:
        需要转白时为 ``True``。
    """
    pixels = logo.load()
    total = count = colorful = 0
    for y in range(0, logo.height, max(1, logo.height // 24)):
        for x in range(0, logo.width, max(1, logo.width // 24)):
            red, green, blue, alpha = pixels[x, y]
            if alpha > 128:
                total += 0.299 * red + 0.587 * green + 0.114 * blue
                if max(red, green, blue) - min(red, green, blue) > 40:
                    colorful += 1
                count += 1
    if count == 0:
        return False
    return total / count < 110 and colorful / count < 0.2


def _white(logo: Any) -> Any:  # noqa: ANN401 - PIL Image
    """把 logo 的 alpha 通道填成白色。

    Args:
        logo: logo 图像。

    Returns:
        白色剪影。
    """
    from PIL import Image

    white = Image.new("RGBA", logo.size, (255, 255, 255, 0))
    white.putalpha(logo.getchannel("A"))
    return white


@lru_cache(maxsize=32)
def _shadowed_logo(slug: str, height: int) -> Any:  # noqa: ANN401 - PIL Image
    """Logo 转白（必要时）并加投影，结果缓存。

    PIL 的 Image 不可哈希，所以按 ``slug + 高度`` 缓存。

    Args:
        slug: logo 文件名。
        height: 目标高度。

    Returns:
        带投影的 RGBA 图像。
    """
    from PIL import Image, ImageFilter

    logo = _logo(slug, height)
    if _is_dark(logo):
        logo = _white(logo)
    blur, offset, alpha = 7, 2, 205
    pad = blur * 2
    canvas = Image.new(
        "RGBA", (logo.width + pad * 2, logo.height + pad * 2), (0, 0, 0, 0)
    )
    shadow = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    shadow.putalpha(logo.getchannel("A").point(lambda v: int(v * alpha / 255)))
    canvas.alpha_composite(shadow, (pad + offset, pad + offset))
    canvas = canvas.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(logo, (pad, pad))
    return canvas


def render(
    result: Any,  # noqa: ANN401 - ParseHub 解析结果
    cover: bytes,
    out_path: Path,
) -> bool:
    """把解析结果渲染成卡片图。

    同步阻塞，调用方必须放进线程池。

    Args:
        result: ParseHub 解析结果。
        cover: 封面图字节。
        out_path: 输出文件路径（JPEG）。

    Returns:
        渲染成功为 ``True``。
    """
    from PIL import Image, ImageDraw

    name, _color, slug = platform_of(result)
    media = getattr(result, "media", None)
    items = [
        item
        for item in (
            list(media) if isinstance(media, (list, tuple)) else [media]
        )
        if item
    ]
    is_ms = platform_uses_milliseconds(result)

    cover_image = Image.open(io.BytesIO(cover)).convert("RGB")

    ratio = min(
        max(cover_image.height / cover_image.width, MIN_RATIO), MAX_RATIO
    )
    card_w = WIDTH
    card_h = int(card_w * ratio)

    scale = max(card_w / cover_image.width, card_h / cover_image.height)
    big = cover_image.resize(
        (int(cover_image.width * scale), int(cover_image.height * scale)),
        Image.BICUBIC,  # LANCZOS 在 armv7 上要 150ms+
    )
    left = (big.width - card_w) // 2
    top = int((big.height - card_h) * 0.28)
    pic = big.crop((left, top, left + card_w, top + card_h))

    title_font = _font(TITLE_SIZE, 700)
    meta_font = _font(META_SIZE, 400)
    kicker_font = _font(KICKER_SIZE, 600)
    title_lines = fit_title(
        getattr(result, "title", "") or "(无标题)",
        title_font,
        card_w - INSET * 2,
        3,
        emoji_size=TITLE_SIZE,
    )
    meta = meta_line(result, len(items), is_ms)

    # 渐变要保证文字所在的位置已经够暗：文字上方到 200/255，再往下到 252/255
    text_top = card_h - INSET - len(title_lines) * 70 - 8 - 34 - 50
    start = max(0, int(card_h * FADE_START))
    ramp_end = max(start + 1, text_top)
    upper = _vertical_mask(
        (card_w, ramp_end), start, ramp_end, peak=FADE_PEAK, curve=1.25
    )
    lower = _vertical_mask(
        (card_w, card_h - ramp_end),
        0,
        card_h - ramp_end,
        peak=FADE_BOTTOM,
        floor=FADE_PEAK,
    )
    layer = Image.new("RGB", (card_w, ramp_end), FADE_COLOR)
    pic.paste(layer, (0, 0), upper)
    layer = Image.new("RGB", (card_w, card_h - ramp_end), FADE_COLOR)
    pic.paste(layer, (0, ramp_end), lower)

    canvas = pic.convert("RGBA")
    logo = _shadowed_logo(slug, LOGO_HEIGHT)
    pad = logo.width - _logo(slug, LOGO_HEIGHT).width
    mark_w = logo.width - pad
    mark_h = logo.height - pad

    y = card_h - INSET - len(title_lines) * 70 - 8 - 34
    kicker_y = y - 44
    # 图标与「平台名」那一行垂直居中，左边缘贴 INSET
    canvas.alpha_composite(
        logo,
        (
            INSET - pad // 2,
            kicker_y + KICKER_SIZE // 2 - mark_h // 2 - pad // 2,
        ),
    )

    draw = ImageDraw.Draw(canvas)
    draw_text(
        draw,
        (INSET + mark_w + 18, kicker_y),
        name.upper(),
        kicker_font,
        (226, 226, 232),
        emoji_size=KICKER_SIZE,
    )
    for line in title_lines:
        draw_text(draw, (INSET, y), line, title_font, (255, 255, 255),
                  emoji_size=TITLE_SIZE)
        y += 70
    draw_text(draw, (INSET, y + 8), meta, meta_font, (214, 214, 222),
              emoji_size=META_SIZE)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, "JPEG", quality=92)
    return True


def available() -> bool:
    """卡片功能是否可用（有字体、有 Pillow）。

    Returns:
        可用时为 ``True``。
    """
    try:
        _font(TITLE_SIZE, 700)
    except Exception:  # noqa: BLE001 - 缺 Pillow 或缺字体都算不可用
        return False
    return True
