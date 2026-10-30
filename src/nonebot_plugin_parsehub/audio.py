# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""音频提取能力：bm（B 站）/ ym（YouTube）共用的目标识别与获取。

ParseHub 不提供「只要音频」的解析，这里自己调两条链路：

- B 站：``finger/spi`` 拿 buvid 规避风控（同 ParseHub 的 ``BiliAPI``），
  ``view`` 拿 cid / 标题，``playurl fnval=16`` 拿 DASH 独立音频流，直接下载。
  免登录封顶 192kbps（30280），Dolby / Hi-Res 需要大会员，不追。
- YouTube：parsehub 本来就用 yt-dlp 子进程，这里直接以同样方式调
  ``-f "bestaudio[ext=m4a]/bestaudio"``，容器原生无需转码。

全部函数都是同步阻塞的，调用方必须放进 ``asyncio.to_thread``。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
from nonebot import logger

from . import keys
from .bilibili import (
    BV_PATTERN as _BV_RE,
)
from .bilibili import (
    HOSTS as _BILI_HOSTS,
)
from .bilibili import (
    SHORT_HOSTS as _BILI_SHORT_HOSTS,
)
from .bilibili import (
    VIDEO_URL as _BILI_VIDEO_URL,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 和 keys._URL_PATTERN 同款：中文标点与引号不会出现在 URL 里，当作边界。
_URL_RE = re.compile(
    r"https?://[^\s\u4e00-\u9fff\"'<>()\[\]{}，。、；：！？]+",
    re.IGNORECASE,
)
_TRAILING = "。，、；：！？)）】》\"'"

# 裸 BV 号 / av 号：裸 BV 号见 :mod:`bilibili`（与 keys 共用同一份模式），
# 前后不能紧贴字母数字，免得切出更长的串。
_AV_RE = re.compile(r"(?<![0-9A-Za-z])[aA][vV]\d+")

# YouTube 相关主机。
_YT_HOSTS = ("youtube.com", "youtu.be")
_YT_ID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")
_YT_BARE_URL = "https://youtu.be/{}"

# 文件名清洗。
_ILLEGAL_CHARS = '/\\:*?"<>|'
_ILLEGAL_RE = re.compile(
    rf"[{re.escape(_ILLEGAL_CHARS)}\x00-\x1f\x7f]", re.IGNORECASE
)
_MAX_STEM_CHARS = 80

# 文件上传模式的开关写法。
_FILE_FLAGS = frozenset({"-f", "--file"})

# B 站接口。
_BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}
_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
_PLAYURL_URL = "https://api.bilibili.com/x/player/playurl"
_HTTP_TIMEOUT = 30.0

# yt-dlp 元信息解析的超时（秒）。
_YT_PARSE_TIMEOUT = 60.0
# yt-dlp 失败信息只留尾部这么多字符。
_YT_ERROR_CHARS = 200


class AudioTooLarge(Exception):
    """音频体积超过上限（下载前预估或下载中途发现）。"""


@dataclass(frozen=True)
class BiliAudio:
    """一个 B 站视频的音频元信息。

    Attributes:
        title: 稿件标题（总标题，不是分P标题）。
        bvid: BV 号。
        part: 分P序号，从 1 开始。
        duration: 该分P时长（秒）。
        audio_url: 音频流直链（签名会过期，拿到后尽快下载）。
        bandwidth: 码率（bit/s），用来预估体积。
    """

    title: str
    bvid: str
    part: int
    duration: int
    audio_url: str
    bandwidth: int


@dataclass(frozen=True)
class YtAudio:
    """一个 YouTube 视频的音频元信息。

    Attributes:
        title: 视频标题。
        video_id: 11 位视频 ID。
        duration: 时长（秒）。
        webpage_url: 规范链接，用来算缓存 key。
    """

    title: str
    video_id: str
    duration: int
    webpage_url: str


