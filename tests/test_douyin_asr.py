from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import asyncio

import pytest

from bilibili_summary_mcp.asr import ASRCoordinator,SubmissionUncertain
from bilibili_summary_mcp.errors import MCPError
from test_video_analysis import manager
from test_quota_admission import fresh_provider_state

NOW=datetime.now(ZoneInfo('Asia/Shanghai'))


class Provider:
    def __init__(self):
        self.urls=[]
    async def submit(self,url):
        self.urls.append(url)
        return 'provider-task'
    async def task(self,task_id):
        return {'output':{'task_status':'RUNNING'}}


@pytest.mark.asyncio
async def test_douyin_uses_full_audio_relay_and_does_not_submit_next_platform_while_running(tmp_path):
    obj,db,tokens=manager(tmp_path)
    p=await obj.prepare('link',1)
    analysis=obj.start(p['context_token'],True,1)
    asr_id=obj.get_job(analysis['job_id'],1)['asr_job_id']
    provider=Provider()
    coordinator=ASRCoordinator(db,object(),provider,obj.relay,cache_ttl_days=30,
        douyin_source=obj.prepare_source,media_lock=obj.media_lock,
        submission_guard=lambda job:db.check_submission_quota(job['job_id'],now=NOW,
            monthly_limit_seconds=36000,provider_stale_after=timedelta(minutes=30)))
    await coordinator.run_once()
    assert db.get_job(asr_id)['state']=='submitted'
    assert '/media/' in provider.urls[0]
    db.reserve_transcription(bvid='BV1queue',cid=1,page=1,duration_seconds=20,now=NOW,
        monthly_limit_seconds=36000,provider_stale_after=timedelta(minutes=30))
    await coordinator.run_once()
    assert len(provider.urls)==1
    # A legitimate wait behind the provider must survive the old ten-minute TTL.
    with db.connect() as conn:
        conn.execute("UPDATE asr_jobs SET updated_at=? WHERE state='reserved'",
            ((NOW-timedelta(minutes=11)).isoformat(),))
    await coordinator.run_once()
    assert db.claim_jobs(('reserved',),1)
    assert len(provider.urls)==1


@pytest.mark.asyncio
async def test_uncertain_douyin_submit_cannot_be_duplicated(tmp_path):
    obj,db,tokens=manager(tmp_path)
    p=await obj.prepare('link',1)
    analysis=obj.start(p['context_token'],True,1)
    class UncertainProvider(Provider):
        async def submit(self,url):
            self.urls.append(url)
            raise SubmissionUncertain('unknown')
    provider=UncertainProvider()
    coordinator=ASRCoordinator(db,object(),provider,obj.relay,cache_ttl_days=30,
        douyin_source=obj.prepare_source,media_lock=obj.media_lock)
    await coordinator.run_once()
    repeated=obj.start(p['context_token'],True,1)
    assert repeated['job_id']==analysis['job_id']
    await coordinator.run_once()
    assert len(provider.urls)==1
    assert db.get_status(now=NOW,monthly_limit_seconds=36000)['local_reserved_seconds']==6


@pytest.mark.asyncio
async def test_quota_must_still_be_fresh_after_media_preparation(tmp_path):
    obj,db,tokens=manager(tmp_path)
    p=await obj.prepare('link',1)
    analysis=obj.start(p['context_token'],True,1)
    asr_id=obj.get_job(analysis['job_id'],1)['asr_job_id']
    async def source(job):
        url=await obj.prepare_source(job)
        db.merge_provider_snapshot({'quota_sync_status':'failed'})
        return url
    provider=Provider()
    coordinator=ASRCoordinator(db,object(),provider,obj.relay,cache_ttl_days=30,
        douyin_source=source,media_lock=obj.media_lock,
        submission_guard=lambda job:db.check_submission_quota(job['job_id'],now=NOW,
            monthly_limit_seconds=36000,provider_stale_after=timedelta(minutes=30)))
    await coordinator.run_once()
    assert provider.urls==[]
    assert db.get_job(asr_id)['error_code']=='ASR_PROVIDER_QUOTA_STALE'
    assert db.get_status(now=NOW,monthly_limit_seconds=36000)['local_reserved_seconds']==0


@pytest.mark.asyncio
async def test_douyin_provider_download_failure_never_uses_bili_fallback(tmp_path):
    obj,db,tokens=manager(tmp_path)
    p=await obj.prepare('link',1)
    analysis=obj.start(p['context_token'],True,1)
    asr_id=obj.get_job(analysis['job_id'],1)['asr_job_id']
    class FailedProvider(Provider):
        async def task(self,task_id):
            return {'output':{'task_status':'FAILED','results':[{'subtask_status':'FAILED','code':'FILE_403_FORBIDDEN'}]}}
    provider=FailedProvider()
    coordinator=ASRCoordinator(db,object(),provider,obj.relay,cache_ttl_days=30,
        douyin_source=obj.prepare_source,media_lock=obj.media_lock)
    await coordinator.run_once()
    await coordinator.run_once()
    assert db.get_job(asr_id)['state']=='failed'
    assert len(provider.urls)==1
    assert list(obj.relay.directory.iterdir())==[]
