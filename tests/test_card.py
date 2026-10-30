# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""卡片渲染模块的离线测试。

字体用 ``tests/assets/test-font.ttf``（Noto Sans SC 的子集），所以不依赖系统字体。
"""

from __future__ import annotations

import hashlib
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


class FakeVideoRef(FakeRef):
    """假视频引用（靠类名让卡片推断类型）。"""


class FakeImageRef(FakeRef):
    """假图片引用。"""


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


def test_format_duration_seconds(card) -> None:
    """秒为单位的平台正常格式化。"""
    assert card.layout.format_duration(33) == "00:33"
    assert card.layout.format_duration(3725) == "1:02:05"
    assert card.layout.format_duration(0) == ""


def test_format_duration_milliseconds(card) -> None:
    """抖音、TikTok 的毫秒要换算，否则 6 秒会显示成 1:42:14。"""
    assert card.layout.format_duration(6134, is_ms=True) == "00:06"
    assert card.layout.format_duration(11900, is_ms=True) == "00:11"


def test_meta_line_video(card) -> None:
    """视频类元信息：类型 · 时长。"""
    result = FakeResult(media=FakeRef(duration=33))
    assert card.layout.meta_line(result, 1, False) == "视频 · 00:33"


def test_tiktok_duration_is_seconds(card) -> None:
    """TikTok 的 duration 是秒（cv2 实测 30fps×366 帧=12.20s），不能按毫秒除。"""
    tiktok = FakeResult(platform_id="tiktok", media=FakeRef(duration=12))
    assert card.layout.platform_uses_milliseconds(tiktok) is False
    assert card.layout.meta_line(tiktok, 1, False) == "视频 · 00:12"

    # 抖音 / 小红书 / 快手仍然是毫秒
    assert card.layout.platform_uses_milliseconds(
        FakeResult(platform_id="douyin")
    )
    assert card.layout.platform_uses_milliseconds(FakeResult(platform_id="xhs"))
    kuaishou = FakeResult(platform_id="kuaishou", media=FakeRef(duration=11933))
    assert card.layout.platform_uses_milliseconds(kuaishou)
    assert card.layout.meta_line(kuaishou, 1, True) == "视频 · 00:11"
    # 毫秒 → 秒的换算由 platforms.duration_seconds 负责，
    # 服务侧行为在 test_duration_limit.py 里经完整链路覆盖。


def test_title_falls_back_to_content(card) -> None:
    """微博经常只有正文：空标题不能让卡片只剩「(无标题)」。"""
    assert card.layout.title_text(FakeResult(title="", content="正文内容")) == (
        "正文内容"
    )
    assert card.layout.title_text(FakeResult(title="标题", content="正文")) == (
        "标题"
    )
    assert (
        card.layout.title_text(FakeResult(title="", content="")) == "(无标题)"
    )


def test_render_uses_content_when_title_missing(
    tmp_path: Path, card, monkeypatch
) -> None:
    """渲染时也要走正文兜底，而不是只在 helper 里。"""
    drawn: list[str] = []
    monkeypatch.setattr(
        card.layout,
        "draw_text",
        lambda *args, **kwargs: drawn.append(args[2]),
    )
    result = FakeResult(title="", content="微博正文")
    out = tmp_path / "weibo.jpg"
    assert card.layout.render(result, _cover_bytes(540, 960), out) is True
    assert "微博正文" in drawn


def test_meta_line_multimedia_infers_media_type(card) -> None:
    """最右统一返回 multimedia：单视频 / 纯图 / 混合分别标对。"""
    video = FakeResult(media=FakeVideoRef(duration=0), post_type="multimedia")
    assert card.layout.meta_line(video, 1, False) == "视频"

    images = FakeResult(
        media=[FakeImageRef(), FakeImageRef(), FakeImageRef()],
        post_type="multimedia",
    )
    assert card.layout.meta_line(images, 3, False) == "图集 · 3 个媒体"

    mixed = FakeResult(
        media=[FakeVideoRef(), FakeImageRef()], post_type="multimedia"
    )
    assert card.layout.meta_line(mixed, 2, False) == "图文 · 2 个媒体"


def test_meta_line_image_with_count(card) -> None:
    """图文帖：类型 · 媒体数，没有时长。"""
    result = FakeResult(
        media=[FakeRef(ext="jpg"), FakeRef(ext="jpg"), FakeRef(ext="jpg")],
        post_type="image",
    )
    assert card.layout.meta_line(result, 3, False) == "图集 · 3 个媒体"


def test_platform_of_known_and_unknown(card) -> None:
    """已知平台给品牌色，未知平台退化成灰色。"""
    name, color, slug = card.layout.platform_of(
        FakeResult(platform_id="douyin")
    )
    assert (name, slug) == ("抖音", "douyin")
    assert color == (254, 44, 85)

    name, color, slug = card.layout.platform_of(
        FakeResult(platform_id="unknown-x")
    )
    assert color == (150, 150, 150)
    assert slug == "x"


@pytest.mark.parametrize(
    ("platform_id", "expected"),
    [
        ("youtube", "YouTube"),
        ("tiktok", "TikTok"),
        ("instagram", "Instagram"),
        ("bilibili", "bilibili"),
    ],
)
def test_kicker_keeps_official_case(
    tmp_path: Path, card, monkeypatch, platform_id: str, expected: str
) -> None:
    """平台名按官方大小写绘制，不能整行转大写。"""
    drawn: list[str] = []
    monkeypatch.setattr(
        card.layout,
        "draw_text",
        lambda *args, **kwargs: drawn.append(args[2]),
    )
    out = tmp_path / f"{platform_id}.jpg"
    assert (
        card.layout.render(
            FakeResult(platform_id=platform_id), _cover_bytes(540, 960), out
        )
        is True
    )
    assert drawn[0] == expected


def test_wrap_keeps_hashtag_whole(card) -> None:
    """话题标签不会被从中间劈开。"""
    font = card.fonts.font_for(58, 700)
    title = "泳装小鸟是对的 #知更鸟晴歌 #崩坏星穹铁道 #知更鸟"
    lines = card.text.wrap(title, font, 900, 3)
    assert len(lines) >= 2
    for line in lines[:-1]:
        assert not line.endswith("#")
    assert all(
        line.strip().startswith("#") or "#" not in line[:1] for line in lines
    )


def test_fit_title_drops_trailing_tags(card) -> None:
    """放不下时整条丢掉末尾标签，而不是截成 #知更…。"""
    font = card.fonts.font_for(58, 700)
    title = "泳装小鸟是对的 #知更鸟晴歌 #崩坏星穹铁道 #知更鸟"
    lines = card.text.fit_title(title, font, 700, 2)
    joined = "".join(lines)
    assert "…" not in joined or "#知更" not in joined


