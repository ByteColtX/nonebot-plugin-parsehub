# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""媒体投递：把本地文件按配置交给协议端。

三种模式：

- ``file``（默认）：直接发本机 ``file://`` 路径，要求协议端与机器人同机；
- ``http``：把文件硬链接进中转服务的随机任务目录，发地址让协议端自己来拉；
- ``base64``：把文件内容内联进消息段，不占端口。

无论哪种模式，文件都必须先落在一个「协议端能读到、且在保留期内不会被清掉」的
位置：缓存目录里的文件会先硬链接进任务目录再发，这样缓存淘汰不会让消息发到一半
文件消失。

所有发送都在这里收口：图片 / 视频 / 文件走同一套 ``try/except``，失败记进
:class:`MediaReport`，不会再让异常穿到 matcher。

模块依赖 OneBot v11 适配器，但不依赖 nonebot 的 matcher。
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed

from .config import Settings, forward_node_name
from .media import MAX_MEDIA_ITEMS, MediaFile, MediaKind
from .relay import MediaRelay

# 字节换算用的常数。
_BYTES_PER_MEGABYTE = 1024 * 1024


class Sender(Protocol):
    """发送消息所需的最小协议端接口。

    ``Bot`` 天然满足这个协议；测试用假对象即可，不必继承适配器。
    """

    async def send(self, event: MessageEvent, message: Message) -> Any:
        """发一条消息。"""
        ...

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        """调用一个 OneBot API。"""
        ...

    @property
    def self_id(self) -> str:
        """机器人自己的 QQ 号。"""
        ...


@dataclass
class RelayEndpoint:
    """媒体中转服务对协议端暴露的地址。

    Attributes:
        host: 协议端可访问的地址，默认本机回环。
        port: 中转服务端口，启动前为 0。
    """

    host: str = "127.0.0.1"
    port: int = 0


@dataclass
class MediaReport:
    """一次媒体投递的结果。

    Attributes:
        sent: 成功发出的文件。
        failed: 发送失败的文件。
        skipped: 因为体积上限被跳过的文件。
    """

    sent: list[MediaFile] = field(default_factory=list)
    failed: list[MediaFile] = field(default_factory=list)
    skipped: list[MediaFile] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """这次投递算不算成功（至少发出去一个、且没有失败）。

        Returns:
            成功为 True。
        """
        return bool(self.sent) and not self.failed


# 合并转发里显示的节点名在配置层取（见 :func:`config.forward_node_name`）。


def _to_base64(path: Path) -> str:
    """把文件读成 ``base64://`` 内联串。

    Args:
        path: 本地文件路径。

    Returns:
        形如 ``base64://<b64>`` 的字符串。
    """
    return "base64://" + base64.b64encode(path.read_bytes()).decode("ascii")


def _segment(file: MediaFile, ref: str) -> MessageSegment | None:
    """把媒体映射成对应的消息段。

    Args:
        file: 待发送的媒体。
        ref: 协议端能取到的引用。

    Returns:
        图片 / 视频 / 语音的消息段；文件类没有对应段，返回 ``None``，由调用方
        改走文件上传。
    """
    if file.kind is MediaKind.IMAGE:
        return MessageSegment.image(file=ref)
    if file.kind is MediaKind.VIDEO:
        return MessageSegment.video(file=ref)
    if file.kind is MediaKind.RECORD:
        return MessageSegment.record(file=ref)
    return None


