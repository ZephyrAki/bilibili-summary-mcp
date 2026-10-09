import json
from types import SimpleNamespace

import pytest

from bilibili_summary_mcp import server as server_module
from bilibili_summary_mcp.server import create_server
from bilibili_summary_mcp.service import ZephyrService
from bilibili_summary_mcp.errors import MCPError
from test_tool_annotations import _settings
from test_bilibili_visual import visual_manager, store_frames, BilibiliStub
from test_video_analysis import NOW


class SubtitleBilibili(BilibiliStub):
    async def player_info(self,*args):
        return {'view_points':[]}
    async def subtitle_tracks(self,*args):
        return [{'url':'https://subtitle.hdslb.com/track.json','language':'zh-CN','title':'中文','source':'human_subtitle'}]
    async def subtitle_segments(self,*args):
        return [{'begin_ms':0,'end_ms':6000,'text':'完整讲话'}]
    async def ai_summary(self,*args):
        return None


@pytest.mark.asyncio
async def test_bilibili_visual_tool_schema_and_auth(tmp_path,monkeypatch):
    mcp=create_server(_settings(tmp_path))
    tool=await mcp.get_tool('start_bilibili_visual_analysis')
    assert tool is not None
    assert set(tool.parameters['properties'])=={'bvid','page'}
    assert tool.parameters['properties']['page']['default']==1
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.open_world_hint is True
    for token in [None,SimpleNamespace(claims={'sub':'2'})]:
        monkeypatch.setattr(server_module,'get_access_token',lambda:token)
        with pytest.raises(PermissionError):
            await tool.fn(bvid='BV1test')


@pytest.mark.asyncio
async def test_prepare_with_subtitle_adds_visual_info_without_writes(tmp_path,monkeypatch):
    obj,db,tokens=visual_manager(tmp_path)
    obj.bilibili=SubtitleBilibili()
    monkeypatch.setattr(server_module,'BilibiliClient',lambda *args,**kwargs:obj.bilibili)
    monkeypatch.setattr(server_module,'VideoAnalysis',lambda *args,**kwargs:obj)
    monkeypatch.setattr(server_module,'get_access_token',lambda:SimpleNamespace(claims={'sub':'1'}))
    mcp=create_server(obj.settings)
    store_frames(obj,db)
    service=ZephyrService(obj.settings,db,obj.bilibili,tokens)
    original=await service.prepare_video('BV1test',1,1)
    with db.connect() as conn:
        before=list(conn.iterdump())
    prepared=await (await mcp.get_tool('prepare_bilibili_video')).fn(bvid='BV1test')
    assert {k:v for k,v in prepared.items() if k in original}==original
    assert 'context_token' not in prepared
    assert prepared['visual_status']=={'state':'cached','frame_count':1}
    assert prepared['visual_ref'] and prepared['summary_guidance']
    with db.connect() as conn:
        assert list(conn.iterdump())==before
    tool=await mcp.get_tool('start_bilibili_visual_analysis')
    job=await tool.fn(bvid='BV1test')
    assert job['state']=='succeeded'
    assert obj.get_job(job['job_id'],1)['requested_components']==['visual']


@pytest.mark.asyncio
async def test_bilibili_frames_use_standard_mcp_images_and_user_bound_cursors(tmp_path,monkeypatch):
    obj,db,tokens=visual_manager(tmp_path)
    store_frames(obj,db)
    directory=obj.processor.directory/'cache'
    frames=[]
    for index in range(6):
        filename=f'{index}.jpg'
        (directory/filename).write_bytes(b'jpeg')
        frames.append({'sequence':index,'filename':filename,'timestamp_ms':500+index*1000})
    with db.connect() as conn:
        conn.execute('UPDATE video_visual_cache SET frames_json=?',(json.dumps(frames),))
    ref=obj.visual_materials('bilibili','BV1test','41',1)['visual_ref']
    monkeypatch.setattr(server_module,'VideoAnalysis',lambda *args,**kwargs:obj)
    monkeypatch.setattr(server_module,'get_access_token',lambda:SimpleNamespace(claims={'sub':'1'}))
    mcp=create_server(obj.settings)
    tool=await mcp.get_tool('get_video_frames')
    blocks=tool.fn(visual_ref=ref,limit=100)
    assert [b.type for b in blocks]==['text','image']*4+['text']
    assert all(b.mime_type=='image/jpeg' for b in blocks if b.type=='image')
    assert json.loads(blocks[0].text)['timestamp_ms']==500
    cursor=json.loads(blocks[-1].text)['next_cursor']
    assert cursor
    next_page=tool.fn(visual_ref=ref,cursor=cursor)
    assert len([b for b in next_page if b.type=='image'])==2
    assert json.loads(next_page[-1].text)['next_cursor'] is None
    assert str(obj.processor.directory) not in ''.join(b.text for b in blocks if b.type=='text')
    other_ref=obj.visual_materials('bilibili','BV1test','41',2)['visual_ref']
    expired_ref=tokens.issue_visual_ref({'visual_id':'cache','github_user_id':1,'exp':int(NOW.timestamp())-10})
    for visual_ref,bad_cursor in [(other_ref,None),(expired_ref,None),
        (ref,tokens.issue_cursor(other_ref,offset=4))]:
        with pytest.raises(MCPError):
            tool.fn(visual_ref=visual_ref,cursor=bad_cursor)
