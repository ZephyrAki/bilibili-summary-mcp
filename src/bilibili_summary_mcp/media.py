from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import urlguard
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bilibili import BilibiliClient
from .errors import MCPError
from .tokens import SignedTokenService


logger = logging.getLogger("bilibili_summary_mcp.media")

# B站 DASH 音频分片后缀为 .m4s；保留常见音频/容器后缀白名单。
ALLOWED_MEDIA_SUFFIXES = {
    ".m4s",
    ".m4a",
    ".mp3",
    ".aac",
    ".flac",
    ".wav",
    ".mp4",
    ".webm",
}
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024


class MediaRelay:
    def __init__(
        self,
        directory: Path,
        tokens: SignedTokenService,
        public_base_url: str,
    ) -> None:
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.tokens = tokens
        self.public_base_url = public_base_url.rstrip("/")

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as media:
            for chunk in iter(lambda: media.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    async def download(
        self,
        job_id: str,
        source: str | list[str],
        bilibili: BilibiliClient,
    ) -> Path:
        """安全下载音频，支持在多个候选间降级。

        每个候选都经过 urlguard 的 scheme/host/端口/DNS 公网/逐跳重定向校验。
        仅对 CDN 不可用类错误（403/404/410/416/5xx、超时、连接失败、单节点
        DNS 失败）降级到下一个候选；任何安全拒绝（不可信主机、内网 IP、危险
        重定向等）立即抛出，绝不通过换候选绕过。
        """
        candidates = [source] if isinstance(source, str) else list(source)
        if not candidates:
            raise MCPError(
                "BILIBILI_NO_TRUSTED_MEDIA_URL", "没有可用的音频候选地址。"
            )

        last_error: Exception | None = None
        for index, requested_url in enumerate(candidates):
            response: httpx.Response | None = None
            target: Path | None = None
            success = False
            try:
                response, static = await urlguard.open_media_stream(
                    bilibili.client,
                    requested_url,
                    resolver=bilibili.resolve_media_ips,
                )
                if response.status_code in urlguard.RETRIABLE_HTTP_STATUSES:
                    logger.warning(
                        "candidate[%d] host=%s returned %s; trying backup",
                        index,
                        static.host,
                        response.status_code,
                    )
                    last_error = MCPError(
                        "BILIBILI_MEDIA_HTTP_ERROR",
                        f"CDN 返回 {response.status_code}。",
                    )
                    continue
                response.raise_for_status()

                suffix = Path(urlparse(str(response.url)).path).suffix.lower()
                if suffix not in ALLOWED_MEDIA_SUFFIXES:
                    suffix = ".media"
                target = (self.directory / f"{job_id}{suffix}").resolve()
                if target.parent != self.directory:
                    raise MCPError("MEDIA_PATH_INVALID", "临时媒体路径无效。")

                length = int(response.headers.get("content-length", "0") or 0)
                if length > MAX_MEDIA_BYTES:
                    raise MCPError(
                        "MEDIA_TOO_LARGE", "临时媒体文件超过 2 GiB 限制。"
                    )
                written = 0
                with target.open("wb") as output:
                    os.chmod(target, 0o600)
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        written += len(chunk)
                        if written > MAX_MEDIA_BYTES:
                            raise MCPError(
                                "MEDIA_TOO_LARGE", "临时媒体文件超过 2 GiB 限制。"
                            )
                        output.write(chunk)
                if written == 0:
                    last_error = MCPError(
                        "BILIBILI_MEDIA_EMPTY", "音频候选返回了空内容。"
                    )
                    continue
                logger.info(
                    "downloaded audio candidate[%d] host=%s bytes=%d suffix=%s",
                    index,
                    static.host,
                    written,
                    suffix,
                )
                success = True
                return target
            except MCPError as exc:
                if exc.code in urlguard.HARD_REJECTION_CODES:
                    raise
                last_error = exc
            except (
                httpx.TimeoutException,
                httpx.TransportError,
                httpx.HTTPStatusError,
            ) as exc:
                last_error = exc
            finally:
                if response is not None:
                    with contextlib.suppress(Exception):
                        await response.aclose()
                if not success and target is not None:
                    with contextlib.suppress(Exception):
                        target.unlink(missing_ok=True)

        logger.error("all %d audio candidates failed", len(candidates))
        raise MCPError(
            "BILIBILI_MEDIA_DOWNLOAD_FAILED", "所有音频候选地址均下载失败。"
        ) from last_error

    def create_url(
        self,
        job_id: str,
        path: Path,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> str:
        path = path.resolve()
        if path.parent != self.directory or not path.is_file():
            raise MCPError("MEDIA_PATH_INVALID", "临时媒体路径无效。")
        token = self.tokens.issue_media_ref(
            job_id=job_id,
            filename=path.name,
            content_hash=self._hash(path),
            now=now,
            ttl=ttl,
        )
        return f"{self.public_base_url}/media/{token}"

    def resolve(self, token: str, *, now: datetime) -> Path:
        payload = self.tokens.verify_media_ref(token, now=now)
        filename = payload.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise MCPError("MEDIA_TOKEN_INVALID", "临时媒体令牌无效。")
        path = (self.directory / filename).resolve()
        if path.parent != self.directory or not path.is_file():
            raise MCPError("MEDIA_NOT_FOUND", "临时媒体已清理。")
        if self._hash(path) != payload.get("content_hash"):
            raise MCPError("MEDIA_TOKEN_INVALID", "临时媒体内容已变化。")
        return path

    def cleanup(self, job_id: str) -> None:
        for path in self.directory.glob(f"{job_id}.*"):
            if path.is_file() and path.resolve().parent == self.directory:
                path.unlink(missing_ok=True)

    def job_source(self, job_id: str) -> Path:
        candidates = [
            path
            for path in self.directory.glob(f"{job_id}.*")
            if ".remux." not in path.name and ".transcoded." not in path.name
        ]
        if len(candidates) != 1:
            raise MCPError("MEDIA_NOT_FOUND", "找不到唯一的原始临时媒体。")
        return candidates[0]

    async def ffmpeg_prepare(self, job_id: str, *, transcode: bool) -> Path:
        source = self.job_source(job_id)
        label = "transcoded" if transcode else "remux"
        target = self.directory / f"{job_id}.{label}.m4a"
        codec = ["-c:a", "aac", "-b:a", "128k"] if transcode else ["-c", "copy"]
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            *codec,
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=900)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                process.kill()
            await process.wait()
            target.unlink(missing_ok=True)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise MCPError("FFMPEG_TIMEOUT", "媒体处理超时。") from None
        if process.returncode != 0 or not target.is_file():
            raise MCPError("FFMPEG_FAILED", "媒体重新封装或转码失败。")
        target.chmod(0o600)
        return target

    def cleanup_orphans(self, active_job_ids: set[str]) -> int:
        removed = 0
        for path in self.directory.iterdir():
            if not path.is_file():
                continue
            job_id = path.name.split(".", 1)[0]
            if job_id not in active_job_ids:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
