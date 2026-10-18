"""合并转发节点名的解析测试。

节点名要跟随 ``NICKNAME``，而且必须是「第一项」——nonebot 把 NICKNAME 收成
set，顺序会丢，所以这里逐个覆盖三种取值途径。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nonebot import get_driver


@pytest.fixture(autouse=True)
def _clear_cache(plugin, monkeypatch: pytest.MonkeyPatch, delivery):
    """每个用例前清掉缓存和环境变量，保证互不干扰。"""
    monkeypatch.delenv("NICKNAME", raising=False)
    delivery.forward_node_name.cache_clear()
    yield
    delivery.forward_node_name.cache_clear()


def test_uses_first_nickname_from_env(
    plugin, monkeypatch: pytest.MonkeyPatch, delivery
) -> None:
    """环境变量里是 JSON 列表时取第一项。"""
    monkeypatch.setenv("NICKNAME", '["粉色奶龙", "Anon"]')

    assert delivery.forward_node_name() == "粉色奶龙"


def test_uses_first_nickname_from_env_file(
    plugin, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, delivery
) -> None:
    """环境变量没设时，从 nonebot 声明的 .env 文件里按书写顺序取第一项。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'NICKNAME=["粉色奶龙", "千早爱音", "小爱"]\n', encoding="utf-8"
    )
    config = get_driver().config
    monkeypatch.setitem(config.model_config, "env_file", (str(env_file),))

    assert delivery.forward_node_name() == "粉色奶龙"


def test_plain_value_with_comment(
    plugin, monkeypatch: pytest.MonkeyPatch, delivery
) -> None:
    """单值写法（不是 JSON）也能用，顺带吃掉行尾注释和引号。"""
    monkeypatch.setenv("NICKNAME", '"小爱"  # 就一个昵称')

    assert delivery.forward_node_name() == "小爱"


def test_falls_back_when_nothing_configured(
    plugin, monkeypatch: pytest.MonkeyPatch, delivery
) -> None:
    """完全没配昵称时退回老行为，不留空名字。"""
    config = get_driver().config
    monkeypatch.setitem(config.model_config, "env_file", ())
    monkeypatch.setattr(config, "nickname", set(), raising=False)

    assert delivery.forward_node_name() == "ParseHub"
