# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""媒体缓存与缓存命中路径的测试（既有纯文件操作，也有经 service 的端到端路径）。"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from fake import make_group_event
from fakes import FakeBot, FakeParseResult


def _files(tmp_path: Path, *names: str, size: int = 1024) -> list[Path]:
    """造几个假媒体文件。"""
    out = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"x" * size)
        out.append(path)
    return out


def _age(entry: Path, seconds: int) -> None:
    """把条目的最后使用时间调旧（直接改 meta，TTL 以它为准）。"""
    meta_path = entry / "meta.json"
    meta = json.loads(meta_path.read_text("utf-8"))
    meta["updated_at"] = time.time() - seconds
    meta_path.write_text(json.dumps(meta), "utf-8")


def _meta(entry: Path) -> dict:
    """读条目的 meta。"""
    return json.loads((entry / "meta.json").read_text("utf-8"))


def test_entry_name_is_hashed_and_stable(cache) -> None:
    """目录名由 key 摘要而来：不可猜、同 key 稳定、不同 key 不撞。"""
    name = cache.entry_name("bilibili:BV1yupv6yeue")
    assert name.startswith("h_")
    assert len(name) == len("h_") + 20
    assert name == cache.entry_name("bilibili:BV1yupv6yeue")
    assert name != cache.entry_name("bilibili:BV1yupv6yeuf")
    assert "bilibili" not in name


def test_store_then_get(tmp_path: Path, cache, card) -> None:
    """存进去能取出来，媒体、卡片、文字、时长都在。"""
    store = cache.MediaCache(tmp_path / "media", ttl_seconds=3600, max_bytes=0)
    files = _files(tmp_path, "a.jpg", "b.mp4")
    card = tmp_path / "card.jpg"
    card.write_bytes(b"card")
    entry = store.store(
        ["bilibili:bv1"], files, card=card, caption="标题", seconds=42
    )
    assert entry is not None

    hit = store.get("bilibili:bv1")
    assert hit is not None
    assert hit.root == entry
    assert [item.name for item in hit.files] == ["a.jpg", "b.mp4"]
    assert hit.card is not None and hit.card.read_bytes() == b"card"
    assert hit.caption == "标题"
    assert hit.seconds == 42
    assert hit.keys == ("bilibili:bv1",)


def test_get_missing_key(tmp_path: Path, cache) -> None:
    """没缓存过的 key 返回 None。"""
    store = cache.MediaCache(tmp_path / "media", ttl_seconds=3600, max_bytes=0)
    assert store.get("douyin:123456") is None


def test_disabled_cache(tmp_path: Path, cache) -> None:
    """ttl 为 0 就是不缓存：不写也不读。"""
    store = cache.MediaCache(tmp_path / "media", ttl_seconds=0, max_bytes=0)
    files = _files(tmp_path, "a.jpg")
    assert store.store(["k"], files) is None
    assert store.get("k") is None


def test_store_without_files_is_noop(tmp_path: Path, cache) -> None:
    """没有媒体就不建条目（纯文字帖不进缓存）。"""
    store = cache.MediaCache(tmp_path / "media", ttl_seconds=3600, max_bytes=0)
    assert store.store(["k"], []) is None
    assert store.get("k") is None


def test_expired_entry_is_gone(tmp_path: Path, cache) -> None:
    """超过 ttl 的条目读不到，sweep 会把它删掉。"""
    store = cache.MediaCache(tmp_path / "media", ttl_seconds=60, max_bytes=0)
    entry = store.store(["k"], _files(tmp_path, "a.jpg"))
    assert entry is not None

    _age(entry, 120)
    assert store.get("k") is None
    assert store.sweep() == 1
    assert not entry.exists()


def test_hit_refreshes_ttl(tmp_path: Path, cache) -> None:
    """命中会刷新时间，所以常看的作品不会过期。"""
    store = cache.MediaCache(tmp_path / "media", ttl_seconds=60, max_bytes=0)
    entry = store.store(["k"], _files(tmp_path, "a.jpg"))
    assert entry is not None
    _age(entry, 50)

    assert store.get("k") is not None
    assert time.time() - _meta(entry)["updated_at"] < 5


def test_capacity_evicts_oldest(tmp_path: Path, cache) -> None:
    """超过容量上限时，最久没用过的先被删。"""
    store = cache.MediaCache(
        tmp_path / "media", ttl_seconds=3600, max_bytes=2500
    )
    first = store.store(["k1"], _files(tmp_path, "one.jpg", size=1000))
    second = store.store(["k2"], _files(tmp_path, "two.jpg", size=1000))
    assert first is not None and second is not None

    _age(first, 100)
    third = store.store(["k3"], _files(tmp_path, "three.jpg", size=1000))
    assert third is not None
    store.sweep()
    assert not first.exists(), "最久没用过的那个该被删"
    assert second.exists() and third.exists()


