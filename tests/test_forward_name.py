"""合并转发节点名的测试。

有昵称就用配置里的昵称，没有就退回 ``"ParseHub"``。
"""

from __future__ import annotations

from nonebot import get_driver


def test_uses_configured_nickname(plugin, monkeypatch) -> None:
    """配置了昵称时用它（集合里的任意一个）。"""
    config = get_driver().config
    monkeypatch.setattr(config, "nickname", {"粉色奶龙"})

    assert plugin.delivery.forward_node_name() == "粉色奶龙"


def test_uses_one_of_configured_nicknames(plugin, monkeypatch) -> None:
    """多个昵称时取其中一个，不要求顺序稳定。"""
    config = get_driver().config
    monkeypatch.setattr(config, "nickname", {"小爱", "Anon"})

    assert plugin.delivery.forward_node_name() in {"小爱", "Anon"}


def test_falls_back_when_nothing_configured(plugin, monkeypatch) -> None:
    """完全没配昵称时退回老行为，不留空名字。"""
    config = get_driver().config
    monkeypatch.setattr(config, "nickname", set(), raising=False)

    assert plugin.delivery.forward_node_name() == "ParseHub"
