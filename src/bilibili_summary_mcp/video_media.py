"""Bounded local extraction only: no ASR, vision model, or audio encoding."""
from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
from pathlib import Path

from .errors import MCPError


def sample_times(duration: float) -> list[float]:
    if not math.isfinite(duration) or duration <= 0:
        raise MCPError('INVALID_VIDEO_DURATION','媒体时长无效。')
    count = min(12,math.ceil(duration/5))
    return [duration*(index+0.5)/count for index in range(count)]


async def run_media_command(command: list[str], *, timeout: float = 90) -> tuple[bytes,bytes]:
    process = await asyncio.create_subprocess_exec(*command,stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        stdout,stderr = await asyncio.wait_for(process.communicate(),timeout)
    except (TimeoutError,asyncio.CancelledError) as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(exc,asyncio.CancelledError):
            raise
        raise MCPError('MEDIA_PROCESS_TIMEOUT','媒体处理超时，子进程已终止。') from None
    if process.returncode:
        raise MCPError('MEDIA_PROCESS_FAILED','媒体解析或处理失败；首版不启用音频重新编码。')
    return stdout,stderr


class VideoMedia:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.directory.chmod(0o700)

    async def probe(self,path: Path) -> dict:
        stdout,_ = await run_media_command(['ffprobe','-v','error','-protocol_whitelist','file,pipe',
            '-show_streams','-show_format','-of','json',str(path)],timeout=30)
        data = json.loads(stdout)
        streams = data.get('streams') or []
        audio = next((s for s in streams if s.get('codec_type') == 'audio'),{})
        video = next((s for s in streams if s.get('codec_type') == 'video'),{})
        return {'duration_seconds':float((data.get('format') or {}).get('duration') or 0),
                'has_audio':bool(audio),'audio_codec':audio.get('codec_name'),
                'video_codec':video.get('codec_name'),
                'video_start_time_seconds':float(video['start_time']) if video.get('start_time') not in (None,'N/A') else None,
                'video_duration_seconds':float(video['duration']) if video.get('duration') not in (None,'N/A') else None,
                'width':int(video.get('width') or 0),'height':int(video.get('height') or 0)}

    @staticmethod
    def _command() -> list[str]:
        return ['ffmpeg','-nostdin','-hide_banner','-y','-threads','1',
                '-filter_threads','1','-filter_complex_threads','1','-protocol_whitelist','file,pipe']

    async def audio(self,source: Path,target: Path) -> Path:
        info = await self.probe(source)
        if not info['has_audio']:
            raise MCPError('VIDEO_NO_AUDIO','视频没有音轨；仍可提供抽样画面。')
        suffix = {'aac':'.m4a','alac':'.m4a','mp3':'.mp3','flac':'.flac','pcm_s16le':'.wav'}.get(info['audio_codec'])
        if not suffix:
            raise MCPError('ASR_FORMAT_UNSUPPORTED','该音轨无法直接分离为支持的格式；首版不重新编码。')
        target = target.with_suffix(suffix)
        try:
            await run_media_command(self._command()+['-v','error','-i',str(source),
                '-vn','-map','0:a:0','-c:a','copy',str(target)])
            target.chmod(0o600)
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    async def frames(self,source: Path,visual_id: str,duration: float,*,timestamp_origin_seconds: float=0.0) -> list[dict]:
        directory = (self.directory/visual_id).resolve()
        if directory.parent != self.directory:
            raise MCPError('MEDIA_PATH_INVALID','画面缓存路径无效。')
        directory.mkdir(mode=0o700,exist_ok=True)
        frames = []
        try:
            for index,timestamp in enumerate(sample_times(duration)):
                filename = f'{index:02}.jpg'
                target = directory/filename
                # Input seeking is relative to the stream start; copyts retains the actual PTS.
                _,stderr = await run_media_command(self._command()+['-v','info','-ss',str(timestamp),
                    '-copyts','-i',str(source),'-map','0:v:0','-frames:v','1','-an',
                    '-vf',"scale=w='min(960,iw)':h='min(960,ih)':force_original_aspect_ratio=decrease,showinfo",
                    '-threads','1','-q:v','3',str(target)])
                timestamps = re.findall(rb'pts_time:([-+0-9.eE]+)',stderr)
                if not timestamps or not target.is_file() or not target.stat().st_size:
                    raise MCPError('VIDEO_FRAME_MISSING','未能提取带实际时间戳的画面。')
                target.chmod(0o600)
                actual_time = float(timestamps[0])-timestamp_origin_seconds
                if not math.isfinite(actual_time) or not 0 <= actual_time < duration:
                    raise MCPError('VIDEO_TIMELINE_INVALID','画面时间戳不在所选视频内。')
                frames.append({'sequence':index,'timestamp_ms':round(actual_time*1000),'filename':filename})
            return frames
        except BaseException:
            shutil.rmtree(directory,ignore_errors=True)
            raise
