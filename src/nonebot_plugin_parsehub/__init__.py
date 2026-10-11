# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""ParseHub 链接解析插件（OneBot v11）。

群里出现受支持的分享链接时自动解析，并把视频 / 图集 / 正文发回群里；也可以
用命令 ``解析 <链接或分享文案>`` 手动触发。

处理流程：

1. 收到链接立刻给原消息点一个表情回应（不等解析结果）；
2. 解析出平台、标题、正文和媒体直链；
3. 下载媒体到本机临时目录，再用内置 HTTP 服务给协议端一个 http 地址；
4. 媒体多于一个时合并转发，否则逐条发送。

解析与下载全部由 ``parsehub`` 完成，本插件只负责调度和发送。parsehub 是同步
阻塞的（内部还用了 requests），因此所有调用都通过 ``asyncio.to_thread`` 执行，
不会阻塞 NoneBot 的事件循环。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from nonebot import get_driver, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

# 包内用相对导入：插件既可能作为已安装的包加载，也可能作为本地插件被放在
# src/plugins 下（此时模块名带前缀），绝对自引用在后一种情况下会失败。
from . import card, core, mediacache, server, switches
from .config import Config, plugin_config

__plugin_meta__ = PluginMetadata(
    name="ParseHub 链接解析",
    description=(
        "群里出现受支持的分享链接时，用 ParseHub 自动解析并发送媒体"
        "（视频 / 图集 / 正文）。"
    ),
    usage=(
        "发链接即可自动解析；命令：解析 <链接或分享文案>、解析平台、"
        "解析开关 [开|关]（超管 / 群管理）、解析状态"
    ),
    type="application",
    homepage="https://github.com/ByteColtX/nonebot-plugin-parsehub",
    config=Config,
    supported_adapters={"~onebot.v11"},
    extra={"parsehub": "https://github.com/z-mio/ParseHub"},
)

# 表情回应用的 QQ 表情 ID，写死在代码里（改动请直接改这里）。
REACT_START = "424"  # 收到链接，立即回应
REACT_OK = "144"  # 视频发送成功
REACT_FAIL = "5"  # 视频发送失败

# 合并转发里显示的节点名。
_FORWARD_NODE_NAME = "ParseHub"

# 判断媒体类型用的后缀集合。
_IMAGE_EXTENSIONS = frozenset(
    {
        ".avif",
        ".bmp",
        ".gif",
        ".heic",
        ".ico",
        ".jpeg",
        ".jpg",
        ".png",
        ".webp",
    }
)
_VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".flv",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".ts",
        ".webm",
        ".wmv",
    }
)

# 单条消息最多处理几个链接。
_MAX_LINKS_PER_MESSAGE = 3

# 临时文件清扫间隔（秒）。
_SWEEP_INTERVAL_SECONDS = 300

# 命令触发时用来跳过自动解析的前缀。
_COMMAND_PREFIXES = ("解析", "ph", "/解析", "/ph")

# 字节换算用的常数。
_BYTES_PER_MEGABYTE = 1024 * 1024

# 去重缓存：同一链接在窗口期内只解析一次。
DEDUPE = core.DedupeCache()

# 按会话（群 / 私聊）记录的解析开关，落在插件数据目录里。
SWITCHES = switches.SwitchStore(server.data_root() / "sessions.json")

# 作品级媒体缓存：命中就直接发本地那份，不解析、不下载、不画卡。
MEDIA_CACHE = mediacache.MediaCache(
    server.cache_root() / mediacache.CACHE_DIR_NAME,
    ttl_seconds=max(0, plugin_config.parsehub_cache_hours) * 3600,
    max_bytes=max(0, plugin_config.parsehub_cache_max_mb) * _BYTES_PER_MEGABYTE,
)

# 「解析开关」命令能接受的参数写法。
_ON_WORDS = frozenset({"开", "开启", "打开", "on", "true", "1"})
_OFF_WORDS = frozenset({"关", "关闭", "关上", "off", "false", "0"})
_STATUS_WORDS = frozenset({"状态", "status"})


