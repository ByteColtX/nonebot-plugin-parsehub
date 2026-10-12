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

import io
import logging
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

# 中文字体候选下载地址（按顺序尝试）。
FONT_URLS: tuple[str, ...] = (
    "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/notosanssc/"
    "NotoSansSC%5Bwght%5D.ttf",
    "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/"
    "NotoSansSC%5Bwght%5D.ttf",
    "https://github.com/google/fonts/raw/main/ofl/notosanssc/"
    "NotoSansSC%5Bwght%5D.ttf",
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

    local = cache_dir / "NotoSansSC.ttf"
    if local.exists() and local.stat().st_size > 1_000_000:
        _CUSTOM_FONT = local
        _font_file.cache_clear()
        _font.cache_clear()
        return local

    system = _font_file()
    if system is not None:
        return system

    for candidate in ([url] if url else []) + list(FONT_URLS):
        if not candidate:
            continue
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with httpx.stream(
                "GET", candidate, timeout=120, follow_redirects=True
            ) as resp:
                if resp.status_code != 200:
                    continue
                tmp = local.with_suffix(".part")
                with tmp.open("wb") as handle:
                    for chunk in resp.iter_bytes(1 << 16):
                        handle.write(chunk)
            if tmp.stat().st_size < 1_000_000:
                tmp.unlink(missing_ok=True)
                continue
            tmp.replace(local)
        except Exception as exc:  # noqa: BLE001 - 网络问题都算下载失败
            logger.warning(f"[parsehub] 字体下载失败 {candidate}: {exc}")
            continue
        logger.info(f"[parsehub] 字体已下载 {local}")
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
) -> list[str]:
    """按"词"折行，话题标签和网址不会被从中间劈开。

    Args:
        text: 原始文本。
        font: 字体对象。
        max_w: 每行最大像素宽度。
        max_lines: 最多行数，超出时最后一行加省略号。

    Returns:
        折行后的文本列表。
    """
    import re

    lines: list[str] = []
    for para in text.split("\n"):
        if not para:
            continue
        cur = ""
        for token in re.findall(r"\S+\s*", para):
            if font.getlength(cur + token) <= max_w:
                cur += token
                continue
            if cur.strip():
                lines.append(cur.rstrip())
                cur = ""
                if len(lines) == max_lines:
                    break
            if font.getlength(token) > max_w:  # 单个词就超宽，只能按字符切
                piece = ""
                for char in token:
                    if font.getlength(piece + char) <= max_w:
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
) -> list[str]:
    """标题折行；末尾的话题标签放不下就整条丢掉，不从中间截断。

    Args:
        title: 标题。
        font: 字体对象。
        max_w: 每行最大像素宽度。
        max_lines: 最多行数。

    Returns:
        折行后的标题。
    """
    lines = wrap(title, font, max_w, max_lines)
    if len(_flat("".join(lines).rstrip("…"))) >= len(_flat(title)):
        return lines
    tokens = title.split(" ")
    while len(tokens) > 1 and tokens[-1].startswith("#"):
        tokens.pop()
        trial = " ".join(tokens)
        trial_lines = wrap(trial, font, max_w, max_lines)
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


def fetch_cover(result: Any, local_files: list[Path]) -> bytes | None:  # noqa: ANN401
    """取封面图，三级兜底。

    顺序：``thumb_url`` → 本身就是图片的媒体 → 本地视频抽首帧。

    Args:
        result: ParseHub 解析结果。
        local_files: 已下载到本地的媒体文件。

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
            timeout=30, follow_redirects=True, headers=headers
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
    draw.text(
        (INSET + mark_w + 18, kicker_y),
        name.upper(),
        font=kicker_font,
        fill=(226, 226, 232),
    )
    for line in title_lines:
        draw.text((INSET, y), line, font=title_font, fill=(255, 255, 255))
        y += 70
    draw.text((INSET, y + 8), meta, font=meta_font, fill=(214, 214, 222))

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
