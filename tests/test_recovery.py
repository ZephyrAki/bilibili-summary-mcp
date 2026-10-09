from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bilibili_summary_mcp.db import Database


def test_stale_reserved_job_releases_but_submitting_becomes_uncertain(tmp_path):
    tz = ZoneInfo("Asia/Shanghai")
    old = datetime(2026, 9, 20, 10, 0, tzinfo=tz)
    now = old + timedelta(hours=1)
    db = Database(tmp_path / "mcp.db")
    db.initialize()
    db.save_provider_snapshot(
        {
            "quota_total_seconds": 36_000,
            "quota_remaining_seconds": 36_000,
            "quota_used_seconds": 0,
            "quota_expires_at": "2026-10-01T00:00:00+08:00",
            "quota_observed_at": old.isoformat(),
            "quota_synced_at": old.isoformat(),
            "quota_sync_status": "success",
            "free_tier_autostop": True,
            "autostop_observed_at": old.isoformat(),
            "autostop_synced_at": old.isoformat(),
            "autostop_sync_status": "success",
        }
    )
    first = db.reserve_transcription(
        bvid="BV1stale", cid=1, page=1, duration_seconds=60, now=old,
        monthly_limit_seconds=36_000, provider_stale_after=timedelta(hours=2)
    )
    second = db.reserve_transcription(
        bvid="BV1submit", cid=2, page=1, duration_seconds=60, now=old,
        monthly_limit_seconds=36_000, provider_stale_after=timedelta(hours=2)
    )
    db.transition_job(
        second.job_id, expected=("reserved",), state="submitting", now=old,
        submission_started=True
    )

    recovered = db.recover_stale_jobs(now=now)
    assert recovered == {"released": 1, "uncertain": 1}
    assert db.get_job(first.job_id)["state"] == "failed"
    assert db.get_job(second.job_id)["state"] == "submission_uncertain"
    assert db.get_status(now=now, monthly_limit_seconds=36_000)["local_reserved_seconds"] == 60


def test_stale_downloading_media_after_submit_keeps_quota_uncertain(tmp_path):
    tz = ZoneInfo("Asia/Shanghai")
    old = datetime(2026, 9, 20, 10, 0, tzinfo=tz)
    now = old + timedelta(minutes=25)
    db = Database(tmp_path / "mcp.db")
    db.initialize()
    db.save_provider_snapshot(
        {
            "quota_total_seconds": 36_000,
            "quota_remaining_seconds": 36_000,
            "quota_used_seconds": 0,
            "quota_expires_at": "2026-10-01T00:00:00+08:00",
            "quota_observed_at": old.isoformat(),
            "quota_synced_at": old.isoformat(),
            "quota_sync_status": "success",
            "free_tier_autostop": True,
            "autostop_observed_at": old.isoformat(),
            "autostop_synced_at": old.isoformat(),
            "autostop_sync_status": "success",
        }
    )
    job = db.reserve_transcription(
        bvid="BV1download", cid=3, page=1, duration_seconds=60, now=old,
        monthly_limit_seconds=36_000, provider_stale_after=timedelta(hours=2)
    )
    # 模拟直连已提交、进入中转下载（downloading_media）后进程卡死。
    assert db.transition_job(
        job.job_id, expected=("reserved",), state="resolving_source", now=old
    )
    assert db.transition_job(
        job.job_id, expected=("resolving_source",), state="submitting", now=old,
        submission_started=True
    )
    assert db.transition_job(
        job.job_id, expected=("submitting",), state="submitted", now=old
    )
    assert db.transition_job(
        job.job_id, expected=("submitted", "processing"),
        state="downloading_media", now=old
    )

    recovered = db.recover_stale_jobs(now=now)
    assert recovered["uncertain"] == 1
    assert db.get_job(job.job_id)["state"] == "submission_uncertain"
    # 直连任务可能已提交，无法确认是否计费，必须保留额度、禁止自动重试。
    assert (
        db.get_status(now=now, monthly_limit_seconds=36_000)["local_reserved_seconds"]
        == 60
    )
