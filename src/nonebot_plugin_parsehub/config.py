# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""插件配置。

所有配置项都从 NoneBot 的 ``.env`` / 环境变量读取，字段名即配置名（大小写不
敏感），例如字段 ``parsehub_max_mb`` 对应 ``PARSEHUB_MAX_MB=100``。

注意：``.env`` 里的值会先被解析成 int / bool / list 再做校验，因此数字类配置
必须声明成 ``int``，列表类配置必须是合法 JSON（``["twitter", "youtube"]``），
否则插件会在加载阶段直接失败。
"""

from __future__ import annotations

from nonebot import get_plugin_config
from pydantic import BaseModel, Field

# 需要走代理的海外平台（ParseHub 的平台 id）。
OVERSEAS_PLATFORMS: tuple[str, ...] = (
    "twitter",
    "instagram",
    "threads",
    "youtube",
    "tiktok",
    "facebook",
    "snapchat",
    "medium",
)


class Config(BaseModel):
    """ParseHub 链接解析插件配置。

    Attributes:
        parsehub_auto: 群里出现受支持的链接时是否自动解析。具体到某个会话
            （群 / 私聊）的开与关用命令 ``解析开关`` 控制，不在这里配。
        parsehub_concurrency: 同时处理的任务数上限。
        parsehub_proxy: 解析 / 下载代理地址，留空表示直连。
        parsehub_proxy_platforms: 只对这些平台走代理，留空表示所有平台都走。
        parsehub_cookie: 兜底 cookie，优先使用 ParseHub 自己的 cookies.toml。
        parsehub_timeout: 解析加下载的总超时（秒）。
        parsehub_max_mb: 单个媒体文件体积上限（MB），超过则跳过。
        parsehub_card: 是否把解析结果画成卡片图发送（失败自动退回文字）。
        parsehub_card_font: 中文字体文件路径，留空则自动找系统字体或下载。
        parsehub_send_text: 是否发送标题与正文文字。
        parsehub_max_chars: 正文最多发送的字符数。
        parsehub_forward_timeout: 合并转发等待协议端的秒数。协议端要把节点里的
            每个 URL 都抓一遍再上传，健康时 6-7 秒，但它在忙别的消息时会明显变慢
            （实测超过 nonebot 默认的 30 秒上限被掐断）。这里给足余量，
            真超时了才退回逐条发送。
        parsehub_fail_notice: 视频发送失败时是否额外发一条文字提示。
        parsehub_files_host: 媒体中转服务对外的地址，留空则自动探测内网 IP。
        parsehub_files_port: 媒体中转服务的端口。
        parsehub_keep_minutes: 临时文件保留时长（分钟）。
    """

    # ---- 触发 ----
    parsehub_auto: bool = True
    parsehub_concurrency: int = 2

    # ---- 解析 / 下载 ----
    parsehub_proxy: str = ""
    parsehub_proxy_platforms: list[str] = Field(
        default_factory=lambda: list(OVERSEAS_PLATFORMS)
    )
    parsehub_cookie: str = ""
    parsehub_timeout: int = 180
    parsehub_max_mb: int = 100

    # ---- 发送 ----
    parsehub_card: bool = True
    parsehub_card_font: str = ""
    parsehub_send_text: bool = True
    parsehub_max_chars: int = 300
    parsehub_forward_timeout: int = 60
    parsehub_fail_notice: bool = True

    # ---- 媒体中转服务 ----
    parsehub_files_host: str = ""
    parsehub_files_port: int = 21473
    parsehub_keep_minutes: int = 30


plugin_config = get_plugin_config(Config)
