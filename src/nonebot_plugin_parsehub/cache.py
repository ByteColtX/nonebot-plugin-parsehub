# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""按作品缓存下载好的媒体。

同一个作品（不管用户发的是短链还是完整链）第二次出现时，直接从本地把卡片和媒体
发出去，**不解析、不下载、不画卡**。缓存按作品 key 存，跨会话共享——A 群发过的
视频，B 群再发也是直接发本地那份。

目录结构::

    <root>/h_<sha256(key)[:20]>/       条目（不可猜，避免被中转服务枚举）
        files/<...>                    下载好的媒体
        card.jpg                       信息卡片（有就发）
        meta.json                      keys / caption / seconds / updated_at
    <root>/.tmp/<uuid>/                写入中的临时目录

写盘是原子的：先写 ``.tmp``，再 ``os.replace`` 到最终目录；过期条目重新缓存时
不会混进上一轮的文件。

所有方法都是同步的（磁盘 IO），调用方负责放进线程池。

除 ``nonebot.logger`` 外不依赖 nonebot，方便单独测试。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from nonebot import logger

# 目录与文件名。
FILES_DIR_NAME = "files"
CARD_NAME = "card.jpg"
META_NAME = "meta.json"
TMP_DIR_NAME = ".tmp"

# meta.json 的格式版本。
META_SCHEMA = 1

# 条目目录名的哈希长度（十六进制字符数）。
_HASH_LENGTH = 20

# 写入中的临时目录最多留这么久（秒），防止进程被杀后留下垃圾。
_TMP_TTL_SECONDS = 3600


@dataclass(frozen=True)
class CacheEntry:
    """一次缓存命中的结果。

    Attributes:
        root: 条目目录。
        files: 媒体文件（按文件名排序）。
        card: 卡片图路径；没有时为 None。
        caption: 没出卡时发过的文字。
        seconds: 媒体时长（秒），0 表示未知。
        keys: 条目对应的所有作品 key。
    """

    root: Path
    files: tuple[Path, ...]
    card: Path | None
    caption: str
    seconds: int
    keys: tuple[str, ...]


