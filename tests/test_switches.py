# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""会话解析开关的存储测试（纯逻辑，不依赖 nonebot）。"""

from __future__ import annotations

from pathlib import Path


def test_default_on_for_group_and_private(tmp_path: Path, switches) -> None:
    """群和私聊都默认允许解析。"""
    store = switches.SwitchStore(tmp_path / "sessions.json")
    assert store.is_on(switches.group_key(123456789)) is True
    assert store.is_on(switches.private_key(20000)) is True
    assert store.off_keys() == []


def test_set_off_then_persist(tmp_path: Path, switches) -> None:
    """关掉之后要落盘，换一个实例读回来还是关的。"""
    path = tmp_path / "sessions.json"
    store = switches.SwitchStore(path)
    assert store.set(switches.group_key(123), False) is False
    assert path.exists()

    again = switches.SwitchStore(path)
    assert again.is_on(switches.group_key(123)) is False
    assert again.is_on(switches.group_key(456)) is True
    assert again.off_keys() == ["group:123"]


def test_set_back_to_default_removes_record(tmp_path: Path, switches) -> None:
    """改回默认值就把记录删掉，文件不会越写越大。"""
    path = tmp_path / "sessions.json"
    store = switches.SwitchStore(path)
    store.set(switches.group_key(123), False)
    assert '"group:123"' in path.read_text("utf-8")
    store.set(switches.group_key(123), True)
    assert store.is_on(switches.group_key(123)) is True
    assert store.off_keys() == []
    assert '"group:123"' not in path.read_text("utf-8")


def test_private_disable_is_recorded(tmp_path: Path, switches) -> None:
    """私聊也能单独关掉，关掉是相对默认值的变化所以要落盘。"""
    path = tmp_path / "sessions.json"
    store = switches.SwitchStore(path)
    assert store.set(switches.private_key(20000), False) is False
    assert store.is_on(switches.private_key(20000)) is False
    assert '"private:20000": false' in path.read_text("utf-8")

    again = switches.SwitchStore(path)
    assert again.is_on(switches.private_key(20000)) is False
    again.set(switches.private_key(20000), True)  # 开回来 = 默认值
    assert '"private:20000"' not in path.read_text("utf-8")


def test_toggle(tmp_path: Path, switches) -> None:
    """不带参数就是翻转。"""
    store = switches.SwitchStore(tmp_path / "sessions.json")
    key = switches.group_key(1)
    assert store.toggle(key) is False
    assert store.is_on(key) is False
    assert store.toggle(key) is True
    assert store.is_on(key) is True


def test_broken_file_falls_back_to_default(tmp_path: Path, switches) -> None:
    """文件坏了不能让插件起不来，按默认值处理。"""
    path = tmp_path / "sessions.json"
    path.write_text("{ 这不是 json", "utf-8")
    store = switches.SwitchStore(path)
    assert store.is_on(switches.group_key(1)) is True


def test_non_dict_json_falls_back(tmp_path: Path, switches) -> None:
    """顶层不是对象的 json 也当没有记录。"""
    path = tmp_path / "sessions.json"
    path.write_text("[1, 2, 3]", "utf-8")
    store = switches.SwitchStore(path)
    assert store.is_on(switches.group_key(1)) is True


def test_save_creates_parent_dir(tmp_path: Path, switches) -> None:
    """数据目录还不存在时要能自己建出来。"""
    store = switches.SwitchStore(tmp_path / "deep" / "nested" / "s.json")
    store.set(switches.group_key(1), False)
    assert (tmp_path / "deep" / "nested" / "s.json").exists()


def test_keys_are_namespaced(switches) -> None:
    """群和私聊的键不能撞车。"""
    assert switches.group_key(123) != switches.private_key(123)
    assert switches.group_key(123) == "group:123"
    assert switches.private_key(123) == "private:123"
