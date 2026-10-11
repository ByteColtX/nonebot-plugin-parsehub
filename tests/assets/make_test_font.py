# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""重新生成 ``tests/assets/test-font.ttf``。

仓库里只放 Noto Sans SC 的子集（150KB），完整字体 18MB 不进版本库。测试用例里出现
过的字都在 ``CHARS`` 里，改测试用词之后重跑一次即可::

    uv run --with fonttools python tests/assets/make_test_font.py \
        /path/to/NotoSansSC.ttf
"""

from __future__ import annotations

import sys
from pathlib import Path

from fontTools import subset

OUT = Path(__file__).resolve().parent / "test-font.ttf"

CHARS = (
    "好女孩刻猫猫视频图集知更鸟晴歌崩坏星穹铁道泳装小鸟是对的雷军新"
    "logo看不习惯大师说七天就你了爱蜜莉雅抖音小红书微博知乎快手贴吧"
    "百度微信哔哩无标题媒体个测试卡片渲染失败下载链接解析超时后再试"
    "没有可用封面跳过媒体数量"
    "0123456789:·×（）#"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    ".,-_/!?@%&()[]{}<>+=*"
    "，。、！？：；（）【】《》“”‘’…—·"
)


def main() -> None:
    """按 ``CHARS`` 生成子集字体。"""
    if len(sys.argv) != 2:
        raise SystemExit("用法: python make_test_font.py <NotoSansSC.ttf>")
    src = Path(sys.argv[1])
    if not src.exists():
        raise SystemExit(f"找不到字体: {src}")
    opts = subset.Options()
    opts.layout_features = ["*"]
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    font = subset.load_font(str(src), opts)
    subsetter = subset.Subsetter(options=opts)
    subsetter.populate(text=CHARS)
    subsetter.subset(font)
    subset.save_font(font, str(OUT), opts)
    print(f"已生成 {OUT} ({OUT.stat().st_size // 1024}KB)")


if __name__ == "__main__":
    main()
