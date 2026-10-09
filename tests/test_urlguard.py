import httpx
import pytest

from bilibili_summary_mcp import urlguard
from bilibili_summary_mcp.errors import MCPError


# --- 主机名严格边界匹配 ---
@pytest.mark.parametrize(
    "host",
    [
        "upos-sz-mirrorcosov.bilivideo.com",
        "upos-sz-mirrorcoso1.bilivideo.com",
        "xy111x225x237x178xy.mcdn.bilivideo.cn",
        "i0.hdslb.com",
        "aisubtitle.hdslb.com",
        "www.bilibili.com",
        "upos-hz-mirrorakam.akamaized.net",
        "svx22w.edge.mountaintoys.cn",
        "b-iba6wv17j4h3alp2d9brt9fe7rc48za.edge.mountaintoys.cn",
        "bilivideo.com",
    ],
)
def test_trusted_media_hosts_accepted(host):
    assert urlguard.is_allowed_media_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "evilbilivideo.com",  # 伪造后缀，前面不是点边界
        "bilivideo.com.evil.com",
        "evilbilivideo.cn",
        "upos-sz-mirrorcosov.bilivideo.com.evil.cn",
        "evil.akamaized.net",  # 非 B站 upos 镜像
        "akamaized.net",
        "upos-hz-mirrorakam.akamaized.net.evil.com",
        "mountaintoys.cn",  # 合作域主域本身不放行，仅放行 edge 子域
        "edge.mountaintoys.cn.evil.com",
        "hdslb.com.evil.com",
        "localhost",
        "",
    ],
)
def test_untrusted_media_hosts_rejected(host):
    assert not urlguard.is_allowed_media_host(host)


# --- 静态校验 ---
def test_static_accepts_real_cdn_urls():
    for url in (
        "https://upos-sz-mirrorcosov.bilivideo.com/p/a.m4s",
        "https://xy.mcdn.bilivideo.cn:8082/p/a.m4s",
        "https://svx22w.edge.mountaintoys.cn:4483/p/a.m4s",
        "https://upos-hz-mirrorakam.akamaized.net/p/a.m4s",
    ):
        static = urlguard.check_media_url_static(url)
        assert static.host


@pytest.mark.parametrize(
    "url,code",
    [
        ("http://upos-sz-mirrorcosov.bilivideo.com/a.m4s", "BILIBILI_BAD_SCHEME"),
        ("https://user:pass@upos-sz-mirrorcosov.bilivideo.com/a", "BILIBILI_URL_USERINFO_FORBIDDEN"),
        ("https://127.0.0.1/a.m4s", "BILIBILI_CDN_HOST_REJECTED"),
        ("https://169.254.169.254/a.m4s", "BILIBILI_CDN_HOST_REJECTED"),
        ("https://10.0.0.5/a.m4s", "BILIBILI_CDN_HOST_REJECTED"),
        ("https://localhost/a.m4s", "BILIBILI_CDN_HOST_REJECTED"),
        ("https://evilbilivideo.com/a.m4s", "BILIBILI_CDN_HOST_REJECTED"),
        ("https://evil.akamaized.net/a.m4s", "BILIBILI_CDN_HOST_REJECTED"),
        ("https://upos-sz-mirrorcosov.bilivideo.com:8080/a", "BILIBILI_PORT_NOT_ALLOWED"),
        ("https://xy.mcdn.bilivideo.cn:9999/a", "BILIBILI_PORT_NOT_ALLOWED"),
    ],
)
def test_static_rejections(url, code):
    with pytest.raises(MCPError) as exc:
        urlguard.check_media_url_static(url)
    assert exc.value.code == code


# --- DNS 公网校验 ---
async def _resolver_factory(mapping):
    async def resolve(host):
        if host in mapping:
            return mapping[host]
        return ["218.61.116.95"]  # 默认公网

    return resolve


async def test_dns_private_ip_rejected():
    resolver = await _resolver_factory({"upos-sz-mirrorcosov.bilivideo.com": ["10.0.0.1"]})
    with pytest.raises(MCPError) as exc:
        await urlguard.check_media_url(
            "https://upos-sz-mirrorcosov.bilivideo.com/a.m4s", resolver=resolver
        )
    assert exc.value.code == "BILIBILI_RESOLVED_PRIVATE_IP"


