import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bilibili_summary_mcp.bilibili import BilibiliClient
from bilibili_summary_mcp.errors import MCPError
from bilibili_summary_mcp.media import MediaRelay
from bilibili_summary_mcp.tokens import SignedTokenService


def test_media_reference_is_bound_to_job_hash_and_expiry(tmp_path):
    tokens = SignedTokenService(b"m" * 32)
    relay = MediaRelay(tmp_path, tokens, "https://mcp.example.com")
    path = tmp_path / "job-1.m4a"
    path.write_bytes(b"audio")
    now = datetime.now(timezone.utc)
    url = relay.create_url("job-1", path, now=now, ttl=timedelta(minutes=5))
    token = url.rsplit("/", 1)[-1]

    assert relay.resolve(token, now=now) == path
    path.write_bytes(b"changed")
    with pytest.raises(MCPError) as exc:
        relay.resolve(token, now=now)
    assert exc.value.code == "MEDIA_TOKEN_INVALID"


def test_media_reference_expires(tmp_path):
    relay = MediaRelay(
        tmp_path, SignedTokenService(b"m" * 32), "https://mcp.example.com"
    )
    path = tmp_path / "job-2.m4a"
    path.write_bytes(b"audio")
    now = datetime.now(timezone.utc)
    token = relay.create_url(
        "job-2", path, now=now, ttl=timedelta(seconds=1)
    ).rsplit("/", 1)[-1]
    with pytest.raises(MCPError):
        relay.resolve(token, now=now + timedelta(seconds=2))


def _bilibili(handler, resolver_map=None):
    async def make():
        client = BilibiliClient(None)
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        mapping = resolver_map or {}

        async def resolve(host):
            return mapping.get(host, ["218.61.116.95"])

        client.resolve_media_ips = resolve
        return client

    return asyncio.run(make())


def _relay(tmp_path):
    return MediaRelay(
        tmp_path, SignedTokenService(b"m" * 32), "https://mcp.example.com"
    )


def test_download_falls_back_to_backup_on_403(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "upos-hz-mirrorakam.akamaized.net":
            return httpx.Response(403, request=request)
        return httpx.Response(
            200,
            content=b"ftyp-audio-bytes",
            headers={"content-type": "video/mp4"},
            request=request,
        )

    bili = _bilibili(handler)
    try:
        relay = _relay(tmp_path)
        candidates = [
            "https://upos-hz-mirrorakam.akamaized.net/a.m4s",
            "https://upos-sz-mirrorcosov.bilivideo.com/a.m4s",
        ]
        path = asyncio.run(relay.download("job1", candidates, bili))
        assert path.read_bytes() == b"ftyp-audio-bytes"
        assert path.name == "job1.m4s"
        assert calls == [
            "upos-hz-mirrorakam.akamaized.net",
            "upos-sz-mirrorcosov.bilivideo.com",
        ]
    finally:
        asyncio.run(bili.close())


def test_download_private_ip_aborts_without_fallback(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, content=b"must-not-reach", request=request)

    bili = _bilibili(
        handler, {"upos-sz-mirrorcosov.bilivideo.com": ["10.0.0.1"]}
    )
    try:
        relay = _relay(tmp_path)
        candidates = [
            "https://upos-sz-mirrorcosov.bilivideo.com/a.m4s",
            "https://upos-sz-mirrorbd.bilivideo.com/b.m4s",
        ]
        with pytest.raises(MCPError) as exc:
            asyncio.run(relay.download("job2", candidates, bili))
        assert exc.value.code == "BILIBILI_RESOLVED_PRIVATE_IP"
        # 安全拒绝：未发起任何请求，也不得切换到备用候选绕过。
        assert calls == []
    finally:
        asyncio.run(bili.close())


def test_download_redirect_to_private_aborts_without_fallback(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(
            302,
            headers={"location": "https://upos-evil.bilivideo.com/x.m4s"},
            request=request,
        )

    bili = _bilibili(
        handler, {"upos-evil.bilivideo.com": ["169.254.169.254"]}
    )
    try:
        relay = _relay(tmp_path)
        candidates = [
            "https://upos-sz-mirrorcosov.bilivideo.com/start.m4s",
            "https://upos-sz-mirrorbd.bilivideo.com/ok.m4s",
        ]
        with pytest.raises(MCPError) as exc:
            asyncio.run(relay.download("job3", candidates, bili))
        assert exc.value.code == "BILIBILI_REDIRECT_REJECTED"
        assert calls == ["upos-sz-mirrorcosov.bilivideo.com"]
    finally:
        asyncio.run(bili.close())


def test_download_all_candidates_unavailable_raises(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    bili = _bilibili(handler)
    try:
        relay = _relay(tmp_path)
        candidates = [
            "https://upos-hz-mirrorakam.akamaized.net/a.m4s",
            "https://upos-sz-mirrorcosov.bilivideo.com/a.m4s",
        ]
        with pytest.raises(MCPError) as exc:
            asyncio.run(relay.download("job4", candidates, bili))
        assert exc.value.code == "BILIBILI_MEDIA_DOWNLOAD_FAILED"
    finally:
        asyncio.run(bili.close())
