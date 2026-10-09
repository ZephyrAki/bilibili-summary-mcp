import asyncio
import json
from datetime import timedelta

import httpx
import pytest

from bilibili_summary_mcp import bilibili as bili_module
from bilibili_summary_mcp.bilibili import BilibiliClient
from bilibili_summary_mcp.errors import MCPError
from bilibili_summary_mcp.bilibili import VideoPart
from test_video_analysis import manager, NOW
from bilibili_summary_mcp import video_analysis as analysis_module
from test_video_analysis import ProcessorStub


class BilibiliStub:
    async def get_part(self,bvid,page=1):
        return VideoPart(bvid,40+page,page,'视频','分P',6), {}

    async def download_video(self,bvid,cid,target):
        self.downloads=getattr(self,'downloads',[])+[(bvid,cid)]
        target.write_bytes(b'video-only')
        return target


class VisualProcessor(ProcessorStub):
    async def probe(self,path):
        return {'duration_seconds':16,'video_duration_seconds':6,'video_start_time_seconds':10,
                'width':1280,'height':720,'has_audio':False,'video_codec':'h264'}

    async def audio(self,*args):
        pytest.fail('Visual-only worker must not extract audio')

    async def frames(self,source,visual_id,duration,*,timestamp_origin_seconds=0):
        assert duration==6 and timestamp_origin_seconds==10
        directory=self.directory/visual_id
        directory.mkdir()
        frames=[]
        for index,timestamp in enumerate([1500,4500]):
            name=f'{index}.jpg'
            (directory/name).write_bytes(b'jpeg')
            frames.append({'sequence':index,'timestamp_ms':timestamp,'filename':name})
        return frames


def visual_manager(tmp_path):
    obj,db,tokens=manager(tmp_path)
    obj.bilibili=BilibiliStub()
    processor=VisualProcessor()
    processor.directory=obj.processor.directory
    obj.processor=processor
    return obj,db,tokens


@pytest.mark.asyncio
async def test_visual_worker_extracts_only_selected_part(tmp_path):
    obj,db,tokens=visual_manager(tmp_path)
    first=await obj.start_bilibili_visual('BV1test',2,1)
    await obj.run_once()
    status=obj.get_job(first['job_id'],1)
    assert status['state']=='succeeded'
    assert status['speech']['state']=='not_requested'
    assert status['visual']['frame_count']==2
    assert obj.bilibili.downloads==[('BV1test',42)]
    assert [f['timestamp_ms'] for f in obj.frame_page(status['visual_ref'],None,4,1)['frames']]==[1500,4500]
    assert list(obj.relay.directory.iterdir())==[]
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM asr_jobs').fetchone()[0]==0


@pytest.mark.asyncio
async def test_media_lock_shared_across_platforms_and_cross_user_cache_reuse(tmp_path):
    obj,db,tokens=visual_manager(tmp_path)
    first=await obj.start_bilibili_visual('BV1test',1,1)
    second=await obj.start_bilibili_visual('BV1test',1,2)
    async with obj.media_lock:
        task=asyncio.create_task(obj.run_once())
        await asyncio.sleep(.02)
        assert not getattr(obj.bilibili,'downloads',[])
    await task
    assert obj.bilibili.downloads==[('BV1test',41)]
    assert obj.get_job(first['job_id'],1)['state']=='succeeded'
    assert obj.get_job(second['job_id'],2)['state']=='succeeded'


@pytest.mark.asyncio
async def test_one_users_failure_does_not_fail_another_users_active_job(tmp_path):
    obj,db,tokens=visual_manager(tmp_path)
    first=await obj.start_bilibili_visual('BV1test',1,1)
    second=await obj.start_bilibili_visual('BV1test',1,2)
    started,release=asyncio.Event(),asyncio.Event()
    original=obj.bilibili.download_video
    attempts=0
    async def download(*args):
        nonlocal attempts
        attempts+=1
        if attempts==1:
            raise MCPError('BILIBILI_VIDEO_UNAVAILABLE','Temporary source failure')
        started.set()
        await release.wait()
        return await original(*args)
    obj.bilibili.download_video=download
    task=asyncio.create_task(obj.run_once())
    try:
        await asyncio.wait_for(started.wait(),1)
        assert obj.get_job(first['job_id'],1)['state']=='failed'
        assert obj.get_job(second['job_id'],2)['state']=='preparing_media'
        duplicate=await obj.start_bilibili_visual('BV1test',1,2)
        assert duplicate['job_id']==second['job_id'] and not duplicate['created']
    finally:
        release.set()
        await task
    assert obj.get_job(second['job_id'],2)['state']=='succeeded'


