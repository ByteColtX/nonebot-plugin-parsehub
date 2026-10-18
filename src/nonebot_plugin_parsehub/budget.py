# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""单次解析工作的总时间预算。

parse 和 download 共享一个 deadline：重试只在预算还够、且失败是可重试类型时
发生。超时不重试——``asyncio.to_thread`` 里的线程无法取消，再开一次只会多一份
下载，把协议端和带宽一起拖垮。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import AbstractAsyncContextManager

# 任何一段至少留这么多秒，避免 budget 见底时 timeout(0) 立刻抛错。
_MIN_SLICE_SECONDS = 0.1


class Budget:
    """一次工作的截止时间。

    Attributes:
        total: 总预算（秒）。
    """

    def __init__(self, total: float) -> None:
        """记录总预算并开始计时。

        Args:
            total: 总预算（秒）。
        """
        self.total = float(total)
        self._deadline = time.monotonic() + max(self.total, _MIN_SLICE_SECONDS)

    @property
    def remaining(self) -> float:
        """还剩多少秒。

        Returns:
            剩余秒数，可能为负。
        """
        return self._deadline - time.monotonic()

    @property
    def expired(self) -> bool:
        """预算是否已经用完。

        Returns:
            用完为 True。
        """
        return self.remaining <= 0

    def timeout(self) -> AbstractAsyncContextManager[None]:
        """套在某一段异步调用外面的超时上下文。

        Returns:
            ``asyncio.timeout`` 上下文管理器。
        """
        return asyncio.timeout(max(_MIN_SLICE_SECONDS, self.remaining))