class Delivery:
    """把本地媒体交给协议端。

    Attributes:
        settings: 归一化后的设置（投递模式、体积上限、合并转发超时）。
        relay: 中转服务。
        endpoint: 协议端可访问的中转地址。
    """

    def __init__(
        self,
        settings: Settings,
        relay: MediaRelay,
        endpoint: RelayEndpoint,
    ) -> None:
        """记住设置与依赖。

        Args:
            settings: 归一化后的设置。
            relay: 中转服务。
            endpoint: 协议端可访问的中转地址。
        """
        self.settings = settings
        self.relay = relay
        self.endpoint = endpoint

    # ---- 引用 ----

    async def ref(self, path: Path) -> str:
        """把本地文件变成协议端能取到的引用。

        Args:
            path: 本地媒体文件路径。

        Returns:
            可直接赋给消息段 ``file`` 字段的字符串。
        """
        mode = self.settings.delivery_mode
        if mode == "base64":
            # 读整份文件 + 编码是纯 CPU/IO 活，放线程池里，不阻塞事件循环
            return await asyncio.to_thread(_to_base64, path)
        served = await self._served_path(path)
        if mode == "file":
            # as_uri() 生成规范里的 file:// 写法，顺带处理 Windows 盘符与转义
            return served.resolve().as_uri()
        return self.relay.url_for(
            served, self.endpoint.host, self.endpoint.port
        )

    async def _served_path(self, path: Path) -> Path:
        """保证文件在保留期内可读：缓存里的文件先硬链接进任务目录。

        Args:
            path: 本地文件路径。

        Returns:
            协议端可读的路径。
        """
        return await asyncio.to_thread(self._link_into_task, path)

    def _link_into_task(self, path: Path) -> Path:
        """把文件放进一个随机任务目录（同盘硬链接，跨盘退回复制）。

        Args:
            path: 本地文件路径。

        Returns:
            任务目录里的路径；已在任务目录下时原样返回。
        """
        root = self.relay.root.resolve()
        resolved = path.resolve()
        if resolved.is_relative_to(root):
            return resolved
        target = self.relay.new_task_dir() / resolved.name
        try:
            os.link(resolved, target)
        except OSError:
            try:
                shutil.copy2(resolved, target)
            except OSError as exc:
                # 返回不存在的 target 只会发出必败的投递，宁可报错让调用方记失败
                logger.warning(f"[parsehub] 媒体复制失败 {resolved}: {exc}")
                raise
        return target

    # ---- 发送 ----

    async def send_text(
        self,
        sender: Sender,
        event: MessageEvent,
        text: str,
    ) -> bool:
        """发一条纯文字消息，失败只记警告。

        Args:
            sender: 协议端接口。
            event: 触发消息事件。
            text: 消息内容。

        Returns:
            发出去了为 True。
        """
        try:
            await sender.send(event, Message(text))
        except Exception as exc:  # 协议端原因很多，统一兜底
            logger.warning(
                f"[parsehub] 消息发送失败: {type(exc).__name__}: {exc}"
            )
            return False
        return True

    async def send_card(
        self,
        sender: Sender,
        event: MessageEvent,
        path: Path,
    ) -> bool:
        """发一张卡片图，失败只记警告（调用方退回文字）。

        Args:
            sender: 协议端接口。
            event: 触发消息事件。
            path: 卡片图路径。

        Returns:
            发出去了为 True。
        """
        try:
            ref = await self.ref(path)
            await sender.send(event, MessageSegment.image(file=ref))
        except Exception as exc:  # 发图失败就退回文字
            logger.warning(
                f"[parsehub] 卡片发送失败: {type(exc).__name__}: {exc}"
            )
            return False
        return True

    async def send_media(
        self,
        sender: Sender,
        event: MessageEvent,
        files: list[MediaFile],
    ) -> MediaReport:
        """发送解析出的媒体文件。

        超过体积上限的文件直接跳过；多于一个文件时优先合并转发，失败或类型不合适
        时退回逐条发送。失败记账在这里完成，调用方据此点表情。

        Args:
            sender: 协议端接口。
            event: 触发消息事件。
            files: 待发送的本地媒体。

        Returns:
            本次投递的结果。
        """
        report = MediaReport()
        items = await asyncio.to_thread(self._filter_by_size, files, report)

        if report.skipped:
            # 聚合成一条：多图集里每个超限文件各发一条会把群刷屏。
            await self.send_text(
                sender,
                event,
                f"（{len(report.skipped)} 个媒体超过体积上限，已跳过）",
            )
        if not items:
            return report

        # 兜底：缓存命中的路径不经过上层截断，这里再保证一次
        items = items[:MAX_MEDIA_ITEMS]

        # 引用只算一次：合并转发与逐条兜底共用同一份，base64 模式不会整文件
        # 重读，file / http 模式不会重复建任务目录。拿不到引用（硬链接和
        # 复制都失败）的文件直接记失败，不发必败的投递。
        refs: list[str] = []
        ready: list[MediaFile] = []
        for file in items:
            try:
                refs.append(await self.ref(file.path))
                ready.append(file)
            except Exception as exc:  # 引用拿不到，发出去也必然失败
                logger.warning(
                    f"[parsehub] 媒体引用获取失败 {file.path}: "
                    f"{type(exc).__name__}: {exc}"
                )
                report.failed.append(file)
                if self.settings.fail_notice:
                    await self.send_text(sender, event, self._fail_text(file))
        items, refs = ready, refs
        if not items:
            return report

        if len(items) > 1 and await self.send_forward(
            sender, event, items, refs
        ):
            report.sent.extend(items)
            return report

        for file, ref in zip(items, refs, strict=True):
            if await self._send_one(sender, event, file, ref):
                report.sent.append(file)
            else:
                report.failed.append(file)
                if self.settings.fail_notice:
                    await self.send_text(sender, event, self._fail_text(file))
        return report

    def _filter_by_size(
        self,
        files: list[MediaFile],
        report: MediaReport,
    ) -> list[MediaFile]:
        """按体积上限过滤，顺手把跳过的记进 ``report``。

        ``stat`` 是磁盘 IO，调用方必须放进线程池。

        Args:
            files: 待发送的本地媒体。
            report: 本次投递的结果，``skipped`` 会被就地追加。

        Returns:
            体积允许发送的媒体。
        """
        limit = self.settings.max_file_bytes
        items: list[MediaFile] = []
        for file in files:
            try:
                size = file.path.stat().st_size
            except OSError:
                report.skipped.append(file)
                continue
            if limit and size > limit:
                report.skipped.append(file)
                continue
            items.append(file)
        return items

    async def _send_one(
        self,
        sender: Sender,
        event: MessageEvent,
        file: MediaFile,
        ref: str,
    ) -> bool:
        """逐条发送一个媒体。

        Args:
            sender: 协议端接口。
            event: 触发消息事件。
            file: 待发送的媒体。
            ref: 预先算好的引用（与合并转发共用，不重复计算）。

        Returns:
            成功为 True。
        """
        try:
            segment = _segment(file, ref)
            if segment is None:  # 文件类没有对应消息段，走上传
                return await self._send_file(sender, event, ref, file.path.name)
            await sender.send(event, segment)
        except ActionFailed as exc:
            logger.warning(f"[parsehub] 媒体被协议端拒绝: {exc}")
            if file.kind is MediaKind.VIDEO:
                # 部分协议端不支持 video 段，但能传群文件；别把能发的东西丢掉。
                logger.info("[parsehub] video 段被拒，改走群文件")
                return await self._send_file(sender, event, ref, file.path.name)
            return False
        except Exception as exc:  # 协议端行为不确定，统一兜底
            logger.warning(
                f"[parsehub] 媒体发送异常: {type(exc).__name__}: {exc}"
            )
            if file.kind is MediaKind.VIDEO:
                return await self._send_file(sender, event, ref, file.path.name)
            return False
        return True

    async def _send_file(
        self,
        sender: Sender,
        event: MessageEvent,
        ref: str,
        name: str,
    ) -> bool:
        """以群文件 / 私聊文件的形式上传媒体。

        Args:
            sender: 协议端接口。
            event: 触发消息事件。
            ref: 协议端能取到的文件引用。
            name: 展示用的文件名。

        Returns:
            上传成功返回 True。
        """
        try:
            if isinstance(event, GroupMessageEvent):
                await sender.call_api(
                    "upload_group_file",
                    group_id=event.group_id,
                    file=ref,
                    name=name,
                )
            else:
                await sender.call_api(
                    "upload_private_file",
                    user_id=event.user_id,
                    file=ref,
                    name=name,
                )
        except Exception as exc:  # 协议端可能不支持文件上传
            logger.warning(f"[parsehub] 文件上传失败: {exc}")
            return False
        return True

    async def send_forward(
        self,
        sender: Sender,
        event: MessageEvent,
        files: list[MediaFile],
        refs: list[str],
    ) -> bool:
        """把多个媒体合并成一条转发消息。

        Args:
            sender: 协议端接口。
            event: 触发消息事件。
            files: 待发送的本地媒体。
            refs: 与 ``files`` 一一对应的引用（调用方只算一次）。

        Returns:
            合并转发成功返回 True；媒体类型不适合转发时返回 False，由调用方改走
            逐条发送。
        """
        unsuitable = (MediaKind.FILE, MediaKind.RECORD)
        if any(file.kind in unsuitable for file in files):
            return False
        # 昵称直接取配置（没配就用 "ParseHub"），节点构造是纯内存活
        name = forward_node_name()
        nodes = [
            {
                "type": "node",
                "data": {
                    "uin": str(sender.self_id),
                    "name": name,
                    "content": [
                        {
                            "type": str(file.kind),
                            "data": {"file": ref},
                        }
                    ],
                },
            }
            for file, ref in zip(files, refs, strict=True)
        ]

        timeout = max(1, self.settings.forward_timeout)
        started = time.monotonic()
        try:
            if isinstance(event, GroupMessageEvent):
                await sender.call_api(
                    "send_group_forward_msg",
                    group_id=event.group_id,
                    messages=nodes,
                    _timeout=timeout,
                )
            else:
                await sender.call_api(
                    "send_private_forward_msg",
                    user_id=event.user_id,
                    messages=nodes,
                    _timeout=timeout,
                )
        except Exception as exc:  # 协议端可能不支持合并转发
            logger.warning(
                f"[parsehub] 合并转发失败（等了 "
                f"{time.monotonic() - started:.1f}s，上限 {timeout}s），"
                f"改逐条发送: {exc}"
            )
            return False

        logger.info(
            f"[parsehub] 已合并转发 {len(nodes)} 条"
            f"（耗时 {time.monotonic() - started:.1f}s）"
        )
        return True

    @staticmethod
    def _fail_text(file: MediaFile) -> str:
        """单条媒体发送失败时补的提示。

        Args:
            file: 失败的媒体。

        Returns:
            提示文案。
        """
        if file.kind is MediaKind.VIDEO:
            return "⚠️ 视频发送失败（协议端拒绝了 video 消息段）"
        if file.kind is MediaKind.RECORD:
            return "⚠️ 语音发送失败（协议端拒绝了 record 消息段）"
        if file.kind is MediaKind.FILE:
            return "⚠️ 文件发送失败"
        return "⚠️ 图片发送失败"

    async def send_file_audio(
        self,
        sender: Sender,
        event: MessageEvent,
        file: MediaFile,
    ) -> bool:
        """以群文件 / 私聊文件的形式上传音频（bm / ym 的 -f 模式）。

        Args:
            sender: 协议端接口。
            event: 触发消息事件。
            file: 待上传的音频文件（文件名已是清洗后的标题）。

        Returns:
            上传成功返回 True。
        """
        try:
            ref = await self.ref(file.path)
            return await self._send_file(sender, event, ref, file.path.name)
        except Exception as exc:  # 协议端行为不确定，统一兜底
            logger.warning(
                f"[parsehub] 音频文件上传失败: {type(exc).__name__}: {exc}"
            )
            return False
