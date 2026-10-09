"""B站媒体 URL 的专用可信校验与安全下载层。

设计原则（不放宽全局 SSRF 防护）：

1. 媒体地址只允许 ``https``，禁止携带用户名/密码，禁止裸 IP 与 localhost。
2. 主机名使用严格边界匹配（``host == domain`` 或 ``host.endswith("." + domain)``），
   避免 ``evilbilivideo.com`` 这类伪造后缀被信任。
3. 域名集合按 B站 ``playurl`` 接口真实返回建立为最小可信集合：官方主域 + 收敛到
   具体子域后缀的合作边缘 CDN + B站在 Akamai 上的 ``upos-*mirror*`` 镜像形态。
4. 端口限制为 B站媒体 CDN 实测使用的 https 端口集合。
5. 发起请求前必须解析 DNS，且所有 A/AAAA 记录都必须是公网（global）地址，拒绝
   loopback / 私网 / link-local / 保留 / 云厂商 metadata 等地址，缩小 DNS rebinding
   窗口；重定向时对每一跳重新执行整套校验，且跳转次数受限。

任何命中安全规则的错误码都属于“安全拒绝”，调用方不得通过切换备用候选来绕过。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlparse

import httpx

from .errors import MCPError


logger = logging.getLogger("bilibili_summary_mcp.urlguard")

ALLOWED_SCHEME = "https"

# B站媒体 CDN 官方主域（严格边界匹配）。
OFFICIAL_MEDIA_DOMAINS = (
    "bilivideo.com",
    "bilivideo.cn",
    "hdslb.com",
    "bilibili.com",
)

# B站 playurl 接口调度的合作边缘 CDN，收敛到具体子域后缀（不放开整个主域）。
PARTNER_MEDIA_SUFFIXES = (
    ".edge.mountaintoys.cn",
)

# B站在 Akamai 上的 upos 镜像主机，仅放行 upos-*mirror*.akamaized.net 形态，
# 任意其它 akamaized.net 主机（如 evil.akamaized.net）都不信任。
AKAMAI_UPOS_SUFFIX = ".akamaized.net"

# B站媒体 CDN 实测使用的 https 端口：
# 443 官方 upos / Akamai，8082 mcdn.bilivideo.cn，4483 edge.mountaintoys.cn。
ALLOWED_MEDIA_PORTS = frozenset({443, 8082, 4483})

MAX_REDIRECTS = 5

# 细化后的错误码，取代笼统的 BILIBILI_URL_REJECTED。
CODE_BAD_SCHEME = "BILIBILI_BAD_SCHEME"
CODE_USERINFO_FORBIDDEN = "BILIBILI_URL_USERINFO_FORBIDDEN"
CODE_HOST_REJECTED = "BILIBILI_CDN_HOST_REJECTED"
CODE_PORT_REJECTED = "BILIBILI_PORT_NOT_ALLOWED"
CODE_DNS_FAILED = "BILIBILI_DNS_RESOLUTION_FAILED"
CODE_PRIVATE_IP = "BILIBILI_RESOLVED_PRIVATE_IP"
CODE_REDIRECT_REJECTED = "BILIBILI_REDIRECT_REJECTED"
CODE_REDIRECT_LIMIT = "BILIBILI_REDIRECT_LIMIT"

# 命中即代表 SSRF/可信校验失败：绝不允许通过降级到其它候选 URL 来绕过。
SECURITY_REJECTION_CODES = frozenset(
    {
        CODE_BAD_SCHEME,
        CODE_USERINFO_FORBIDDEN,
        CODE_HOST_REJECTED,
        CODE_PORT_REJECTED,
        CODE_PRIVATE_IP,
        CODE_REDIRECT_REJECTED,
        CODE_REDIRECT_LIMIT,
    }
)

# 与内容/路径相关的硬性失败：所有候选同源，重试无意义，也不允许降级。
HARD_REJECTION_CODES = frozenset(
    SECURITY_REJECTION_CODES | {"MEDIA_TOO_LARGE", "MEDIA_PATH_INVALID"}
)

# 遇到这些 HTTP 状态码才允许从 baseUrl 降级到 backupUrl。
RETRIABLE_HTTP_STATUSES = frozenset({403, 404, 410, 416, 500, 502, 503, 504})

Resolver = Callable[[str], Awaitable[list[str]]]


@dataclass(frozen=True)
class StaticMediaUrl:
    url: str
    host: str
    port: int


def _domain_matches(host: str, domain: str) -> bool:
    """严格边界匹配：host 恰为 domain 或为 domain 的子域。"""
    return host == domain or host.endswith("." + domain)


def is_allowed_media_host(host: str) -> bool:
    host = host.lower().strip(".")
    if not host:
        return False
    if any(_domain_matches(host, domain) for domain in OFFICIAL_MEDIA_DOMAINS):
        return True
    if any(
        host == suffix.lstrip(".") or host.endswith(suffix)
        for suffix in PARTNER_MEDIA_SUFFIXES
    ):
        return True
    if host.endswith(AKAMAI_UPOS_SUFFIX):
        label = host[: -len(AKAMAI_UPOS_SUFFIX)]
        if label.startswith("upos-") and "mirror" in label:
            return True
    return False


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _safe_host_for_log(host: str) -> str:
    """日志脱敏：裸 IP（可能是内网地址）不落日志，只保留域名。"""
    return "<ip-literal>" if _is_ip_literal(host) else host


def check_media_url_static(url: str) -> StaticMediaUrl:
    """只做不触发网络的静态校验（scheme/userinfo/host/端口/裸IP）。"""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise MCPError(CODE_HOST_REJECTED, "媒体地址无法解析。") from exc

    if parsed.scheme.lower() != ALLOWED_SCHEME:
        raise MCPError(CODE_BAD_SCHEME, "仅允许 https 媒体地址。")
    if parsed.username or parsed.password:
        raise MCPError(
            CODE_USERINFO_FORBIDDEN, "媒体地址禁止携带用户名或密码。"
        )

    host = (parsed.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise MCPError(CODE_HOST_REJECTED, "媒体主机名不受信任。")
    if _is_ip_literal(host):
        raise MCPError(CODE_HOST_REJECTED, "媒体地址禁止使用裸 IP。")
    if not is_allowed_media_host(host):
        logger.warning("media host rejected host=%s", _safe_host_for_log(host))
        raise MCPError(
            CODE_HOST_REJECTED, "B站返回了不在可信集合内的媒体主机名。"
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise MCPError(CODE_PORT_REJECTED, "媒体地址端口非法。") from exc
    if port is None:
        port = 443
    if port not in ALLOWED_MEDIA_PORTS:
        logger.warning("media port rejected host=%s port=%s", host, port)
        raise MCPError(
            CODE_PORT_REJECTED, f"媒体端口 {port} 不在允许集合内。"
        )

    return StaticMediaUrl(url=url, host=host, port=port)


async def default_resolve_ips(host: str) -> list[str]:
    infos = await asyncio.to_thread(
        socket.getaddrinfo, host, None, type=socket.SOCK_STREAM
    )
    return sorted({info[4][0] for info in infos})


def _ip_is_global(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if not ip.is_global:
        return False
    # 冗余显式拦截云厂商 metadata / 特殊地址（多数已被 is_global=False 覆盖）。
    metadata = {"169.254.169.254", "fd00:ec2::254", "fd00:a9fe:a9fe::1"}
    if str(ip) in metadata:
        return False
    return True


async def check_media_url(
    url: str, *, resolver: Resolver | None = None, stage: str = "media"
) -> StaticMediaUrl:
    """静态校验 + DNS 公网校验。resolver 可注入以便测试。"""
    static = check_media_url_static(url)
    resolve = resolver or default_resolve_ips
    try:
        addresses = await resolve(static.host)
    except Exception:  # 解析失败本身可能是单个 CDN 节点故障，交由调用方降级
        logger.warning("dns resolution failed host=%s stage=%s", static.host, stage)
        raise MCPError(CODE_DNS_FAILED, "媒体域名解析失败。") from None
    if not addresses:
        logger.warning("dns returned no address host=%s stage=%s", static.host, stage)
        raise MCPError(CODE_DNS_FAILED, "媒体域名未解析到地址。")
    for raw in addresses:
        if not _ip_is_global(str(raw)):
            # 不记录具体内网 IP，避免把内网拓扑写入日志。
            logger.warning(
                "resolved non-global address host=%s stage=%s",
                static.host,
                stage,
            )
            raise MCPError(
                CODE_PRIVATE_IP, "媒体域名解析到了内网或保留地址。"
            )
    logger.info(
        "media url accepted stage=%s host=%s port=%s addresses=%d",
        stage,
        static.host,
        static.port,
        len(addresses),
    )
    return static


async def open_media_stream(
    client: httpx.AsyncClient,
    url: str,
    *,
    resolver: Resolver | None = None,
    max_redirects: int = MAX_REDIRECTS,
) -> tuple[httpx.Response, StaticMediaUrl]:
    """安全打开媒体流：手动逐跳跟随重定向，每一跳都重新做完整校验。

    返回处于打开状态（stream）的最终响应与最终地址的静态信息；调用方负责
    ``await response.aclose()``。重定向只接受 https 且目标同样通过可信与公网校验。
    """
    current = url
    static: StaticMediaUrl | None = None
    for hop in range(max_redirects + 1):
        stage = "initial" if hop == 0 else f"redirect{hop}"
        static = await check_media_url(current, resolver=resolver, stage=stage)
        request = client.build_request("GET", current)
        response = await client.send(request, stream=True, follow_redirects=False)

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise MCPError(CODE_REDIRECT_REJECTED, "重定向响应缺少 Location。")
            target = urljoin(current, location)
            target_host = (urlparse(target).hostname or "").lower()
            try:
                await check_media_url(
                    target,
                    resolver=resolver,
                    stage=f"redirect-target{hop + 1}",
                )
            except MCPError as exc:
                logger.warning(
                    "redirect rejected from=%s to=%s code=%s",
                    static.host,
                    _safe_host_for_log(target_host),
                    exc.code,
                )
                raise MCPError(
                    CODE_REDIRECT_REJECTED, "重定向目标未通过可信校验。"
                ) from None
            current = target
            continue

        return response, static

    raise MCPError(
        CODE_REDIRECT_LIMIT, f"媒体重定向超过 {max_redirects} 跳，已中止。"
    )
