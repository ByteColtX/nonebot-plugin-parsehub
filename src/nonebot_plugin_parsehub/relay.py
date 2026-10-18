# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""媒体中转服务。

QQ 协议端（NapCat / Lagrange / LLOneBot）通常跑在另一台机器上，读不到本机的
``file://`` 路径，所以把待投递的媒体用一个随机目录暴露成 http 地址发给它，让它
自己来拉。

设计约束：

- 只读、只服务一个目录（``relay/``，里面只有随机命名的任务目录），不列目录；
- 媒体缓存与插件数据都在这个目录之外，不会被中转服务暴露；
- 过期文件由 :meth:`MediaRelay.cleanup` 定期清理（清扫任务在插件入口里跑）。

模块不依赖 nonebot。
"""

from __future__ import annotations

import http.server
import shutil
import socket
import threading
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Any

# 探测内网 IP 时用的哑地址，只用来让内核选出口网卡，不会真的发包。
_PROBE_ADDRESS = ("10.255.255.255", 1)

# 每次任务目录名的随机长度（十六进制字符数）。
_TASK_DIR_RANDOM_LENGTH = 16


class _RelayHandler(http.server.SimpleHTTPRequestHandler):
    """只读静态文件服务，关掉目录列表和访问日志。"""

    def list_directory(self, path: Any) -> None:
        """禁止列目录，直接返回 404。

        Args:
            path: 请求的目录路径，未使用。
        """
        self.send_error(404, "Not Found")
        return None

    def log_message(self, format: str, *args: Any) -> None:
        """静音访问日志，避免刷屏。

        Args:
            format: 日志格式串，未使用。
            *args: 日志参数，未使用。
        """
        return

    def end_headers(self) -> None:
        """补上禁用缓存的响应头。"""
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class MediaRelay:
    """把本地媒体文件暴露成协议端能下载的 http 地址。

    Attributes:
        root: 任务目录的根目录（只放随机命名的子目录）。
        _server: 已启动的 HTTP 服务，未启动时为 None。
        _lock: 保护启动过程的锁。
    """

    def __init__(self, root: Path) -> None:
        """初始化中转服务。

        Args:
            root: 任务目录的根目录。
        """
        self.root = root
        self._server: http.server.ThreadingHTTPServer | None = None
        self._lock = threading.Lock()

    def start(self, host: str, port: int) -> int:
        """启动 HTTP 服务（重复调用只启动一次）。

        Args:
            host: 绑定地址，例如 ``0.0.0.0``。
            port: 绑定端口。

        Returns:
            实际监听的端口。

        Raises:
            OSError: 端口被占用或地址不可用时抛出，由调用方决定如何报错。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self._server is None:
                handler = partial(_RelayHandler, directory=str(self.root))
                self._server = http.server.ThreadingHTTPServer(
                    (host, port), handler
                )
                thread = threading.Thread(
                    target=self._server.serve_forever,
                    name="parsehub-relay",
                    daemon=True,
                )
                thread.start()
            return int(self._server.server_address[1])

    def new_task_dir(self) -> Path:
        """为一次投递创建随机子目录。

        Returns:
            新建的子目录路径。
        """
        path = self.root / uuid.uuid4().hex[:_TASK_DIR_RANDOM_LENGTH]
        path.mkdir(parents=True, exist_ok=True)
        return path

    def url_for(self, path: Path, host: str, port: int) -> str:
        """把本地文件路径转成协议端能下载的地址。

        Args:
            path: 媒体文件路径，必须位于 :attr:`root` 之下。
            host: 协议端可访问的地址。
            port: HTTP 服务端口。

        Returns:
            形如 ``http://host:port/<随机目录>/<文件名>`` 的地址。

        Raises:
            ValueError: 文件不在 :attr:`root` 之下。
        """
        relative = path.resolve().relative_to(self.root.resolve())
        return f"http://{host}:{port}/{relative.as_posix()}"

    def cleanup(self, keep_seconds: int) -> int:
        """删除过期的任务目录。

        Args:
            keep_seconds: 保留时长（秒），早于该时长的目录会被删除。

        Returns:
            删除的目录数量。
        """
        if not self.root.exists():
            return 0

        now = time.time()
        removed = 0
        for child in self.root.iterdir():
            try:
                expired = (
                    child.is_dir()
                    and now - child.stat().st_mtime > keep_seconds
                )
            except OSError:
                continue
            if expired:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed


def guess_lan_ip() -> str:
    """探测本机内网 IP。

    Returns:
        本机在默认出口网卡上的地址；探测失败时返回 ``127.0.0.1``。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(_PROBE_ADDRESS)
            return str(sock.getsockname()[0])
        except OSError:
            return "127.0.0.1"
