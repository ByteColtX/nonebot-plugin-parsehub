# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""真实链路自检：解析 → 下载 → 通过中转服务取回。

用法::

    uv run poe live [url] [proxy]

需要联网；在开发机和部署机上都可跑，部署机上建议直接用机器人所在 venv 的
解释器（系统库不全时按需补 ``LD_LIBRARY_PATH``）。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import httpx
import nonebot

# 插件的配置类依赖 NoneBot 已初始化；媒体目录交给 localstore 管理。
nonebot.init(parsehub_relay_port=18899)
nonebot.load_plugin("nonebot_plugin_localstore")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from nonebot_plugin_parsehub import (  # noqa: E402
    parsehub_client,
    paths,
    relay,
)

_DEFAULT_URL = "https://www.bilibili.com/video/BV1GJ411x7h7"
_CHECK_PORT = 18899
_BYTES_PER_MEGABYTE = 1024 * 1024

URL = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_URL
PROXY = sys.argv[2] if len(sys.argv) > 2 else ""


async def main() -> int:
    """跑一遍解析 → 下载 → 中转取回的完整链路。

    Returns:
        进程退出码，0 表示链路正常。
    """
    client = parsehub_client.ParseHubClient()
    media_relay = relay.MediaRelay(paths.relay_root())

    print("== 1. 加载 parsehub ==")
    platforms = await client.platforms()
    print(f"   平台数: {len(platforms)}")
    print(f"   平台 id: {await client.platform_id(URL)}")

    print("== 2. 解析 ==")
    result = await client.parse(URL, PROXY)
    display_name = result.platform.display_name if result.platform else "?"
    print(f"   {display_name} / {result.title}")

    print("== 3. 下载 ==")
    task_dir = media_relay.new_task_dir()
    downloaded = await client.download(URL, str(task_dir), PROXY)
    media = downloaded.media
    media_list = list(media) if isinstance(media, (list, tuple)) else [media]
    files = [Path(item.path) for item in media_list]
    for path in files:
        size = path.stat().st_size / _BYTES_PER_MEGABYTE
        print(f"   {path.name}  {size:.1f} MB")

    print("== 4. 中转服务取回 ==")
    port = media_relay.start("0.0.0.0", _CHECK_PORT)
    url = media_relay.url_for(files[0], "127.0.0.1", port)
    print(f"   {url}")

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.get(url)
        expected = files[0].stat().st_size
        ok = response.status_code == 200 and len(response.content) == expected
        print(f"   HTTP {response.status_code}, {len(response.content)} bytes")
        print("== 结果:", "PASS" if ok else "FAIL", "==")
    finally:
        # 这个脚本里没有插件的清扫任务，下载的媒体必须自己删，
        # 否则每跑一次就往缓存目录里留一份
        shutil.rmtree(task_dir, ignore_errors=True)
        print(f"   已清理临时目录 {task_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
