# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""解析结果没给时长时，在下载前从媒体容器头探测。

ffmpeg 只把媒体直链当输入、不给输出文件：读到 MP4 的 moov 打出
``Duration:`` 就退出。全程不落盘，也不拉完整文件——实测视频号 2~9MB 的
视频每次只取 0.24~0.34MB（容器头），耗时 0.1~0.2s。

视频号（元宝 ``get_parse_result`` 与 ``get_feed_info``）和 Threads 上游都
没有 duration 字段，解析结果的 ``VideoRef.duration`` 恒为 0，只能这样补。
Instagram / 最右自 ParseHub 2.3.17 起由上游返回时长；其它平台偶发 0 时仍走
同一套兜底，调用方只对视频引用触发。
"""

from __future__ import annotations

import re
import subprocess

from nonebot import logger

from ..parsehub_client import ffmpeg_exe

# ``Duration: 00:01:07.34``；``Duration: N/A``（无时长 / 流式）匹配不到。
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")

# 单次探测超时（秒）：起进程 + 读容器头，正常远小于 1s。
PROBE_TIMEOUT_SECONDS = 8.0


def parse_duration(stderr: str) -> int:
    """从 ffmpeg 的 stderr 里取时长。

    Args:
        stderr: ffmpeg 的诊断输出。

    Returns:
        秒数；拿不到时返回 0。
    """
    match = _DURATION_RE.search(stderr)
    if not match:
        return 0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(float(seconds))


def probe(
    url: str,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    proxy: str = "",
) -> int:
    """不落盘地读媒体时长。

    Args:
        url: 媒体直链（签名会过期，用刚解析出来的那个）。
        timeout: 探测超时（秒）；超时会杀掉 ffmpeg 子进程。
        proxy: 走代理时传入 http 代理地址。

    Returns:
        时长（秒）；探测失败返回 0。
    """
    if not url:
        return 0
    exe = ffmpeg_exe()
    if not exe:
        return 0
    args = [exe, "-hide_banner", "-nostdin"]
    if proxy:
        # ffmpeg 的 http 协议只认 http 代理；socks 地址会打开失败，按探测
        # 失败处理（返回 0），不影响主流程。
        args += ["-http_proxy", proxy]
    args += ["-i", url]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"[parsehub] 时长探测失败: {type(exc).__name__}: {exc}")
        return 0
    seconds = parse_duration(proc.stderr)
    if not seconds:
        logger.debug("[parsehub] 时长探测没拿到 Duration")
    return seconds
