# Copyright 2026 ByteColtX
# SPDX-License-Identifier: MIT

"""皮皮虾多图：ParseHub 只取 cover 第一张，插件要从 RENDER_DATA 补全。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from parsehub.types import ImageRef


def _render_html(urls: list[str]) -> str:
    """造一个带 RENDER_DATA 的皮皮虾详情页。"""
    images = [
        {
            "width": 1080,
            "height": 1338 + index,
            "url_list": [{"url": url}],
            "download_list": [{"url": f"{url}?logo"}],
        }
        for index, url in enumerate(urls)
    ]
    data = {
        "ppxItemDetail": {
            "item": {
                "note": {"multi_image": images},
            }
        }
    }
    payload = quote(json.dumps(data, ensure_ascii=False), safe="")
    return f'<html><script id="RENDER_DATA">{payload}</script></html>'


def test_pipix_images_reads_multi_image(parsehub_client) -> None:
    """完整图集在 note.multi_image，不在 cover。"""
    urls = [
        "https://img.example.com/1.jpg",
        "https://img.example.com/2.jpg",
        "https://img.example.com/3.jpg",
    ]
    images = parsehub_client._pipix_images(_render_html(urls))

    assert [url for url, _, _ in images] == urls
    assert images[0][1:] == (1080, 1338)
    assert images[2][1:] == (1080, 1340)


@pytest.mark.asyncio
async def test_enrich_pipix_replaces_single_media(
    parsehub_client, monkeypatch
) -> None:
    """解析结果只有一张时，用完整图集替换。"""
    urls = [
        "https://img.example.com/1.jpg",
        "https://img.example.com/2.jpg",
        "https://img.example.com/3.jpg",
    ]
    result = SimpleNamespace(
        platform=SimpleNamespace(id="pipix"),
        raw_url="https://h5.pipix.com/ppx/item/1",
        media=[ImageRef(url=urls[0], ext="jpg")],
    )
    monkeypatch.setattr(
        parsehub_client,
        "_fetch_pipix_images",
        lambda url, proxy="": [(url, 1080, 1338) for url in urls],
    )

    await parsehub_client._enrich_pipix(result, "https://h5.pipix.com/s/x", "")

    assert [item.url for item in result.media] == urls
    assert all(isinstance(item, ImageRef) for item in result.media)
