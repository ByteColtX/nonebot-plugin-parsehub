# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""按作品缓存下载好的媒体。

同一个作品（不管用户发的是短链还是完整链）第二次出现时，直接从本地把卡片和媒体
发出去，**不解析、不下载、不画卡**。缓存按作品 key 存，跨会话共享——A 群发过的
视频，B 群再发也是直接发本地那份。

目录结构（``root`` 就是中转服务的根目录，所以协议端能直接来拉）::

    <root>/media/<slug>/card.jpg      信息卡片（有就发）
    <root>/media/<slug>/caption.txt   没出卡时发的文字
    <root>/media/<slug>/files/<...>   下载好的媒体文件

只保留 ``ttl_seconds`` 内用过的条目（命中会刷新时间，所以常看的一直在），总量
超过 ``max_bytes`` 时按最久没用过的先删。模块不依赖 nonebot，方便单独测试。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger("nonebot_plugin_parsehub.mediacache")

# 缓存目录名（放在中转根目录下，协议端要能拉到）。
CACHE_DIR_NAME = "media"

# 卡片图与文字的文件名。
CARD_NAME = "card.jpg"
CAPTION_NAME = "caption.txt"

# 记下这个条目对应哪些作品 key（命中时要把它们一起记进去重，否则同一个作品
# 换个写法再发会被当成新作品）。
KEYS_NAME = "keys.txt"

# 条目里媒体的时长（秒），供时长上限在命中缓存时判断，省得为已知的长视频再解析。
SECONDS_NAME = "seconds.txt"

# 媒体文件放在条目目录的这个子目录里，避免和卡片图混在一起。
FILES_DIR_NAME = "files"

# 目录名的非法字符（作品 key 里会有 ``:`` 和 ``/``）。
_UNSAFE = re.compile(r"[^0-9A-Za-z._-]")


def slugify(key: str) -> str:
    """把作品 key 变成安全的目录名。

    Args:
        key: :func:`nonebot_plugin_parsehub.core.content_key` 生成的 key。

    Returns:
        只含字母数字和 ``._-`` 的目录名。
    """
    return _UNSAFE.sub("_", key)[:120] or "unknown"