def entry_name(key: str) -> str:
    """把作品 key 变成不可猜的目录名。

    Args:
        key: :func:`nonebot_plugin_parsehub.keys.content_key` 生成的 key。

    Returns:
        ``h_`` 加 20 位十六进制摘要。
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    return f"h_{digest}"


def _remove(path: Path) -> None:
    """删除文件、符号链接或目录。

    Args:
        path: 待删除的路径。
    """
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:
        logger.warning(f"[parsehub] 缓存条目删除失败 {path}: {exc}")


def _entry_bytes(entry: Path) -> int:
    """一个条目目录里所有文件的大小之和。

    Args:
        entry: 条目目录。

    Returns:
        字节数；读不到的文件按 0 计。
    """
    total = 0
    for item in entry.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


class MediaCache:
    """作品级的媒体缓存。

    Attributes:
        root: 缓存根目录。
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
        return self.root / entry_name(key)

    # ---- 读取 ----

    def get(self, key: str) -> CacheEntry | None:
        """取缓存条目，命中就刷新它的时间戳。

        Args:
            key: 作品 key。

        Returns:
            条目；没缓存、已过期或文件缺失时为 ``None``。
        """
        if not self.enabled:
            return None
        path = self.entry_dir(key)
        files = self.media_files(path)
        if not files:
            return None
        meta = self._read_meta(path)
        if meta is None:
            return None
        if time.time() - float(meta.get("updated_at") or 0) > self.ttl_seconds:
            return None
        self._touch(path, meta)
        card = path / CARD_NAME
        return CacheEntry(
            root=path,
            files=tuple(files),
            card=card if card.is_file() else None,
            caption=str(meta.get("caption") or ""),
            seconds=int(meta.get("seconds") or 0),
            keys=tuple(meta.get("keys") or ()),
        )

    def remember_key(self, entry: Path, key: str) -> None:
        """把 key 补记到条目里（老条目命中时顺手补上）。

        Args:
            entry: 条目目录。
            key: 作品 key。
        """
        meta = self._read_meta(entry)
        if meta is None:
            return
        keys = [str(item) for item in meta.get("keys") or () if item]
        if key in keys:
            return
        keys.append(key)
        meta["keys"] = keys
        self._write_meta(entry, meta)

    # ---- 写入 ----

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

        先写 ``.tmp/<uuid>``，再整体 rename 成最终目录：过期条目重新缓存时不会
        混进上一轮的文件。

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

        tmp = self.root / TMP_DIR_NAME / uuid.uuid4().hex
        try:
            (tmp / FILES_DIR_NAME).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(f"[parsehub] 缓存临时目录建不出来: {exc}")
            return None

        stored = 0
        for source in files:
            target = tmp / FILES_DIR_NAME / source.name
            try:
                os.link(source, target)
            except OSError:
                try:
                    shutil.copy2(source, target)
                except OSError as exc:
                    logger.warning(f"[parsehub] 缓存文件失败 {source}: {exc}")
                    continue
            stored += 1
        if not stored:
            _remove(tmp)
            return None

        if card is not None and card.is_file():
            try:
                shutil.copy2(card, tmp / CARD_NAME)
            except OSError as exc:
                logger.warning(f"[parsehub] 缓存卡片失败: {exc}")

        primary = self.entry_dir(keys[0])
        self._write_meta(
            tmp,
            {
                "schema": META_SCHEMA,
                "keys": keys,
                "caption": caption,
                "seconds": int(seconds),
                "updated_at": time.time(),
            },
        )
        try:
            primary.parent.mkdir(parents=True, exist_ok=True)
            _remove(primary)
            os.replace(tmp, primary)
        except OSError as exc:
            logger.warning(f"[parsehub] 缓存提交失败 {primary}: {exc}")
            _remove(tmp)
            return None

        for key in keys[1:]:
            self._alias(primary, self.entry_dir(key))
        return primary

    def _alias(self, primary: Path, alias: Path) -> None:
        """让另一个 key 也指向同一份缓存。

        用符号链接指过去，省得再复制一遍文件；符号链接坏了就当没有这条缓存。

        Args:
            primary: 真正存文件的条目目录。
            alias: 别名条目目录。
        """
        if alias == primary:
            return
        _remove(alias)
        try:
            alias.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(primary.name, alias)
        except OSError as exc:
            logger.warning(f"[parsehub] 缓存别名失败 {alias}: {exc}")

    # ---- 清理 ----

    def sweep(self) -> int:
        """删掉过期条目与写入残留，再按容量上限淘汰最久没用过的。

        容量淘汰只载入一次各条目的大小，循环里删最旧、减总量，不再每删一条
        就全树重算一遍。

        Returns:
            删掉的条目数量。
        """
        if not self.enabled:
            return 0
        removed = self._drop_broken_aliases()
        removed += self._drop_stale_tmp()
        now = time.time()
        entries = self._entries()
        for entry, updated_at in entries:
            if now - updated_at > self.ttl_seconds:
                _remove(entry)
                removed += 1
        if self.max_bytes > 0:
            alive = [(entry, _at) for entry, _at in entries if entry.exists()]
            sizes = {entry: _entry_bytes(entry) for entry, _ in alive}
            total = sum(sizes.values())
            for entry, _updated_at in alive:
                if total <= self.max_bytes:
                    break
                total -= sizes[entry]
                _remove(entry)
                removed += 1
        return removed

    def total_bytes(self) -> int:
        """缓存占用的字节数。

        Returns:
            所有条目加起来的大小。
        """
        return sum(_entry_bytes(entry) for entry, _ in self._entries())

    # ---- 内部 ----

    @staticmethod
    def media_files(entry: Path) -> list[Path]:
        """列出条目里的媒体文件（按文件名排序）。

        Args:
            entry: 条目目录。

        Returns:
            媒体文件列表，不含卡片图和 meta。
        """
        files_dir = entry / FILES_DIR_NAME
        if not files_dir.is_dir():
            return []
        try:
            return sorted(
                (item for item in files_dir.iterdir() if item.is_file()),
                key=lambda item: item.name,
            )
        except OSError:
            return []

    def _entries(self) -> list[tuple[Path, float]]:
        """列出所有真实条目及其最后使用时间（跳过别名与临时目录）。

        Returns:
            ``(目录, updated_at)`` 列表，按时间升序。
        """
        if not self.root.is_dir():
            return []
        entries: list[tuple[Path, float]] = []
        for item in self.root.iterdir():
            if item.name == TMP_DIR_NAME:
                continue
            if item.is_symlink() or not item.is_dir():
                continue
            meta = self._read_meta(item)
            updated = float((meta or {}).get("updated_at") or 0)
            if not updated:
                try:
                    updated = item.stat().st_mtime
                except OSError:
                    updated = 0.0
            entries.append((item, updated))
        return sorted(entries, key=lambda pair: pair[1])

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

    def _drop_stale_tmp(self) -> int:
        """清掉写了一半的临时目录。

        Returns:
            清掉的数量。
        """
        tmp_root = self.root / TMP_DIR_NAME
        if not tmp_root.is_dir():
            return 0
        removed = 0
        now = time.time()
        for item in tmp_root.iterdir():
            try:
                stale = now - item.stat().st_mtime > _TMP_TTL_SECONDS
            except OSError:
                continue
            if stale:
                _remove(item)
                removed += 1
        return removed

    @staticmethod
    def _read_meta(entry: Path) -> dict | None:
        """读条目里的 meta.json。

        Args:
            entry: 条目目录。

        Returns:
            解析后的字典；不存在或损坏时为 None。
        """
        path = entry / META_NAME
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            float(data.get("updated_at") or 0)
            int(data.get("seconds") or 0)
        except (TypeError, ValueError):
            return None
        keys = data.get("keys")
        if keys is not None and (
            not isinstance(keys, (list, tuple))
            or any(not isinstance(key, str) for key in keys)
        ):
            return None
        return data

    def _write_meta(self, entry: Path, meta: dict) -> None:
        """原子写 meta.json。

        Args:
            entry: 条目目录。
            meta: 要写入的内容。
        """
        path = entry / META_NAME
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning(f"[parsehub] 缓存 meta 写入失败 {path}: {exc}")

    def _touch(self, entry: Path, meta: dict) -> None:
        """命中时刷新最后使用时间。

        Args:
            entry: 条目目录。
            meta: 已读出的 meta 内容。
        """
        meta["updated_at"] = time.time()
        self._write_meta(entry, meta)