@pytest.mark.asyncio
async def test_media_lock_serializes_actual_bilibili_and_douyin_downloads(tmp_path):
    obj,db,tokens=visual_manager(tmp_path)
    class BothProcessor(VisualProcessor):
        async def probe(self,path):
            if path.read_bytes()==b'full-video':
                return await ProcessorStub.probe(self,path)
            return await super().probe(path)
        async def frames(self,source,visual_id,duration,**kwargs):
            if not kwargs:
                return await ProcessorStub.frames(self,source,visual_id,duration)
            return await super().frames(source,visual_id,duration,**kwargs)
        async def audio(self,source,target):
            return await ProcessorStub.audio(self,source,target)
    processor=BothProcessor()
    processor.directory=obj.processor.directory
    obj.processor=processor
    active=peak=0
    dy_download,bili_download=obj.douyin.download,obj.bilibili.download_video
    async def counted(function,*args):
        nonlocal active,peak
        active+=1
        peak=max(peak,active)
        try:
            await asyncio.sleep(.02)
            return await function(*args)
        finally:
            active-=1
    async def dy(*args):
        return await counted(dy_download,*args)
    async def bili(*args):
        return await counted(bili_download,*args)
    obj.douyin.download=dy
    obj.bilibili.download_video=bili
    first=await obj.start_bilibili_visual('BV1test',1,1)
    context=await obj.prepare('link',1)
    dy_job=obj.start(context['context_token'],True,1)
    asr_id=obj.get_job(dy_job['job_id'],1)['asr_job_id']
    await asyncio.gather(obj.run_once(),obj.prepare_source(db.get_job(asr_id)))
    assert peak==1 and active==0
    assert obj.get_job(first['job_id'],1)['state']=='succeeded'
    assert obj.get_job(dy_job['job_id'],1)['visual']['state']=='succeeded'


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['download_cancel','frames_cancel','timeout','dimensions','timeline','codec'])
async def test_visual_restart_and_cancel_clean_partial_media(tmp_path,monkeypatch,case):
    obj,db,tokens=visual_manager(tmp_path)
    first=await obj.start_bilibili_visual('BV1test',1,1)
    started=asyncio.Event()
    original_probe=obj.processor.probe
    if case=='download_cancel':
        async def download(bvid,cid,target):
            target.write_bytes(b'partial')
            started.set()
            await asyncio.sleep(10)
        obj.bilibili.download_video=download
    elif case in ('frames_cancel','timeout'):
        async def frames(source,visual_id,duration,**kwargs):
            directory=obj.processor.directory/visual_id
            directory.mkdir()
            (directory/'partial.jpg').write_bytes(b'partial')
            started.set()
            await asyncio.sleep(10)
        obj.processor.frames=frames
        monkeypatch.setattr(analysis_module,'BILIBILI_PROCESS_TIMEOUT_SECONDS',.03,raising=False)
    else:
        async def probe(path):
            data=await original_probe(path)
            data.update({'dimensions':{'width':1920,'height':1080},
                'timeline':{'video_start_time_seconds':None},'codec':{'video_codec':'hevc'}}[case])
            return data
        obj.processor.probe=probe
    task=asyncio.create_task(obj.run_once())
    if case.endswith('cancel'):
        await asyncio.wait_for(started.wait(),.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        obj.startup_cleanup()
        assert obj.get_job(first['job_id'],1)['state']=='queued'
    else:
        await task
        status=obj.get_job(first['job_id'],1)
        assert status['state']=='failed' and status['visual']['missing_reason']
    assert list(obj.relay.directory.iterdir())==[]
    assert list(obj.processor.directory.iterdir())==[]


def store_frames(obj,db,platform='bilibili',video_id='BV1test',part='41',visual_id='cache'):
    directory=obj.processor.directory/visual_id
    directory.mkdir(exist_ok=True)
    (directory/'0.jpg').write_bytes(b'jpeg')
    with db.connect() as conn:
        conn.execute('INSERT INTO video_visual_cache VALUES (?,?,?,?,?,?,?)',
            (visual_id,platform,video_id,part,json.dumps([{'sequence':0,'filename':'0.jpg','timestamp_ms':1500}]),
             NOW.isoformat(),(NOW+timedelta(days=30)).isoformat()))


@pytest.mark.asyncio
@pytest.mark.parametrize('quota_state',['empty','stale','exhausted'])
async def test_visual_only_start_works_without_quota_or_context(tmp_path,quota_state):
    obj,db,tokens=visual_manager(tmp_path)
    with db.connect() as conn:
        if quota_state=='empty':
            conn.execute('DELETE FROM provider_quota_snapshot')
        elif quota_state=='stale':
            conn.execute("UPDATE provider_quota_snapshot SET quota_sync_status='failed'")
        else:
            conn.execute('UPDATE provider_quota_snapshot SET quota_remaining_seconds=0')
    first=await obj.start_bilibili_visual('BV1test',1,1)
    assert (await obj.start_bilibili_visual('BV1test',1,1))['job_id']==first['job_id']
    status=obj.get_job(first['job_id'],1)
    assert status['requested_components']==['visual']
    assert status['speech']['state']=='not_requested'
    assert status['asr_job_id'] is status['transcript_ref'] is None
    assert (status['platform'],status['video_id'],status['part'])==('bilibili','BV1test','41')
    with db.connect() as conn:
        for table in ['asr_jobs','asr_job_attempts','asr_usage_ledger','transcript_documents']:
            assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==0


@pytest.mark.asyncio
async def test_visual_cache_isolated_by_platform_and_cid(tmp_path):
    obj,db,tokens=visual_manager(tmp_path)
    store_frames(obj,db)
    assert obj.visual_materials('bilibili','BV1test','41',1)['visual_status']=={'state':'cached','frame_count':1}
    for platform,part in [('bilibili','42'),('douyin','41')]:
        info=obj.visual_materials(platform,'BV1test',part,1)
        assert info['visual_ref'] is None
        assert info['visual_status']=={'state':'not_requested','frame_count':0}
    first=await obj.start_bilibili_visual('BV1test',1,1)
    assert first['state']=='succeeded'
    assert obj.get_job(first['job_id'],1)['speech']['state']=='not_requested'
    assert (await obj.start_bilibili_visual('BV1test',1,1))['job_id']==first['job_id']
    second=await obj.start_bilibili_visual('BV1test',2,1)
    assert second['state']=='queued' and second['job_id']!=first['job_id']
    other=await obj.start_bilibili_visual('BV1test',1,2)
    assert other['job_id']!=first['job_id'] and other['state']=='succeeded'
    with pytest.raises(MCPError):
        obj.get_job(first['job_id'],2)
    with pytest.raises(MCPError):
        obj.frame_page(obj.get_job(first['job_id'],1)['visual_ref'],None,4,2)


@pytest.mark.asyncio
@pytest.mark.parametrize('expired',[False,True])
async def test_visual_start_requeues_expired_or_missing_cache(tmp_path,expired):
    obj,db,tokens=visual_manager(tmp_path)
    store_frames(obj,db)
    first=await obj.start_bilibili_visual('BV1test',1,1)
    if expired:
        with db.connect() as conn:
            conn.execute('UPDATE video_visual_cache SET expires_at=?',((NOW-timedelta(seconds=1)).isoformat(),))
    else:
        (obj.processor.directory/'cache'/'0.jpg').unlink()
    second=await obj.start_bilibili_visual('BV1test',1,1)
    assert second['job_id']!=first['job_id'] and second['state']=='queued'

MAIN = 'https://upos-sz-mirrorcosov.bilivideo.com/main.m4s'
BACKUP = 'https://upos-sz-mirrorbd.bilivideo.com/backup.m4s'


def track(width=1280, height=720, bandwidth=1000, codecs='avc1.64001F', **fields):
    return dict(width=width, height=height, bandwidth=bandwidth, codecs=codecs,
                codecid=7, baseUrl=MAIN, backupUrl=[BACKUP, MAIN], **fields)


async def client_with_tracks(monkeypatch, tracks, media_handler=None, resolver=None):
    async def public_ips(host):
        return ['218.61.116.95']
    client = BilibiliClient('api-cookie=private', resolver=resolver or public_ips)
    await client.client.aclose()
    def api(request):
        assert request.url.params['qn'] == '64'
        assert request.url.params['fnval'] == '16'
        assert request.headers['cookie'] == 'api-cookie=private'
        return httpx.Response(200, json={'code':0, 'data':{'dash':{
            'video':tracks, 'audio':[{'baseUrl':'https://audio.bilivideo.com/audio.m4s'}]}}})
    real_client = httpx.AsyncClient
    client.client = real_client(headers={'Cookie':'api-cookie=private'}, transport=httpx.MockTransport(api))
    if media_handler:
        monkeypatch.setattr(bili_module.httpx, 'AsyncClient',
            lambda **kwargs:real_client(transport=httpx.MockTransport(media_handler), **kwargs))
    return client


@pytest.mark.asyncio
async def test_video_candidates_select_bounded_avc_track(monkeypatch):
    client = await client_with_tracks(monkeypatch, [track(1920,1080),track(codecs='av01'),
        track(codecs='hev1'),track(bandwidth=2000),track(),track(854,480)])
    try:
        assert await client.video_candidates('BV1test',42) == [MAIN,BACKUP]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('tracks,available', [([track(720,1280)],True),
    ([],False),([track(1920,1080)],False),([track(codecs='')],False),
    ([track(bandwidth=0)],False),([{'baseUrl':MAIN}],False)])
async def test_video_track_requires_confirmed_codec_dimensions_and_bandwidth(monkeypatch,tracks,available):
    client = await client_with_tracks(monkeypatch,tracks)
    try:
        if available:
            assert await client.video_candidates('BV1test',42) == [MAIN,BACKUP]
        else:
            with pytest.raises(MCPError) as exc:
                await client.video_candidates('BV1test',42)
            assert exc.value.code == 'BILIBILI_VIDEO_UNAVAILABLE'
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_video_download_does_not_forward_api_cookie_and_retries_403(tmp_path,monkeypatch):
    requests = []
    def media(request):
        requests.append(str(request.url))
        assert 'cookie' not in request.headers and 'authorization' not in request.headers
        if str(request.url) == MAIN:
            return httpx.Response(403)
        return httpx.Response(200,content=b'full-video')
    client = await client_with_tracks(monkeypatch,[track()],media)
    try:
        target = tmp_path/'video.m4s'
        assert await client.download_video('BV1test',42,target) == target
        assert target.read_bytes() == b'full-video'
        assert target.stat().st_mode & 0o777 == 0o600
        assert requests == [MAIN,BACKUP]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case,code', [('private','BILIBILI_RESOLVED_PRIVATE_IP'),
    ('redirect','BILIBILI_REDIRECT_REJECTED'),('large','MEDIA_TOO_LARGE'),
    ('stream_large','MEDIA_TOO_LARGE'),('invalid_length','MEDIA_INVALID_LENGTH'),
    ('empty','BILIBILI_VIDEO_UNAVAILABLE')])
