# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""按会话（群 / 私聊）记录解析开关。

只有被显式改过的会话才会落盘，没记录的按默认值（开）算——这样文件一直很小，
新群、新私聊也不需要任何初始化。

状态存成 JSON，写的时候先写临时文件再 ``os.replace``，避免进程被杀的瞬间留下半
个文件。模块本身不依赖 nonebot，方便单独测试。

用法::

    store = SwitchStore(Path("sessions.json"))
    store.is_on(group_key(123456789))   # True（默认开）
    store.is_on(private_key(20000))     # True（私聊也默认开）
    store.set(group_key(123456789), False)   # 关掉这个群
    store.set(private_key(20000), False)     # 关掉这个私聊
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("nonebot_plugin_parsehub.switches")


# 没有记录时的默认值：群和私聊一样，都是允许解析。
DEFAULT_ON = True

# 落盘格式版本：只有这个版本会被读取，旧格式直接忽略（不做迁移）。
SCHEMA = 2


def group_key(group_id: int) -> str:
    """群会话的键。

    Args:
        group_id: 群号。

    Returns:
        形如 ``group:123456789`` 的键。
    """
    return f"group:{group_id}"


def private_key(user_id: int) -> str:
    """私聊会话的键。

    Args:
        user_id: QQ 号。

    Returns:
        形如 ``private:20000`` 的键。
    """
    return f"private:{user_id}"


class SwitchStore:
    """按会话存解析开关。

    Attributes:
        path: 状态文件位置。
        default: 没有记录时的默认值。
    """

    def __init__(self, path: Path, *, default: bool = DEFAULT_ON) -> None:
        """读入已有状态。

        Args:
            path: 状态文件位置。
            default: 没有记录时的默认值。
        """
        self.path = path
        self.default = default
        self._state: dict[str, bool] = {}
        self.load()

    def load(self) -> None:
        """从磁盘读回状态；文件不存在或坏了都当成「没有任何记录」。"""
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            self._state = {}
            return
        except (OSError, ValueError) as exc:
            logger.warning(
                f"[parsehub] 解析开关文件读不出来，按默认值处理: {exc}"
            )
            self._state = {}
            return
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            logger.warning(
                f"[parsehub] 解析开关文件不是 schema {SCHEMA}，按默认值处理"
            )
            self._state = {}
            return
        sessions = raw.get("sessions")
        if not isinstance(sessions, dict):
            logger.warning("[parsehub] 解析开关文件缺少 sessions，按默认值处理")
            self._state = {}
            return
        self._state = {str(key): bool(value) for key, value in sessions.items()}

    def save(self) -> None:
        """原子写盘（先写 ``*.tmp`` 再 rename）。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            payload = {"schema": SCHEMA, "sessions": self._state}
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning(f"[parsehub] 解析开关写盘失败: {exc}")

    def is_on(self, key: str) -> bool:
        """这个会话允不允许解析。

        Args:
            key: :func:`group_key` / :func:`private_key` 生成的键。

        Returns:
            允许为 ``True``。
        """
        return self._state.get(key, self.default)

    def set(self, key: str, on: bool) -> bool:
        """设置开关并落盘。

        Args:
            key: 会话键。
            on: 是否允许解析。

        Returns:
            设置之后的值。
        """
        if on == self.default:
            self._state.pop(key, None)  # 和默认值一样就不留记录
        else:
            self._state[key] = on
        self.save()
        return on

    def toggle(self, key: str) -> bool:
        """翻转开关。

        Args:
            key: 会话键。

        Returns:
            翻转之后的值。
        """
        return self.set(key, not self.is_on(key))

    def off_keys(self) -> list[str]:
        """所有被关掉的会话键（按字母序）。

        Returns:
            会话键列表。
        """
        return sorted(key for key, on in self._state.items() if not on)
