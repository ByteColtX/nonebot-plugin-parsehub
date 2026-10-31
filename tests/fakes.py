"""测试共用的替身：假 Bot 与假解析结果。

事件构造器在 ``fake.py``；这里只放跨文件复用的协议端替身和 parsehub
结果的假实现。全部离线，不发任何请求。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from nonebot.adapters.onebot.v11 import Bot, Message
from nonebot.adapters.onebot.v11.exception import ActionFailed


class FakeBot(Bot):
    """记录所有 API 调用的假 Bot。

    Attributes:
        calls: 所有 ``call_api`` 调用的 ``(api, kwargs)``。
        fail_video: 让 ``video`` 消息段被协议端拒绝。
        fail_record: 让 ``record`` 消息段被协议端拒绝。
    """

    def __init__(
        self, *, fail_video: bool = False, fail_record: bool = False
    ) -> None:
        super().__init__(adapter=MagicMock(), self_id="10000")
        self.calls: list[tuple[str, dict]] = []
        self.fail_video = fail_video
        self.fail_record = fail_record

    async def call_api(self, api: str, **kwargs):  # type: ignore[override]
        self.calls.append((api, kwargs))
        if api == "send_msg":
            message = kwargs.get("message")
            if isinstance(message, Message):
                for seg_type, enabled in (
                    ("video", self.fail_video),
                    ("record", self.fail_record),
                ):
                    if enabled and any(seg.type == seg_type for seg in message):
                        raise ActionFailed(
                            status="failed",
                            retcode=100,
                            data=None,
                            message=f"{seg_type} not supported",
                        )
        return {"status": "ok", "retcode": 0, "data": None}

    def _segment_files(self, seg_type: str) -> list[str]:
        out: list[str] = []
        for api, kwargs in self.calls:
            if api != "send_msg":
                continue
            message = kwargs.get("message")
            if isinstance(message, Message):
                out.extend(
                    seg.data.get("file", "")
                    for seg in message
                    if seg.type == seg_type
                )
        return out

    @property
    def sent_texts(self) -> list[str]:
        out: list[str] = []
        for api, kwargs in self.calls:
            if api != "send_msg":
                continue
            message = kwargs.get("message")
            if isinstance(message, Message):
                out.append(message.extract_plain_text())
        return out

    @property
    def sent_images(self) -> list[str]:
        return self._segment_files("image")

    @property
    def sent_records(self) -> list[str]:
        return self._segment_files("record")

    @property
    def sent_videos(self) -> list[str]:
        return self._segment_files("video")

    def uploaded_files(self) -> list[tuple[str, str]]:
        return [
            (kw.get("file", ""), kw.get("name", ""))
            for api, kw in self.calls
            if api == "upload_group_file"
        ]


class FakeMediaRef:
    def __init__(self, url: str, ext: str = "mp4") -> None:
        self.url = url
        self.ext = ext
        self.width = 1920
        self.height = 1080
        self.duration = 10


class FakeParseResult:
    def __init__(
        self,
        *,
        title="标题",
        content="正文",
        media=None,
        raw_url="https://b23.tv/abcd",
    ) -> None:
        self.title = title
        self.content = content
        self.media = media
        self.raw_url = raw_url
        self.platform = SimpleNamespace(id="bilibili", display_name="哔哩哔哩")


class FakeMediaFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.width = 1920
        self.height = 1080
        self.duration = 10


class FakeDownloadResult:
    def __init__(self, files: list[Path], media=None) -> None:
        self.media = (
            media if media is not None else [FakeMediaFile(p) for p in files]
        )
