from __future__ import annotations

import html
import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import urlguard
from .errors import MCPError
from .bilibili_credentials import load_credentials, cookie_header


logger = logging.getLogger("bilibili_summary_mcp.bilibili")
MAX_VIDEO_BYTES = 512 * 1024 * 1024
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class VideoPart:
    bvid: str
    cid: int
    page: int
    title: str
    part_title: str
    duration_seconds: int


class BilibiliClient:
    def __init__(
        self,
        cookie: str | None,
        *,
        timeout: float = 15,
        resolver: urlguard.Resolver | None = None,
        credentials_path: Path | None = None,
    ) -> None:
        headers = {
            "User-Agent": "Mozilla/5.0 ZephyrMCP/0.1",
            "Referer": "https://www.bilibili.com/",
        }
        if cookie:
            headers["Cookie"] = cookie
        self.client = httpx.AsyncClient(headers=headers, timeout=timeout)
        self.credentials_path = credentials_path
        if credentials_path is not None:
            self.client.event_hooks['request'].append(self._apply_credentials)
        # DNS 解析器可注入，便于测试；默认解析并要求所有地址为公网。
        self.resolve_media_ips = resolver or urlguard.default_resolve_ips

    async def _apply_credentials(self, request: httpx.Request) -> None:
        # Load the atomic file for each API request; rotation needs no MCP restart.
        if (request.url.scheme != 'https' or request.url.port not in (None, 443)
                or request.url.host not in ('api.bilibili.com', 'passport.bilibili.com', 'www.bilibili.com')):
            request.headers.pop('Cookie', None)
            return
        try:
            request.headers['Cookie'] = cookie_header(load_credentials(self.credentials_path))
        except (OSError, ValueError, KeyError, TypeError):
            raise MCPError('BILIBILI_LOGIN_REQUIRED', 'B站专用登录凭据不可用，需要重新扫码登录。') from None

    async def close(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _data(payload: dict[str, Any]) -> Any:
        if payload.get("code") != 0:
            raise MCPError("BILIBILI_API_ERROR", "B站接口暂时不可用。")
        return payload.get("data")

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        response = await self.client.get(
            "https://api.bilibili.com/x/web-interface/search/type",
            params={"search_type": "video", "keyword": query, "page": 1},
        )
        response.raise_for_status()
        data = self._data(response.json()) or {}
        results = data.get("result") or []
        return [
            {
                "bvid": item.get("bvid"),
                "title": html.unescape(re.sub(r"<[^>]+>", "", item.get("title", ""))),
                "author": item.get("author"),
                "duration": item.get("duration"),
                "description": item.get("description"),
            }
            for item in results[: max(1, min(limit, 20))]
        ]

    async def get_part(self, bvid: str, page: int = 1) -> tuple[VideoPart, dict[str, Any]]:
        response = await self.client.get(
            "https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}
        )
        if response.status_code == 412:
            return await self._get_part_from_pagelist(bvid, page)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            return await self._get_part_from_pagelist(bvid, page)
        data = self._data(payload) or {}
        return self._select_part(bvid, page, data)

    async def _get_part_from_pagelist(
        self, bvid: str, page: int
    ) -> tuple[VideoPart, dict[str, Any]]:
        response = await self.client.get(
            "https://api.bilibili.com/x/player/pagelist", params={"bvid": bvid}
        )
        response.raise_for_status()
        pages = self._data(response.json()) or []
        selected = next((part for part in pages if part.get("page") == page), None)
        fallback_title = str((selected or {}).get("part", ""))
        return self._select_part(
            bvid,
            page,
            {"title": fallback_title, "desc": None, "pages": pages},
        )

    @staticmethod
    def _select_part(
        bvid: str, page: int, data: dict[str, Any]
    ) -> tuple[VideoPart, dict[str, Any]]:
        pages = data.get("pages") or []
        selected = next((part for part in pages if part.get("page") == page), None)
        if selected is None:
            raise MCPError("BILIBILI_PART_NOT_FOUND", "找不到指定分P。")
        return (
            VideoPart(
                bvid=bvid,
                cid=int(selected["cid"]),
                page=page,
                title=str(data.get("title", "")),
                part_title=str(selected.get("part", "")),
                duration_seconds=int(selected.get("duration", 0)),
            ),
            data,
        )

    async def subtitle_tracks(self, bvid: str, cid: int) -> list[dict[str, Any]]:
        data = await self.player_info(bvid, cid)
        tracks = (data.get("subtitle") or {}).get("subtitles") or []
        return [
            {
                "url": "https:" + track["subtitle_url"]
                if str(track.get("subtitle_url", "")).startswith("//")
                else track.get("subtitle_url"),
                "language": track.get("lan"),
                "title": track.get("lan_doc"),
                "source": "bilibili_ai_subtitle"
                if int(track.get("type", 0)) == 1
                else "human_subtitle",
            }
            for track in tracks
            if track.get("subtitle_url")
        ]

    async def player_info(self, bvid: str, cid: int) -> dict[str, Any]:
        response = await self.client.get(
            "https://api.bilibili.com/x/player/v2", params={"bvid": bvid, "cid": cid}
        )
        response.raise_for_status()
        return self._data(response.json()) or {}

    async def ai_summary(
        self, bvid: str, cid: int, up_mid: int | None
    ) -> dict[str, Any] | None:
        if up_mid is None:
            return None
        try:
            response = await self.client.get(
                "https://api.bilibili.com/x/web-interface/view/conclusion/get",
                params={"bvid": bvid, "cid": cid, "up_mid": up_mid},
            )
            response.raise_for_status()
            if response.json().get("code") != 0:
                return None
            data = response.json().get("data") or {}
            summary = data.get("model_result") or data.get("summary")
            return summary if isinstance(summary, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def subtitle_segments(self, url: str) -> list[dict[str, Any]]:
        response, _ = await urlguard.open_media_stream(
            self.client, url, resolver=self.resolve_media_ips
        )
        try:
            response.raise_for_status()
            await response.aread()
            body = response.json().get("body") or []
        finally:
            await response.aclose()
        return [
            {
                "begin_ms": round(float(item.get("from", 0)) * 1000),
                "end_ms": round(float(item.get("to", 0)) * 1000),
                "text": str(item.get("content", "")),
            }
            for item in body
        ]

    async def audio_candidates(self, bvid: str, cid: int) -> list[str]:
        """返回按优先级排序、且已通过静态可信校验的音频地址候选。

        仅取 DASH 的 audio 轨（不下载视频轨），按带宽从高到低排序；每条音轨
        依次考虑 baseUrl 与 backupUrl。主机名/端口/scheme 不合法的候选会被丢弃
        并记录非敏感诊断；一个可信候选都没有时才报错。DNS/IP 与重定向校验在
        实际下载阶段（urlguard.open_media_stream）逐候选执行。
        """
        response = await self.client.get(
            "https://api.bilibili.com/x/player/playurl",
            params={"bvid": bvid, "cid": cid, "fnval": 16, "qn": 80},
        )
        response.raise_for_status()
        data = self._data(response.json()) or {}
        tracks = (data.get("dash") or {}).get("audio") or []
        if not tracks:
            raise MCPError("BILIBILI_AUDIO_UNAVAILABLE", "无法取得视频音频地址。")

        tracks = sorted(
            tracks, key=lambda item: -int(item.get("bandwidth") or 0)
        )
        candidates: list[str] = []
        seen: set[str] = set()
        rejected: list[tuple[str, str | None, str]] = []

        def consider(raw: Any, origin: str) -> None:
            if not raw or raw in seen:
                return
            seen.add(str(raw))
            try:
                static = urlguard.check_media_url_static(str(raw))
            except MCPError as exc:
                rejected.append((origin, urlparse(str(raw)).hostname, exc.code))
                return
            logger.info(
                "audio candidate accepted origin=%s host=%s port=%s",
                origin,
                static.host,
                static.port,
            )
            candidates.append(str(raw))

        for index, track in enumerate(tracks):
            consider(track.get("baseUrl") or track.get("base_url"), f"audio{index}.base")
            for backup_index, backup in enumerate(
                track.get("backupUrl") or track.get("backup_url") or []
            ):
                consider(backup, f"audio{index}.backup{backup_index}")

        if rejected:
            logger.warning("rejected audio candidates: %s", rejected)
        if not candidates:
            raise MCPError(
                "BILIBILI_NO_TRUSTED_MEDIA_URL",
                "B站返回的音频地址均未通过可信校验。",
            )
        return candidates

    async def audio_url(self, bvid: str, cid: int) -> str:
        return (await self.audio_candidates(bvid, cid))[0]

    async def video_candidates(self, bvid: str, cid: int) -> list[str]:
        response = await self.client.get(
            "https://api.bilibili.com/x/player/playurl",
            params={"bvid": bvid, "cid": cid, "fnval": 16, "qn": 64},
        )
        response.raise_for_status()
        data = self._data(response.json()) or {}
        eligible = []
        for track in (data.get("dash") or {}).get("video") or []:
            try:
                width, height = int(track['width']), int(track['height'])
                bandwidth = int(track['bandwidth'])
            except (KeyError, TypeError, ValueError):
                continue
            if (str(track.get('codecs', '')).lower().startswith('avc1.') and
                    0 < min(width, height) <= 720 and max(width, height) <= 1280 and bandwidth > 0):
                eligible.append((-(width * height), bandwidth, track))
        if not eligible:
            raise MCPError('BILIBILI_VIDEO_UNAVAILABLE', '没有符合 AVC、720p 和带宽信息要求的视频轨。')
        selected = sorted(eligible, key=lambda item: item[:2])[0][2]
        urls = [selected.get('baseUrl') or selected.get('base_url')]
        urls += selected.get('backupUrl') or selected.get('backup_url') or []
        candidates = []
        for url in urls:
            if not isinstance(url, str) or not url or url in candidates:
                continue
            urlguard.check_media_url_static(url)
            candidates.append(url)
        if not candidates:
            raise MCPError('BILIBILI_VIDEO_UNAVAILABLE', '所选视频轨缺少可用地址。')
        return candidates

    async def download_video(self, bvid: str, cid: int, target: Path) -> Path:
        async def strip_credentials(request):
            request.headers.pop('Cookie', None)
            request.headers.pop('Authorization', None)

        try:
            async with asyncio.timeout(VIDEO_DOWNLOAD_TIMEOUT_SECONDS):
                candidates = await self.video_candidates(bvid, cid)
                async with httpx.AsyncClient(timeout=30, follow_redirects=False,
                        headers={'User-Agent':'Mozilla/5.0 ZephyrMCP/0.1',
                                 'Referer':'https://www.bilibili.com/'},
                        event_hooks={'request':[strip_credentials]}) as media_client:
                    for url in candidates:
                        response = None
                        try:
                            response, _ = await urlguard.open_media_stream(
                                media_client, url, resolver=self.resolve_media_ips)
                            if response.status_code in urlguard.RETRIABLE_HTTP_STATUSES:
                                continue
                            response.raise_for_status()
                            length = response.headers.get('content-length')
                            if length is not None and (not length.isdigit()):
                                raise MCPError('MEDIA_INVALID_LENGTH', '媒体响应长度无效。')
                            if length is not None and int(length) > MAX_VIDEO_BYTES:
                                raise MCPError('MEDIA_TOO_LARGE', '视频轨超过 512 MiB 限制。')
                            total = 0
                            fd = os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                            with os.fdopen(fd, 'wb') as file:
                                os.chmod(target, 0o600)
                                async for chunk in response.aiter_bytes():
                                    total += len(chunk)
                                    if total > MAX_VIDEO_BYTES:
                                        raise MCPError('MEDIA_TOO_LARGE', '视频轨超过 512 MiB 限制。')
                                    file.write(chunk)
                            if not total:
                                raise MCPError('BILIBILI_VIDEO_UNAVAILABLE', '视频轨为空。')
                            return target
                        except httpx.TransportError:
                            target.unlink(missing_ok=True)
                        except MCPError as exc:
                            if exc.code != urlguard.CODE_DNS_FAILED:
                                raise
                        finally:
                            if response is not None:
                                await response.aclose()
                raise MCPError('BILIBILI_VIDEO_UNAVAILABLE', '所选视频轨及备用地址暂时不可用。')
        except TimeoutError:
            target.unlink(missing_ok=True)
            raise MCPError('VIDEO_DOWNLOAD_TIMEOUT', '视频下载超过总时限。') from None
        except BaseException:
            target.unlink(missing_ok=True)
            raise
