import asyncio

import httpx
import pytest

from bilibili_summary_mcp.bilibili import BilibiliClient
from bilibili_summary_mcp.errors import MCPError


def test_view_412_falls_back_to_pagelist():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(412, request=request)
        assert request.url.path == "/x/player/pagelist"
        return httpx.Response(
            200,
            request=request,
            json={
                "code": 0,
                "data": [
                    {"cid": 123, "page": 1, "part": "回退标题", "duration": 456}
                ],
            },
        )

    async def run():
        client = BilibiliClient(None)
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.get_part("BV1fallback", 1)
        finally:
            await client.close()

    part, metadata = asyncio.run(run())
    assert part.cid == 123
    assert part.title == "回退标题"
    assert part.duration_seconds == 456
    assert metadata["desc"] is None


def test_view_business_error_falls_back_to_pagelist():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(
                200, request=request, json={"code": -352, "message": "风控校验失败"}
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "code": 0,
                "data": [
                    {"cid": 321, "page": 1, "part": "分页回退", "duration": 42}
                ],
            },
        )

    async def run():
        client = BilibiliClient(None)
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.get_part("BV1riskcontrol", 1)
        finally:
            await client.close()

    part, _ = asyncio.run(run())
    assert part.cid == 321
    assert part.title == "分页回退"


def _client_with_playurl(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/x/player/playurl"
        return httpx.Response(200, request=request, json=payload)

    async def run():
        client = BilibiliClient(None)
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return client

    return asyncio.run(run())


def test_audio_candidates_are_filtered_sorted_and_deduped():
    payload = {
        "code": 0,
        "data": {
            "dash": {
                "audio": [
                    {
                        "id": 30216,
                        "bandwidth": 66563,
                        "baseUrl": "https://evil.akamaized.net/x.m4s",
                        "backupUrl": [
                            "https://upos-sz-mirrorbd.bilivideo.com/b.m4s"
                        ],
                    },
                    {
                        "id": 30232,
                        "bandwidth": 88981,
                        "baseUrl": "https://xy123.mcdn.bilivideo.cn:8082/a.m4s",
                        "backupUrl": [
                            "https://upos-sz-mirrorcoso1.bilivideo.com/c.m4s"
                        ],
                    },
                ]
            }
        },
    }
    client = _client_with_playurl(payload)
    try:
        candidates = asyncio.run(client.audio_candidates("BV1x", 1))
        first = asyncio.run(client.audio_url("BV1x", 1))
    finally:
        asyncio.run(client.close())

    # 高带宽音轨优先；伪造的 evil.akamaized.net 被丢弃；base 先于 backup。
    assert candidates == [
        "https://xy123.mcdn.bilivideo.cn:8082/a.m4s",
        "https://upos-sz-mirrorcoso1.bilivideo.com/c.m4s",
        "https://upos-sz-mirrorbd.bilivideo.com/b.m4s",
    ]
    assert first == candidates[0]


def test_audio_candidates_all_untrusted_raises():
    payload = {
        "code": 0,
        "data": {
            "dash": {
                "audio": [
                    {
                        "id": 30216,
                        "bandwidth": 66563,
                        "baseUrl": "https://evil.example.com/a.m4s",
                        "backupUrl": [],
                    }
                ]
            }
        },
    }
    client = _client_with_playurl(payload)
    try:
        with pytest.raises(MCPError) as exc:
            asyncio.run(client.audio_candidates("BV1x", 1))
        assert exc.value.code == "BILIBILI_NO_TRUSTED_MEDIA_URL"
    finally:
        asyncio.run(client.close())


def test_no_dash_audio_raises_unavailable():
    payload = {"code": 0, "data": {"dash": {}}}
    client = _client_with_playurl(payload)
    try:
        with pytest.raises(MCPError) as exc:
            asyncio.run(client.audio_candidates("BV1x", 1))
        assert exc.value.code == "BILIBILI_AUDIO_UNAVAILABLE"
    finally:
        asyncio.run(client.close())