def test_vertical_mask_monotonic(card) -> None:
    """竖直遮罩尺寸正确、从上到下单调变深。"""
    mask = card.compose.vertical_mask((64, 100), 20, 80, peak=200)
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
    assert card.layout.render(result, _cover_bytes(), out) is True
    image = Image.open(out)
    assert image.width == card.layout.WIDTH
    assert image.height == int(card.layout.WIDTH * 1.25)  # 竖屏封面裁到上限比例
    assert out.stat().st_size > 10_000


def test_render_landscape_cover(tmp_path: Path, card) -> None:
    """横屏封面出来的卡片更扁。"""
    result = FakeResult(media=FakeRef(duration=10))
    out = tmp_path / "card.jpg"
    assert card.layout.render(result, _cover_bytes(1920, 1080), out) is True
    image = Image.open(out)
    assert image.height == int(card.layout.WIDTH * card.layout.MIN_RATIO)


def test_render_without_font_raises(tmp_path: Path, card) -> None:
    """没有字体时渲染抛错，由调用方退回文字。"""
    card.fonts._CUSTOM_FONT = None  # noqa: SLF001 - 测试里直接改模块状态
    card.fonts._font_file.cache_clear()  # noqa: SLF001
    card.fonts.font_for.cache_clear()  # noqa: SLF001
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(card.fonts, "SYSTEM_FONTS", ("/nonexistent/font.ttf",))
    try:
        with pytest.raises(RuntimeError):
            card.layout.render(FakeResult(), _cover_bytes(), tmp_path / "c.jpg")
    finally:
        monkeypatch.undo()
        card.fonts.set_font(FONT)


def test_ensure_font_prefers_local_file(
    tmp_path: Path, card, monkeypatch
) -> None:
    """缓存目录里已有字体、且 sha256 对得上时不再下载。"""
    local = tmp_path / card.fonts.FONT_NAME
    payload = FONT.read_bytes() + b"\0" * 1_000_000
    local.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        card.fonts,
        "FONT_SOURCES",
        (card.fonts.FontSource("https://example.test/font.ttf", digest),),
    )
    card.fonts._CUSTOM_FONT = None  # noqa: SLF001
    card.fonts._font_file.cache_clear()  # noqa: SLF001
    assert card.fonts.ensure_font(tmp_path) == local
    card.fonts.set_font(FONT)


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
    assert card.compose.is_dark(_stripe_mark([(0, 0, 0)])) is True
    assert (
        card.compose.is_dark(_stripe_mark([(10, 10, 10), (120, 120, 120)]))
        is True
    )


def test_is_dark_keeps_brand_coloured_marks(card) -> None:
    """带品牌色的标（抖音的黑 + 青 + 红）转白会把颜色抹掉，保留原色。"""
    douyin = [(0, 0, 0), (38, 244, 238), (251, 44, 83)]
    assert card.compose.is_dark(_stripe_mark(douyin)) is False


def test_is_dark_keeps_bright_marks(card) -> None:
    """本来就不暗的标（B 站粉）不用转白。"""
    assert card.compose.is_dark(_stripe_mark([(251, 114, 153)])) is False


def test_logo_sits_on_kicker_row(tmp_path: Path, card) -> None:
    """平台图标落在「平台名」那一行：左边缘贴 INSET，位置在卡片下方。"""
    out = tmp_path / "card.jpg"
    assert card.layout.render(FakeResult(), _cover_bytes(), out) is True
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
    assert min(xs) == pytest.approx(card.layout.INSET, abs=8), min(xs)
    assert (min(ys) + max(ys)) / 2 > image.height * 0.6