# ---------------------------------------------------------------------------
# 命令参数与目标提取（纯字符串处理，不发请求）
# ---------------------------------------------------------------------------
def parse_command(text: str) -> tuple[bool, str]:
    """剥掉命令参数里的文件模式开关，返回是否以文件发送与剩余文本。

    开关只认第一个 token（``-f`` / ``--file``），写在后面的当普通文本。

    Args:
        text: 命令参数（去掉命令名后的全部文本）。

    Returns:
        ``(是否文件模式, 剩余文本)``。
    """
    text = (text or "").strip()
    if not text:
        return False, ""
    tokens = text.split(maxsplit=1)
    if tokens[0] in _FILE_FLAGS:
        return True, tokens[1] if len(tokens) > 1 else ""
    return False, text


def _host_of(url: str) -> str:
    """取链接的主机名（小写）。"""
    return (urlsplit(url).hostname or "").lower()


def _host_in(host: str, hosts: tuple[str, ...]) -> bool:
    """主机是否落在给定域名集合里（含子域名）。"""
    return any(host == item or host.endswith("." + item) for item in hosts)


def extract_bilibili(text: str) -> str | None:
    """从命令参数里挑出 B 站视频目标。

    认完整链接（``bilibili.com/video/BVxxx``，分P参数保留）、短链
    （``b23.tv``，展开推迟到获取阶段）和裸 BV 号；动态 / 专栏 / av 号链接
    不算目标。多个目标时取最先出现的。

    Args:
        text: 剩余命令参数，可以是分享文案。

    Returns:
        目标链接；没有时为 ``None``。
    """
    text = text or ""
    candidates: list[tuple[int, str]] = []
    spans: list[tuple[int, int]] = []
    for match in _URL_RE.finditer(text):
        url = match.group().rstrip(_TRAILING)
        spans.append((match.start(), match.start() + len(url)))
        host = _host_of(url)
        if not _host_in(host, _BILI_HOSTS):
            continue
        if _host_in(host, _BILI_SHORT_HOSTS) or _BV_RE.search(url):
            candidates.append((match.start(), url))
    for match in _BV_RE.finditer(text):
        start, end = match.span()
        if any(
            start < span_end and span_start < end
            for span_start, span_end in spans
        ):
            continue
        candidates.append((start, _BILI_VIDEO_URL.format(match.group(1))))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def extract_youtube(text: str) -> str | None:
    """从命令参数里挑出 YouTube 目标。

    认 ``youtube.com``（含 music. / shorts）/ ``youtu.be`` 链接；裸 11 位
    视频 ID 只在参数**恰好是一个单独 token** 时才算，混在文案里不猜。

    Args:
        text: 剩余命令参数，可以是分享文案。

    Returns:
        目标链接；没有时为 ``None``。
    """
    text = text or ""
    for match in _URL_RE.finditer(text):
        url = match.group().rstrip(_TRAILING)
        if _host_in(_host_of(url), _YT_HOSTS):
            return url
    stripped = text.strip()
    if (
        stripped
        and not any(char.isspace() for char in stripped)
        and _YT_ID_RE.match(stripped)
        # 纯小写字母的 11 位串多半是普通单词，要求至少一个大写或数字
        and re.search(r"[0-9A-Z]", stripped)
    ):
        return _YT_BARE_URL.format(stripped)
    return None


def has_av_number(text: str) -> bool:
    """文本里有没有 av 号（老编号），用来给专门的提示。

    Args:
        text: 剩余命令参数。

    Returns:
        有 av 号为 ``True``。
    """
    return bool(_AV_RE.search(text or ""))


def file_stem(title: str, fallback: str, part: int = 1) -> str:
    """把作品标题清洗成可以当文件名的词。

    各平台文件系统的非法字符换成空格，空白折叠，去首尾的点和空白；
    超长截断到 80 字符；清洗后为空就回退到作品号。分P大于 1 时追加
    ``- PN``，且追加发生在截断之后，标签不会被切掉。

    Args:
        title: 作品标题。
        fallback: 标题不可用时的回退（BV 号 / 视频 ID）。
        part: 分P序号。

    Returns:
        不带扩展名的文件名主体。
    """
    cleaned = _ILLEGAL_RE.sub(" ", title or "")
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.strip(" .")
    if len(cleaned) > _MAX_STEM_CHARS:
        cleaned = cleaned[:_MAX_STEM_CHARS].rstrip(" .")
    if not cleaned:
        cleaned = (fallback or "").strip() or "audio"
    if part > 1:
        cleaned = f"{cleaned} - P{part}"
    return cleaned