class MediaCache:
    """作品级的媒体缓存。

    Attributes:
        root: 缓存根目录（``<中转根目录>/media``）。
        ttl_seconds: 条目多久没用就删（秒），小于等于 0 表示不缓存。
        max_bytes: 总容量上限（字节），小于等于 0 表示不限。
    """

    def __init__(
        self,
        root: Path,
        *,
        ttl_seconds: int,
        max_bytes: int,
    ) -> None:
        """记住配置，不碰磁盘。

        Args:
            root: 缓存根目录。
            ttl_seconds: 条目过期时间（秒）。
            max_bytes: 总容量上限（字节）。
        """
        self.root = root
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes

    @property
    def enabled(self) -> bool:
        """有没有开启缓存。

        Returns:
            开启为 ``True``。
        """
        return self.ttl_seconds > 0

    def entry_dir(self, key: str) -> Path:
        """某个作品对应的目录。

        Args:
            key: 作品 key。

        Returns:
            条目目录路径（可能不存在）。
        """
        return self.root / slugify(key)

    def get(self, key: str) -> Path | None:
        """取缓存条目，命中就刷新它的时间戳。

        Args:
            key: 作品 key。

        Returns:
            条目目录；没缓存或已过期时为 ``None``。
        """
        if not self.enabled:
            return None
        path = self.entry_dir(key)
        if not (path / FILES_DIR_NAME).is_dir():
            return None
        try:
            if time.time() - path.stat().st_mtime > self.ttl_seconds:
                return None
            os.utime(path, None)  # 命中即续命，常看的一直留着
        except OSError:
            return None
        if not self.media_files(path):
            return None
        return path

    @staticmethod
    def media_files(entry: Path) -> list[Path]:
        """列出条目里的媒体文件（按文件名排序）。

        Args:
            entry: 条目目录。

        Returns:
            媒体文件列表，不含卡片图和文字。
        """
        files_dir = entry / FILES_DIR_NAME
        if not files_dir.is_dir():
            return []
        return sorted(
            (item for item in files_dir.iterdir() if item.is_file()),
            key=lambda item: item.name,
        )

    @staticmethod
    def card_path(entry: Path) -> Path | None:
        """条目里的卡片图。

        Args:
            entry: 条目目录。

        Returns:
            卡片图路径；没有时为 ``None``。
        """
        card = entry / CARD_NAME
        return card if card.is_file() else None

    @staticmethod
    def entry_keys(entry: Path) -> list[str]:
        """条目对应的所有作品 key。

        Args:
            entry: 条目目录。

        Returns:
            作品 key 列表；没记录过时为空列表。
        """
        keys = entry / KEYS_NAME
        if not keys.is_file():
            return []
        try:
            return [
                line.strip()
                for line in keys.read_text("utf-8").splitlines()
                if line.strip()
            ]
        except OSError:
            return []

    @staticmethod
    def entry_seconds(entry: Path) -> int:
        """条目里媒体的时长（秒）。

        Args:
            entry: 条目目录。

        Returns:
            时长（秒）；没记录过或读不出来时返回 0（调用方按「不超限」处理）。
        """
        stored = entry / SECONDS_NAME
        if not stored.is_file():
            return 0
        try:
            return max(0, int(stored.read_text("utf-8").strip() or 0))
        except (OSError, ValueError):
            return 0

    @staticmethod
    def remember_key(entry: Path, key: str) -> None:
        """把 key 补记到条目里（老条目没有 keys.txt，命中时顺手补上）。

        Args:
            entry: 条目目录。
            key: 作品 key。
        """
        keys = MediaCache.entry_keys(entry)
        if key in keys:
            return
        keys.append(key)
        try:
            (entry / KEYS_NAME).write_text("\n".join(keys), "utf-8")
        except OSError as exc:
            logger.warning(f"[parsehub] 缓存 key 补记失败: {exc}")

    @staticmethod
    def caption_path(entry: Path) -> Path | None:
        """条目里的文字。

        Args:
            entry: 条目目录。

        Returns:
            文字文件路径；没有时为 ``None``。
        """
        caption = entry / CAPTION_NAME
        return caption if caption.is_file() else None

    def store(
        self,
        keys: Iterable[str],
        files: list[Path],
        *,
        card: Path | None = None,
        caption: str = "",
        seconds: int = 0,
    ) -> Path | None:
        """把一个作品存进缓存（多个 key 指向同一份文件）。

        Args:
            keys: 作品 key，通常同时传「短链 key」和「解析后的 key」。
            files: 已下载到本地的媒体文件。
            card: 卡片图路径，没有就传 ``None``。
            caption: 没出卡时发的文字。
            seconds: 媒体的时长（秒），0 表示未知。

        Returns:
            条目目录；没开启缓存或没有媒体时为 ``None``。
        """
        if not self.enabled or not files:
            return None
        keys = [key for key in keys if key]
        if not keys:
            return None

        # 先把文件收进第一个 key 的目录，再给别的 key 建硬链接指向同一份
        primary = self.entry_dir(keys[0])
        try:
            (primary / FILES_DIR_NAME).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(f"[parsehub] 缓存目录建不出来: {exc}")
            return None

        for source in files:
            target = primary / FILES_DIR_NAME / source.name
            if target.exists():
                continue
            try:
                os.link(source, target)  # 同一个文件系统，硬链接零成本
            except OSError:
                try:
                    shutil.copy2(source, target)
                except OSError as exc:
                    logger.warning(f"[parsehub] 缓存文件失败 {source}: {exc}")
        if not self.media_files(primary):
            shutil.rmtree(primary, ignore_errors=True)
            return None

        if card is not None and card.is_file():
            try:
                shutil.copy2(card, primary / CARD_NAME)
            except OSError as exc:
                logger.warning(f"[parsehub] 缓存卡片失败: {exc}")
        if caption:
            try:
                (primary / CAPTION_NAME).write_text(caption, "utf-8")
            except OSError as exc:
                logger.warning(f"[parsehub] 缓存文字失败: {exc}")
        if seconds > 0:
            try:
                (primary / SECONDS_NAME).write_text(str(int(seconds)), "utf-8")
            except OSError as exc:
                logger.warning(f"[parsehub] 缓存时长失败: {exc}")
        try:
            (primary / KEYS_NAME).write_text("\n".join(keys), "utf-8")
        except OSError as exc:
            logger.warning(f"[parsehub] 缓存 key 记录失败: {exc}")

        os.utime(primary, None)
        for key in keys[1:]:
            self._alias(primary, self.entry_dir(key))
        self.sweep()
        return primary

    def _alias(self, primary: Path, alias: Path) -> None:
        """让另一个 key 也指向同一份缓存。

        用符号链接指过去，省得再复制一遍文件；符号链接坏了就当没有这条缓存。

        Args:
            primary: 真正存文件的条目目录。
            alias: 别名条目目录。
        """
        if alias == primary or alias.exists():
            return
        try:
            alias.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(primary.name, alias)
        except OSError as exc:
            logger.warning(f"[parsehub] 缓存别名失败 {alias}: {exc}")

    def total_bytes(self) -> int:
        """缓存占用的字节数。

        Returns:
            所有条目加起来的大小。
        """
        total = 0
        for entry in self._entries():
            for item in entry.rglob("*"):
                try:
                    if item.is_file():
                        total += item.stat().st_size
                except OSError:
                    continue
        return total

    def _entries(self) -> list[Path]:
        """列出所有条目目录（跳过符号链接别名）。

        Returns:
            真实存放文件的条目目录列表。
        """
        if not self.root.is_dir():
            return []
        return sorted(
            (
                item
                for item in self.root.iterdir()
                if item.is_dir() and not item.is_symlink()
            ),
            key=lambda item: self._mtime(item),
        )

    @staticmethod
    def _mtime(path: Path) -> float:
        """取目录的修改时间，取不到就当 0。

        Args:
            path: 目录路径。

        Returns:
            时间戳。
        """
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _drop_broken_aliases(self) -> int:
        """清掉指向已被删掉条目的别名符号链接。

        Returns:
            清掉的数量。
        """
        if not self.root.is_dir():
            return 0
        removed = 0
        for item in self.root.iterdir():
            if item.is_symlink() and not item.is_dir():
                try:
                    item.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    def sweep(self) -> int:
        """删掉过期条目，再按容量上限淘汰最久没用过的。

        Returns:
            删掉的条目数量。
        """
        if not self.enabled:
            return 0
        removed = 0
        now = time.time()
        self._drop_broken_aliases()
        for entry in self._entries():
            if now - self._mtime(entry) > self.ttl_seconds:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        if self.max_bytes > 0:
            while True:
                entries = self._entries()
                if not entries or self.total_bytes() <= self.max_bytes:
                    break
                shutil.rmtree(entries[0], ignore_errors=True)
                removed += 1
        return removed