@dataclass
class _RelayEndpoint:
    """媒体中转服务对协议端暴露的地址。

    Attributes:
        host: 协议端可访问的地址，默认本机回环。
        port: 中转服务端口，启动前为 0。
    """

    host: str = "127.0.0.1"
    port: int = 0


_ENDPOINT = _RelayEndpoint()
_SEMAPHORE: asyncio.Semaphore | None = None


@dataclass
class _SentInfo:
    """一次「卡片或文字」发送的结果，顺手带上内容好写进媒体缓存。

    Attributes:
        sent: 是否真的发出去了。
        card: 发出去的卡片图路径，没出卡为 ``None``。
        caption: 发出去的文字，没发文字为空串。
    """

    sent: bool = False
    card: Path | None = None
    caption: str = ""


def _concurrency_limit() -> asyncio.Semaphore:
    """返回限制并发任务数的信号量，必要时先创建。

    Returns:
        进程内共享的信号量，上限取自配置，至少为 1。
    """
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(
            max(1, plugin_config.parsehub_concurrency)
        )
    return _SEMAPHORE


def _size_limit_bytes() -> int:
    """返回单文件体积上限。

    Returns:
        上限的字节数，配置为 0 表示不限制。
    """
    return max(0, plugin_config.parsehub_max_mb) * _BYTES_PER_MEGABYTE


