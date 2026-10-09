import asyncio
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('worker',['quota','media'])
async def test_shutdown_cancellation_reaps_worker_subprocess(tmp_path,monkeypatch,worker):
    from bilibili_summary_mcp.aliyun import _subprocess_runner
    from bilibili_summary_mcp.media import MediaRelay
    from bilibili_summary_mcp.tokens import SignedTokenService
    started=asyncio.Event()
    class Process:
        returncode=None
        killed=False
        wait=AsyncMock()
        def kill(self):
            self.killed=True
        async def communicate(self):
            started.set()
            await asyncio.Event().wait()
    process=Process()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',AsyncMock(return_value=process))
    if worker=='quota':
        call=_subprocess_runner(['bl','usage','free'],30)
    else:
        relay=MediaRelay(tmp_path,SignedTokenService(b'x'*32),'https://mcp.example.com')
        (tmp_path/'job.m4s').write_bytes(b'audio')
        (tmp_path/'job.remux.m4a').write_bytes(b'partial')
        call=relay.ffmpeg_prepare('job',transcode=False)
    task=asyncio.create_task(call)
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed
    process.wait.assert_awaited_once()
    if worker=='media':
        assert not (tmp_path/'job.remux.m4a').exists()