def cache_key(url: str) -> str:
    """音频专用的作品 key：视频 key 加 ``audio:`` 前缀。

    Args:
        url: 目标链接。

    Returns:
        归一化后的 key，与视频缓存互不干扰。
    """
    return f"audio:{keys.content_key(url)}"


# ---------------------------------------------------------------------------
# B 站：元信息获取与下载（同步阻塞）
# ---------------------------------------------------------------------------
def _part_of(url: str) -> int:
    """取链接里的分P序号，没写或非法时为 1。"""
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key.lower() != "p":
            continue
        try:
            part = int(value)
        except ValueError:
            return 1
        return part if part > 0 else 1
    return 1


def _get_json(
    client: httpx.Client,
    url: str,
    params: dict,
    cookies: dict,
) -> dict:
    """请求 B 站接口并校验返回码。

    Args:
        client: 复用的 httpx 客户端。
        url: 接口地址。
        params: 查询参数。
        cookies: buvid 风控 cookie。

    Returns:
        接口的 ``data`` 字段。

    Raises:
        RuntimeError: 被风控 / 接口报错 / 网络失败。
    """
    response = client.get(url, params=params, cookies=cookies)
    if response.status_code == 412:
        raise RuntimeError("触发 B 站风控，稍后再试")
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(
            f"B 站接口：{payload.get('message') or payload.get('code')}"
        )
    return payload.get("data") or {}


def bilibili_audio(url: str, proxy: str = "") -> BiliAudio:
    """获取一个 B 站视频的音频元信息（含直链）。

    短链在这里展开。音频流直链的签名会过期，拿到后应尽快下载。

    Args:
        url: 视频链接 / 短链 / 带 BV 号的任何写法。
        proxy: 请求代理；空串表示直连。

    Returns:
        音频元信息。

    Raises:
        RuntimeError: 不是视频、视频不存在、没有独立音频流或被风控。
    """
    with httpx.Client(
        timeout=_HTTP_TIMEOUT,
        follow_redirects=True,
        proxy=proxy or None,
        headers=_BILI_HEADERS,
    ) as client:
        # buvid 规避风控（同 ParseHub BiliAPI 的做法）；拿不到就裸着试
        try:
            spi = client.get(_SPI_URL).json()
            cookies = {
                "buvid3": spi["data"]["b_3"],
                "buvid4": spi["data"]["b_4"],
            }
        except Exception as exc:  # 风控指纹拿不到不该拦住解析
            logger.debug(f"[parsehub] buvid 获取失败（忽略）: {exc}")
            cookies = {}

        if _host_in(_host_of(url), _BILI_SHORT_HOSTS):
            url = str(client.get(url).url)

        bvid_match = _BV_RE.search(url)
        if not bvid_match:
            raise RuntimeError("不是 B 站视频链接（只支持 BV 号或视频链接）")
        bvid = bvid_match.group(1)
        part = _part_of(url)

        view = _get_json(client, _VIEW_URL, {"bvid": bvid}, cookies)
        pages = view.get("pages") or []
        if not 1 <= part <= len(pages):
            raise RuntimeError(f"这个视频没有第 {part} P")
        page = pages[part - 1]

        play = _get_json(
            client,
            _PLAYURL_URL,
            {
                "bvid": bvid,
                "cid": page.get("cid"),
                "qn": 64,
                "fnval": 16,  # DASH：才有独立音频流
                "fnver": 0,
                "fourk": 1,
            },
            cookies,
        )
        streams = (play.get("dash") or {}).get("audio") or []
        if not streams:
            raise RuntimeError("这个视频没有独立音频流")
        # 同码率并列时按流 id 排序，保证选择确定
        best = max(
            streams,
            key=lambda item: (item.get("bandwidth") or 0, item.get("id") or 0),
        )
        audio_url = best.get("baseUrl") or best.get("base_url") or ""
        if not audio_url:
            raise RuntimeError("音频流地址为空")
        return BiliAudio(
            title=str(view.get("title") or ""),
            bvid=bvid,
            part=part,
            duration=int(page.get("duration") or view.get("duration") or 0),
            audio_url=audio_url,
            bandwidth=int(best.get("bandwidth") or 0),
        )