def _excerpt(text: str, limit: int) -> str:
    """把正文压成单行并截断。

    Args:
        text: 原始正文。
        limit: 最大字符数。

    Returns:
        压缩后的正文，超长时以省略号结尾。
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def _is_unknown_platform(exc: Exception) -> bool:
    """判断异常是否表示「平台不支持」。

    parsehub 的 import 代价很高，这里延迟导入它的异常类型。

    Args:
        exc: 捕获到的异常。

    Returns:
        是 UnknownPlatform 时返回 True。
    """
    try:
        from parsehub.errors import UnknownPlatform
    except ImportError:
        return False
    return isinstance(exc, UnknownPlatform)


async def _send(
    bot: Bot,
    event: MessageEvent,
    message: Message,
) -> None:
    """发送一条消息。

    Args:
        bot: 当前 Bot 实例。
        event: 触发本次发送的事件。
        message: 待发送的消息。
    """
    await bot.send(event, message)


async def _react(bot: Bot, event: MessageEvent, emoji_id: str) -> None:
    """给触发消息点一个表情回应。

    协议端不支持该 API 时只记 debug 日志，不影响主流程。

    Args:
        bot: 当前 Bot 实例。
        event: 触发消息所在的事件。
        emoji_id: QQ 表情 ID。
    """
    try:
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=event.message_id,
            emoji_id=emoji_id,
            set=True,
        )
    except Exception as exc:  # noqa: BLE001 - 回应只是锦上添花
        logger.debug(f"[parsehub] 表情回应 {emoji_id} 失败（忽略）: {exc}")


async def _send_file(
    bot: Bot,
    event: MessageEvent,
    url: str,
    name: str,
) -> bool:
    """以群文件 / 私聊文件的形式上传媒体。

    Args:
        bot: 当前 Bot 实例。
        event: 触发消息事件。
        url: 协议端可下载的地址。
        name: 展示用的文件名。

    Returns:
        上传成功返回 True。
    """
    try:
        if isinstance(event, GroupMessageEvent):
            await bot.call_api(
                "upload_group_file",
                group_id=event.group_id,
                file=url,
                name=name,
            )
        else:
            await bot.call_api(
                "upload_private_file",
                user_id=event.user_id,
                file=url,
                name=name,
            )
    except Exception as exc:  # noqa: BLE001 - 协议端可能不支持文件上传
        logger.warning(f"[parsehub] 文件上传失败: {exc}")
        return False
    return True


async def _send_video(
    bot: Bot,
    event: MessageEvent,
    url: str,
    name: str,
) -> bool:
    """用 video 消息段发送视频（不占群文件）。

    Args:
        bot: 当前 Bot 实例。
        event: 触发消息事件。
        url: 协议端可下载的地址。
        name: 展示用的文件名，仅用于日志。

    Returns:
        发送成功返回 True。
    """
    try:
        await bot.send(event, MessageSegment.video(file=url))
    except ActionFailed as exc:
        logger.warning(f"[parsehub] video 消息段被拒绝: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - 协议端行为不确定，统一兜底
        logger.warning(f"[parsehub] video 消息段发送异常: {exc}")
        return False
    return True


async def _send_forward(
    bot: Bot,
    event: MessageEvent,
    files: list[Path],
) -> bool:
    """把多个媒体合并成一条转发消息。

    Args:
        bot: 当前 Bot 实例。
        event: 触发消息事件。
        files: 待发送的本地文件列表。

    Returns:
        合并转发成功返回 True；媒体类型不适合转发时返回 False，由调用方改走
        逐条发送。
    """
    nodes: list[dict] = []
    for path in files:
        suffix = path.suffix.lower()
        url = server.RELAY.url_for(path, _ENDPOINT.host, _ENDPOINT.port)
        if suffix in _IMAGE_EXTENSIONS:
            content = {"type": "image", "data": {"file": url}}
        elif suffix in _VIDEO_EXTENSIONS:
            content = {"type": "video", "data": {"file": url}}
        else:
            return False
        nodes.append(
            {
                "type": "node",
                "data": {
                    "uin": str(bot.self_id),
                    "name": _FORWARD_NODE_NAME,
                    "content": [content],
                },
            }
        )

    timeout = max(1, plugin_config.parsehub_forward_timeout)
    started = time.monotonic()
    try:
        if isinstance(event, GroupMessageEvent):
            await bot.call_api(
                "send_group_forward_msg",
                group_id=event.group_id,
                messages=nodes,
                _timeout=timeout,
            )
        else:
            await bot.call_api(
                "send_private_forward_msg",
                user_id=event.user_id,
                messages=nodes,
                _timeout=timeout,
            )
    except Exception as exc:  # noqa: BLE001 - 协议端可能不支持合并转发
        logger.warning(
            f"[parsehub] 合并转发失败（等了 {time.monotonic() - started:.1f}s，"
            f"上限 {timeout}s），改逐条发送: {exc}"
        )
        return False

    logger.info(
        f"[parsehub] 已合并转发 {len(nodes)} 条"
        f"（耗时 {time.monotonic() - started:.1f}s）"
    )
    return True


async def _send_media_files(
    bot: Bot,
    event: MessageEvent,
    files: list[Path],
) -> None:
    """发送解析出的媒体文件。

    超过体积上限的文件直接跳过；多于一个文件时优先合并转发，失败或类型不合适
    时退回逐条发送。发送结束后用表情回应表示成功或失败。

    Args:
        bot: 当前 Bot 实例。
        event: 触发消息事件。
        files: 待发送的本地文件列表。
    """
    limit = _size_limit_bytes()
    items: list[Path] = []
    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if limit and size > limit:
            await _send(
                bot,
                event,
                Message(
                    f"（{path.name} 约 {size / _BYTES_PER_MEGABYTE:.0f}MB，"
                    "超过上限，已跳过）"
                ),
            )
            continue
        items.append(path)

    if not items:
        return

    if len(items) > 1 and await _send_forward(bot, event, items):
        await _react(bot, event, REACT_OK)
        return

    failed = False
    for path in items:
        url = server.RELAY.url_for(path, _ENDPOINT.host, _ENDPOINT.port)
        suffix = path.suffix.lower()
        if suffix in _IMAGE_EXTENSIONS:
            await bot.send(event, MessageSegment.image(file=url))
        elif suffix in _VIDEO_EXTENSIONS:
            if await _send_video(bot, event, url, path.name):
                continue
            failed = True
            if plugin_config.parsehub_fail_notice:
                await _send(
                    bot,
                    event,
                    Message("⚠️ 视频发送失败（协议端拒绝了 video 消息段）"),
                )
        elif not await _send_file(bot, event, url, path.name):
            failed = True
            if plugin_config.parsehub_fail_notice:
                await _send(bot, event, Message("⚠️ 文件发送失败"))

    await _react(bot, event, REACT_FAIL if failed else REACT_OK)


async def _collect_local_files(result: object) -> list[Path]:
    """把下载结果里的本地文件收集成列表。

    实况照片会同时产出图片和一段短视频，两个都要发，否则实况照片会退化成静态图。

    Args:
        result: ParseHub 的 DownloadResult。

    Returns:
        本地文件路径列表，可能为空。
    """
    media = getattr(result, "media", None)
    media_list = list(media) if isinstance(media, (list, tuple)) else [media]

    files: list[Path] = []
    live_photos = 0
    for item in media_list:
        path = getattr(item, "path", None)
        if path:
            files.append(Path(path))
        video_path = getattr(item, "video_path", None)
        if video_path:
            files.append(Path(video_path))
            live_photos += 1

    if live_photos:
        logger.info(
            f"[parsehub] 含 {live_photos} 张实况照片，图片 + 视频一起发"
        )
    return files


async def handle_link(
    bot: Bot,
    event: MessageEvent,
    url: str,
    *,
    manual: bool = False,
) -> None:
    """解析一个链接并把结果发出去。

    Args:
        bot: 当前 Bot 实例。
        event: 触发消息事件。
        url: 待解析的链接。
        manual: 是否为命令触发；为 True 时无论成败都会给用户反馈。
    """
    cfg = plugin_config
    # 收到链接就先回应，不等解析结果，也不等并发信号量。
    await _react(bot, event, REACT_START)

    async with _concurrency_limit():
        await _parse_and_send(bot, event, url, manual=manual, cfg=cfg)


async def _parse_and_send(
    bot: Bot,
    event: MessageEvent,
    url: str,
    *,
    manual: bool,
    cfg: Config,
) -> None:
    """解析链接并发送文字与媒体（并发限制已由调用方持有）。

    Args:
        bot: 当前 Bot 实例。
        event: 触发消息事件。
        url: 待解析的链接。
        manual: 是否为命令触发。
        cfg: 当前插件配置。
    """
    # 本地已经有这个作品的媒体就直接发，省掉解析、下载和画卡。
    pre_key = core.content_key(url)
    cached = MEDIA_CACHE.get(pre_key)
    if cached is not None:
        await _send_cached(bot, event, cached, cfg)
        return

    if not await core.CLIENT.is_supported(url):
        if manual:
            await _send(bot, event, Message("这个链接 ParseHub 不支持。"))
        return

    platform = await core.CLIENT.platform_id(url) or ""
    proxy = core.proxy_for(platform)
    cookie = core.platform_cookie(platform, cfg.parsehub_cookie)
    logger.debug(f"[parsehub] {platform} 代理={proxy or '直连'}")

    try:
        result = await asyncio.wait_for(
            core.CLIENT.parse(url, proxy, cookie),
            cfg.parsehub_timeout,
        )
    except TimeoutError:
        if manual:
            await _send(bot, event, Message("解析超时，稍后再试。"))
        return
    except Exception as exc:  # noqa: BLE001 - parsehub 异常类型很多，统一兜底
        if _is_unknown_platform(exc):
            if manual:
                await _send(bot, event, Message("这个链接 ParseHub 不支持。"))
            return
        logger.warning(
            f"[parsehub] 解析失败 {url}: {type(exc).__name__}: {exc}"
        )
        if manual:
            await _send(bot, event, Message(f"解析失败：{exc}"))
        return

    display_name = (
        result.platform.display_name if result.platform else "未知平台"
    )
    title = result.title or "(无标题)"
    logger.info(f"[parsehub] {display_name} 解析成功: {url} -> {title}")

    # 解析结果里的 raw_url 是短链展开后的地址，用它再查一次去重：同一个作品换个
    # 写法（b23.tv/xxx 与 bilibili.com/video/BVxxx）在同一个会话里也只发一次。
    # 手动「解析」是明确要求，不做这层拦截。
    post_key = core.content_key(getattr(result, "raw_url", "") or url)
    if not manual:
        window = max(0, cfg.parsehub_dedupe_seconds)
        if DEDUPE.should_skip(_session_key(event), post_key, window):
            logger.info(f"[parsehub] {post_key} 在本会话刚发过，跳过发送")
            return

    # 短链没命中缓存（缓存里记的是展开后的 key），解析完再查一次：命中就省掉下载
    cached = MEDIA_CACHE.get(post_key)
    if cached is not None:
        await _send_cached(bot, event, cached, cfg)
        return

    if not result.media:
        # 纯文字帖（比如 B 站动态）：没有媒体，只发卡片或文字
        await _send_card_or_text(
            bot, event, result, display_name, [], None, cfg
        )
        return

    media_list = (
        list(result.media)
        if isinstance(result.media, (list, tuple))
        else [result.media]
    )
    if len(media_list) == 1:
        oversized = await _oversized_size(media_list[0], proxy)
        if oversized:
            await _send(
                bot,
                event,
                Message(
                    f"文件约 {oversized / _BYTES_PER_MEGABYTE:.0f}MB，"
                    "超过上限，已跳过。"
                ),
            )
            return

    # 先出卡：thumb_url / 图片媒体通常够用，不必等下载。此时不发文字兜底，
    # 免得下载抽帧那一步白做
    info = await _send_card_or_text(
        bot, event, result, display_name, [], None, cfg, allow_text=False
    )

    downloaded = await _download_task(bot, event, url, proxy, cookie, cfg)
    if downloaded is None:
        return
    task_dir, files = downloaded

    # 上面没出成卡（封面得从视频里抽帧）就带上下载好的文件再试一次
    if not info.sent:
        info = await _send_card_or_text(
            bot, event, result, display_name, files, task_dir, cfg
        )
    if files:
        await _send_media_files(bot, event, files)
        logger.info(
            f"[parsehub] 已发送 {len(files)} 个文件，"
            f"临时目录 {task_dir}（稍后自动清理）"
        )
        # 存两份 key：短链那份让同一个短链下次直接命中，展开那份让别的写法也命中
        MEDIA_CACHE.store(
            (pre_key, post_key), files, card=info.card, caption=info.caption
        )


def _build_caption(result: object, display_name: str, cfg: Config) -> Message:
    """拼装解析结果的文字消息（不含原始链接）。

    Args:
        result: ParseHub 的解析结果。
        display_name: 平台中文名。
        cfg: 当前插件配置。

    Returns:
        待发送的文字消息。
    """
    lines = [f"📦 {display_name}"]
    title = getattr(result, "title", "")
    content = getattr(result, "content", "")
    if title:
        lines.append(title)
    if content:
        lines.append(_excerpt(content, cfg.parsehub_max_chars))
    return Message("\n".join(lines))


async def _oversized_size(media: object, proxy: str) -> int | None:
    """在下载前探测单媒体体积，判断是否超过上限。

    Args:
        media: ParseHub 的媒体引用，需要有 ``url`` 属性。
        proxy: 探测体积时使用的代理。

    Returns:
        超过上限时返回其字节数；没超过或探测不到时返回 None。
    """
    limit = _size_limit_bytes()
    if not limit:
        return None

    size = await core.probe_size(getattr(media, "url", "") or "", proxy=proxy)
    if not size or size <= limit:
        return None

    logger.info(
        f"[parsehub] 媒体约 {size / _BYTES_PER_MEGABYTE:.0f}MB，超过上限"
    )
    return size


async def _download_task(
    bot: Bot,
    event: MessageEvent,
    url: str,
    proxy: str,
    cookie: str,
    cfg: Config,
) -> tuple[Path, list[Path]] | None:
    """下载媒体到新的任务目录。

    Args:
        bot: 当前 Bot 实例。
        event: 触发消息事件。
        url: 待解析的链接。
        proxy: 解析与下载代理。
        cookie: 登录态 cookie。
        cfg: 当前插件配置。

    Returns:
        ``(任务目录, 本地文件列表)``；下载失败时为 ``None``。
    """
    task_dir = server.RELAY.new_task_dir()
    try:
        downloaded = await asyncio.wait_for(
            core.CLIENT.download(url, str(task_dir), proxy, cookie),
            cfg.parsehub_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 下载失败原因很多，统一兜底
        logger.warning(
            f"[parsehub] 下载失败 {url}: {type(exc).__name__}: {exc}"
        )
        await _send(bot, event, Message(f"下载失败：{exc}"))
        return None

    return task_dir, await _collect_local_files(downloaded)


async def _render_card(
    result: object,
    files: list[Path],
    task_dir: Path | None,
    cfg: Config,
) -> Path | None:
    """渲染卡片图（阻塞活走线程池）。

    Args:
        result: ParseHub 解析结果。
        files: 已下载的本地媒体，抽帧兜底要用。
        task_dir: 任务目录；为 ``None`` 时另建一个。
        cfg: 当前插件配置。

    Returns:
        卡片图片路径；不可用时为 ``None``（调用方退回文字）。
    """
    try:
        font = await asyncio.to_thread(card.ensure_font, server.cache_root())
        if font is None:
            return None
        cover = await asyncio.to_thread(card.fetch_cover, result, files)
        if cover is None:
            logger.info("[parsehub] 没有可用封面，卡片跳过")
            return None
        target_dir = task_dir or server.RELAY.new_task_dir()
        out_path = target_dir / "card.jpg"
        await asyncio.to_thread(card.render, result, cover, out_path)
    except Exception as exc:  # noqa: BLE001 - 渲染失败不能让解析白做
        logger.warning(f"[parsehub] 卡片渲染失败: {type(exc).__name__}: {exc}")
        return None
    return out_path


async def _send_card_or_text(
    bot: Bot,
    event: MessageEvent,
    result: object,
    display_name: str,
    files: list[Path],
    task_dir: Path | None,
    cfg: Config,
    *,
    allow_text: bool = True,
) -> _SentInfo:
    """优先发卡片图，卡片不可用时退回文字。

    Args:
        bot: 当前 Bot 实例。
        event: 触发消息事件。
        result: ParseHub 解析结果。
        display_name: 平台中文名。
        files: 已下载的本地媒体。
        task_dir: 任务目录。
        cfg: 当前插件配置。
        allow_text: 出不了卡时是否立刻退回文字。下载前那次调用要传 ``False``，
            否则文字先发了，等下载完再抽帧出卡就成了重复消息。

    Returns:
        发出去了什么（卡片路径 / 文字），用来写媒体缓存。
    """
    if cfg.parsehub_card:
        out_path = await _render_card(result, files, task_dir, cfg)
        if out_path is not None:
            url = server.RELAY.url_for(out_path, _ENDPOINT.host, _ENDPOINT.port)
            try:
                await bot.send(event, MessageSegment.image(file=url))
                return _SentInfo(sent=True, card=out_path)
            except Exception as exc:  # noqa: BLE001 - 发图失败就退回文字
                logger.warning(
                    f"[parsehub] 卡片发送失败: {type(exc).__name__}: {exc}"
                )
    if allow_text and cfg.parsehub_send_text:
        caption = _build_caption(result, display_name, cfg)
        await _send(bot, event, caption)
        return _SentInfo(sent=True, caption=caption.extract_plain_text())
    return _SentInfo()


async def _send_cached(
    bot: Bot,
    event: MessageEvent,
    entry: Path,
    cfg: Config,
) -> bool:
    """把缓存里的卡片 / 文字 + 媒体直接发出去。

    缓存目录就在中转服务的根目录下，所以协议端能直接来拉，不用再复制一份。

    Args:
        bot: 当前 Bot 实例。
        event: 触发消息事件。
        entry: 缓存条目目录。
        cfg: 当前插件配置。

    Returns:
        发出去了为 ``True``。
    """
    files = mediacache.MediaCache.media_files(entry)
    if not files:
        return False

    card = mediacache.MediaCache.card_path(entry)
    if cfg.parsehub_card and card is not None:
        url = server.RELAY.url_for(card, _ENDPOINT.host, _ENDPOINT.port)
        try:
            await bot.send(event, MessageSegment.image(file=url))
        except Exception as exc:  # noqa: BLE001 - 发图失败就退回文字
            logger.warning(
                f"[parsehub] 缓存卡片发送失败: {type(exc).__name__}: {exc}"
            )
            card = None
    if card is None and cfg.parsehub_send_text:
        caption = mediacache.MediaCache.caption_path(entry)
        if caption is not None:
            try:
                await _send(bot, event, Message(caption.read_text("utf-8")))
            except OSError as exc:
                logger.warning(f"[parsehub] 缓存文字读不出来: {exc}")

    logger.info(
        f"[parsehub] 命中媒体缓存，直接发送 {len(files)} 个文件（{entry.name}）"
    )
    await _send_media_files(bot, event, files)
    return True


def _session_key(event: MessageEvent) -> str:
    """取这条消息所属的会话键。

    Args:
        event: 消息事件。

    Returns:
        :mod:`switches` 里的会话键（群按群号，私聊按对方 QQ）。
    """
    if isinstance(event, GroupMessageEvent):
        return switches.group_key(event.group_id)
    return switches.private_key(int(event.get_user_id()))


def _session_name(event: MessageEvent) -> str:
    """给用户看的会话称呼。

    Args:
        event: 消息事件。

    Returns:
        ``本群`` 或 ``本会话``。
    """
    return "本群" if isinstance(event, GroupMessageEvent) else "本会话"


def _is_superuser(event: MessageEvent) -> bool:
    """发消息的人是不是 NoneBot 超管。

    Args:
        event: 消息事件。

    Returns:
        在 ``SUPERUSERS`` 名单里为 ``True``。
    """
    return event.get_user_id() in get_driver().config.superusers


def _is_group_admin(event: MessageEvent) -> bool:
    """发消息的人是不是本群群主或管理员。

    Args:
        event: 消息事件。

    Returns:
        群主 / 管理员为 ``True``；私聊永远是 ``False``。
    """
    return isinstance(event, GroupMessageEvent) and event.sender.role in (
        "owner",
        "admin",
    )


def _can_control(event: MessageEvent) -> bool:
    """有没有权限操作解析开关。

    Args:
        event: 消息事件。

    Returns:
        超管或群管理为 ``True``。
    """
    return _is_superuser(event) or _is_group_admin(event)


async def _auto_rule(event: MessageEvent) -> bool:
    """判断是否应该自动解析这条消息。

    Args:
        event: 收到的消息事件。

    Returns:
        需要自动解析时返回 True。
    """
    if not plugin_config.parsehub_auto:
        return False
    if not SWITCHES.is_on(_session_key(event)):
        return False  # 这个会话被关掉了（私聊默认就是关的，要超管「解析开」）

    text = event.get_plaintext()
    if text.lstrip().startswith(_COMMAND_PREFIXES):
        return False  # 命令触发交给命令匹配器
    return bool(core.extract_urls(text))


auto_matcher = on_message(rule=Rule(_auto_rule), priority=20, block=False)

parse_cmd = on_command(
    "解析",
    aliases={"ph", "解析链接"},
    priority=10,
    block=True,
)
platform_cmd = on_command(
    "解析平台",
    aliases={"ph平台", "解析支持"},
    priority=10,
    block=True,
)
switch_cmd = on_command(
    "解析开关",
    aliases={"解析开", "解析关", "解析状态"},
    priority=10,
    block=True,
)


@parse_cmd.handle()
async def _handle_parse_cmd(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
) -> None:
    """处理 ``解析 <链接>`` 命令。

    Args:
        bot: 当前 Bot 实例。
        event: 触发消息事件。
        args: 命令后面的参数。
    """
    if not SWITCHES.is_on(_session_key(event)) and not _can_control(event):
        await parse_cmd.finish(
            f"{_session_name(event)}的解析已关闭，"
            "只有超管或群管理员能手动解析。"
        )
    text = args.extract_plain_text().strip() or event.get_plaintext()
    urls = core.extract_urls(text)
    if not urls:
        await parse_cmd.finish("用法：解析 <链接或分享文案>")
    for url in urls[:_MAX_LINKS_PER_MESSAGE]:
        await handle_link(bot, event, url, manual=True)


@platform_cmd.handle()
async def _handle_platform_cmd() -> None:
    """处理 ``解析平台`` 命令，列出 ParseHub 支持的平台。"""
    platforms = await core.CLIENT.platforms()
    if not platforms:
        await platform_cmd.finish("ParseHub 没有加载到任何平台。")

    names = "、".join(f"{item['name']}({item['id']})" for item in platforms)
    await platform_cmd.finish(
        f"ParseHub 支持 {len(platforms)} 个平台：\n{names}"
    )


@switch_cmd.handle()
async def _handle_switch_cmd(
    event: MessageEvent,
    args: Message = CommandArg(),
) -> None:
    """处理 ``解析开关`` / ``解析开`` / ``解析关`` / ``解析状态``。

    查看状态谁都可以；改开关只允许超管和本群管理员。

    Args:
        event: 触发消息事件。
        args: 命令后面的参数。
    """
    key = _session_key(event)
    raw = event.get_plaintext().strip()
    head = raw.split(maxsplit=1)[0].lstrip("/#") if raw else ""
    arg = args.extract_plain_text().strip().lower()

    if head == "解析状态" or arg in _STATUS_WORDS:
        state = "开启" if SWITCHES.is_on(key) else "关闭"
        await switch_cmd.finish(f"{_session_name(event)}的解析当前是：{state}")

    if not _can_control(event):
        await switch_cmd.finish("只有超管或本群管理员能改解析开关。")

    if head == "解析开" or arg in _ON_WORDS:
        want = True
    elif head == "解析关" or arg in _OFF_WORDS:
        want = False
    elif arg:
        await switch_cmd.finish(
            "用法：解析开关 [开|关]，不带参数就翻转；解析状态 看当前值"
        )
        return
    else:
        want = not SWITCHES.is_on(key)

    SWITCHES.set(key, want)
    name = _session_name(event)
    logger.info(
        f"[parsehub] {name}解析开关 -> {'开' if want else '关'}"
        f"（{key}，操作人 {event.get_user_id()}）"
    )
    if want:
        await switch_cmd.finish(f"已开启{name}的解析。")
    await switch_cmd.finish(
        f"已关闭{name}的解析：链接不再自动解析，"
        "手动解析也只有超管和群管理员能用。"
    )


@auto_matcher.handle()
async def _handle_auto(bot: Bot, event: MessageEvent) -> None:
    """自动解析消息里的链接。

    Args:
        bot: 当前 Bot 实例。
        event: 触发消息事件。
    """
    urls = core.extract_urls(event.get_plaintext())
    session = _session_key(event)
    window = max(0, plugin_config.parsehub_dedupe_seconds)
    for url in urls[:_MAX_LINKS_PER_MESSAGE]:
        if DEDUPE.should_skip(session, core.content_key(url), window):
            continue
        await handle_link(bot, event, url)


driver = get_driver()


@driver.on_startup
async def _on_startup() -> None:
    """启动媒体中转服务，并开始定期清理临时文件。"""
    cfg = plugin_config
    bind_host = cfg.parsehub_files_host or "0.0.0.0"
    _ENDPOINT.port = server.RELAY.start(bind_host, cfg.parsehub_files_port)
    _ENDPOINT.host = cfg.parsehub_files_host or server.guess_lan_ip()
    logger.info(
        f"[parsehub] 媒体中转服务: "
        f"http://{_ENDPOINT.host}:{_ENDPOINT.port}/  "
        f"根目录: {server.RELAY.root}"
    )
    if cfg.parsehub_card and cfg.parsehub_card_font:
        font_path = Path(cfg.parsehub_card_font).expanduser()
        if font_path.exists():
            card.set_font(font_path)
            logger.info(f"[parsehub] 卡片字体: {font_path}")
        else:
            logger.warning(
                f"[parsehub] 配置的字体不存在: {font_path}，改用自动查找"
            )
    asyncio.create_task(_sweep_expired_media())


async def _sweep_expired_media() -> None:
    """定期清理过期的临时媒体目录。"""
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
        try:
            keep_seconds = plugin_config.parsehub_keep_minutes * 60
            removed = server.RELAY.cleanup(
                keep_seconds, skip={mediacache.CACHE_DIR_NAME}
            )
            expired = MEDIA_CACHE.sweep()
            if expired:
                logger.info(f"[parsehub] 媒体缓存清理了 {expired} 个作品")
        except OSError as exc:
            logger.warning(f"[parsehub] 清理失败: {exc}")
            continue
        if removed:
            logger.info(f"[parsehub] 清理了 {removed} 个过期临时目录")
