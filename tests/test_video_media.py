import asyncio
import shutil
import subprocess

import pytest

from bilibili_summary_mcp.errors import MCPError


@pytest.mark.parametrize('duration,count',[(1,1),(5,1),(6,2),(30,6),(61,12),(360,12)])
def test_frame_sampling_is_bounded_and_inside_video(duration,count):
    from bilibili_summary_mcp.video_media import sample_times
    times = sample_times(duration)
    assert len(times) == count
    assert times == sorted(times)
    assert all(0 < t < duration for t in times)


@pytest.mark.asyncio
async def test_subprocess_timeout_reaps_child():
    from bilibili_summary_mcp.video_media import run_media_command
    with pytest.raises(MCPError) as caught:
        await run_media_command(['sleep','10'],timeout=0.01)
    assert caught.value.code == 'MEDIA_PROCESS_TIMEOUT'


@pytest.mark.asyncio
async def test_real_ffmpeg_frames_and_audio_copy(tmp_path):
    from bilibili_summary_mcp.video_media import VideoMedia
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg unavailable')
    source = tmp_path/'video.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=1200x700:rate=10:duration=6',
        '-f','lavfi','-i','sine=frequency=1000:duration=6','-c:v','libx264','-threads','1','-c:a','aac','-y',str(source)],check=True)
    media = VideoMedia(tmp_path/'frames')
    info = await media.probe(source)
    assert info['has_audio']
    audio = await media.audio(source,tmp_path/'audio.m4a')
    assert (await media.probe(audio))['audio_codec'] == 'aac'
    frames = await media.frames(source,'test-cache',info['duration_seconds'])
    assert len(frames) == 2
    for frame in frames:
        assert 0 < frame['timestamp_ms'] < 6000
        image = await media.probe(tmp_path/'frames'/'test-cache'/frame['filename'])
        assert max(image['width'],image['height']) <= 960


@pytest.mark.asyncio
async def test_no_audio_is_explicit_and_frames_still_work(tmp_path):
    from bilibili_summary_mcp.video_media import VideoMedia
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg unavailable')
    source = tmp_path/'silent.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=size=100x100:duration=1',
        '-c:v','libx264','-threads','1','-y',str(source)],check=True)
    media = VideoMedia(tmp_path/'frames')
    with pytest.raises(MCPError) as caught:
        await media.audio(source,tmp_path/'audio.m4a')
    assert caught.value.code == 'VIDEO_NO_AUDIO'
    assert len(await media.frames(source,'silent',1)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('size',['1280x720','720x1280'])
async def test_video_only_offset_timestamps_are_relative_to_part(tmp_path,size):
    from bilibili_summary_mcp.video_media import VideoMedia
    source=tmp_path/'offset.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'testsrc2=size={size}:rate=10:duration=6',
        '-vf','setpts=PTS+10/TB','-c:v','libx264','-threads','1','-y',str(source)],check=True)
    media=VideoMedia(tmp_path/'frames')
    info=await media.probe(source)
    assert info['video_start_time_seconds']==pytest.approx(10)
    assert info['video_duration_seconds']==pytest.approx(6)
    frames=await media.frames(source,'offset',6,timestamp_origin_seconds=10)
    assert [frame['timestamp_ms'] for frame in frames]==pytest.approx([1500,4500],abs=100)
    for frame in frames:
        image=await media.probe(media.directory/'offset'/frame['filename'])
        assert max(image['width'],image['height'])<=960
        assert image['width']/image['height']==pytest.approx(info['width']/info['height'],abs=.01)
