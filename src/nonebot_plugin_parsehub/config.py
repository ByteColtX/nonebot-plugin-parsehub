# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""插件配置与派生设置。

两层：

- :class:`Config` 是 NoneBot 从 ``.env`` / 环境变量读进来的原始配置，字段名即
  配置名（大小写不敏感），例如 ``parsehub_max_file_mb`` 对应
  ``PARSEHUB_MAX_FILE_MB=100``。所有数值项都带下界校验：低于下界会在启动时直接
  报错，而不是运行到一半才炸（标 ``0`` = 不限的项允许 0）。
- :class:`Settings` 是把 ``Config`` 归一化之后的只读快照（字节数、秒数、平台
  集合、字体路径）。运行期只读它，测试可以用 ``dataclasses.replace`` 改一项。

注意：``.env`` 里的值会先被解析成 int / bool / list 再做校验，因此数字类配置
必须声明成 ``int``，列表类配置必须是合法 JSON（``["twitter", "youtube"]``），
否则插件会在加载阶段直接失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nonebot import get_plugin_config
from pydantic import BaseModel, Field

from .platforms import OVERSEAS_PLATFORMS, normalise_platform

# 媒体交给协议端的方式。
DeliveryMode = Literal["file", "http", "base64"]

_BYTES_PER_MEGABYTE = 1024 * 1024


class Config(BaseModel):
    """ParseHub 链接解析插件配置。

    Attributes:
        parsehub_auto: 群里出现受支持的链接时是否自动解析。具体到某个会话
            （群 / 私聊）的开与关用命令 ``解析开关`` 控制，不在这里配。
        parsehub_dedupe_seconds: 同一链接在**同一个会话**里的去重窗口（秒）。
            跨会话不去重：同一个链接在别的群 / 私聊里照样解析。
        parsehub_concurrency: 同时处理的任务数上限。
        parsehub_proxy: 解析 / 下载代理地址，留空表示直连。
        parsehub_proxy_platforms: 只对这些平台走代理，留空表示所有平台都走。
        parsehub_cookie: 兜底 cookie，优先使用 ParseHub 自己的 cookies.toml。
        parsehub_timeout: 一次解析工作的总超时（秒），解析与下载共用。
        parsehub_max_file_mb: 单个媒体文件体积上限（MB），超过则跳过。
        parsehub_max_seconds: 视频时长上限（秒），超过只发封面 + 一句提示。
        parsehub_platforms: 平台白名单，留空表示全部。
        parsehub_cache_ttl_hours: 媒体缓存保留小时数，0 表示关闭缓存。
        parsehub_cache_max_mb: 媒体缓存总容量上限（MB）。
        parsehub_card: 是否把解析结果画成卡片图发送（失败自动退回文字）。
        parsehub_card_font: 中文字体文件路径，留空则自动找系统字体或下载。
        parsehub_send_text: 是否发送标题与正文文字。
        parsehub_max_chars: 正文最多发送的字符数。
        parsehub_forward_timeout: 合并转发等待协议端的秒数。
        parsehub_fail_notice: 媒体发送失败时是否额外发文字提示。
        parsehub_delivery: 媒体交给协议端的方式。``file``（默认）直接发本机
            ``file://`` 路径，要求协议端与机器人同机、能读同一路径；``http`` 走
            内置只读 HTTP 服务，发地址让协议端自己来拉，协议端在另一台机器上时
            用这个；``base64`` 把文件内容内联进消息段，不占端口，代价是请求体
            大约 1/3 且大文件更容易被拒。
        parsehub_relay_host: 中转服务对外的地址，留空则自动探测内网 IP。
            只在 ``parsehub_delivery=http`` 时生效。
        parsehub_relay_port: 中转服务的端口，同样只在 http 模式下生效。
        parsehub_task_ttl_minutes: 任务目录保留时长（分钟）。
    """

    # ---- 触发 ----
    parsehub_auto: bool = True
    parsehub_dedupe_seconds: int = Field(default=120, ge=0)
    parsehub_concurrency: int = Field(default=2, ge=1)

    # ---- 解析 / 下载 ----
    parsehub_proxy: str = ""
    parsehub_proxy_platforms: list[str] = Field(
        default_factory=lambda: list(OVERSEAS_PLATFORMS)
    )
    parsehub_cookie: str = ""
    parsehub_timeout: int = Field(default=180, ge=5)
    parsehub_max_file_mb: int = Field(default=100, ge=0)
    parsehub_max_seconds: int = Field(default=300, ge=0)
    parsehub_platforms: list[str] = Field(default_factory=list)

    # ---- 发送 ----
    parsehub_cache_ttl_hours: int = Field(default=24, ge=0)
    parsehub_cache_max_mb: int = Field(default=1024, ge=0)
    parsehub_card: bool = True
    parsehub_card_font: str = ""
    parsehub_send_text: bool = True
    parsehub_max_chars: int = Field(default=300, ge=1)
    parsehub_forward_timeout: int = Field(default=60, ge=1)
    parsehub_fail_notice: bool = True

    # ---- 媒体交给协议端的方式 ----
    parsehub_delivery: DeliveryMode = "file"

    # ---- 媒体中转服务 ----
    parsehub_relay_host: str = ""
    parsehub_relay_port: int = Field(default=21473, ge=1, le=65535)
    parsehub_task_ttl_minutes: int = Field(default=30, ge=1)


