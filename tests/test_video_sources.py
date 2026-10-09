from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from bilibili_summary_mcp.db import Database
from bilibili_summary_mcp.errors import MCPError
from bilibili_summary_mcp.tokens import SignedTokenService
from test_quota_admission import fresh_provider_state

NOW = datetime.now(timezone.utc)


def test_analysis_kind_migration_preserves_existing_rows(tmp_path):
    from test_video_analysis import manager
    obj,db,tokens=manager(tmp_path)
    job=reserve(db,bvid='BV1legacy',cid=42,page=1)
    document=db.complete_transcription(job_id=job.job_id,actual_seconds=18,
        original_duration_ms=20000,content_duration_ms=18000,segments=[{'text':'旧文稿'}],
        content_hash='hash',now=NOW,cache_ttl=timedelta(days=30))
    with db.connect() as conn:
        conn.execute("INSERT INTO video_analysis_jobs (job_id,github_user_id,platform,video_id,part,duration_seconds,asr_job_id,document_id,state,created_at,updated_at) VALUES ('old-analysis',1,'douyin','12345678','1',20,?,?,'queued',?,?)",
            (job.job_id,document,NOW.isoformat(),NOW.isoformat()))
        columns=[row[1] for row in conn.execute('PRAGMA table_info(video_analysis_jobs)')]
        if 'analysis_kind' in columns:
            conn.execute('ALTER TABLE video_analysis_jobs DROP COLUMN analysis_kind')
        before={table:[tuple(row) for row in conn.execute(f'SELECT * FROM {table}')] for table in
            ['asr_jobs','asr_job_attempts','asr_usage_ledger','transcript_documents','transcript_segments']}
        old=dict(conn.execute("SELECT * FROM video_analysis_jobs WHERE job_id='old-analysis'").fetchone())
    db.initialize()
    db.initialize()
    with db.connect() as conn:
        row=dict(conn.execute("SELECT * FROM video_analysis_jobs WHERE job_id='old-analysis'").fetchone())
        assert row.pop('analysis_kind')=='speech_visual'
        assert row==old
        assert list(conn.execute('PRAGMA foreign_key_check'))==[]
        for table,rows in before.items():
            assert [tuple(row) for row in conn.execute(f'SELECT * FROM {table}')]==rows


def reserve(db, **source):
    return db.reserve_transcription(duration_seconds=20, now=NOW,
        monthly_limit_seconds=36000, provider_stale_after=timedelta(minutes=30), **source)


def test_cross_platform_cache_and_quota_are_isolated(tmp_path):
    db = Database(tmp_path / 'mcp.db')
    db.initialize()
    db.save_provider_snapshot(fresh_provider_state(NOW, remaining=35))
    bili = reserve(db, bvid='12345678', cid=1, page=1)
    with pytest.raises(MCPError) as caught:
        reserve(db, platform='douyin', video_id='12345678', part='1')
    assert caught.value.code == 'ASR_FREE_QUOTA_EXHAUSTED'
    db.complete_transcription(job_id=bili.job_id, actual_seconds=20,
        original_duration_ms=20000, content_duration_ms=20000,
        segments=[{'text':'bili'}], content_hash='hash', now=NOW, cache_ttl=timedelta(days=30))
    assert db.get_cached_document(platform='douyin', video_id='12345678', part='1', now=NOW) is None
    db.save_provider_snapshot(fresh_provider_state(NOW))
    dy = reserve(db, platform='douyin', video_id='12345678', part='1')
    job = db.get_job(dy.job_id)
    assert job['bvid'] is None
    assert (job['platform'], job['video_id'], job['part']) == ('douyin','12345678','1')
    assert reserve(db, platform='douyin', video_id='12345678', part='1').job_id == dy.job_id
    db.transition_job(dy.job_id, expected=('reserved',), state='submission_uncertain', now=NOW)
    assert reserve(db, platform='douyin', video_id='12345678', part='1').job_id == dy.job_id


