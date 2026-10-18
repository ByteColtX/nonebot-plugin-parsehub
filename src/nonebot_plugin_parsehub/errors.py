# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""错误分类与用户可见的错误文案。

ParseHub 的异常类型很多，而且 import 代价高（会拉起 numpy / opencv）。这里在
**边界**把异常翻译成 :class:`ParseFailure` + :class:`ErrorKind`，上层只按 kind
分支，不再靠字符串猜「是不是作品被删了」。

``translate`` 首次调用时才 import ``parsehub.errors``，之后复用类对象；拿不到
时退回按类名判断。
"""

from __future__ import annotations

from enum import StrEnum

# 错误提示最长留多少个字（QQ 气泡里一行放不下更长的）。
MAX_ERROR_CHARS = 80

# ParseHub 的错误信息套了好几层「下载错误: 下载失败: …」，发给用户前压平。
_NOISE = ("下载错误", "下载失败", "解析错误", "下载超时")


class ErrorKind(StrEnum):
    """一次解析/下载失败的性质。"""

    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    NETWORK = "network"
    PARSE = "parse"
    UNKNOWN = "unknown"


class ParseFailure(Exception):
    """已分类的解析/下载失败。

    Attributes:
        kind: 失败性质，上层据此决定提示文案与是否重试。
        detail: 压平后的错误描述。
        cause: 原始异常，仅用于日志。
    """

    def __init__(
        self,
        kind: ErrorKind,
        detail: str,
        cause: BaseException | None = None,
    ) -> None:
        """记录分类结果。

        Args:
            kind: 失败性质。
            detail: 压平后的错误描述。
            cause: 原始异常。
        """
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.cause = cause

    @property
    def retryable(self) -> bool:
        """这类失败值不值得重试。

        Returns:
            网络类失败为 True；超时、不可用、不支持都不重试。
        """
        return self.kind is ErrorKind.NETWORK


def short_error(exc: BaseException) -> str:
    """把 ParseHub 的嵌套错误信息压成一句人话。

    Args:
        exc: 捕获到的异常。

    Returns:
        去掉重复前缀后的错误描述。
    """
    text = str(exc).strip()
    for word in _NOISE:
        text = text.replace(f"{word}: ", "").replace(f"{word}：", "")
    text = text.strip()
    if not text:
        return type(exc).__name__
    # 2.3.10 起「所有解析方式均失败」会把每一层的原因列成多行，QQ 里发不出去，
    # 压成「一句话（第一层原因）」。
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        head = lines[0].rstrip(":：")
        first = lines[1].lstrip("-").strip()
        text = f"{head}（{first}）" if first else head
    return truncate_error(text)


def truncate_error(text: str) -> str:
    """错误提示太长时截断。

    Args:
        text: 错误描述。

    Returns:
        不超过 :data:`MAX_ERROR_CHARS` 个字的描述。
    """
    if len(text) <= MAX_ERROR_CHARS:
        return text
    return text[: MAX_ERROR_CHARS - 1] + "…"


# parsehub 的异常类，首次 translate 时加载；空元组表示拿不到。
_PARSEHUB_ERRORS: dict[str, type[BaseException]] | None = None


def _parsehub_errors() -> dict[str, type[BaseException]]:
    """加载并缓存 parsehub 的异常类。

    Returns:
        类名到类对象的映射；parsehub 不可用时为空字典。
    """
    global _PARSEHUB_ERRORS  # noqa: PLW0603 - 单例式缓存
    if _PARSEHUB_ERRORS is None:
        try:
            from parsehub import errors as module
        except ImportError:
            _PARSEHUB_ERRORS = {}
        else:
            _PARSEHUB_ERRORS = {
                name: value
                for name in dir(module)
                if isinstance(value := getattr(module, name), type)
                and issubclass(value, BaseException)
            }
    return _PARSEHUB_ERRORS


def _classify(exc: BaseException) -> ErrorKind:
    """按异常类型判断失败性质。

    Args:
        exc: 捕获到的异常。

    Returns:
        失败性质。
    """
    classes = _parsehub_errors()
    if classes:
        if isinstance(exc, classes.get("UnknownPlatform", ())):
            return ErrorKind.UNSUPPORTED
        if isinstance(exc, classes.get("ContentUnavailableError", ())):
            return ErrorKind.UNAVAILABLE
        if isinstance(exc, classes.get("DownloadError", ())):
            return ErrorKind.NETWORK
        if isinstance(exc, classes.get("ParseError", ())):
            return ErrorKind.PARSE
    # parsehub 不可用时（或异常被包装过）退回类名 / 文案判断。
    name = type(exc).__name__
    if "UnknownPlatform" in name:
        return ErrorKind.UNSUPPORTED
    if "ContentUnavailable" in name or "作品不可用" in str(exc):
        return ErrorKind.UNAVAILABLE
    if "Download" in name:
        return ErrorKind.NETWORK
    text = str(exc)
    if "下载错误" in text or "下载失败" in text or "HTTP错误" in text:
        return ErrorKind.NETWORK
    return ErrorKind.UNKNOWN


def translate(exc: BaseException) -> ParseFailure:
    """把任意异常翻译成已分类的失败。

    Args:
        exc: 捕获到的异常。

    Returns:
        带 kind 与压平描述的失败对象。
    """
    if isinstance(exc, ParseFailure):
        return exc
    kind = _classify(exc)
    if isinstance(exc, TimeoutError):
        kind = ErrorKind.TIMEOUT
    elif kind is ErrorKind.UNKNOWN:
        try:
            import httpx
        except ImportError:  # pragma: no cover - httpx 是硬依赖
            pass
        else:
            if isinstance(exc, httpx.HTTPError):
                kind = ErrorKind.NETWORK
    return ParseFailure(kind, short_error(exc), exc)


def is_unavailable(exc: BaseException) -> bool:
    """这个失败是不是「作品已不可用（被删 / 下架）」。

    Args:
        exc: 捕获到的异常。

    Returns:
        作品不可用时返回 True。
    """
    return translate(exc).kind is ErrorKind.UNAVAILABLE
