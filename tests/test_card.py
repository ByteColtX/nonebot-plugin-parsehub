# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""卡片渲染模块的离线测试。

字体用 ``tests/assets/test-font.ttf``（Noto Sans SC 的子集），所以不依赖系统字体。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

FONT = Path(__file__).resolve().parent / "assets" / "test-font.ttf"


class FakePlatform:
    """假平台对象。"""

    def __init__(self, platform_id: str, display_name: str = "") -> None:
        self.id = platform_id
        self.display_name = display_name or platform_id


class FakeRef:
    """假媒体引用。"""

    def __init__(self, **kwargs: object) -> None:
        self.url = kwargs.get("url", "")
        self.ext = kwargs.get("ext", "mp4")
        self.thumb_url = kwargs.get("thumb_url")
        self.width = kwargs.get("width", 1080)
        self.height = kwargs.get("height", 1920)
        self.duration = kwargs.get("duration", 0)


class FakeType:
    """假 PostType。"""

    def __init__(self, value: str) -> None:
        self.value = value


class FakeResult:
    """假解析结果。"""

    def __init__(
        self,
        *,
        platform_id: str = "bilibili",
        title: str = "好女孩刻猫猫",
        content: str = "",
        media: object = None,
        post_type: str = "video",
    ) -> None:
        self.platform = FakePlatform(platform_id)
        self.title = title
        self.content = content
        self.media = media if media is not None else FakeRef()
        self.type = FakeType(post_type)


@pytest.fixture(autouse=True)
def _use_test_font(card) -> None:
    """所有测试都用子集字体。"""
    card.set_font(FONT)


def test_format_duration_seconds(card) -> None:
    """秒为单位的平台正常格式化。"""
    assert card.format_duration(33) == "00:33"
    assert card.format_duration(3725) == "1:02:05"
    assert card.format_duration(0) == ""


def test_format_duration_milliseconds(card) -> None:
    """抖音、TikTok 的毫秒要换算，否则 6 秒会显示成 1:42:14。"""
    assert card.format_duration(6134, is_ms=True) == "00:06"
    assert card.format_duration(11900, is_ms=True) == "00:11"


def test_meta_line_video(card) -> None:
    """视频类元信息：类型 · 时长。"""
    result = FakeResult(media=FakeRef(duration=33))
    assert card.meta_line(result, 1, False) == "视频 · 00:33"


def test_meta_line_image_with_count(card) -> None:
    """图文帖：类型 · 媒体数，没有时长。"""
    result = FakeResult(
        media=[FakeRef(ext="jpg"), FakeRef(ext="jpg"), FakeRef(ext="jpg")],
        post_type="image",
    )
    assert card.meta_line(result, 3, False) == "图集 · 3 个媒体"


def test_platform_of_known_and_unknown(card) -> None:
    """已知平台给品牌色，未知平台退化成灰色。"""
    name, color, slug = card.platform_of(FakeResult(platform_id="douyin"))
    assert (name, slug) == ("抖音", "douyin")
    assert color == (254, 44, 85)

    name, color, slug = card.platform_of(FakeResult(platform_id="unknown-x"))
    assert color == (150, 150, 150)
    assert slug == "x"


def test_wrap_keeps_hashtag_whole(card) -> None:
    """话题标签不会被从中间劈开。"""
    font = card._font(58, 700)
    title = "泳装小鸟是对的 #知更鸟晴歌 #崩坏星穹铁道 #知更鸟"
    lines = card.wrap(title, font, 900, 3)
    assert len(lines) >= 2
    for line in lines[:-1]:
        assert not line.endswith("#")
    assert all(
        line.strip().startswith("#") or "#" not in line[:1] for line in lines
    )


def test_fit_title_drops_trailing_tags(card) -> None:
    """放不下时整条丢掉末尾标签，而不是截成 #知更…。"""
    font = card._font(58, 700)
    title = "泳装小鸟是对的 #知更鸟晴歌 #崩坏星穹铁道 #知更鸟"
    lines = card.fit_title(title, font, 700, 2)
    joined = "".join(lines)
    assert "…" not in joined or "#知更" not in joined


def test_vertical_mask_monotonic(card) -> None:
    """竖直遮罩尺寸正确、从上到下单调变深。"""
    mask = card._vertical_mask((64, 100), 20, 80, peak=200)
    assert mask.size == (64, 100)
    column = [mask.getpixel((0, y)) for y in range(100)]
    assert column[0] == 0
    assert column[-1] == 200
    assert column == sorted(column)


