# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""B 站直播间封面卡片的数据获取与合成。

ParseHub 不支持直播间链接，这里单独走 B 站直播开放接口拿房间信息，再把结果
拼成和 ParseHub 解析结果同形状的对象，交给现有 :mod:`card` 渲染。这样卡片
绘制、封面下载、版式都复用视频那一条链路，不需要再维护一套画图代码。

接口都是免 cookie 的（只要普通 UA），但毕竟是站方未公开承诺的接口：房间信息
拿不到就返回 ``None``，由 :mod:`service` 退回常规失败路径；主播接口单独挂了
只少一个名字，不阻断卡片。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
from nonebot import logger

from .bilibili import LIVE_HOST, SHORT_HOSTS

_ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
_ANCHOR_URL = (
    "https://api.live.bilibili.com/live_user/v1/UserInfo/get_anchor_in_room"
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://live.bilibili.com/",
}
_ROOM_PATH = re.compile(r"^/(\d+)(?:/|$)")
_TIMEOUT = 15.0


@dataclass(frozen=True)
class LiveInfo:
    """一个直播间的元信息。

    Attributes:
        room_id: 真实房间号（短号会被接口归一成完整号）。
        title: 直播间标题。
        uname: 主播名。
        user_cover: 静态封面 URL，可能为空串。
        keyframe: 当前画面帧 URL，未开播或拿不到时为空串。
        live_status: 0 未开播，1 直播中，2 轮播。
    """

    room_id: int
    title: str
    uname: str
    user_cover: str
    keyframe: str
    live_status: int


def live_url(room_id: int) -> str:
    """拼出标准直播间链接。

    Args:
        room_id: 房间号。

    Returns:
        ``https://live.bilibili.com/<room_id>``。
    """
    return f"https://{LIVE_HOST}/{room_id}"


def is_live_url(url: str) -> bool:
    """判断链接是不是 B 站直播间。

    Args:
        url: 链接。

    Returns:
        主机是 ``live.bilibili.com`` 且路径以房间号开头时为 ``True``。
    """
    parsed = urlsplit(url or "")
    host = (parsed.hostname or "").lower()
    if host != LIVE_HOST:
        return False
    return bool(_ROOM_PATH.match(parsed.path or ""))


def is_short_link(url: str) -> bool:
    """判断链接是不是 B 站短链。

    Args:
        url: 链接。

    Returns:
        主机落在短链域名时为 ``True``。
    """
    host = (urlsplit(url or "").hostname or "").lower()
    return any(
        host == item or host.endswith("." + item) for item in SHORT_HOSTS
    )


def room_id_from(url: str) -> int | None:
    """从直播间链接里取房间号。

    Args:
        url: 链接。

    Returns:
        房间号；认不出来时为 ``None``。
    """
    parsed = urlsplit(url or "")
    match = _ROOM_PATH.match(parsed.path or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # 正则已保证是数字，这里只是兜底
        return None


def expand_short(url: str, proxy: str = "") -> str:
    """展开 B 站短链，返回最终地址。

    同步阻塞，调用方必须放进线程池。

    Args:
        url: 短链。
        proxy: 请求代理；空串表示直连。

    Returns:
        展开后的完整 URL；失败时返回空串。
    """
    try:
        with httpx.Client(
            timeout=_TIMEOUT,
            follow_redirects=True,
            proxy=proxy or None,
            headers=_HEADERS,
        ) as client:
            response = client.get(url)
            return str(response.url)
    except Exception as exc:  # 网络问题都算展开失败
        logger.warning(
            f"[parsehub] 直播短链展开失败 {url}: {type(exc).__name__}: {exc}"
        )
        return ""


def fetch_live(url: str, proxy: str = "") -> LiveInfo | None:
    """拉取一个直播间的封面卡片数据。

    同步阻塞，调用方必须放进线程池。

    房间信息拿不到（网络 / 风控 / 房间不存在）就返回 ``None``；
    主播接口单独挂了只少一个名字，不阻断卡片。

    Args:
        url: 直播间链接（短号也可以，接口会自动归一）。
        proxy: 请求代理；空串表示直连。

    Returns:
        房间信息；房间信息拿不到时为 ``None``。
    """
    room_id = room_id_from(url)
    if room_id is None:
        return None
    try:
        with httpx.Client(
            timeout=_TIMEOUT,
            follow_redirects=True,
            proxy=proxy or None,
            headers=_HEADERS,
        ) as client:
            info_response = client.get(
                _ROOM_INFO_URL, params={"room_id": room_id}
            )
            anchor_response = client.get(
                _ANCHOR_URL, params={"roomid": room_id}
            )
    except Exception as exc:  # 网络问题都算取数失败
        logger.warning(
            f"[parsehub] 直播间信息获取失败 {url}: {type(exc).__name__}: {exc}"
        )
        return None

    try:
        info_payload = info_response.json()
        anchor_payload = anchor_response.json()
    except ValueError as exc:
        logger.warning(f"[parsehub] 直播间接口返回的不是 JSON: {exc}")
        return None
    if info_payload.get("code") != 0:
        logger.warning(
            f"[parsehub] 直播间接口报错 room={room_id}: "
            f"{info_payload.get('code')}/{info_payload.get('message')}"
        )
        return None
    if anchor_payload.get("code") != 0:
        logger.warning(
            f"[parsehub] 主播信息获取失败 room={room_id}: "
            f"{anchor_payload.get('code')}/{anchor_payload.get('message')}"
        )
        anchor_payload = {}

    info = info_payload.get("data") or {}
    anchor_info = ((anchor_payload.get("data") or {}).get("info")) or {}
    try:
        return LiveInfo(
            room_id=int(info.get("room_id") or room_id),
            title=str(info.get("title") or ""),
            uname=str(anchor_info.get("uname") or ""),
            user_cover=str(info.get("user_cover") or ""),
            keyframe=str(info.get("keyframe") or ""),
            live_status=int(info.get("live_status") or 0),
        )
    except (TypeError, ValueError) as exc:
        logger.warning(f"[parsehub] 直播间字段解析失败 room={room_id}: {exc}")
        return None


def _thumb(url: str) -> SimpleNamespace:
    """把封面 URL 包成 fetch_cover 认识的媒体项。

    Args:
        url: 封面地址。

    Returns:
        只有 ``thumb_url`` 的媒体项，顺序即封面优先级。
    """
    return SimpleNamespace(thumb_url=url, ext="", duration=0, url="")


def build_result(info: LiveInfo) -> Any:  # 合成的“解析结果”
    """把直播间信息拼成卡片渲染要的 result 对象。

    封面按用户要求的优先级排：直播中先取当前画面帧（``keyframe``），失败回退
    静态封面（``user_cover``）；未开播直接用静态封面。列表顺序就是
    :func:`nonebot_plugin_parsehub.card.cover.fetch_cover` 的尝试顺序。

    Args:
        info: 房间信息。

    Returns:
        与 ParseHub 解析结果同形状的对象，供卡片渲染复用。
    """
    media: list[SimpleNamespace] = []
    if info.live_status:
        if info.keyframe:
            media.append(_thumb(info.keyframe))
    if info.user_cover:
        media.append(_thumb(info.user_cover))

    return SimpleNamespace(
        title=info.title,
        content="",
        media=media,
        platform=SimpleNamespace(id="bilibili", display_name="哔哩哔哩"),
        type=SimpleNamespace(value="live" if info.live_status else "offline"),
        author=info.uname,
        room_id=info.room_id,
    )
