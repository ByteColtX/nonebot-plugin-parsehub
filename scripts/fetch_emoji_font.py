# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""下载测试用的彩色 emoji 字体。

字体有 3 MB 左右，不进仓库；CI 在跑测试前调用这个脚本，本地想跑真实的
emoji 渲染用例时也可以手动执行：

    uv run python scripts/fetch_emoji_font.py

默认写到 localstore 的缓存目录
（``.../nonebot_plugin_parsehub/emoji-test.ttf``），和
``tests/test_card_emoji.py`` 的默认查找路径一致。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from platformdirs import user_cache_path

# 与 src/nonebot_plugin_parsehub/card/fonts.py 的 EMOJI_FONT_SOURCES 保持一致。
FONT_URL = (
    "https://github.com/JoeBlakeB/ttf-twemoji-aur/releases/download/"
    "15.0.2/Twemoji-15.0.2.ttf"
)
FONT_SHA256 = "2c96ffb14e104ebbc49a7b35303828f6131cf2752034dd79e2050d8795785a60"
DEFAULT_PATH = (
    user_cache_path("nonebot2") / "nonebot_plugin_parsehub" / "emoji-test.ttf"
)
ATTEMPTS = 3
TIMEOUT_SECONDS = 60


def _sha256(path: Path) -> str:
    """算文件的 sha256。

    Args:
        path: 文件路径。

    Returns:
        小写十六进制 sha256。
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(dest: Path) -> None:
    """下载字体到 ``dest``（先写 ``.part``，校验通过再替换）。

    Args:
        dest: 目标文件路径。

    Raises:
        RuntimeError: 所有重试都失败，或下载内容校验不过。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last_error: BaseException | None = None

    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(
                FONT_URL, timeout=TIMEOUT_SECONDS
            ) as response:
                with part.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
            if _sha256(part) != FONT_SHA256:
                part.unlink(missing_ok=True)
                raise RuntimeError("字体 sha256 不匹配")
            part.replace(dest)
            return
        except (OSError, urllib.error.URLError, RuntimeError) as exc:
            last_error = exc
            part.unlink(missing_ok=True)
            if attempt < ATTEMPTS:
                time.sleep(attempt)

    raise RuntimeError(f"下载字体失败：{last_error}")


def main() -> int:
    """命令行入口。

    Returns:
        0 表示字体已就绪，1 表示下载或校验失败。
    """
    parser = argparse.ArgumentParser(description="下载测试用彩色 emoji 字体")
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_PATH,
        help=f"目标路径（默认 {DEFAULT_PATH}）",
    )
    args = parser.parse_args()
    dest = args.path.expanduser()

    if dest.exists() and _sha256(dest) == FONT_SHA256:
        print(f"已存在：{dest}")
        return 0

    try:
        _download(dest)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
