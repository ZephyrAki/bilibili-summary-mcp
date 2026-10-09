from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

import pytest

from bilibili_summary_mcp.db import Database
from bilibili_summary_mcp.errors import MCPError


SHANGHAI = ZoneInfo("Asia/Shanghai")


def fresh_provider_state(now: datetime, remaining: int = 28_800) -> dict:
    return {
        "quota_total_seconds": 36_000,
        "quota_remaining_seconds": remaining,
        "quota_used_seconds": 36_000 - remaining,
        "quota_expires_at": "2026-10-01T00:00:00+08:00",
        "quota_observed_at": now.isoformat(),
        "quota_synced_at": now.isoformat(),
        "quota_sync_status": "success",
        "free_tier_autostop": True,
        "autostop_observed_at": now.isoformat(),
        "autostop_synced_at": now.isoformat(),
        "autostop_sync_status": "success",
    }


def test_admission_uses_provider_remaining_minus_local_reservations(tmp_path):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=SHANGHAI)
    db = Database(tmp_path / "mcp.db")
    db.initialize()
    db.save_provider_snapshot(fresh_provider_state(now))

    first = db.reserve_transcription(
        bvid="BV1first",
        cid=1,
        page=1,
        duration_seconds=7_200,
        now=now,
        monthly_limit_seconds=36_000,
        provider_stale_after=timedelta(minutes=30),
    )
    assert first.reserved_seconds == 7_200

    status = db.get_status(now=now, monthly_limit_seconds=36_000)
    assert status["local_reserved_seconds"] == 7_200
    assert status["local_available_seconds"] == 28_800
    assert status["effective_available_seconds"] == 21_600


def test_reservation_is_idempotent_for_same_bvid_and_cid(tmp_path):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=SHANGHAI)
    db = Database(tmp_path / "mcp.db")
    db.initialize()
    db.save_provider_snapshot(fresh_provider_state(now))

    original = db.reserve_transcription(
        bvid="BV1same",
        cid=99,
        page=1,
        duration_seconds=600,
        now=now,
        monthly_limit_seconds=36_000,
        provider_stale_after=timedelta(minutes=30),
    )
    duplicate = db.reserve_transcription(
        bvid="BV1same",
        cid=99,
        page=1,
        duration_seconds=600,
        now=now,
        monthly_limit_seconds=36_000,
        provider_stale_after=timedelta(minutes=30),
    )

    assert duplicate.job_id == original.job_id
    assert db.get_status(now=now, monthly_limit_seconds=36_000)["local_reserved_seconds"] == 600


def test_stale_provider_snapshot_fails_closed(tmp_path):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=SHANGHAI)
    db = Database(tmp_path / "mcp.db")
    db.initialize()
    db.save_provider_snapshot(fresh_provider_state(now - timedelta(minutes=31)))

    with pytest.raises(MCPError) as exc:
        db.reserve_transcription(
            bvid="BV1stale",
            cid=10,
            page=1,
            duration_seconds=60,
            now=now,
            monthly_limit_seconds=36_000,
            provider_stale_after=timedelta(minutes=30),
        )

    assert exc.value.code == "ASR_PROVIDER_QUOTA_STALE"


def test_autostop_must_be_confirmed_by_provider(tmp_path):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=SHANGHAI)
    db = Database(tmp_path / "mcp.db")
    db.initialize()
    state = fresh_provider_state(now)
    state["free_tier_autostop"] = False
    db.save_provider_snapshot(state)

    with pytest.raises(MCPError) as exc:
        db.reserve_transcription(
            bvid="BV1guard",
            cid=11,
            page=1,
            duration_seconds=60,
            now=now,
            monthly_limit_seconds=36_000,
            provider_stale_after=timedelta(minutes=30),
        )

    assert exc.value.code == "ASR_AUTOSTOP_NOT_CONFIRMED"


def test_concurrent_reservations_cannot_double_spend(tmp_path):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=SHANGHAI)
    db = Database(tmp_path / "mcp.db")
    db.initialize()
    db.save_provider_snapshot(fresh_provider_state(now, remaining=36_000))

    def reserve(index: int):
        try:
            return db.reserve_transcription(
                bvid=f"BV1race{index}", cid=index, page=1,
                duration_seconds=20_000, now=now,
                monthly_limit_seconds=36_000,
                provider_stale_after=timedelta(minutes=30),
            ).job_id
        except MCPError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, [1, 2]))
    assert results.count("ASR_FREE_QUOTA_EXHAUSTED") == 1
    assert db.get_status(now=now, monthly_limit_seconds=36_000)["local_reserved_seconds"] == 20_000
