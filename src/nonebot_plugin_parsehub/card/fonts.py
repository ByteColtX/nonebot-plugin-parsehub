# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""字体获取、校验与回退。

中文字体、彩色 emoji 字体、回退字体都在这里：下载源钉在固定提交上并做 sha256
校验，拿不到就降级（没有 emoji 字体只是 emoji 变方块，不影响出卡）。

模块只在需要时 import Pillow，缺 Pillow 时由调用方处理 ImportError。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from nonebot import logger

_EMOJI_FONT: Path | None = None
_GLYPH_CACHE: dict[tuple[int, str], bool] = {}


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


# 结果类型 → 中文标签
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


# 彩色 emoji 字体：Twemoji 的 TrueType（CBDT 位图）构建，3.2MB。
# 用这个而不是 Twemoji Mozilla：后者是 COLR 彩色字体，FreeType 按基字形的 bbox
# 去裁彩色图层，超出部分被切掉（Pillow issue #9146），emoji 下半截连嘴巴不见。
# 这份是社区从 Twemoji 素材构建的位图版本（上游图形 CC-BY 4.0），渲染正常。
# 注意：release 资产被重传过（AUR 记的哈希与实测不符），所以标签和哈希都钉。
TWEMOJI_TTF_TAG = "15.0.2"
TWEMOJI_TTF_SHA256 = (
    "2c96ffb14e104ebbc49a7b35303828f6131cf2752034dd79e2050d8795785a60"
)
EMOJI_FONT_SOURCES: tuple[FontSource, ...] = (
    FontSource(
        "https://github.com/JoeBlakeB/ttf-twemoji-aur/releases/download/"
        f"{TWEMOJI_TTF_TAG}/Twemoji-{TWEMOJI_TTF_TAG}.ttf",
        TWEMOJI_TTF_SHA256,
    ),
    # release 资产只有这一个正式地址（jsdelivr 只镜像仓库文件，镜像不了
    # release），所以只有一条源；下不到时按既有逻辑退回「没有 emoji 字体」
)
EMOJI_FONT_NAME = "Twemoji-15.0.2.ttf"


# CBDT 位图字体的档位由 _emoji_strike_font 动态探测（这份 Twemoji 是 61px），
# 画别的字号必须先按档位画再缩
# emoji 贴图高度占字号多少（中文墨迹大约 0.9 个字号高）
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

    CBDT 位图字体按档位给字号，随便传一个数字会报 ``invalid pixel size``，所以从
    小到大探一遍，第一个能开的就是它的档位。

    Returns:
        Pillow 字体对象；不可用时为 ``None``。
    """
    from PIL import ImageFont

    path = _emoji_font_file()
    if path is None:
        return None
    # 矢量字体任何字号都能开；位图字体只认它自带的那几档，得探一遍
    for size in range(8, 300):
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
        url: 自定义字体地址，留空则依次尝试 :data:`EMOJI_FONT_SOURCES`。

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
        url: 自定义字体地址，留空则依次尝试 :data:`FONT_SOURCES`。

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