def test_restore_replaces_stale_entry(tmp_path: Path, cache) -> None:
    """过期条目重新缓存时整体替换：不会混进上一轮的文件。"""
    store = cache.MediaCache(tmp_path / "media", ttl_seconds=60, max_bytes=0)
    first = store.store(["k"], _files(tmp_path, "old.jpg"))
    assert first is not None
    _age(first, 120)
    assert store.get("k") is None

    second = store.store(["k"], _files(tmp_path, "new.jpg"))
    assert second == first
    names = sorted(item.name for item in store.media_files(second))
    assert names == ["new.jpg"]


def test_alias_key_points_to_same_files(tmp_path: Path, cache) -> None:
    """两个 key（短链 + 展开）指向同一份文件，容量只算一次。"""
    store = cache.MediaCache(tmp_path / "media", ttl_seconds=3600, max_bytes=0)
    files = _files(tmp_path, "a.jpg", size=2048)
    primary = store.store(["b23.tv_3xdwepo", "bilibili:bv1"], files)
    assert primary is not None

    alias = store.get("bilibili:bv1")
    assert alias is not None
    assert [item.name for item in alias.files] == [
        item.name for item in store.media_files(primary)
    ]
    assert alias.files[0].resolve() == store.media_files(primary)[0].resolve()
    only_primary = sum(
        item.stat().st_size for item in primary.rglob("*") if item.is_file()
    )
    assert store.total_bytes() == only_primary
    assert sorted(alias.keys) == [
        "b23.tv_3xdwepo",
        "bilibili:bv1",
    ]


def test_remember_key_appends(tmp_path: Path, cache) -> None:
    """命中时补记 key，方便把同一个作品的所有写法一起记进去重。"""
    store = cache.MediaCache(tmp_path / "media", ttl_seconds=3600, max_bytes=0)
    entry = store.store(["short"], _files(tmp_path, "a.jpg"))
    assert entry is not None

    def meta_keys() -> list[str]:
        raw = (entry / cache.META_NAME).read_text("utf-8")
        return sorted(json.loads(raw)["keys"])

    store.remember_key(entry, "full")
    assert meta_keys() == ["full", "short"]
    # 重复补记不会写两遍
    store.remember_key(entry, "full")
    assert meta_keys() == ["full", "short"]


def test_corrupt_meta_is_miss(tmp_path: Path, cache) -> None:
    """meta 坏掉就当没有缓存，而不是抛异常。"""
    store = cache.MediaCache(tmp_path / "media", ttl_seconds=3600, max_bytes=0)
    entry = store.store(["k"], _files(tmp_path, "a.jpg"))
    assert entry is not None
    (entry / "meta.json").write_text("{not json", "utf-8")
    assert store.get("k") is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("updated_at", "not-a-number"), ("seconds", "oops")],
)
def test_bad_meta_field_is_miss(
    tmp_path: Path, cache, field: str, value: str
) -> None:
    """meta 字段类型不对也算坏缓存，不能把 get / sweep 打崩。"""
    store = cache.MediaCache(tmp_path / "media", ttl_seconds=3600, max_bytes=0)
    entry = store.store(["k"], _files(tmp_path, "a.jpg"))
    assert entry is not None
    meta = _meta(entry)
    meta[field] = value
    (entry / "meta.json").write_text(json.dumps(meta), "utf-8")

    assert store.get("k") is None
    assert store.sweep() == 0


def test_broken_alias_is_cleaned(tmp_path: Path, cache) -> None:
    """主条目被删掉后，别名符号链接会在 sweep 时清掉。"""
    store = cache.MediaCache(tmp_path / "media", ttl_seconds=60, max_bytes=0)
    primary = store.store(["short"], _files(tmp_path, "a.jpg"))
    assert primary is not None
    alias = store.entry_dir("full")
    os.symlink(primary.name, alias)

    _age(primary, 120)
    store.sweep()
    assert not primary.exists()
    assert not alias.exists()


async def test_post_key_hit_respects_duration_limit(
    instance, service, update_settings, monkeypatch, tmp_path: Path
) -> None:
    """短链展开后按 post_key 命中的缓存条目，也要过时长上限。

    上限开大之前存进来的超时长条目，不能经短链 → 解析 → post_key 这条
    命中路径直接发出去（首次命中有检查，这条漏了）。
    """
    media = tmp_path / "a.mp4"
    media.write_bytes(b"media")
    instance.cache.store(["bilibili:bv1a2xl67emp"], [media], seconds=300)
    update_settings(max_seconds=60)

    monkeypatch.setattr(
        instance.client,
        "platform_id",
        lambda url: asyncio.sleep(0, result="bilibili"),
    )
    monkeypatch.setattr(
        instance.client,
        "is_supported",
        lambda url: asyncio.sleep(0, result=True),
    )

    async def fake_parse(url, proxy="", cookie=""):
        return FakeParseResult(
            raw_url="https://www.bilibili.com/video/BV1A2xL67EMP"
        )

    monkeypatch.setattr(instance.client, "parse", fake_parse)

    bot = FakeBot()
    url = "https://b23.tv/abcd"
    result = await service.handle(instance, bot, make_group_event(url), url)

    assert result.outcome is not service.Outcome.FROM_CACHE, (
        "超时长的条目不能从缓存直接发"
    )
