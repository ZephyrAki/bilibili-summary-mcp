from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .errors import MCPError


ACTIVE_JOB_STATES = (
    "reserved",
    "resolving_source",
    "downloading_media",
    "submitting",
    "submitted",
    "processing",
    "submission_uncertain",
    "pending_reconciliation",
)


@dataclass(frozen=True)
class JobReservation:
    job_id: str
    reserved_seconds: int
    created: bool
    cached_document_id: str | None = None


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asr_jobs (
                    job_id TEXT PRIMARY KEY,
                    bvid TEXT NOT NULL,
                    cid INTEGER NOT NULL,
                    page INTEGER NOT NULL,
                    provider_task_id TEXT,
                    state TEXT NOT NULL,
                    reserved_seconds INTEGER NOT NULL,
                    actual_seconds INTEGER,
                    provider_submit_started_at TEXT,
                    error_code TEXT,
                    error_summary TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS asr_jobs_video_state
                    ON asr_jobs(bvid, cid, state);
                CREATE TABLE IF NOT EXISTS asr_job_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES asr_jobs(job_id) ON DELETE CASCADE,
                    method TEXT NOT NULL,
                    state TEXT NOT NULL,
                    provider_task_id TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asr_usage_ledger (
                    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    month TEXT NOT NULL,
                    job_id TEXT NOT NULL UNIQUE REFERENCES asr_jobs(job_id),
                    reserved_seconds INTEGER NOT NULL,
                    actual_seconds INTEGER,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS asr_usage_month_state
                    ON asr_usage_ledger(month, state);
                CREATE TABLE IF NOT EXISTS provider_quota_snapshot (
                    snapshot_id INTEGER PRIMARY KEY CHECK(snapshot_id = 1),
                    provider TEXT NOT NULL DEFAULT 'aliyun',
                    engine TEXT NOT NULL DEFAULT 'paraformer-v2',
                    region TEXT NOT NULL DEFAULT 'cn-beijing',
                    quota_total_seconds INTEGER,
                    quota_remaining_seconds INTEGER,
                    quota_used_seconds INTEGER,
                    quota_expires_at TEXT,
                    quota_observed_at TEXT,
                    quota_synced_at TEXT,
                    quota_sync_status TEXT NOT NULL,
                    quota_error_code TEXT,
                    free_tier_autostop INTEGER,
                    autostop_observed_at TEXT,
                    autostop_synced_at TEXT,
                    autostop_sync_status TEXT NOT NULL,
                    autostop_error_code TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    recent_error_summary TEXT
                );
                CREATE TABLE IF NOT EXISTS transcript_documents (
                    document_id TEXT PRIMARY KEY,
                    bvid TEXT NOT NULL,
                    cid INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    total_segments INTEGER NOT NULL,
                    original_duration_ms INTEGER,
                    content_duration_ms INTEGER,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS transcript_video_cache
                    ON transcript_documents(bvid, cid, expires_at);
                CREATE TABLE IF NOT EXISTS transcript_segments (
                    document_id TEXT NOT NULL REFERENCES transcript_documents(document_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    begin_ms INTEGER,
                    end_ms INTEGER,
                    text TEXT NOT NULL,
                    speaker_id INTEGER,
                    PRIMARY KEY(document_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS coordinator_lease (
                    lease_name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL
                );

                """
            )

        self._migrate_video_sources()
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS video_visual_cache (
                    visual_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
                    video_id TEXT NOT NULL, part TEXT NOT NULL,
                    frames_json TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS video_visual_source
                    ON video_visual_cache(platform,video_id,part,expires_at);
                CREATE TABLE IF NOT EXISTS video_analysis_jobs (
                    job_id TEXT PRIMARY KEY, github_user_id INTEGER NOT NULL,
                    platform TEXT NOT NULL, video_id TEXT NOT NULL, part TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    asr_job_id TEXT REFERENCES asr_jobs(job_id),
                    document_id TEXT REFERENCES transcript_documents(document_id),
                    visual_id TEXT REFERENCES video_visual_cache(visual_id) ON DELETE SET NULL,
                    state TEXT NOT NULL, visual_error_code TEXT, visual_error_summary TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS video_analysis_source
                    ON video_analysis_jobs(platform,video_id,part,github_user_id,state);
            """)
            columns = {row[1] for row in connection.execute('PRAGMA table_info(video_analysis_jobs)')}
            if 'analysis_kind' not in columns:
                connection.execute("ALTER TABLE video_analysis_jobs ADD COLUMN analysis_kind TEXT NOT NULL DEFAULT 'speech_visual'")
            connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES (2, datetime('now'))")


    def _migrate_video_sources(self) -> None:
        # Rebuild only the two source tables to allow a real NULL BVID for Douyin.
        # Keep table names unchanged for all existing foreign keys and OAuth refs.
        connection = self.connect()
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for table in ("asr_jobs", "transcript_documents"):
                columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
                if "platform" in columns:
                    continue
                ddl = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()[0]
                ddl = ddl.replace(f"CREATE TABLE {table}", f"CREATE TABLE {table}_v2", 1)
                ddl = ddl.replace("bvid TEXT NOT NULL", "bvid TEXT").replace("cid INTEGER NOT NULL", "cid INTEGER")
                ddl = ddl.rstrip().removesuffix(")") + ", platform TEXT NOT NULL DEFAULT 'bilibili', video_id TEXT, part TEXT)"
                connection.execute(ddl)
                names = ",".join(columns)
                connection.execute(f"INSERT INTO {table}_v2 ({names},platform,video_id,part) SELECT {names},'bilibili',bvid,CAST(cid AS TEXT) FROM {table}")
                connection.execute(f"DROP TABLE {table}")
                connection.execute(f"ALTER TABLE {table}_v2 RENAME TO {table}")
            for table in ("asr_jobs", "transcript_documents"):
                connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_legacy_source AFTER INSERT ON {table} WHEN NEW.platform='bilibili' AND NEW.video_id IS NULL BEGIN UPDATE {table} SET video_id=NEW.bvid,part=CAST(NEW.cid AS TEXT) WHERE rowid=NEW.rowid; END")
            connection.execute("CREATE INDEX IF NOT EXISTS asr_jobs_source_state ON asr_jobs(platform,video_id,part,state)")
            connection.execute("CREATE INDEX IF NOT EXISTS transcript_source_cache ON transcript_documents(platform,video_id,part,expires_at)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES (1, datetime('now'))")
            if list(connection.execute("PRAGMA foreign_key_check")):
                raise RuntimeError("video source migration violated foreign keys")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def acquire_coordinator_lease(
        self,
        lease_name: str,
        owner_id: str,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> bool:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_id, expires_at FROM coordinator_lease WHERE lease_name=?",
                (lease_name,),
            ).fetchone()
            if row and row["owner_id"] != owner_id and datetime.fromisoformat(
                row["expires_at"]
            ) > now:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO coordinator_lease (lease_name, owner_id, expires_at, heartbeat_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET owner_id=excluded.owner_id,
                    expires_at=excluded.expires_at, heartbeat_at=excluded.heartbeat_at
                """,
                (
                    lease_name,
                    owner_id,
                    (now + ttl).isoformat(),
                    now.isoformat(),
                ),
            )
            connection.commit()
            return True
        finally:
            connection.close()

    def save_provider_snapshot(self, state: dict[str, Any]) -> None:
        values = {
            "provider": "aliyun",
            "engine": "paraformer-v2",
            "region": "cn-beijing",
            "quota_error_code": None,
            "autostop_error_code": None,
            "consecutive_failures": 0,
            "recent_error_summary": None,
            **state,
        }
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_quota_snapshot (
                    snapshot_id, provider, engine, region,
                    quota_total_seconds, quota_remaining_seconds, quota_used_seconds,
                    quota_expires_at, quota_observed_at, quota_synced_at,
                    quota_sync_status, quota_error_code, free_tier_autostop,
                    autostop_observed_at, autostop_synced_at,
                    autostop_sync_status, autostop_error_code,
                    consecutive_failures, recent_error_summary
                ) VALUES (1, :provider, :engine, :region,
                    :quota_total_seconds, :quota_remaining_seconds, :quota_used_seconds,
                    :quota_expires_at, :quota_observed_at, :quota_synced_at,
                    :quota_sync_status, :quota_error_code, :free_tier_autostop,
                    :autostop_observed_at, :autostop_synced_at,
                    :autostop_sync_status, :autostop_error_code,
                    :consecutive_failures, :recent_error_summary)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    provider=excluded.provider, engine=excluded.engine, region=excluded.region,
                    quota_total_seconds=excluded.quota_total_seconds,
                    quota_remaining_seconds=excluded.quota_remaining_seconds,
                    quota_used_seconds=excluded.quota_used_seconds,
                    quota_expires_at=excluded.quota_expires_at,
                    quota_observed_at=excluded.quota_observed_at,
                    quota_synced_at=excluded.quota_synced_at,
                    quota_sync_status=excluded.quota_sync_status,
                    quota_error_code=excluded.quota_error_code,
                    free_tier_autostop=excluded.free_tier_autostop,
                    autostop_observed_at=excluded.autostop_observed_at,
                    autostop_synced_at=excluded.autostop_synced_at,
                    autostop_sync_status=excluded.autostop_sync_status,
                    autostop_error_code=excluded.autostop_error_code,
                    consecutive_failures=excluded.consecutive_failures,
                    recent_error_summary=excluded.recent_error_summary
                """,
                values,
            )

    def read_provider_snapshot(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_quota_snapshot WHERE snapshot_id=1"
            ).fetchone()
            return dict(row) if row else None

    def merge_provider_snapshot(self, state: dict[str, Any]) -> None:
        current = self.read_provider_snapshot() or {
            "quota_total_seconds": None,
            "quota_remaining_seconds": None,
            "quota_used_seconds": None,
            "quota_expires_at": None,
            "quota_observed_at": None,
            "quota_synced_at": None,
            "quota_sync_status": "never",
            "free_tier_autostop": None,
            "autostop_observed_at": None,
            "autostop_synced_at": None,
            "autostop_sync_status": "never",
            "consecutive_failures": 0,
        }
        current.pop("snapshot_id", None)
        current.update(state)
        self.save_provider_snapshot(current)

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    @staticmethod
    def _month(now: datetime) -> str:
        return now.strftime("%Y-%m")

    def _read_quota_status(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
        monthly_limit_seconds: int,
    ) -> dict[str, Any]:
        snapshot = connection.execute(
            "SELECT * FROM provider_quota_snapshot WHERE snapshot_id=1"
        ).fetchone()
        month = self._month(now)
        finalized_row = connection.execute(
            """
            SELECT COALESCE(SUM(actual_seconds), 0) AS finalized
            FROM asr_usage_ledger WHERE month=? AND state='finalized'
            """,
            (month,),
        ).fetchone()
        reserved_row = connection.execute(
            """
            SELECT COALESCE(SUM(reserved_seconds), 0) AS reserved
            FROM asr_usage_ledger
            WHERE state IN ('reserved','pending_reconciliation')
            """
        ).fetchone()
        finalized = int(finalized_row["finalized"])
        reserved = int(reserved_row["reserved"])
        if snapshot is None:
            return {
                "provider_data_stale": True,
                "quota_total_seconds": None,
                "provider_remaining_seconds": None,
                "provider_used_seconds": None,
                "free_tier_autostop": None,
                "local_finalized_seconds": finalized,
                "local_reserved_seconds": reserved,
                "local_available_seconds": max(
                    0, monthly_limit_seconds - finalized - reserved
                ),
                "effective_available_seconds": 0,
            }
        quota_time = self._parse_time(snapshot["quota_observed_at"])
        autostop_time = self._parse_time(snapshot["autostop_observed_at"])
        stale = (
            snapshot["quota_sync_status"] != "success"
            or snapshot["autostop_sync_status"] != "success"
            or int(snapshot["consecutive_failures"] or 0) >= 3
            or quota_time is None
            or autostop_time is None
        )
        total = int(snapshot["quota_total_seconds"] or 0)
        provider_remaining = int(snapshot["quota_remaining_seconds"] or 0)
        local_cap = min(monthly_limit_seconds, total)
        local_available = max(0, local_cap - finalized - reserved)
        provider_available = max(0, provider_remaining - reserved)
        return {
            "provider_data_stale": stale,
            "quota_observed_at": snapshot["quota_observed_at"],
            "quota_synced_at": snapshot["quota_synced_at"],
            "quota_sync_status": snapshot["quota_sync_status"],
            "autostop_observed_at": snapshot["autostop_observed_at"],
            "autostop_synced_at": snapshot["autostop_synced_at"],
            "autostop_sync_status": snapshot["autostop_sync_status"],
            "quota_total_seconds": total,
            "provider_remaining_seconds": provider_remaining,
            "provider_used_seconds": int(snapshot["quota_used_seconds"] or 0),
            "quota_expires_at": snapshot["quota_expires_at"],
            "free_tier_autostop": bool(snapshot["free_tier_autostop"]),
            "local_finalized_seconds": finalized,
            "local_reserved_seconds": reserved,
            "local_available_seconds": local_available,
            "effective_available_seconds": min(local_available, provider_available),
            "recent_provider_error": snapshot["recent_error_summary"],
            "consecutive_sync_failures": int(snapshot["consecutive_failures"] or 0),
        }

    def get_status(
        self, *, now: datetime, monthly_limit_seconds: int
    ) -> dict[str, Any]:
        with self.connect() as connection:
            status = self._read_quota_status(
                connection, now=now, monthly_limit_seconds=monthly_limit_seconds
            )
            counts = connection.execute(
                """
                SELECT
                  SUM(CASE WHEN state IN ('reserved','resolving_source','downloading_media','submitting','submitted','processing') THEN 1 ELSE 0 END) active,
                  SUM(CASE WHEN state='submission_uncertain' THEN 1 ELSE 0 END) uncertain
                FROM asr_jobs
                """
            ).fetchone()
            cache_count = connection.execute(
                "SELECT COUNT(*) FROM transcript_documents WHERE expires_at>?",
                (now.isoformat(),),
            ).fetchone()[0]
            status.update(
                active_jobs=int(counts["active"] or 0),
                submission_uncertain_jobs=int(counts["uncertain"] or 0),
                valid_cache_documents=int(cache_count),
            )
            return status



    def get_cached_document(
        self, *, bvid: str | None = None, cid: int | None = None, now: datetime,
        platform: str = "bilibili", video_id: str | None = None, part: str | None = None
    ) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM transcript_documents
                WHERE platform=? AND video_id=? AND part=? AND julianday(expires_at)>julianday(?)
                ORDER BY created_at DESC LIMIT 1
                """,
                (platform, video_id or bvid, part or str(cid), now.isoformat()),
            ).fetchone()

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM asr_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise MCPError("ASR_JOB_NOT_FOUND", "找不到本地转写任务。")
            document = connection.execute(
                """SELECT document_id, total_segments FROM transcript_documents
                WHERE platform=? AND video_id=? AND part=? AND julianday(expires_at)>julianday(?) ORDER BY created_at DESC LIMIT 1""",
                (row["platform"], row["video_id"], row["part"], datetime.now().astimezone().isoformat()),
            ).fetchone()
            result = dict(row)
            result["document_id"] = document["document_id"] if document else None
            result["total_segments"] = int(document["total_segments"]) if document else 0
            return result

    def active_job_ids(self) -> set[str]:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT job_id FROM asr_jobs WHERE state IN ({placeholders})",
                ACTIVE_JOB_STATES,
            ).fetchall()
            return {str(row["job_id"]) for row in rows}

    def claim_jobs(self, states: tuple[str, ...], limit: int = 1) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in states)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM asr_jobs WHERE state IN ({placeholders}) ORDER BY created_at LIMIT ?",
                (*states, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def transition_job(
        self,
        job_id: str,
        *,
        expected: tuple[str, ...],
        state: str,
        now: datetime,
        provider_task_id: str | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
        submission_started: bool = False,
    ) -> bool:
        placeholders = ",".join("?" for _ in expected)
        fields = ["state=?", "updated_at=?"]
        values: list[Any] = [state, now.isoformat()]
        if provider_task_id is not None:
            fields.append("provider_task_id=?")
            values.append(provider_task_id)
        if error_code is not None:
            fields.append("error_code=?")
            values.append(error_code)
        if error_summary is not None:
            fields.append("error_summary=?")
            values.append(error_summary[:500])
        if submission_started:
            fields.append("provider_submit_started_at=?")
            values.append(now.isoformat())
        values.extend([job_id, *expected])
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE asr_jobs SET {','.join(fields)} WHERE job_id=? AND state IN ({placeholders})",
                values,
            )
            return cursor.rowcount == 1

    def add_attempt(
        self,
        job_id: str,
        *,
        method: str,
        state: str,
        now: datetime,
        provider_task_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO asr_job_attempts (
                    job_id, method, state, provider_task_id, error_code,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    method,
                    state,
                    provider_task_id,
                    error_code,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

    def has_attempt(self, job_id: str, method: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM asr_job_attempts WHERE job_id=? AND method=? LIMIT 1",
                (job_id, method),
            ).fetchone()
            return row is not None

    def fail_job(
        self, job_id: str, *, code: str, summary: str, now: datetime
    ) -> None:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE asr_jobs SET state='failed', error_code=?, error_summary=?, updated_at=? WHERE job_id=?",
                (code, summary[:500], now.isoformat(), job_id),
            )
            connection.execute(
                "UPDATE asr_usage_ledger SET state='released', updated_at=? WHERE job_id=?",
                (now.isoformat(), job_id),
            )
            connection.commit()
        finally:
            connection.close()

    def mark_pending_reconciliation(self, job_id: str, *, now: datetime) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE asr_jobs SET state='pending_reconciliation', updated_at=? WHERE job_id=?",
                (now.isoformat(), job_id),
            )
            connection.execute(
                "UPDATE asr_usage_ledger SET state='pending_reconciliation', updated_at=? WHERE job_id=?",
                (now.isoformat(), job_id),
            )

    def recover_stale_jobs(self, *, now: datetime, expire_reserved: bool = True) -> dict[str, int]:
        reserved_before = (now - timedelta(minutes=10)).isoformat()
        resolving_before = (now - timedelta(minutes=45)).isoformat()
        submitting_before = (now - timedelta(minutes=10)).isoformat()
        # 音频通常仅数 MB，给中转下载 20 分钟余量。
        downloading_before = (now - timedelta(minutes=20)).isoformat()
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            safe_rows = connection.execute(
                """
                SELECT job_id FROM asr_jobs
                WHERE (state='reserved' AND updated_at<? AND ?)
                   OR (state='resolving_source' AND updated_at<? AND provider_submit_started_at IS NULL)
                """,
                (reserved_before, expire_reserved, resolving_before),
            ).fetchall()
            safe_ids = [row["job_id"] for row in safe_rows]
            for job_id in safe_ids:
                connection.execute(
                    """UPDATE asr_jobs SET state='failed', error_code='ASR_STALE_BEFORE_SUBMISSION',
                    error_summary='任务在调用 Provider 前超时，预留已安全释放。', updated_at=? WHERE job_id=?""",
                    (now.isoformat(), job_id),
                )
                connection.execute(
                    "UPDATE asr_usage_ledger SET state='released', updated_at=? WHERE job_id=?",
                    (now.isoformat(), job_id),
                )
            # downloading_media 只出现在直连失败后的中转阶段（此时直连任务已提交过），
            # 进程崩溃后无法自动确认是否计费，保守标记为待对账并保留额度，禁止自动重试。
            uncertain = connection.execute(
                """
                UPDATE asr_jobs SET state='submission_uncertain',
                    error_code='ASR_SUBMISSION_UNCERTAIN',
                    error_summary='提交或中转下载阶段超时，保留额度且禁止自动重试。', updated_at=?
                WHERE (state='submitting' AND updated_at<? AND provider_submit_started_at IS NOT NULL)
                   OR (state='downloading_media' AND updated_at<? AND provider_submit_started_at IS NOT NULL)
                """,
                (now.isoformat(), submitting_before, downloading_before),
            ).rowcount
            connection.commit()
            return {"released": len(safe_ids), "uncertain": int(uncertain)}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reconcile_job(
        self,
        job_id: str,
        *,
        now: datetime,
        actual_seconds: int | None = None,
        confirm_never_submitted: bool = False,
    ) -> None:
        if actual_seconds is None and not confirm_never_submitted:
            raise MCPError(
                "RECONCILIATION_EVIDENCE_REQUIRED",
                "必须提供实际计量秒数，或明确确认 Provider 从未收到任务。",
            )
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT state FROM asr_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if job is None or job["state"] not in {
                "submission_uncertain",
                "pending_reconciliation",
            }:
                raise MCPError("JOB_NOT_RECONCILABLE", "任务不处于待对账状态。")
            if actual_seconds is not None:
                if actual_seconds < 0:
                    raise MCPError("INVALID_ACTUAL_SECONDS", "实际秒数不能为负。")
                connection.execute(
                    "UPDATE asr_usage_ledger SET actual_seconds=?, state='finalized', updated_at=? WHERE job_id=?",
                    (actual_seconds, now.isoformat(), job_id),
                )
                document = connection.execute(
                    """SELECT 1 FROM transcript_documents d JOIN asr_jobs j
                    ON d.platform=j.platform AND d.video_id=j.video_id AND d.part=j.part WHERE j.job_id=? LIMIT 1""",
                    (job_id,),
                ).fetchone()
                terminal_state = "succeeded" if document else "failed"
                connection.execute(
                    "UPDATE asr_jobs SET actual_seconds=?, state=?, error_code='ASR_RECONCILED', updated_at=? WHERE job_id=?",
                    (actual_seconds, terminal_state, now.isoformat(), job_id),
                )
            else:
                connection.execute(
                    "UPDATE asr_usage_ledger SET state='released', updated_at=? WHERE job_id=?",
                    (now.isoformat(), job_id),
                )
                connection.execute(
                    "UPDATE asr_jobs SET state='failed', error_code='ASR_CONFIRMED_NOT_SUBMITTED', updated_at=? WHERE job_id=?",
                    (now.isoformat(), job_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reserve_transcription(
        self,
        *,
        bvid: str | None = None,
        cid: int | None = None,
        page: int = 1,
        platform: str = "bilibili",
        video_id: str | None = None,
        part: str | None = None,
        duration_seconds: int,
        now: datetime,
        monthly_limit_seconds: int,
        provider_stale_after: timedelta,
        transaction_guard: Callable[[], None] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> JobReservation:
        video_id = video_id or bvid
        part = part or str(cid)
        if platform not in {"bilibili", "douyin"} or not video_id or (platform == "bilibili" and (not bvid or cid is None)):
            raise MCPError("INVALID_VIDEO_SOURCE", "视频来源无效。")
        if duration_seconds <= 0:
            raise MCPError("INVALID_VIDEO_DURATION", "视频时长必须大于零。")
        owns_connection = connection is None
        connection = connection or self.connect()
        try:
            if owns_connection:
                connection.execute("BEGIN IMMEDIATE")
            if transaction_guard is not None:
                transaction_guard()
            cached = connection.execute(
                """
                SELECT document_id FROM transcript_documents
                WHERE platform=? AND video_id=? AND part=? AND julianday(expires_at)>julianday(?)
                ORDER BY created_at DESC LIMIT 1
                """,
                (platform, video_id or bvid, part or str(cid), now.isoformat()),
            ).fetchone()
            if cached:
                if owns_connection:
                    connection.commit()
                return JobReservation(
                    job_id="",
                    reserved_seconds=0,
                    created=False,
                    cached_document_id=str(cached["document_id"]),
                )
            placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
            existing = connection.execute(
                f"SELECT job_id, reserved_seconds FROM asr_jobs "
                f"WHERE platform=? AND video_id=? AND part=? AND state IN ({placeholders}) "
                "ORDER BY created_at DESC LIMIT 1",
                (platform, video_id, part, *ACTIVE_JOB_STATES),
            ).fetchone()
            if existing:
                if owns_connection:
                    connection.commit()
                return JobReservation(
                    existing["job_id"], int(existing["reserved_seconds"]), False
                )

            status = self._read_quota_status(
                connection, now=now, monthly_limit_seconds=monthly_limit_seconds
            )
            quota_time = self._parse_time(status.get("quota_observed_at"))
            autostop_time = self._parse_time(status.get("autostop_observed_at"))
            stale = (
                status["provider_data_stale"]
                or quota_time is None
                or autostop_time is None
                or now - quota_time > provider_stale_after
                or now - autostop_time > provider_stale_after
            )
            if stale:
                raise MCPError(
                    "ASR_PROVIDER_QUOTA_STALE",
                    "阿里云免费额度或用完即停状态已过期，暂不允许新建转写。",
                )
            if not status["free_tier_autostop"]:
                raise MCPError(
                    "ASR_AUTOSTOP_NOT_CONFIRMED",
                    "阿里云免费额度用完即停尚未确认开启。",
                )
            if duration_seconds > status["effective_available_seconds"]:
                raise MCPError(
                    "ASR_FREE_QUOTA_EXHAUSTED", "本月安全可用免费额度不足。"
                )

            job_id = str(uuid.uuid4())
            timestamp = now.isoformat()
            connection.execute(
                """
                INSERT INTO asr_jobs (
                    job_id, bvid, cid, page, platform, video_id, part, state, reserved_seconds,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
                """,
                (job_id, bvid, cid, page, platform, video_id, part, duration_seconds, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO asr_usage_ledger (
                    month, job_id, reserved_seconds, state, created_at, updated_at
                ) VALUES (?, ?, ?, 'reserved', ?, ?)
                """,
                (
                    self._month(now),
                    job_id,
                    duration_seconds,
                    timestamp,
                    timestamp,
                ),
            )
            if owns_connection:
                connection.commit()
            return JobReservation(job_id, duration_seconds, True)
        except Exception:
            if owns_connection:
                connection.rollback()
            raise
        finally:
            if owns_connection:
                connection.close()

    def check_submission_quota(self, job_id: str, *, now: datetime,
                               monthly_limit_seconds: int,
                               provider_stale_after: timedelta) -> None:
        status = self.get_status(now=now, monthly_limit_seconds=monthly_limit_seconds)
        times = [self._parse_time(status.get(key)) for key in ("quota_observed_at", "autostop_observed_at")]
        if status["provider_data_stale"] or any(t is None or now-t > provider_stale_after for t in times):
            raise MCPError("ASR_PROVIDER_QUOTA_STALE", "提交前额度或用完即停观测已失效。")
        if not status["free_tier_autostop"]:
            raise MCPError("ASR_AUTOSTOP_NOT_CONFIRMED", "用完即停未确认开启。")
        if (status["provider_remaining_seconds"] < status["local_reserved_seconds"] or
                min(monthly_limit_seconds,status["quota_total_seconds"]) <
                status["local_finalized_seconds"] + status["local_reserved_seconds"]):
            raise MCPError("ASR_FREE_QUOTA_EXHAUSTED", "提交前安全免费额度不足。")

    def complete_transcription(
        self,
        *,
        job_id: str,
        actual_seconds: int | None,
        original_duration_ms: int | None,
        content_duration_ms: int,
        segments: list[dict[str, Any]],
        content_hash: str,
        now: datetime,
        cache_ttl: timedelta,
    ) -> str:
        document_id = str(uuid.uuid4())
        timestamp = now.isoformat()
        expires_at = (now + cache_ttl).isoformat()
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT bvid, cid, platform, video_id, part FROM asr_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if job is None:
                raise MCPError("ASR_JOB_NOT_FOUND", "找不到本地转写任务。")
            connection.execute(
                """
                INSERT INTO transcript_documents (
                    document_id, bvid, cid, platform, video_id, part, source_type, source_version,
                    schema_version, content_hash, total_segments,
                    original_duration_ms, content_duration_ms, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'aliyun_paraformer', 'paraformer-v2', 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    job["bvid"],
                    job["cid"],
                    job["platform"], job["video_id"], job["part"],
                    content_hash,
                    len(segments),
                    original_duration_ms,
                    content_duration_ms,
                    timestamp,
                    expires_at,
                ),
            )
            connection.executemany(
                """
                INSERT INTO transcript_segments (
                    document_id, sequence, begin_ms, end_ms, text, speaker_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        document_id,
                        sequence,
                        segment.get("begin_ms"),
                        segment.get("end_ms"),
                        segment["text"],
                        segment.get("speaker_id"),
                    )
                    for sequence, segment in enumerate(segments)
                ],
            )
            if actual_seconds is None:
                connection.execute(
                    "UPDATE asr_usage_ledger SET state='pending_reconciliation', updated_at=? WHERE job_id=?",
                    (timestamp, job_id),
                )
                connection.execute(
                    "UPDATE asr_jobs SET state='pending_reconciliation', error_code='ASR_USAGE_RECONCILIATION_REQUIRED', updated_at=? WHERE job_id=?",
                    (timestamp, job_id),
                )
            else:
                connection.execute(
                    "UPDATE asr_usage_ledger SET actual_seconds=?, state='finalized', updated_at=? WHERE job_id=?",
                    (actual_seconds, timestamp, job_id),
                )
                connection.execute(
                    "UPDATE asr_jobs SET actual_seconds=?, state='succeeded', updated_at=? WHERE job_id=?",
                    (actual_seconds, timestamp, job_id),
                )
            connection.commit()
            return document_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_transcript_page(
        self, *, document_id: str, offset: int, limit: int
    ) -> dict[str, Any]:
        if offset < 0 or limit < 1:
            raise MCPError("INVALID_PAGINATION", "分页参数无效。")
        limit = min(limit, 100)
        with self.connect() as connection:
            document = connection.execute(
                "SELECT source_type, total_segments FROM transcript_documents WHERE document_id=? AND julianday(expires_at)>julianday('now')",
                (document_id,),
            ).fetchone()
            if document is None:
                raise MCPError("TRANSCRIPT_NOT_FOUND", "字幕不存在或已过期。")
            rows = connection.execute(
                """
                SELECT sequence, begin_ms, end_ms, text, speaker_id
                FROM transcript_segments WHERE document_id=? AND sequence>=?
                ORDER BY sequence LIMIT ?
                """,
                (document_id, offset, limit),
            ).fetchall()
        next_offset = offset + len(rows)
        total = int(document["total_segments"])
        truncated = next_offset < total
        return {
            "source": document["source_type"],
            "total_segments": total,
            "segments": [dict(row) for row in rows],
            "next_offset": next_offset if truncated else None,
            "truncated": truncated,
        }
