# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""ParseHub 链接解析插件（OneBot v11）。

群里出现受支持的分享链接时自动解析，并把视频 / 图集 / 正文发回群里；也可以用
命令 ``解析 <链接或分享文案>`` 手动触发。

分层：

- ``triggers``：matcher 与命令，只负责「该不该处理」和「把链接交给谁」；
- ``service``：解析编排，每个结局都是 ``Outcome`` 里的一个值；
- ``delivery`` / ``feedback``：媒体投递与用户可见反馈；
- ``cache`` / ``dedupe`` / ``relay`` / ``card``：缓存、去重、中转、卡片；
- ``app``：把上面这些装在一起，由本模块构建一次。

解析与下载全部由 ``parsehub`` 完成，插件只负责调度和发送。parsehub 是同步阻塞
的（内部还用了 requests），因此所有调用都通过 ``asyncio.to_thread`` 执行，不会
阻塞 NoneBot 的事件循环。
"""

from __future__ import annotations

from nonebot import get_driver
from nonebot.plugin import PluginMetadata

from . import (
    audio,
    bilibili,
    bilibili_live,
    budget,
    cache,
    card,
    dedupe,
    delivery,
    errors,
    feedback,
    keys,
    media,
    parsehub_client,
    paths,
    platforms,
    relay,
    service,
    switches,
    triggers,
)
from .app import App
from .config import Config, Settings, plugin_config

__all__ = [
    "APP",
    "App",
    "Config",
    "Settings",
    "audio",
    "bilibili",
    "bilibili_live",
    "budget",
    "cache",
    "card",
    "dedupe",
    "delivery",
    "errors",
    "feedback",
    "keys",
    "media",
    "parsehub_client",
    "paths",
    "platforms",
    "relay",
    "service",
    "switches",
    "triggers",
]

__plugin_meta__ = PluginMetadata(
    name="轻量、异步、开箱即用的社交媒体聚合解析插件",
    description=(
        "群里出现受支持的分享链接时，用 ParseHub 自动解析并发送媒体"
        "（视频 / 图集 / 正文）。"
    ),
    usage=(
        "发链接即可自动解析；命令：/解析 <链接或分享文案>、/解析平台、"
        "/解析开关 [开|关]（超管 / 群管理）、/解析状态、"
        "/bm <BV 号或 B 站链接>（B 站音频）、"
        "/ym <YouTube 链接>（YouTube 音频），bm / ym 加 -f 以文件发送"
    ),
    type="application",
    homepage="https://github.com/ByteColtX/nonebot-plugin-parsehub",
    config=Config,
    supported_adapters={"~onebot.v11"},
    extra={"parsehub": "https://github.com/z-mio/ParseHub"},
)

# 进程内唯一的插件实例；测试可以自己 App.build() 一个。
APP = App.build(Settings.from_config(plugin_config))

# matcher 注册（处理函数闭包捕获 APP）；注册本身有副作用，不需要保留返回值。
triggers.create_matchers(APP)

driver = get_driver()
driver.on_startup(APP.startup)
driver.on_shutdown(APP.shutdown)
