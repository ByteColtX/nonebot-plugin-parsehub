# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""会话级去重。

两个语义要分开：

- **窗口内重复**：同一个会话里同一个作品刚发过，静默跳过；
- **处理中**：同一个作品正在被处理，后来的消息不重复开工。

旧实现只做前者，而且「开始处理」就记下时间戳，导致解析失败后 120 秒内重发同一
个链接被静默吞掉。现在改成 claim → commit / release：失败会 release，下一次消息
仍然会重新尝试。

跨会话不去重：同一个链接在 A 群发过，B 群再发照样解析。

模块不依赖 nonebot。
"""

from __future__ import annotations

import time
from collections.abc import Iterable

# 处理中的记录最多留这么久（秒），防止异常路径忘了 release 导致永久占用。
_INFLIGHT_TTL_SECONDS = 3600


class Dedupe:
    """按 ``(会话, 作品 key)`` 记录窗口内是否处理过。

    Attributes:
        _seen: ``(会话, key)`` → 最近一次成功处理的时间戳。
        _inflight: ``(会话, key)`` → 开始处理的时间戳。
    """

    def __init__(self) -> None:
        """初始化空缓存。"""
        self._seen: dict[tuple[str, str], float] = {}
        self._inflight: dict[tuple[str, str], float] = {}

    # ---- 查询 ----

    def seen(self, session: str, key: str, window: int) -> bool:
        """这个会话里该 key 是否还在窗口内（只看不记）。

        Args:
            session: 会话标识（群 / 私聊）。
            key: 作品 key。
            window: 去重窗口（秒），小于等于 0 表示不去重。

        Returns:
            窗口内出现过返回 True。
        """
        if window <= 0:
            return False
        self._prune(window)
        last_seen = self._seen.get((session, key))
        return last_seen is not None and time.time() - last_seen < window

    # ---- 旧式「看到即记」（手动路径之外的兼容入口）----

    def mark(self, session: str, key: str) -> None:
        """记下这个会话里该 key 刚被处理过。

        Args:
            session: 会话标识。
            key: 作品 key。
        """
        self._seen[(session, key)] = time.time()

    def should_skip(self, session: str, key: str, window: int) -> bool:
        """查一下并顺手记上（「处理过了」的语义）。

        Args:
            session: 会话标识。
            key: 作品 key。
            window: 去重窗口（秒）。

        Returns:
            窗口内重复出现返回 True。
        """
        repeated = self.seen(session, key, window)
        self.mark(session, key)
        return repeated

    # ---- claim / commit / release ----

    def claim(self, session: str, keys: Iterable[str], window: int) -> bool:
        """认领这批 key：窗口内没出现过、也没人在处理才成功。

        Args:
            session: 会话标识。
            keys: 这个作品的所有 key（短链 key + 展开后的 key）。
            window: 去重窗口（秒），小于等于 0 表示不检查窗口。

        Returns:
            认领成功返回 True；重复或处理中返回 False。
        """
        keys = [key for key in keys if key]
        if not keys:
            return True
        if window > 0:
            self._prune(window)
            now = time.time()
            for key in keys:
                last_seen = self._seen.get((session, key))
                if last_seen is not None and now - last_seen < window:
                    return False
        for key in keys:
            if (session, key) in self._inflight:
                return False
        stamp = time.time()
        for key in keys:
            self._inflight[(session, key)] = stamp
        return True

    def commit(self, session: str, keys: Iterable[str]) -> None:
        """处理成功：结束处理中状态，并把 key 记进窗口。

        Args:
            session: 会话标识。
            keys: 这个作品的所有 key。
        """
        stamp = time.time()
        for key in keys:
            if not key:
                continue
            self._inflight.pop((session, key), None)
            self._seen[(session, key)] = stamp

    def release(self, session: str, keys: Iterable[str]) -> None:
        """处理失败：只结束处理中状态，不记窗口。

        Args:
            session: 会话标识。
            keys: 这个作品的所有 key。
        """
        for key in keys:
            self._inflight.pop((session, key), None)

    def _prune(self, window: int) -> None:
        """清掉窗口外与超时的记录。

        Args:
            window: 去重窗口（秒）。
        """
        now = time.time()
        for key, stamp in list(self._seen.items()):
            if now - stamp > window:
                self._seen.pop(key, None)
        for key, stamp in list(self._inflight.items()):
            if now - stamp > _INFLIGHT_TTL_SECONDS:
                self._inflight.pop(key, None)