async def test_dns_link_local_metadata_rejected():
    resolver = await _resolver_factory(
        {"upos-sz-mirrorcosov.bilivideo.com": ["169.254.169.254"]}
    )
    with pytest.raises(MCPError) as exc:
        await urlguard.check_media_url(
            "https://upos-sz-mirrorcosov.bilivideo.com/a.m4s", resolver=resolver
        )
    assert exc.value.code == "BILIBILI_RESOLVED_PRIVATE_IP"


async def test_dns_failure_is_distinct_code():
    async def boom(host):
        raise OSError("no dns")

    with pytest.raises(MCPError) as exc:
        await urlguard.check_media_url(
            "https://upos-sz-mirrorcosov.bilivideo.com/a.m4s", resolver=boom
        )
    assert exc.value.code == "BILIBILI_DNS_RESOLUTION_FAILED"


async def test_dns_all_public_passes():
    resolver = await _resolver_factory({})
    static = await urlguard.check_media_url(
        "https://upos-sz-mirrorcosov.bilivideo.com/a.m4s", resolver=resolver
    )
    assert static.host == "upos-sz-mirrorcosov.bilivideo.com"


# --- 逐跳重定向 ---
async def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_direct_200_returned():
    def handler(request):
        return httpx.Response(200, content=b"audio", request=request)

    client = await _client(handler)
    resolver = await _resolver_factory({})
    try:
        response, static = await urlguard.open_media_stream(
            client,
            "https://upos-sz-mirrorcosov.bilivideo.com/a.m4s",
            resolver=resolver,
        )
        assert response.status_code == 200
        assert static.host == "upos-sz-mirrorcosov.bilivideo.com"
        await response.aclose()
    finally:
        await client.aclose()


async def test_redirect_to_trusted_host_followed():
    def handler(request):
        if request.url.host == "upos-sz-mirrorcosov.bilivideo.com":
            return httpx.Response(
                302,
                headers={"location": "https://upos-hz-mirrorakam.akamaized.net/b.m4s"},
                request=request,
            )
        return httpx.Response(206, content=b"audio", request=request)

    client = await _client(handler)
    resolver = await _resolver_factory({})
    try:
        response, static = await urlguard.open_media_stream(
            client,
            "https://upos-sz-mirrorcosov.bilivideo.com/a.m4s",
            resolver=resolver,
        )
        assert response.status_code == 206
        assert static.host == "upos-hz-mirrorakam.akamaized.net"
        await response.aclose()
    finally:
        await client.aclose()


async def test_redirect_to_untrusted_host_rejected():
    def handler(request):
        return httpx.Response(
            302, headers={"location": "https://evil.example.com/x.m4s"}, request=request
        )

    client = await _client(handler)
    resolver = await _resolver_factory({})
    try:
        with pytest.raises(MCPError) as exc:
            await urlguard.open_media_stream(
                client,
                "https://upos-sz-mirrorcosov.bilivideo.com/a.m4s",
                resolver=resolver,
            )
        assert exc.value.code == "BILIBILI_REDIRECT_REJECTED"
    finally:
        await client.aclose()


async def test_redirect_to_private_ip_rejected():
    def handler(request):
        return httpx.Response(
            302,
            headers={"location": "https://upos-evil.bilivideo.com/x.m4s"},
            request=request,
        )

    client = await _client(handler)
    resolver = await _resolver_factory({"upos-evil.bilivideo.com": ["10.0.0.7"]})
    try:
        with pytest.raises(MCPError) as exc:
            await urlguard.open_media_stream(
                client,
                "https://upos-sz-mirrorcosov.bilivideo.com/a.m4s",
                resolver=resolver,
            )
        assert exc.value.code == "BILIBILI_REDIRECT_REJECTED"
    finally:
        await client.aclose()


async def test_redirect_limit_enforced():
    def handler(request):
        return httpx.Response(
            302,
            headers={"location": "https://upos-sz-mirrorcosov.bilivideo.com/loop"},
            request=request,
        )

    client = await _client(handler)
    resolver = await _resolver_factory({})
    try:
        with pytest.raises(MCPError) as exc:
            await urlguard.open_media_stream(
                client,
                "https://upos-sz-mirrorcosov.bilivideo.com/start",
                resolver=resolver,
                max_redirects=2,
            )
        assert exc.value.code == "BILIBILI_REDIRECT_LIMIT"
    finally:
        await client.aclose()
