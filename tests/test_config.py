"""配置校验与派生设置。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_defaults(plugin, config_mod) -> None:
    """零配置即可加载，且默认值与文档一致。"""
    cfg = config_mod()
    assert cfg.parsehub_delivery == "file"
    assert cfg.parsehub_timeout == 180
    assert cfg.parsehub_max_file_mb == 100
    assert cfg.parsehub_task_ttl_minutes == 30
    assert cfg.parsehub_cache_ttl_hours == 24


@pytest.mark.parametrize(
    "kwargs",
    [
        {"parsehub_timeout": 0},
        {"parsehub_task_ttl_minutes": 0},
        {"parsehub_concurrency": 0},
        {"parsehub_max_chars": 0},
        {"parsehub_forward_timeout": 0},
        {"parsehub_relay_port": 0},
        {"parsehub_relay_port": 70000},
        {"parsehub_delivery": "ftp"},
    ],
)
def test_invalid_values_are_rejected(plugin, kwargs, config_mod) -> None:
    """0 / 负数 / 非法枚举在启动时就拒绝，而不是运行到一半才炸。"""
    with pytest.raises(ValidationError):
        config_mod(**kwargs)


def test_env_style_values_are_coerced(plugin, config_mod) -> None:
    """.env 里的字符串数字要能接住。"""
    cfg = config_mod(parsehub_max_file_mb="100", parsehub_auto="false")
    assert cfg.parsehub_max_file_mb == 100
    assert cfg.parsehub_auto is False


def test_settings_normalise_units_and_platforms(plugin, config_mod) -> None:
    """Settings 把 MB / 小时 / 平台别名都归一掉。"""
    cfg = config_mod(
        parsehub_max_file_mb=100,
        parsehub_cache_ttl_hours=2,
        parsehub_task_ttl_minutes=5,
        parsehub_platforms=["小红书", "B站"],
        parsehub_proxy="http://127.0.0.1:7890",
        parsehub_proxy_platforms=["youtube"],
        parsehub_card_font="~/font.ttf",
    )
    settings = plugin.config.Settings.from_config(cfg)

    assert settings.max_file_bytes == 100 * 1024 * 1024
    assert settings.cache_ttl_seconds == 2 * 3600
    assert settings.task_ttl_seconds == 5 * 60
    assert settings.allowed_platforms == {"xhs", "bilibili"}
    assert settings.platform_allowed("xhs")
    assert not settings.platform_allowed("douyin")
    assert settings.proxy_for("youtube") == "http://127.0.0.1:7890"
    assert settings.proxy_for("bilibili") == ""
    assert settings.card_font is not None
    assert settings.card_font.name == "font.ttf"


def test_settings_without_whitelist_allows_everything(
    plugin, config_mod
) -> None:
    """白名单留空 = 全部平台。"""
    settings = plugin.config.Settings.from_config(config_mod())
    assert settings.allowed_platforms == frozenset()
    assert settings.platform_allowed("anything")
