# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""合成层：渐变遮罩与平台 logo 的处理。

卡片里所有「图像加工」都在这里：竖直渐变遮罩、logo 的抠底 / 转白 / 投影。
全部同步阻塞，调用方放进线程池。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

# logo 资源在包根目录的 assets/logos 下（card 包比原单文件深一层）。
ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "logos"


def vertical_mask(
    size: tuple[int, int],
    start: int,
    end: int,
    *,
    peak: int = 255,
    floor: int = 0,
    curve: float = 1.0,
) -> Any:  # PIL Image
    """用 numpy 一次算出竖直渐变遮罩。

    逐行画线会长时间占着 GIL，放线程里也会把事件循环卡住，所以整张图一次性
    拼好再输出。

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
def logo(slug: str, height: int) -> Any:  # PIL Image
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
    logo_image = Image.open(path).convert("RGBA")
    if logo_image.getchannel("A").getextrema() == (255, 255):
        # 抖音的 favicon 是黑底不透明 PNG，不抠掉黑底就是个白方块
        pixels = logo_image.load()
        for y in range(logo_image.height):
            for x in range(logo_image.width):
                red, green, blue, _ = pixels[x, y]
                pixels[x, y] = (
                    red,
                    green,
                    blue,
                    0 if max(red, green, blue) < 40 else 255,
                )
    box = logo_image.getbbox()
    if box:
        logo_image = logo_image.crop(box)
    width = max(1, int(logo_image.width * height / logo_image.height))
    return logo_image.resize((width, height), Image.LANCZOS)


def is_dark(image: Any) -> bool:  # PIL Image
    """判断 logo 是否要转成白色剪影。

    只有「偏暗且基本没有颜色」的标才转白：X、Threads 这类纯黑标在深色
    封面上会消失，必须转白；而抖音的青红、微博的红、YouTube 的红这些带品牌色的
    标，转白会把品牌色抹掉，所以只要颜色够多就保留原色。

    Args:
        image: logo 图像。

    Returns:
        需要转白时为 ``True``。
    """
    pixels = image.load()
    total = count = colorful = 0
    for y in range(0, image.height, max(1, image.height // 24)):
        for x in range(0, image.width, max(1, image.width // 24)):
            red, green, blue, alpha = pixels[x, y]
            if alpha > 128:
                total += 0.299 * red + 0.587 * green + 0.114 * blue
                if max(red, green, blue) - min(red, green, blue) > 40:
                    colorful += 1
                count += 1
    if count == 0:
        return False
    return total / count < 110 and colorful / count < 0.2


def whiten(image: Any) -> Any:  # PIL Image
    """把 logo 的 alpha 通道填成白色。

    Args:
        image: logo 图像。

    Returns:
        白色剪影。
    """
    from PIL import Image

    silhouette = Image.new("RGBA", image.size, (255, 255, 255, 0))
    silhouette.putalpha(image.getchannel("A"))
    return silhouette


@lru_cache(maxsize=32)
def shadowed_logo(slug: str, height: int) -> Any:  # PIL Image
    """Logo 转白（必要时）并加投影，结果缓存。

    PIL 的 Image 不可哈希，所以按 ``slug + 高度`` 缓存。

    Args:
        slug: logo 文件名。
        height: 目标高度。

    Returns:
        带投影的 RGBA 图像。
    """
    from PIL import Image, ImageFilter

    image = logo(slug, height)
    if is_dark(image):
        image = whiten(image)
    blur, offset, alpha = 7, 2, 205
    pad = blur * 2
    canvas = Image.new(
        "RGBA", (image.width + pad * 2, image.height + pad * 2), (0, 0, 0, 0)
    )
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow.putalpha(image.getchannel("A").point(lambda v: int(v * alpha / 255)))
    canvas.alpha_composite(shadow, (pad + offset, pad + offset))
    canvas = canvas.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(image, (pad, pad))
    return canvas
