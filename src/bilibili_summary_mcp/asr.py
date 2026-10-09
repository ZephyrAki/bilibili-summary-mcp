from __future__ import annotations

import asyncio
import hashlib
import math
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bilibili import BilibiliClient
from .db import Database
from .errors import MCPError
from .media import MediaRelay
from .transcripts import ParsedTranscript, parse_paraformer_result


SHANGHAI = ZoneInfo("Asia/Shanghai")


class SubmissionUncertain(RuntimeError):
    pass


def _is_provider_download_failure(item: dict[str, Any]) -> bool:
    if str(item.get("subtask_status", "")).upper() != "FAILED":
        return False
    code = str(item.get("code", "")).upper()
    return code in {
        "INVALIDFILE.DOWNLOADFAILED",
        "FILE_401_UNAUTHORIZED",
        "FILE_403_FORBIDDEN",
    }


class AliyunASRClient:
    def __init__(self, api_key: str, workspace_id: str, *, timeout: float = 30) -> None:
        if not workspace_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in workspace_id):
            raise ValueError("invalid Aliyun workspace ID")
        self.endpoint = f"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1"
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self.result_client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()
        await self.result_client.aclose()

    async def submit(self, file_url: str) -> str:
        try:
            response = await self.client.post(
                f"{self.endpoint}/services/audio/asr/transcription",
                headers={"X-DashScope-Async": "enable"},
                json={
                    "model": "paraformer-v2",
                    "input": {"file_urls": [file_url]},
                    "parameters": {"channel_id": [0], "language_hints": ["zh", "en"]},
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise SubmissionUncertain("提交结果不确定，禁止自动重试。") from exc
        if response.status_code == 408 or response.status_code >= 500:
            raise SubmissionUncertain("Provider 未明确确认提交失败，禁止自动重试。")
        payload = response.json()
        if response.status_code >= 400:
            code = str(payload.get("code", "ASR_SUBMIT_FAILED"))
            message = str(payload.get("message", "阿里云拒绝了转写任务。"))
            raise MCPError(code, message)
        task_id = (payload.get("output") or {}).get("task_id")
        if not task_id:
            raise SubmissionUncertain("提交响应缺少 task_id，禁止自动重试。")
        return str(task_id)

    async def task(self, task_id: str) -> dict[str, Any]:
        response = await self.client.get(f"{self.endpoint}/tasks/{task_id}")
        response.raise_for_status()
        return response.json()

    async def result(self, url: str) -> dict[str, Any]:
        hostname = (urlparse(url).hostname or "").lower()
        if not hostname.endswith(".aliyuncs.com"):
            raise MCPError("ASR_RESULT_URL_REJECTED", "Provider 结果地址域名不受信任。")
        response = await self.result_client.get(url)
        response.raise_for_status()
        return response.json()


class ASRCoordinator:
    def __init__(
        self,
        db: Database,
        bilibili: BilibiliClient,
        provider: AliyunASRClient,
        media: MediaRelay,
        *,
        cache_ttl_days: int,
        enable_transcode: bool = False,
        terminal_quota_sync=None,
        douyin_source=None,
        media_lock=None,
        submission_guard=None,
    ) -> None:
        self.db = db
        self.bilibili = bilibili
        self.provider = provider
        self.media = media
        self.cache_ttl = timedelta(days=cache_ttl_days)
        self.enable_transcode = enable_transcode
        self.terminal_quota_sync = terminal_quota_sync
        self.owner_id = str(uuid.uuid4())
        self.douyin_source = douyin_source
        self.media_lock = media_lock or asyncio.Lock()
        self.submission_guard = submission_guard

    def _schedule_quota_sync(self) -> None:
        if self.terminal_quota_sync is not None:
            asyncio.create_task(self.terminal_quota_sync())

    def _provider_error(self, exc: MCPError, now: datetime) -> None:
        if exc.code == "AllocationQuota.FreeTierOnly":
            self.db.merge_provider_snapshot(
                {
                    "quota_remaining_seconds": 0,
                    "quota_observed_at": now.isoformat(),
                    "quota_synced_at": now.isoformat(),
                    "quota_sync_status": "success",
                    "recent_error_summary": "AllocationQuota.FreeTierOnly",
                }
            )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                # Per-job errors are persisted in run_once; the process must remain alive.
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass

    async def run_once(self) -> None:
        now = datetime.now(SHANGHAI)
        if not self.db.acquire_coordinator_lease(
            "asr", self.owner_id, now=now, ttl=timedelta(seconds=30)
        ):
            return
        # Reserved work intentionally waits behind the single provider slot.
        # Its quota is checked afresh when it reaches the front of the queue.
        self.db.recover_stale_jobs(now=now, expire_reserved=False)
        jobs = self.db.claim_jobs(("submitted", "processing"), 1)
        if jobs:
            await self._poll(jobs[0], now)
            return
        # One provider task across both platforms; unresolved submissions retain the slot.
        if self.db.claim_jobs(("submission_uncertain", "pending_reconciliation", "submitting", "resolving_source", "downloading_media"), 1):
            return
        new_jobs = self.db.claim_jobs(("reserved",), 1)
        if new_jobs:
            await self._submit(new_jobs[0], now)

    async def _submit(self, job: dict[str, Any], now: datetime) -> None:
        job_id = job["job_id"]
        task_id: str | None = None
        if not self.db.transition_job(
            job_id, expected=("reserved",), state="resolving_source", now=now
        ):
            return
        try:
            if self.submission_guard is not None:
                self.submission_guard(job)
            if job.get("platform", "bilibili") == "douyin":
                if self.douyin_source is None:
                    raise MCPError("DOUYIN_NOT_READY", "抖音媒体处理尚未就绪。")
                url = await self.douyin_source(job)
            else:
                url = await self.bilibili.audio_url(job["bvid"], int(job["cid"]))
            if self.submission_guard is not None:
                self.submission_guard(job)
            transitioned = self.db.transition_job(
                job_id,
                expected=("resolving_source",),
                state="submitting",
                now=datetime.now(SHANGHAI),
                submission_started=True,
            )
            if not transitioned:
                return
            task_id = await self.provider.submit(url)
            self.db.add_attempt(
                job_id,
                method="douyin_audio_copy" if job.get("platform") == "douyin" else "direct_url",
                state="submitted",
                now=datetime.now(SHANGHAI),
                provider_task_id=task_id,
            )
            self.db.transition_job(
                job_id,
                expected=("submitting",),
                state="submitted",
                now=datetime.now(SHANGHAI),
                provider_task_id=task_id,
            )
        except SubmissionUncertain as exc:
            self.db.transition_job(
                job_id,
                expected=("submitting",),
                state="submission_uncertain",
                now=datetime.now(SHANGHAI),
                error_code="ASR_SUBMISSION_UNCERTAIN",
                error_summary=str(exc),
            )
        except MCPError as exc:
            self._provider_error(exc, datetime.now(SHANGHAI))
            self.db.fail_job(job_id, code=exc.code, summary=exc.message, now=datetime.now(SHANGHAI))
            self.media.cleanup(job_id)
            self._schedule_quota_sync()
        except Exception as exc:
            current = self.db.get_job(job_id)
            if current.get("provider_submit_started_at"):
                self.db.transition_job(
                    job_id,
                    expected=("submitting",),
                    state="submission_uncertain",
                    now=datetime.now(SHANGHAI),
                    provider_task_id=task_id,
                    error_code="ASR_SUBMISSION_UNCERTAIN",
                    error_summary=type(exc).__name__,
                )
            else:
                self.db.fail_job(
                    job_id,
                    code="ASR_SOURCE_RESOLUTION_FAILED",
                    summary=type(exc).__name__,
                    now=datetime.now(SHANGHAI),
                )
                self.media.cleanup(job_id)

    async def _poll(self, job: dict[str, Any], now: datetime) -> None:
        task_id = job.get("provider_task_id")
        if not task_id:
            self.db.transition_job(
                job["job_id"],
                expected=("submitted", "processing"),
                state="submission_uncertain",
                now=now,
                error_code="ASR_TASK_ID_MISSING",
                error_summary="活动任务缺少 Provider task_id。",
            )
            return
        payload = await self.provider.task(task_id)
        output = payload.get("output") or {}
        state = str(output.get("task_status", "")).upper()
        if state in {"PENDING", "RUNNING"}:
            self.db.transition_job(
                job["job_id"],
                expected=("submitted", "processing"),
                state="processing",
                now=now,
            )
            return
        results = [item for item in output.get("results", []) if isinstance(item, dict)]
        download_failed = any(_is_provider_download_failure(item) for item in results)
        if download_failed and job.get("platform") != "douyin":
            await self._submit_relay(job, now)
            return
        format_failed = any(
            item.get("subtask_status") == "FAILED"
            and any(
                marker in str(item.get("code", "")).lower()
                for marker in ("format", "decode", "codec")
            )
            for item in results
        )
        if format_failed and job.get("platform") != "douyin" and self.db.has_attempt(job["job_id"], "relay_original"):
            if not self.db.has_attempt(job["job_id"], "remux_copy"):
                await self._submit_processed(job, now, transcode=False)
                return
            if self.enable_transcode and not self.db.has_attempt(
                job["job_id"], "transcode_aac"
            ):
                await self._submit_processed(job, now, transcode=True)
                return
            self.db.fail_job(
                job["job_id"],
                code="ASR_FORMAT_UNSUPPORTED",
                summary="重新封装后仍不兼容，且安全转码路径未启用或已失败。",
                now=now,
            )
            self.media.cleanup(job["job_id"])
            self._schedule_quota_sync()
            return
        if state != "SUCCEEDED":
            self.db.fail_job(
                job["job_id"],
                code=str(output.get("code", "ASR_PROVIDER_FAILED")),
                summary=str(output.get("message", "Provider 转写失败。")),
                now=now,
            )
            self.media.cleanup(job["job_id"])
            self._schedule_quota_sync()
            return
        result_urls = [
            item.get("transcription_url")
            for item in results
            if isinstance(item, dict) and item.get("transcription_url")
        ]
        parsed = await self._collect_results(result_urls)
        content = "\n".join(segment["text"] for segment in parsed.segments)
        self.db.complete_transcription(
            job_id=job["job_id"],
            actual_seconds=parsed.actual_seconds,
            original_duration_ms=parsed.original_duration_ms,
            content_duration_ms=parsed.content_duration_ms,
            segments=parsed.segments,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            now=now,
            cache_ttl=self.cache_ttl,
        )
        self.media.cleanup(job["job_id"])
        self._schedule_quota_sync()

    async def _submit_relay(self, job: dict[str, Any], now: datetime) -> None:
        job_id = job["job_id"]
        submission_started = False
        task_id: str | None = None
        if self.db.has_attempt(job_id, "relay_original"):
            self.db.fail_job(
                job_id,
                code="ASR_RELAY_ALREADY_ATTEMPTED",
                summary="中转原始媒体已经提交过，不自动重复提交。",
                now=now,
            )
            self.media.cleanup(job_id)
            self._schedule_quota_sync()
            return
        try:
            candidates = await self.bilibili.audio_candidates(
                job["bvid"], int(job["cid"])
            )
            entered = self.db.transition_job(
                job_id,
                expected=("submitted", "processing"),
                state="downloading_media",
                now=datetime.now(SHANGHAI),
            )
            if not entered:
                return
            async with self.media_lock:
                path = await self.media.download(job_id, candidates, self.bilibili)
            relay_url = self.media.create_url(
                job_id, path, now=now, ttl=timedelta(minutes=30)
            )
            transitioned = self.db.transition_job(
                job_id,
                expected=("downloading_media",),
                state="submitting",
                now=datetime.now(SHANGHAI),
                submission_started=True,
            )
            if not transitioned:
                return
            submission_started = True
            task_id = await self.provider.submit(relay_url)
            self.db.add_attempt(
                job_id,
                method="relay_original",
                state="submitted",
                now=datetime.now(SHANGHAI),
                provider_task_id=task_id,
            )
            self.db.transition_job(
                job_id,
                expected=("submitting",),
                state="submitted",
                now=datetime.now(SHANGHAI),
                provider_task_id=task_id,
            )
        except SubmissionUncertain as exc:
            self.db.transition_job(
                job_id,
                expected=("submitting",),
                state="submission_uncertain",
                now=datetime.now(SHANGHAI),
                error_code="ASR_SUBMISSION_UNCERTAIN",
                error_summary=str(exc),
            )
        except MCPError as exc:
            self._provider_error(exc, datetime.now(SHANGHAI))
            self.db.fail_job(
                job_id,
                code=exc.code,
                summary=exc.message,
                now=datetime.now(SHANGHAI),
            )
            self.media.cleanup(job_id)
            self._schedule_quota_sync()
        except Exception as exc:
            if submission_started:
                self.db.transition_job(
                    job_id,
                    expected=("submitting",),
                    state="submission_uncertain",
                    now=datetime.now(SHANGHAI),
                    provider_task_id=task_id,
                    error_code="ASR_SUBMISSION_UNCERTAIN",
                    error_summary=type(exc).__name__,
                )
            else:
                self.db.fail_job(
                    job_id,
                    code="ASR_RELAY_FAILED",
                    summary=type(exc).__name__,
                    now=datetime.now(SHANGHAI),
                )
                self.media.cleanup(job_id)
                self._schedule_quota_sync()

    async def _submit_processed(
        self, job: dict[str, Any], now: datetime, *, transcode: bool
    ) -> None:
        job_id = job["job_id"]
        method = "transcode_aac" if transcode else "remux_copy"
        submission_started = False
        task_id: str | None = None
        try:
            async with self.media_lock:
                path = await self.media.ffmpeg_prepare(job_id, transcode=transcode)
            url = self.media.create_url(
                job_id, path, now=now, ttl=timedelta(minutes=30)
            )
            transitioned = self.db.transition_job(
                job_id,
                expected=("submitted", "processing"),
                state="submitting",
                now=now,
                submission_started=True,
            )
            if not transitioned:
                return
            submission_started = True
            task_id = await self.provider.submit(url)
            self.db.add_attempt(
                job_id,
                method=method,
                state="submitted",
                now=datetime.now(SHANGHAI),
                provider_task_id=task_id,
            )
            self.db.transition_job(
                job_id,
                expected=("submitting",),
                state="submitted",
                now=datetime.now(SHANGHAI),
                provider_task_id=task_id,
            )
        except SubmissionUncertain as exc:
            self.db.transition_job(
                job_id,
                expected=("submitting",),
                state="submission_uncertain",
                now=datetime.now(SHANGHAI),
                error_code="ASR_SUBMISSION_UNCERTAIN",
                error_summary=str(exc),
            )
        except MCPError as exc:
            self._provider_error(exc, datetime.now(SHANGHAI))
            self.db.fail_job(
                job_id,
                code=exc.code,
                summary=exc.message,
                now=datetime.now(SHANGHAI),
            )
            self.media.cleanup(job_id)
            self._schedule_quota_sync()
        except Exception as exc:
            if submission_started:
                self.db.transition_job(
                    job_id,
                    expected=("submitting",),
                    state="submission_uncertain",
                    now=datetime.now(SHANGHAI),
                    provider_task_id=task_id,
                    error_code="ASR_SUBMISSION_UNCERTAIN",
                    error_summary=type(exc).__name__,
                )
            else:
                self.db.fail_job(
                    job_id,
                    code="ASR_MEDIA_PROCESSING_FAILED",
                    summary=type(exc).__name__,
                    now=datetime.now(SHANGHAI),
                )
                self.media.cleanup(job_id)
                self._schedule_quota_sync()

    async def _collect_results(self, urls: list[str]) -> ParsedTranscript:
        if not urls:
            raise MCPError("ASR_RESULT_INVALID", "成功任务未提供结果地址。")
        parsed = [parse_paraformer_result(await self.provider.result(url)) for url in urls]
        metered = all(item.actual_seconds is not None for item in parsed)
        total_content_duration = sum(item.content_duration_ms for item in parsed)
        return ParsedTranscript(
            actual_seconds=math.ceil(total_content_duration / 1000) if metered else None,
            content_duration_ms=total_content_duration,
            original_duration_ms=sum(item.original_duration_ms or 0 for item in parsed) or None,
            segments=[segment for item in parsed for segment in item.segments],
        )