def download_bilibili(
    url: str,
    dest: Path,
    proxy: str = "",
    max_bytes: int = 0,
) -> Path:
    """流式下载音频流到指定文件。

    Args:
        url: 音频流直链。
        dest: 目标文件路径。
        proxy: 下载代理；空串表示直连。
        max_bytes: 体积上限（字节），0 表示不限；中途超过就中断。

    Returns:
        下载好的文件路径。

    Raises:
        AudioTooLarge: 下载中途超过体积上限。
        RuntimeError: HTTP 失败或其他网络错误。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    received = 0
    try:
        with (
            httpx.Client(
                timeout=_HTTP_TIMEOUT,
                follow_redirects=True,
                proxy=proxy or None,
                headers=_BILI_HEADERS,
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise RuntimeError(f"音频下载失败：HTTP {response.status_code}")
            with dest.open("wb") as file:
                for chunk in response.iter_bytes(1 << 16):
                    received += len(chunk)
                    if max_bytes and received > max_bytes:
                        raise AudioTooLarge()
                    file.write(chunk)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"音频下载失败：{exc}") from exc
    return dest


# ---------------------------------------------------------------------------
# YouTube：yt-dlp 子进程（同步阻塞）
# ---------------------------------------------------------------------------
def _yt_dlp_cmd() -> list[str]:
    """用当前解释器调 yt-dlp（parsehub 的依赖，必然存在）。"""
    return [sys.executable, "-m", "yt_dlp"]


def _yt_error(proc: subprocess.CompletedProcess) -> str:
    """把 yt-dlp 的输出尾部压成一句人话。"""
    text = (proc.stderr or proc.stdout or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tail = " ".join(lines[-3:]) if lines else f"退出码 {proc.returncode}"
    return f"yt-dlp: {tail[-_YT_ERROR_CHARS:]}"


@contextmanager
def _cookie_file(cookie: str, cookie_dir: Path | None) -> Iterator[list[str]]:
    """把 cookie 写成 yt-dlp 的 Netscape 文件，用完即删。

    yt-dlp 只认 Netscape 格式的 cookie 文件；这里把 ``k=v; k2=v2`` 写进
    插件缓存目录下的一次性文件（目录 0700、文件 0600）。空 cookie 返回
    空参数表。

    Args:
        cookie: cookie 字符串（优先来自 ParseHub 的 ``cookies.toml``）。
        cookie_dir: 一次性文件目录；带 cookie 时必填。

    Yields:
        追加到命令行的参数。

    Raises:
        ValueError: 带 cookie 却没有提供 ``cookie_dir``。
    """
    args: list[str] = []
    path: Path | None = None
    if cookie.strip():
        if cookie_dir is None:
            raise ValueError("cookie_dir is required when cookie is set")
        domain = ".youtube.com"
        lines = ["# Netscape HTTP Cookie File"]
        for pair in cookie.split(";"):
            if "=" not in pair:
                continue
            name, _, value = pair.strip().partition("=")
            if name:
                lines.append(
                    f"{domain}\tTRUE\t/\tFALSE\t0\t{name}\t{value.strip()}"
                )
        if len(lines) > 1:
            try:
                cookie_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    cookie_dir.chmod(0o700)
                except OSError:
                    pass
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    prefix="parsehub-yt-",
                    suffix=".cookies",
                    dir=cookie_dir,
                    delete=False,
                ) as file:
                    path = Path(file.name)
                    file.write("\n".join(lines) + "\n")
                args = ["--cookies", str(path)]
            except OSError:
                if path is not None:
                    path.unlink(missing_ok=True)
                args = []
    try:
        yield args
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def youtube_audio(
    url: str,
    proxy: str = "",
    cookie: str = "",
    cookie_dir: Path | None = None,
) -> YtAudio:
    """用 yt-dlp 拿一个 YouTube 视频的元信息（不下载）。

    Args:
        url: 视频链接。
        proxy: 代理；空串表示直连。
        cookie: 登录态 cookie（规避 bot 检测）；空串表示不带。
        cookie_dir: 一次性 cookie 文件目录；带 cookie 时必填。

    Returns:
        视频元信息。

    Raises:
        ValueError: 带 cookie 但没有提供 ``cookie_dir``。
        RuntimeError: 解析失败、直播 / 预告、超时。
    """
    args = [*_yt_dlp_cmd(), "-J", "--no-playlist", "--no-warnings"]
    if proxy:
        args += ["--proxy", proxy]
    with _cookie_file(cookie, cookie_dir) as cookie_args:
        try:
            proc = subprocess.run(
                args + cookie_args + [url],
                capture_output=True,
                text=True,
                timeout=_YT_PARSE_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("解析视频信息超时") from exc
    if proc.returncode != 0:
        raise RuntimeError(_yt_error(proc))
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError("yt-dlp 返回的不是 JSON") from exc

    if data.get("live_status") in ("is_live", "is_upcoming", "was_live"):
        raise RuntimeError("不支持直播")
    if data.get("_type") == "playlist":
        entries = data.get("entries") or []
        if not entries:
            raise RuntimeError("播放列表里没有视频")
        data = entries[0]

    video_id = str(data.get("id") or "")
    if not video_id:
        raise RuntimeError("解析不到视频 ID")
    return YtAudio(
        title=str(data.get("title") or ""),
        video_id=video_id,
        duration=int(data.get("duration") or 0),
        webpage_url=str(
            data.get("webpage_url") or _YT_BARE_URL.format(video_id)
        ),
    )


def download_youtube(
    url: str,
    dest_dir: Path,
    proxy: str = "",
    timeout: float = 0,
    max_bytes: int = 0,
    cookie: str = "",
    cookie_dir: Path | None = None,
) -> Path:
    """用 yt-dlp 下载最佳音频流。

    ``--max-filesize`` 超限时 yt-dlp 以退出码 0 结束但不留文件（实测），
    因此「退出码 0 且无产物」在设置了上限时就判定为超限。

    Args:
        url: 视频链接。
        dest_dir: 下载目录（文件名由 yt-dlp 决定）。
        proxy: 下载代理；空串表示直连。
        timeout: 子进程超时（秒），0 表示不限。
        max_bytes: 体积上限（字节），0 表示不限。
        cookie: 登录态 cookie；空串表示不带。
        cookie_dir: 一次性 cookie 文件目录；带 cookie 时必填。

    Returns:
        下载好的文件路径。

    Raises:
        AudioTooLarge: 超过体积上限。
        ValueError: 带 cookie 但没有提供 ``cookie_dir``。
        RuntimeError: 下载失败或超时。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    args = [
        *_yt_dlp_cmd(),
        "-f",
        "bestaudio[ext=m4a]/bestaudio",
        "-o",
        str(dest_dir / "%(id)s.%(ext)s"),
        "--no-playlist",
        "--quiet",
        "--no-progress",
        "--no-warnings",
    ]
    if proxy:
        args += ["--proxy", proxy]
    if max_bytes:
        args += ["--max-filesize", str(max_bytes)]
    with _cookie_file(cookie, cookie_dir) as cookie_args:
        try:
            proc = subprocess.run(
                args + cookie_args + [url],
                capture_output=True,
                text=True,
                timeout=timeout or None,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("音频下载超时") from exc
    if proc.returncode != 0:
        raise RuntimeError(_yt_error(proc))
    files = (
        sorted(path for path in dest_dir.iterdir() if path.is_file())
        if dest_dir.is_dir()
        else []
    )
    if not files:
        if max_bytes:
            raise AudioTooLarge()
        raise RuntimeError("yt-dlp 没有产出文件")
    return files[0]
