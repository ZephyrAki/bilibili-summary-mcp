from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bilibili_summary_mcp.db import Database
from bilibili_summary_mcp.errors import MCPError
from bilibili_summary_mcp.transcripts import parse_paraformer_result


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_paraformer_actual_seconds_come_from_content_duration():
    parsed = parse_paraformer_result(
        {
            "properties": {"original_duration_in_milliseconds": 10_000},
            "transcripts": [
                {
                    "channel_id": 0,
                    "content_duration_in_milliseconds": 3_201,
                    "sentences": [
                        {"begin_time": 100, "end_time": 1_000, "text": "你好"},
                        {
                            "begin_time": 1_100,
                            "end_time": 3_301,
                            "text": "FastMCP",
                        },
                    ],
                }
            ],
        }
    )

    assert parsed.actual_seconds == 4
    assert parsed.content_duration_ms == 3_201
    assert parsed.original_duration_ms == 10_000
    assert parsed.segments[1]["text"] == "FastMCP"


def test_missing_content_duration_requires_reconciliation():
    parsed = parse_paraformer_result(
        {
            "properties": {"original_duration_in_milliseconds": 5_000},
            "transcripts": [{"channel_id": 0, "sentences": []}],
        }
    )
    assert parsed.actual_seconds is None


def test_successful_transcript_finalizes_ledger_and_pages(tmp_path):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=SHANGHAI)
    db = Database(tmp_path / "mcp.db")
    db.initialize()
    db.save_provider_snapshot(
        {
            "quota_total_seconds": 36_000,
            "quota_remaining_seconds": 36_000,
            "quota_used_seconds": 0,
            "quota_expires_at": "2026-10-01T00:00:00+08:00",
            "quota_observed_at": now.isoformat(),
            "quota_synced_at": now.isoformat(),
            "quota_sync_status": "success",
            "free_tier_autostop": True,
            "autostop_observed_at": now.isoformat(),
            "autostop_synced_at": now.isoformat(),
            "autostop_sync_status": "success",
        }
    )
    job = db.reserve_transcription(
        bvid="BV1result",
        cid=101,
        page=1,
        duration_seconds=600,
        now=now,
        monthly_limit_seconds=36_000,
        provider_stale_after=timedelta(minutes=30),
    )
    document_id = db.complete_transcription(
        job_id=job.job_id,
        actual_seconds=4,
        original_duration_ms=10_000,
        content_duration_ms=3_201,
        segments=[
            {"begin_ms": 0, "end_ms": 1_000, "text": "one"},
            {"begin_ms": 1_000, "end_ms": 2_000, "text": "two"},
            {"begin_ms": 2_000, "end_ms": 3_000, "text": "three"},
        ],
        content_hash="abc123",
        now=now,
        cache_ttl=timedelta(days=30),
    )

    status = db.get_status(now=now, monthly_limit_seconds=36_000)
    page = db.get_transcript_page(document_id=document_id, offset=1, limit=1)
    assert status["local_finalized_seconds"] == 4
    assert status["local_reserved_seconds"] == 0
    assert page["segments"][0]["text"] == "two"
    assert page["next_offset"] == 2
    assert page["truncated"] is True

    cached = db.reserve_transcription(
        bvid="BV1result",
        cid=101,
        page=1,
        duration_seconds=600,
        now=now + timedelta(minutes=1),
        monthly_limit_seconds=36_000,
        provider_stale_after=timedelta(minutes=30),
    )
    assert cached.cached_document_id == document_id
    assert cached.reserved_seconds == 0
    assert db.get_status(
        now=now + timedelta(minutes=1), monthly_limit_seconds=36_000
    )["local_finalized_seconds"] == 4


def test_unmetered_result_is_saved_but_reservation_stays_pending(tmp_path):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=SHANGHAI)
    db = Database(tmp_path / "mcp.db")
    db.initialize()
    db.save_provider_snapshot(
        {
            "quota_total_seconds": 36_000,
            "quota_remaining_seconds": 36_000,
            "quota_used_seconds": 0,
            "quota_expires_at": "2026-10-01T00:00:00+08:00",
            "quota_observed_at": now.isoformat(),
            "quota_synced_at": now.isoformat(),
            "quota_sync_status": "success",
            "free_tier_autostop": True,
            "autostop_observed_at": now.isoformat(),
            "autostop_synced_at": now.isoformat(),
            "autostop_sync_status": "success",
        }
    )
    job = db.reserve_transcription(
        bvid="BV1pending", cid=202, page=1, duration_seconds=600, now=now,
        monthly_limit_seconds=36_000, provider_stale_after=timedelta(minutes=30)
    )
    document_id = db.complete_transcription(
        job_id=job.job_id,
        actual_seconds=None,
        original_duration_ms=10_000,
        content_duration_ms=0,
        segments=[{"begin_ms": 0, "end_ms": 1000, "text": "saved"}],
        content_hash="pending",
        now=now,
        cache_ttl=timedelta(days=30),
    )
    assert db.get_job(job.job_id)["state"] == "pending_reconciliation"
    assert db.get_transcript_page(document_id=document_id, offset=0, limit=10)[
        "segments"
    ][0]["text"] == "saved"
    assert db.get_status(now=now, monthly_limit_seconds=36_000)[
        "local_reserved_seconds"
    ] == 600
