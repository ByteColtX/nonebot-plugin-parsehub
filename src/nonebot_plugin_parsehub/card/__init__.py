# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""卡片渲染入口。

把字体获取、封面获取、版式绘制串起来，并统一兜底：任何一步失败都返回 ``None``，
由调用方退回纯文字，而不是让解析白做。

内部按职责拆成五个子模块：

- :mod:`fonts`：字体来源、sha256 校验、emoji / 回退字体；
- :mod:`cover`：封面三级兜底（thumb_url → 图片媒体 → 视频抽帧）；
- :mod:`text`：emoji 切分、字形链、折行与绘制；
- :mod:`compose`：渐变遮罩与平台 logo 处理；
- :mod:`layout`：版式与渲染。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nonebot import logger

from . import cover, fonts, layout

__all__ = ["CardRenderer"]


class CardRenderer:
    """把解析结果渲染成卡片图。

    Attributes:
        cache_dir: 字体等资源的缓存目录。
        _new_task_dir: 没有现成任务目录时用来新建一个。
    """

    def __init__(
        self,
        cache_dir: Path,
        new_task_dir: Callable[[], Path],
    ) -> None:
        """记住缓存目录与任务目录工厂。

        Args:
            cache_dir: 字体等资源的缓存目录。
            new_task_dir: 新建任务目录的回调。
        """
        self.cache_dir = cache_dir
        self._new_task_dir = new_task_dir

    def set_font(self, path: Path) -> None:
        """指定中文字体文件。

        Args:
            path: 字体文件路径。
        """
        fonts.set_font(path)

    def available(self) -> bool:
        """卡片功能是否可用（有字体、有 Pillow）。

        Returns:
            可用时为 ``True``。
        """
        return layout.available()

    async def render(
        self,
        result: Any,  # ParseHub 解析结果
        files: list[Path],
        task_dir: Path | None,
        proxy: str = "",
    ) -> Path | None:
        """渲染卡片图（阻塞活走线程池）。

        Args:
            result: ParseHub 解析结果。
            files: 已下载的本地媒体，抽帧兜底要用。
            task_dir: 任务目录；为 ``None`` 时另建一个。
            proxy: 取封面用的代理（海外平台的封面 CDN 直连会超时）。

        Returns:
            卡片图片路径；不可用时为 ``None``（调用方退回文字）。
        """
        try:
            font = await asyncio.to_thread(fonts.ensure_font, self.cache_dir)
            if font is None:
                return None
            # 彩色 emoji 字体：拿不到就算了，标题里的 emoji 会退回方块
            await asyncio.to_thread(fonts.ensure_emoji_font, self.cache_dir)
            # 回退字体（DejaVu / Noto Sans）：补主字体没有的符号
            await asyncio.to_thread(fonts.ensure_fallback_fonts, self.cache_dir)
            picture = await asyncio.to_thread(
                cover.fetch_cover, result, files, proxy
            )
            if picture is None:
                return None
            target_dir = task_dir or self._new_task_dir()
            out_path = target_dir / "card.jpg"
            await asyncio.to_thread(layout.render, result, picture, out_path)
        except Exception as exc:  # 渲染失败不能让解析白做
            logger.warning(
                f"[parsehub] 卡片渲染失败: {type(exc).__name__}: {exc}"
            )
            return None
        return out_path