def test_legacy_database_migration_preserves_ids_and_foreign_keys(tmp_path):
    path = tmp_path / 'mcp.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
        CREATE TABLE asr_jobs (job_id TEXT PRIMARY KEY,bvid TEXT NOT NULL,cid INTEGER NOT NULL,
        page INTEGER NOT NULL,provider_task_id TEXT,state TEXT NOT NULL,reserved_seconds INTEGER NOT NULL,
        actual_seconds INTEGER,provider_submit_started_at TEXT,error_code TEXT,error_summary TEXT,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        INSERT INTO asr_jobs VALUES ('old','BV1legacy',42,2,NULL,'reserved',20,NULL,NULL,NULL,NULL,'2026-10-09','2026-10-09');
        ''')
    db = Database(path)
    db.initialize()
    db.initialize()
    job = db.get_job('old')
    assert (job['platform'],job['video_id'],job['part']) == ('bilibili','BV1legacy','42')
    assert job['bvid'] == 'BV1legacy' and job['page'] == 2
    with db.connect() as conn:
        assert list(conn.execute('PRAGMA foreign_key_check')) == []


def test_legacy_context_defaults_to_bilibili_and_douyin_is_user_bound():
    tokens = SignedTokenService(b'x'*32)
    old = tokens.issue_context(github_user_id=1,bvid='BV1old',cid=2,page=1,
        duration_seconds=20,now=NOW,ttl=timedelta(minutes=30))
    assert tokens.verify_context(old,github_user_id=1,now=NOW)['platform'] == 'bilibili'
    new = tokens.issue_video_context(github_user_id=1,platform='douyin',video_id='12345678',part='1',
        duration_seconds=20,now=NOW,ttl=timedelta(minutes=30))
    payload = tokens.verify_context(new,github_user_id=1,now=NOW)
    assert payload['platform'] == 'douyin' and 'bvid' not in payload
    with pytest.raises(MCPError):
        tokens.verify_context(new,github_user_id=2,now=NOW)


def test_migration_preserves_document_segments_and_ledger(tmp_path):
    path = tmp_path/'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
        CREATE TABLE asr_jobs (job_id TEXT PRIMARY KEY,bvid TEXT NOT NULL,cid INTEGER NOT NULL,
        page INTEGER NOT NULL,provider_task_id TEXT,state TEXT NOT NULL,reserved_seconds INTEGER NOT NULL,
        actual_seconds INTEGER,provider_submit_started_at TEXT,error_code TEXT,error_summary TEXT,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        INSERT INTO asr_jobs VALUES ('old','BV1legacy',42,2,'provider-id','succeeded',20,18,NULL,NULL,NULL,'2026-10-09','2026-10-09');
        CREATE TABLE transcript_documents (document_id TEXT PRIMARY KEY,bvid TEXT NOT NULL,cid INTEGER NOT NULL,
        source_type TEXT NOT NULL,source_version TEXT NOT NULL,schema_version INTEGER NOT NULL,
        content_hash TEXT NOT NULL,total_segments INTEGER NOT NULL,original_duration_ms INTEGER,
        content_duration_ms INTEGER,created_at TEXT NOT NULL,expires_at TEXT NOT NULL);
        INSERT INTO transcript_documents VALUES ('document-old','BV1legacy',42,'aliyun_paraformer','paraformer-v2',1,'hash',1,20000,18000,'2026-10-09','2099-10-09');
        CREATE TABLE transcript_segments (document_id TEXT NOT NULL REFERENCES transcript_documents(document_id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL,begin_ms INTEGER,end_ms INTEGER,text TEXT NOT NULL,speaker_id INTEGER,
        PRIMARY KEY(document_id,sequence));
        INSERT INTO transcript_segments VALUES ('document-old',0,0,18000,'原来的文稿',NULL);
        CREATE TABLE asr_usage_ledger (ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,month TEXT NOT NULL,
        job_id TEXT NOT NULL UNIQUE REFERENCES asr_jobs(job_id),reserved_seconds INTEGER NOT NULL,
        actual_seconds INTEGER,state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        INSERT INTO asr_usage_ledger VALUES (1,'2026-10','old',20,18,'finalized','2026-10-09','2026-10-09');
        ''')
    db = Database(path)
    db.initialize()
    db.initialize()
    assert db.get_job('old')['document_id'] == 'document-old'
    assert db.get_transcript_page(document_id='document-old',offset=0,limit=4)['segments'][0]['text'] == '原来的文稿'
    with db.connect() as conn:
        assert dict(conn.execute('SELECT * FROM asr_usage_ledger').fetchone())['actual_seconds'] == 18
        assert list(conn.execute('PRAGMA foreign_key_check')) == []
    assert db.get_cached_document(bvid='BV1legacy',cid=42,now=NOW)['document_id'] == 'document-old'


def test_expired_transcript_is_not_returned_by_reference(tmp_path):
    db = Database(tmp_path/'cache.db')
    db.initialize()
    past = NOW-timedelta(days=31)
    db.save_provider_snapshot(fresh_provider_state(past))
    job = db.reserve_transcription(bvid='BV1expired',cid=1,page=1,duration_seconds=20,now=past,
        monthly_limit_seconds=36000,provider_stale_after=timedelta(minutes=30))
    document_id = db.complete_transcription(job_id=job.job_id,actual_seconds=20,original_duration_ms=20000,
        content_duration_ms=20000,segments=[{'text':'expired'}],content_hash='hash',now=past,cache_ttl=timedelta(days=30))
    with pytest.raises(MCPError) as caught:
        db.get_transcript_page(document_id=document_id,offset=0,limit=4)
    assert caught.value.code == 'TRANSCRIPT_NOT_FOUND'
    assert db.get_job(job.job_id)['document_id'] is None