async def test_video_download_hard_failures_remove_partial_file(tmp_path,monkeypatch,case,code):
    calls=[]
    async def resolver(host):
        return ['10.0.0.1'] if case=='private' else ['218.61.116.95']
    def media(request):
        calls.append(str(request.url))
        if case=='redirect':
            return httpx.Response(302,headers={'location':'https://evil.example/private'})
        if case=='large':
            return httpx.Response(200,headers={'content-length':'11'},content=b'one')
        if case=='invalid_length':
            return httpx.Response(200,headers={'content-length':'invalid'},content=b'one')
        if case=='stream_large':
            return httpx.Response(200,headers={'content-length':'1'},content=b'over-ten-bytes')
        return httpx.Response(200,content=b'')
    client = await client_with_tracks(monkeypatch,[track()],media,resolver)
    monkeypatch.setattr(bili_module,'MAX_VIDEO_BYTES',10,raising=False)
    try:
        target=tmp_path/'partial.m4s'
        with pytest.raises(MCPError) as exc:
            await client.download_video('BV1test',42,target)
        assert exc.value.code==code
        assert not target.exists()
        assert len(calls)<=1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel',[False,True])
async def test_video_download_deadline_and_cancel_cleanup(tmp_path,monkeypatch,cancel):
    started=asyncio.Event()
    async def media(request):
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(200,content=b'never')
    client=await client_with_tracks(monkeypatch,[track()],media)
    monkeypatch.setattr(bili_module,'VIDEO_DOWNLOAD_TIMEOUT_SECONDS',0.02,raising=False)
    target=tmp_path/'partial.m4s'
    target.write_bytes(b'partial')
    task=asyncio.create_task(client.download_video('BV1test',42,target))
    try:
        if cancel:
            await asyncio.wait_for(started.wait(),.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(MCPError) as exc:
                await task
            assert exc.value.code=='VIDEO_DOWNLOAD_TIMEOUT'
        assert not target.exists()
    finally:
        await client.close()
