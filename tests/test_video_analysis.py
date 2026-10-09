from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
import json

import pytest

from bilibili_summary_mcp.db import Database
from bilibili_summary_mcp.errors import MCPError
from bilibili_summary_mcp.tokens import SignedTokenService
from bilibili_summary_mcp.media import MediaRelay
from test_tool_annotations import _settings
from test_quota_admission import fresh_provider_state

NOW = datetime.now(ZoneInfo('Asia/Shanghai'))
VIDEO_ID = '7412345678901234567'


class DouyinStub:
    async def video_id(self,link):
        return VIDEO_ID

    async def detail(self,video_id):
        return {'platform':'douyin','video_id':video_id,'part':'1','title':'test','author':'作者',
                'duration_seconds':6,'video_urls':['https://v3.douyinvod.com/video.mp4']}

    async def download(self,urls,target):
        target.write_bytes(b'full-video')
        return target


class ProcessorStub:
    async def probe(self,path):
        return {'duration_seconds':6,'width':100,'height':100,'has_audio':True}

    async def audio(self,source,target):
        target.write_bytes(b'audio')
        return target

    async def frames(self,source,visual_id,duration):
        self.directory.joinpath(visual_id).mkdir()
        result=[]
        for i in range(6):
            name=f'{i}.jpg'
            self.directory.joinpath(visual_id,name).write_bytes(b'jpeg')
            result.append({'sequence':i,'timestamp_ms':1000*i,'filename':name})
        return result


def manager(tmp_path):
    from bilibili_summary_mcp.video_analysis import VideoAnalysis
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.initialize()
    db.save_provider_snapshot(fresh_provider_state(NOW))
    tokens = SignedTokenService(settings.token_signing_secret)
    relay = MediaRelay(tmp_path/'tmp',tokens,settings.public_base_url)
    obj = VideoAnalysis(settings,db,DouyinStub(),tokens,relay,asyncio.Lock())
    processor = ProcessorStub()
    processor.directory = obj.processor.directory
    obj.processor = processor
    return obj,db,tokens


@pytest.mark.asyncio
async def test_prepare_has_no_database_side_effects_and_start_deduplicates(tmp_path):
    obj,db,tokens = manager(tmp_path)
    prepared = await obj.prepare('link',1)
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM asr_jobs').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM video_analysis_jobs').fetchone()[0] == 0
    first = obj.start(prepared['context_token'],True,1)
    second = obj.start(prepared['context_token'],True,1)
    assert first['job_id'] == second['job_id']
    assert db.get_status(now=NOW,monthly_limit_seconds=36000)['local_reserved_seconds'] == 6
    with pytest.raises(MCPError):
        obj.get_job(first['job_id'],2)
    with pytest.raises(MCPError):
        obj.start(prepared['context_token'],True,2)


@pytest.mark.asyncio
async def test_frames_partial_result_and_user_bound_pagination(tmp_path):
    obj,db,tokens = manager(tmp_path)
    prepared = await obj.prepare('link',1)
    job = obj.start(prepared['context_token'],True,1)
    asr_id = obj.get_job(job['job_id'],1)['asr_job_id']
    source = await obj.prepare_source(db.get_job(asr_id))
    assert '/media/' in source
    db.fail_job(asr_id,code='TEST_FAILURE',summary='failed',now=NOW)
    status = obj.get_job(job['job_id'],1)
    assert status['state'] == 'partial'
    assert status['speech']['state'] == 'failed'
    assert status['visual']['state'] == 'succeeded'
    page = obj.frame_page(status['visual_ref'],None,100,1)
    assert len(page['frames']) == 4 and page['next_cursor']
    next_page = obj.frame_page(status['visual_ref'],page['next_cursor'],4,1)
    assert len(next_page['frames']) == 2 and next_page['next_cursor'] is None
    with pytest.raises(MCPError):
        obj.frame_page(status['visual_ref'],None,4,2)
    await obj.run_once()
    assert list(obj.relay.directory.iterdir()) == []


@pytest.mark.asyncio
async def test_cached_speech_only_repairs_frames_without_reserving_or_submitting(tmp_path):
    obj,db,tokens = manager(tmp_path)
    prepared = await obj.prepare('link',1)
    first = obj.start(prepared['context_token'],True,1)
    asr_id = obj.get_job(first['job_id'],1)['asr_job_id']
    db.complete_transcription(job_id=asr_id,actual_seconds=6,original_duration_ms=6000,
        content_duration_ms=6000,segments=[{'text':'讲话'}],content_hash='hash',now=NOW,cache_ttl=timedelta(days=30))
    # No frame cache yet; the analysis should repair only frames even if quota is stale.
    with db.connect() as conn:
        conn.execute("UPDATE video_analysis_jobs SET state='failed' WHERE job_id=?",(first['job_id'],))
    db.merge_provider_snapshot({'quota_sync_status':'failed'})
    repaired = obj.start(prepared['context_token'],False,1)
    assert obj.get_job(repaired['job_id'],1)['asr_job_id'] is None
    await obj.run_once()
    status = obj.get_job(repaired['job_id'],1)
    assert status['state'] == 'succeeded'
    assert status['transcript_ref']
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM asr_jobs').fetchone()[0] == 1
    assert list(obj.relay.directory.iterdir()) == []


@pytest.mark.asyncio
async def test_start_rolls_back_analysis_and_reservation_when_quota_stale(tmp_path):
    obj,db,tokens = manager(tmp_path)
    prepared = await obj.prepare('link',1)
    db.merge_provider_snapshot({'autostop_sync_status':'failed'})
    with pytest.raises(MCPError) as caught:
        obj.start(prepared['context_token'],True,1)
    assert caught.value.code == 'ASR_PROVIDER_QUOTA_STALE'
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM asr_jobs').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM video_analysis_jobs').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_expired_speech_is_missing_and_can_be_reserved_again(tmp_path):
    obj,db,tokens = manager(tmp_path)
    prepared = await obj.prepare('link',1)
    first = obj.start(prepared['context_token'],True,1)
    asr_id = obj.get_job(first['job_id'],1)['asr_job_id']
    db.complete_transcription(job_id=asr_id,actual_seconds=6,original_duration_ms=6000,
        content_duration_ms=6000,segments=[{'text':'expired'}],content_hash='hash',now=NOW,
        cache_ttl=timedelta(days=30))
    with db.connect() as conn:
        conn.execute("UPDATE video_analysis_jobs SET state='succeeded' WHERE job_id=?",(first['job_id'],))
        conn.execute("UPDATE transcript_documents SET expires_at=?",((NOW-timedelta(seconds=1)).isoformat(),))
    status = obj.get_job(first['job_id'],1)
    assert status['speech']['state'] == 'missing'
    assert status['speech']['missing_reason']
    assert status['transcript_ref'] is None
    second = obj.start(prepared['context_token'],True,1)
    assert second['job_id'] != first['job_id']
    assert obj.get_job(second['job_id'],1)['asr_job_id'] != asr_id
