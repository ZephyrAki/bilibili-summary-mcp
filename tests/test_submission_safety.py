from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import httpx

from bilibili_summary_mcp.asr import (
    ASRCoordinator,
    AliyunASRClient,
    SubmissionUncertain,
    _is_provider_download_failure,
)


@pytest.mark.parametrize(
    "code",
    ["InvalidFile.DownloadFailed", "FILE_401_UNAUTHORIZED", "FILE_403_FORBIDDEN"],
)
def test_explicit_provider_download_failures_use_relay(code):
    assert _is_provider_download_failure(
        {"subtask_status": "FAILED", "code": code}
    )


def test_unrelated_provider_failure_does_not_use_relay():
    assert not _is_provider_download_failure(
        {"subtask_status": "FAILED", "code": "FILE_TOO_LARGE"}
    )


@pytest.mark.asyncio
async def test_failed_task_download_error_is_relayed_before_terminal_failure():
    relayed: list[str] = []

    class DatabaseStub:
        def fail_job(self, *args, **kwargs):
            pytest.fail("an explicit download failure must use the relay path")

    class ProviderStub:
        async def task(self, task_id):
            return {
                "output": {
                    "task_status": "FAILED",
                    "code": "FILE_403_FORBIDDEN",
                    "results": [
                        {
                            "subtask_status": "FAILED",
                            "code": "FILE_403_FORBIDDEN",
                        }
                    ],
                }
            }

    class Coordinator(ASRCoordinator):
        async def _submit_relay(self, job, now):
            relayed.append(job["job_id"])

    coordinator = Coordinator(
        DatabaseStub(), object(), ProviderStub(), object(), cache_ttl_days=30
    )
    now = datetime(2026, 9, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    await coordinator._poll(
        {"job_id": "job-download", "provider_task_id": "provider-task"}, now
    )
    assert relayed == ["job-download"]


@pytest.mark.asyncio
async def test_provider_server_error_is_submission_uncertain():
    async def handler(request):
        return httpx.Response(500, json={"code": "InternalError"})

    client = AliyunASRClient("test-key", "workspace")
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SubmissionUncertain):
            await client.submit("https://example.invalid/audio.m4a")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_relay_persistence_failure_keeps_reservation_uncertain(tmp_path):
    transitions: list[tuple[str, str | None]] = []

    class DatabaseStub:
        def has_attempt(self, job_id, method):
            return False

        def transition_job(self, job_id, *, state, **kwargs):
            transitions.append((state, kwargs.get("provider_task_id")))
            return True

        def add_attempt(self, *args, **kwargs):
            raise RuntimeError("local database unavailable after Provider submission")

        def fail_job(self, *args, **kwargs):
            pytest.fail("a possibly submitted job must not release its reservation")

    class BilibiliStub:
        async def audio_url(self, bvid, cid):
            return "https://example.invalid/audio.m4a"

        async def audio_candidates(self, bvid, cid):
            return ["https://example.invalid/audio.m4a"]

    class MediaStub:
        async def download(self, job_id, source_url, bilibili):
            return Path(tmp_path / "audio.m4a")

        def create_url(self, job_id, path, **kwargs):
            return "https://mcp.example.invalid/media/token"

        def cleanup(self, job_id):
            pytest.fail("uncertain submission media must remain available for reconciliation")

    class ProviderStub:
        async def submit(self, url):
            return "provider-task-id"

    coordinator = ASRCoordinator(
        DatabaseStub(),
        BilibiliStub(),
        ProviderStub(),
        MediaStub(),
        cache_ttl_days=30,
    )
    now = datetime(2026, 9, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    await coordinator._submit_relay(
        {"job_id": "job-1", "bvid": "BV1safe", "cid": 1}, now
    )

    assert transitions == [
        ("downloading_media", None),
        ("submitting", None),
        ("submission_uncertain", "provider-task-id"),
    ]