@dataclass(frozen=True)
class Settings:
    """``Config`` 的归一化只读快照。

    Attributes:
        auto: 是否自动解析。
        dedupe_seconds: 会话内去重窗口（秒）。
        concurrency: 同时处理的任务数。
        proxy: 代理地址，空串表示直连。
        proxy_platforms: 走代理的平台集合；空集表示全部走。
        cookie: 兜底 cookie。
        timeout: 一次工作的总超时（秒）。
        max_file_bytes: 单文件体积上限（字节），0 表示不限。
        max_seconds: 视频时长上限（秒），0 表示不限。
        allowed_platforms: 归一后的平台白名单；空集表示不限。
        cache_ttl_seconds: 媒体缓存保留时长（秒），0 表示关闭。
        cache_max_bytes: 缓存容量上限（字节），0 表示不限。
        card_enabled: 是否出卡片。
        card_font: 配置指定的字体路径。
        send_text: 是否发标题 / 正文。
        max_chars: 正文最大字符数。
        forward_timeout: 合并转发超时（秒）。
        fail_notice: 发送失败时是否补文字提示。
        delivery_mode: 媒体交给协议端的方式。
        relay_host: 中转服务对外地址，空串表示自动探测。
        relay_port: 中转服务端口。
        task_ttl_seconds: 任务目录保留时长（秒）。
    """

    auto: bool
    dedupe_seconds: int
    concurrency: int
    proxy: str
    proxy_platforms: frozenset[str]
    cookie: str
    timeout: int
    max_file_bytes: int
    max_seconds: int
    allowed_platforms: frozenset[str]
    cache_ttl_seconds: int
    cache_max_bytes: int
    card_enabled: bool
    card_font: Path | None
    send_text: bool
    max_chars: int
    forward_timeout: int
    fail_notice: bool
    delivery_mode: DeliveryMode
    relay_host: str
    relay_port: int
    task_ttl_seconds: int

    @classmethod
    def from_config(cls, cfg: Config) -> Settings:
        """把原始配置归一化成设置快照。

        Args:
            cfg: NoneBot 读进来的配置。

        Returns:
            归一化后的只读设置。
        """
        font = cfg.parsehub_card_font.strip()
        return cls(
            auto=cfg.parsehub_auto,
            dedupe_seconds=cfg.parsehub_dedupe_seconds,
            concurrency=cfg.parsehub_concurrency,
            proxy=cfg.parsehub_proxy.strip(),
            proxy_platforms=frozenset(
                item.strip().lower()
                for item in cfg.parsehub_proxy_platforms
                if item.strip()
            ),
            cookie=cfg.parsehub_cookie,
            timeout=cfg.parsehub_timeout,
            max_file_bytes=cfg.parsehub_max_file_mb * _BYTES_PER_MEGABYTE,
            max_seconds=cfg.parsehub_max_seconds,
            allowed_platforms=frozenset(
                normalise_platform(item)
                for item in cfg.parsehub_platforms
                if item.strip()
            ),
            cache_ttl_seconds=cfg.parsehub_cache_ttl_hours * 3600,
            cache_max_bytes=cfg.parsehub_cache_max_mb * _BYTES_PER_MEGABYTE,
            card_enabled=cfg.parsehub_card,
            card_font=Path(font).expanduser() if font else None,
            send_text=cfg.parsehub_send_text,
            max_chars=cfg.parsehub_max_chars,
            forward_timeout=cfg.parsehub_forward_timeout,
            fail_notice=cfg.parsehub_fail_notice,
            delivery_mode=cfg.parsehub_delivery,
            relay_host=cfg.parsehub_relay_host.strip(),
            relay_port=cfg.parsehub_relay_port,
            task_ttl_seconds=cfg.parsehub_task_ttl_minutes * 60,
        )

    def platform_allowed(self, platform_id: str) -> bool:
        """该平台是否在白名单里。

        Args:
            platform_id: parsehub 的平台 id。

        Returns:
            白名单为空（不限）或平台命中时返回 True。
        """
        if not self.allowed_platforms:
            return True
        return platform_id.lower() in self.allowed_platforms

    def proxy_for(self, platform_id: str) -> str:
        """该平台要不要走代理。

        Args:
            platform_id: parsehub 的平台 id。

        Returns:
            代理地址；不需要时为空串。
        """
        if not self.proxy:
            return ""
        if (
            self.proxy_platforms
            and platform_id.lower() not in self.proxy_platforms
        ):
            return ""
        return self.proxy


plugin_config = get_plugin_config(Config)