def _cover_bytes(width: int = 1080, height: int = 1920) -> bytes:
    """造一张彩色测试封面。"""
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(0, width, 8):
            color = (x * 255 // width, y * 255 // height, 128)
            for dx in range(8):
                if x + dx < width:
                    pixels[x + dx, y] = color
    buffer = __import__("io").BytesIO()
    image.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


def test_render_produces_expected_size(tmp_path: Path, card) -> None:
    """渲染出的卡片尺寸和比例规则一致。"""
    result = FakeResult(media=FakeRef(duration=33))
    out = tmp_path / "card.jpg"
    assert card.render(result, _cover_bytes(), out) is True
    image = Image.open(out)
    assert image.width == card.WIDTH
    assert image.height == int(card.WIDTH * 1.25)  # 竖屏封面裁到上限比例
    assert out.stat().st_size > 10_000


def test_render_landscape_cover(tmp_path: Path, card) -> None:
    """横屏封面出来的卡片更扁。"""
    result = FakeResult(media=FakeRef(duration=10))
    out = tmp_path / "card.jpg"
    assert card.render(result, _cover_bytes(1920, 1080), out) is True
    image = Image.open(out)
    assert image.height == int(card.WIDTH * card.MIN_RATIO)


def test_render_without_font_raises(tmp_path: Path, card) -> None:
    """没有字体时渲染抛错，由调用方退回文字。"""
    card._CUSTOM_FONT = None  # noqa: SLF001 - 测试里直接改模块状态
    card._font_file.cache_clear()  # noqa: SLF001
    card._font.cache_clear()  # noqa: SLF001
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(card, "SYSTEM_FONTS", ("/nonexistent/font.ttf",))
    try:
        with pytest.raises(RuntimeError):
            card.render(FakeResult(), _cover_bytes(), tmp_path / "c.jpg")
    finally:
        monkeypatch.undo()
        card.set_font(FONT)


def test_ensure_font_prefers_local_file(tmp_path: Path, card) -> None:
    """缓存目录里已有字体时不再下载。"""
    local = tmp_path / "NotoSansSC.ttf"
    local.write_bytes(FONT.read_bytes() + b"\0" * 1_000_000)
    card._CUSTOM_FONT = None  # noqa: SLF001
    card._font_file.cache_clear()  # noqa: SLF001
    assert card.ensure_font(tmp_path) == local
    card.set_font(FONT)


def _stripe_mark(colors: list[tuple[int, int, int]]) -> Image.Image:
    """造一张 logo：按给定颜色竖向等分成几条，全部不透明。"""
    width = 24 * len(colors)
    image = Image.new("RGBA", (width, 24), (0, 0, 0, 0))
    for index, color in enumerate(colors):
        for y in range(24):
            for x in range(index * 24, (index + 1) * 24):
                image.putpixel((x, y), (*color, 255))
    return image


def test_is_dark_whitens_greyscale_marks(card) -> None:
    """纯黑白的标在深色封面上会消失，必须转白。"""
    assert card._is_dark(_stripe_mark([(0, 0, 0)])) is True
    assert card._is_dark(_stripe_mark([(10, 10, 10), (120, 120, 120)])) is True


def test_is_dark_keeps_brand_coloured_marks(card) -> None:
    """带品牌色的标（抖音的黑 + 青 + 红）转白会把颜色抹掉，保留原色。"""
    douyin = [(0, 0, 0), (38, 244, 238), (251, 44, 83)]
    assert card._is_dark(_stripe_mark(douyin)) is False


def test_is_dark_keeps_bright_marks(card) -> None:
    """本来就不暗的标（B 站粉）不用转白。"""
    assert card._is_dark(_stripe_mark([(251, 114, 153)])) is False


def test_logo_sits_on_kicker_row(tmp_path: Path, card) -> None:
    """平台图标落在「平台名」那一行：左边缘贴 INSET，位置在卡片下方。"""
    out = tmp_path / "card.jpg"
    assert card.render(FakeResult(), _cover_bytes(), out) is True
    image = Image.open(out).convert("RGB")
    pink = [
        (x, y)
        for y in range(image.height)
        for x in range(image.width)
        if abs(image.getpixel((x, y))[0] - 251) < 24
        and abs(image.getpixel((x, y))[1] - 114) < 24
        and abs(image.getpixel((x, y))[2] - 153) < 24
    ]
    assert pink, "卡片上没找到品牌粉的图标"
    xs = [x for x, _ in pink]
    ys = [y for _, y in pink]
    assert min(xs) == pytest.approx(card.INSET, abs=8), min(xs)
    assert (min(ys) + max(ys)) / 2 > image.height * 0.6
