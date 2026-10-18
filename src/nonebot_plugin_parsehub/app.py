# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""插件装配与生命周期。

``App`` 显式持有全部状态（设置、客户端、缓存、去重、开关、中转、投递、卡片），
由 ``__init__`` 在导入时构建一次；测试可自建实例（临时目录 + 假客户端），
不再依赖模块级单例，也不需要 monkeypatch 模块全局。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from nonebot import logger

from . import paths
from .cache import MediaCache
from .card import CardRenderer
from .config import Settings
from .dedupe import Dedupe
from .delivery import Delivery, RelayEndpoint
from .parsehub_client import ParseHubClient
from .relay import MediaRelay, guess_lan_ip
from .switches import SwitchStore

# 临时目录清扫间隔（秒）。
SWEEP_INTERVAL_SECONDS = 300


@dataclass
class App:
    """插件运行期依赖的集合。

    Attributes:
        settings: 归一化后的设置。
        client: ParseHub 客户端。
        cache: 作品级媒体缓存。
        dedupe: 会话级去重。
        switches: 按会话的解析开关。
        relay: 中转服务。
        delivery: 媒体投递。
        cards: 卡片渲染。
        endpoint: 协议端可访问的中转地址。
        semaphore: 同时处理的任务数限制。
        _tasks: 后台任务（预热、清扫），关闭时取消。
    """

    settings: Settings
    client: ParseHubClient
    cache: MediaCache
    dedupe: Dedupe
    switches: SwitchStore
    relay: MediaRelay
    delivery: Delivery
    cards: CardRenderer
    endpoint: RelayEndpoint
    semaphore: asyncio.Semaphore
    _tasks: list[asyncio.Task] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        cache_dir: Path | None = None,
        relay_root: Path | None = None,
        data_dir: Path | None = None,
    ) -> App:
        """按设置装配一个实例。

        Args:
            settings: 归一化后的设置。
            cache_dir: 缓存根目录，默认走 localstore。
            relay_root: 中转根目录，默认 ``<cache>/relay``。
            data_dir: 数据目录，默认走 localstore。

        Returns:
            装配好的 ``App``。
        """
        cache_root = cache_dir or paths.cache_root()
        data_root = data_dir or paths.data_root()
        relay = MediaRelay(relay_root or cache_root / paths.RELAY_DIR_NAME)
        endpoint = RelayEndpoint()
        return cls(
            settings=settings,
            client=ParseHubClient(),
            cache=MediaCache(
                cache_root / paths.MEDIA_DIR_NAME,
                ttl_seconds=settings.cache_ttl_seconds,
                max_bytes=settings.cache_max_bytes,
            ),
            dedupe=Dedupe(),
            switches=SwitchStore(data_root / "sessions.json"),
            relay=relay,
            delivery=Delivery(settings, relay, endpoint),
            cards=CardRenderer(cache_root, relay.new_task_dir),
            endpoint=endpoint,
            semaphore=asyncio.Semaphore(settings.concurrency),
        )

    async def startup(self) -> None:
        """启动钩子：按投递模式启动中转、应用字体、预热并开始清扫。

        Raises:
            RuntimeError: ``http`` 模式下中转服务起不来（端口被占/地址不可用）。
        """
        if self.settings.delivery_mode == "http":
            # 绑定所有网卡；PARSEHUB_RELAY_HOST 只是「告诉协议端来这个地址」，
            # 允许填公网域名 / NAT 地址，不能拿来当 bind 地址。
            try:
                self.endpoint.port = self.relay.start(
                    "0.0.0.0", self.settings.relay_port
                )
            except OSError as exc:
                raise RuntimeError(
                    "[parsehub] 媒体中转服务启动失败"
                    f"（0.0.0.0:{self.settings.relay_port}）：{exc}。"
                    "请换端口或改用 PARSEHUB_DELIVERY=file。"
                ) from exc
            self.endpoint.host = self.settings.relay_host or guess_lan_ip()
            logger.info(
                f"[parsehub] 媒体中转服务: "
                f"http://{self.endpoint.host}:{self.endpoint.port}/  "
                f"根目录: {self.relay.root}"
            )
        else:
            logger.info(
                f"[parsehub] 媒体上传走 {self.settings.delivery_mode}，"
                "中转服务未启动（不占端口）；"
                f"根目录: {self.relay.root}"
            )

        font = self.settings.card_font
        if self.settings.card_enabled and font is not None:
            if font.exists():
                self.cards.set_font(font)
                logger.info(f"[parsehub] 卡片字体: {font}")
            else:
                logger.warning(
                    f"[parsehub] 配置的字体不存在: {font}，改用自动查找"
                )

        self._tasks.append(asyncio.create_task(self._warm()))
        self._tasks.append(asyncio.create_task(self._sweep_forever()))

    async def shutdown(self) -> None:
        """关闭钩子：取消并等待后台任务结束。"""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _warm(self) -> None:
        """后台预热 parsehub（首次 import numpy + opencv 要几秒）。"""
        try:
            await self.client.hub()
        except Exception as exc:  # noqa: BLE001 - 预热失败不影响按需加载
            logger.warning(f"[parsehub] 预热失败: {type(exc).__name__}: {exc}")
        else:
            logger.info("[parsehub] parsehub 已预热")

    async def _sweep_forever(self) -> None:
        """定期清理过期的任务目录与媒体缓存。"""
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                removed = await asyncio.to_thread(
                    self.relay.cleanup, self.settings.task_ttl_seconds
                )
                expired = await asyncio.to_thread(self.cache.sweep)
            except Exception as exc:  # noqa: BLE001 - 清扫失败不能停掉循环
                logger.warning(f"[parsehub] 清理失败: {exc}")
                continue
            if removed:
                logger.info(f"[parsehub] 清理了 {removed} 个过期临时目录")
            if expired:
                logger.info(f"[parsehub] 媒体缓存清理了 {expired} 个作品")
