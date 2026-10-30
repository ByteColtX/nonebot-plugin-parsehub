# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""解析编排：从链接到「发出去什么」。

一次处理固定按这个顺序走，每一步的结局都是 :class:`Outcome` 里的一个值：

1. 平台白名单（命中才继续，否则连「收到」都不点）；
2. 会话去重（claim，失败不记窗口）；
3. 点「收到」表情；
4. 直播间封面卡片（``live.bilibili.com`` 或短链展开后；成功即结束，
   失败继续往下走常规失败路径）；
5. 缓存命中（作品级，跨会话共享，含时长上限检查）；
6. 解析（共享超时预算）；
7. 时长上限（超了只发封面 + 一句提示）；
8. 下载（只在网络类失败时重试，超时不重试）；
9. 卡片与下载并行，卡片失败再用下载好的文件补一次；
10. 投递（体积过滤、合并转发、逐条兜底都在 delivery）；
11. 只把**真正发出去**的媒体写进缓存（部分失败不写）。

最后按结局点成功 / 失败表情，并把要补发给用户的话统一由 ``Result.notice``
发出。内部按职责拆成六个子模块：

- :mod:`result`：结局枚举与 :class:`Result`；
- :mod:`works`：作品流水线（链接 → 卡片 + 媒体）；
- :mod:`download`：下载重试与下载前的体积预检；
- :mod:`message`：卡片或文字的发送与文案拼装；
- :mod:`audio`：音频流水线（bm / ym 指令）；
- :mod:`run`：两条流水线共用的入口与收尾骨架。
"""

from __future__ import annotations

from . import audio, download, message, works
from .result import Outcome, Result
from .run import handle, handle_audio, session_key, session_name

__all__ = [
    "Outcome",
    "Result",
    "audio",
    "download",
    "handle",
    "handle_audio",
    "message",
    "result",
    "run",
    "session_key",
    "session_name",
    "works",
]
