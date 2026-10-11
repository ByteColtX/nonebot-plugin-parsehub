# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""媒体缓存的单元测试（纯文件操作，不依赖 nonebot）。"""

from __future__ import annotations

import os
import time
from pathlib import Path


def _files(tmp_path: Path, *names: str, size: int = 1024) -> list[Path]:
    """造几个假媒体文件。"""
    out = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"x" * size)
        out.append(path)
    return out


def test_slugify(mediacache) -> None:
    """作品 key 里的冒号和斜杠要变成安全的目录名。"""
    assert (
        mediacache.slugify("bilibili:bv1yupv6yeue") == "bilibili_bv1yupv6yeue"
    )
    assert mediacache.slugify("b23.tv/3xdwepo") == "b23.tv_3xdwepo"


def test_store_then_get(tmp_path: Path, mediacache) -> None:
    """存进去能取出来，媒体文件在。"""
    cache = mediacache.MediaCache(
        tmp_path / "media", ttl_seconds=3600, max_bytes=0
    )
    files = _files(tmp_path, "a.jpg", "b.mp4")
    entry = cache.store(["bilibili:bv1"], files, caption="标题")
    assert entry is not None

    hit = cache.get("bilibili:bv1")
    assert hit == entry
    names = [item.name for item in mediacache.MediaCache.media_files(hit)]
    assert names == ["a.jpg", "b.mp4"]
    assert mediacache.MediaCache.card_path(hit) is None
    caption = mediacache.MediaCache.caption_path(hit)
    assert caption is not None and caption.read_text("utf-8") == "标题"


def test_get_missing_key(tmp_path: Path, mediacache) -> None:
    """没缓存过的 key 返回 None。"""
    cache = mediacache.MediaCache(
        tmp_path / "media", ttl_seconds=3600, max_bytes=0
    )
    assert cache.get("douyin:123456") is None


def test_disabled_cache(tmp_path: Path, mediacache) -> None:
    """ttl 为 0 就是不缓存：不写也不读。"""
    cache = mediacache.MediaCache(
        tmp_path / "media", ttl_seconds=0, max_bytes=0
    )
    files = _files(tmp_path, "a.jpg")
    assert cache.store(["k"], files) is None
    assert cache.get("k") is None


def test_expired_entry_is_gone(tmp_path: Path, mediacache) -> None:
    """超过 ttl 的条目读不到，sweep 会把它删掉。"""
    cache = mediacache.MediaCache(
        tmp_path / "media", ttl_seconds=60, max_bytes=0
    )
    entry = cache.store(["k"], _files(tmp_path, "a.jpg"))
    assert entry is not None

    old = time.time() - 120
    os.utime(entry, (old, old))
    assert cache.get("k") is None
    assert cache.sweep() == 1
    assert not entry.exists()


def test_hit_refreshes_ttl(tmp_path: Path, mediacache) -> None:
    """命中会刷新时间，所以常看的作品不会过期。"""
    cache = mediacache.MediaCache(
        tmp_path / "media", ttl_seconds=60, max_bytes=0
    )
    entry = cache.store(["k"], _files(tmp_path, "a.jpg"))
    old = time.time() - 50
    os.utime(entry, (old, old))

    assert cache.get("k") == entry
    assert time.time() - entry.stat().st_mtime < 5


def test_capacity_evicts_oldest(tmp_path: Path, mediacache) -> None:
    """超过容量上限时，最久没用过的先被删。"""
    cache = mediacache.MediaCache(
        tmp_path / "media", ttl_seconds=3600, max_bytes=2500
    )
    first = cache.store(["k1"], _files(tmp_path, "one.jpg", size=1000))
    second = cache.store(["k2"], _files(tmp_path, "two.jpg", size=1000))
    assert first is not None and second is not None

    # 把 first 的时间调旧，再加一个把总量顶过上限
    old = time.time() - 100
    os.utime(first, (old, old))
    third = cache.store(["k3"], _files(tmp_path, "three.jpg", size=1000))
    assert third is not None
    assert not first.exists(), "最久没用过的那个该被删"
    assert second.exists() and third.exists()


def test_alias_key_points_to_same_files(tmp_path: Path, mediacache) -> None:
    """两个 key（短链 + 展开）指向同一份文件，容量只算一次。"""
    cache = mediacache.MediaCache(
        tmp_path / "media", ttl_seconds=3600, max_bytes=0
    )
    files = _files(tmp_path, "a.jpg", size=2048)
    primary = cache.store(["b23.tv_3xdwepo", "bilibili:bv1"], files)
    assert primary is not None

    alias = cache.get("bilibili:bv1")
    assert alias is not None
    # 别名是符号链接，路径不同但指向同一批文件
    assert [item.name for item in mediacache.MediaCache.media_files(alias)] == [
        item.name for item in mediacache.MediaCache.media_files(primary)
    ]
    assert mediacache.MediaCache.media_files(alias)[0].resolve() == (
        mediacache.MediaCache.media_files(primary)[0].resolve()
    )
    # 别名不该被重复计算：总量就是主条目那份
    only_primary = sum(
        item.stat().st_size for item in primary.rglob("*") if item.is_file()
    )
    assert cache.total_bytes() == only_primary
    # 条目要记下自己对应哪些 key，命中时好把它们一起记进去重
    assert sorted(mediacache.MediaCache.entry_keys(primary)) == [
        "b23.tv_3xdwepo",
        "bilibili:bv1",
    ]


def test_broken_alias_is_cleaned(tmp_path: Path, mediacache) -> None:
    """主条目被删掉后，别名符号链接会在 sweep 时清掉。"""
    cache = mediacache.MediaCache(
        tmp_path / "media", ttl_seconds=60, max_bytes=0
    )
    primary = cache.store(["short"], _files(tmp_path, "a.jpg"))
    alias = cache.entry_dir("full")
    os.symlink(primary.name, alias)

    old = time.time() - 120
    os.utime(primary, (old, old))
    cache.sweep()
    assert not primary.exists()
    assert not alias.exists()
