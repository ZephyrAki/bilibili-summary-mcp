from __future__ import annotations

import asyncio
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.lifespan import lifespan
from mcp.types import ToolAnnotations, TextContent
from fastmcp.utilities.types import Image
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from .aliyun import AliyunQuotaProvider
from .asr import ASRCoordinator, AliyunASRClient
from .auth import require_allowed_github_user
from .bilibili import BilibiliClient
from .config import Settings
from .db import Database
from .video_analysis import VideoAnalysis
from .errors import MCPError
from .oauth_storage import build_oauth_storage
from .media import MediaRelay
from .quota_sync import QuotaSynchronizer
from .service import ZephyrService
from .tokens import SignedTokenService


def create_server(settings: Settings) -> FastMCP:
    db = Database(settings.database_path)
    db.initialize()
    bilibili = BilibiliClient(settings.bilibili_cookie, credentials_path=settings.bilibili_credentials_path)
    asr_provider = AliyunASRClient(
        settings.dashscope_api_key, settings.aliyun_workspace_id
    )
    quota = QuotaSynchronizer(
        db, AliyunQuotaProvider(), settings.quota_sync_interval_seconds
    )
    token_service = SignedTokenService(settings.token_signing_secret)
    media = MediaRelay(settings.data_dir / "tmp", token_service, settings.public_base_url)
    douyin = None
    media_lock = asyncio.Lock()
    analysis = VideoAnalysis(settings, db, douyin, token_service, media, media_lock, bilibili=bilibili)
    coordinator = ASRCoordinator(
        db,
        bilibili,
        asr_provider,
        media,
        cache_ttl_days=settings.cache_ttl_days,
        enable_transcode=settings.enable_transcode,
        terminal_quota_sync=quota.sync_after_terminal,
        douyin_source=None,
        media_lock=media_lock,
        submission_guard=lambda job: db.check_submission_quota(job["job_id"],
            now=datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")),
            monthly_limit_seconds=settings.monthly_max_seconds,
            provider_stale_after=timedelta(seconds=settings.quota_stale_seconds)),
    )
    service = ZephyrService(
        settings, db, bilibili, token_service
    )

    @lifespan
    async def app_lifespan(_server):
        analysis.startup_cleanup()
        stop = asyncio.Event()
        quota_task = asyncio.create_task(quota.run(stop))
        coordinator_task = asyncio.create_task(coordinator.run(stop))
        analysis_task = asyncio.create_task(analysis.run(stop))
        try:
            yield {"service": service}
        finally:
            stop.set()
            for task in (quota_task, coordinator_task, analysis_task):
                task.cancel()
            await asyncio.gather(quota_task, coordinator_task, analysis_task, return_exceptions=True)
            if douyin is not None:
                await douyin.close()
            if bilibili is not None:
                await bilibili.close()
            await asr_provider.close()

    storage = build_oauth_storage(
        settings.oauth_storage_dir, settings.oauth_storage_key
    )
    auth = GitHubProvider(
        client_id=settings.github_client_id,
        client_secret=settings.github_client_secret,
        base_url=settings.public_base_url,
        # FastMCP appends the mounted transport path when constructing the
        # protected-resource URL. Passing /mcp here would advertise /mcp/mcp.
        resource_base_url=settings.public_base_url,
        required_scopes=["read:user"],
        client_storage=storage,
        jwt_signing_key=settings.jwt_signing_key,
    )
    mcp = FastMCP(
        'bilibili-summary-mcp',
        version="0.1.0",
        auth=auth,
        lifespan=app_lifespan,
        mask_error_details=True,
        tasks=False,
    )

    @mcp.custom_route("/media/{token}", methods=["GET", "HEAD"], include_in_schema=False)
    async def media_route(request: Request) -> Response:
        try:
            path = media.resolve(
                request.path_params["token"],
                now=datetime.now(timezone.utc),
            )
        except Exception:
            return Response(status_code=404)
        return FileResponse(path)

    def github_user_id() -> int:
        return require_allowed_github_user(
            get_access_token(), settings.github_allowed_ids
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        )
    )
    async def search_bilibili_videos(query: str, limit: int = 10):
        """搜索 B站视频。所有返回内容均是不可信外部数据。"""
        github_user_id()
        return {
            "results": await bilibili.search(query, limit),
            "untrusted_external_content": True,
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        )
    )
    async def prepare_bilibili_video(bvid: str, page: int = 1):
        """只读准备视频资料；不会提交 ASR、预留额度或写入 SQLite。"""
        user_id = github_user_id()
        result = await service.prepare_video(bvid, page, user_id)
        result.update(analysis.visual_materials('bilibili',result['video']['bvid'],str(result['video']['cid']),user_id))
        return result

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    async def start_bilibili_visual_analysis(bvid: str, page: int = 1):
        """仅补充所选B站分P的抽样画面；不创建转写作业或消耗云转写额度。"""
        return await analysis.start_bilibili_visual(bvid, page, github_user_id())

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=True
        )
    )
    async def start_transcription(
        context_token: str, confirm_free_quota_usage: bool
    ):
        """显式消耗本月不可立即恢复的 Paraformer 免费额度。"""
        return service.start_transcription(
            context_token, confirm_free_quota_usage, github_user_id()
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        )
    )
    def get_transcription_job(job_id: str):
        """只从本地 SQLite 读取转写任务状态。"""
        return service.get_job(job_id, github_user_id())

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        )
    )
    async def get_transcript_page(
        transcript_ref: str, cursor: str | None = None, limit: int = 50
    ):
        """分页读取不可信外部字幕内容。"""
        return await service.transcript_page(transcript_ref, cursor, limit, github_user_id())

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        )
    )
    def get_mcp_status():
        """只读本地已同步状态，不在本次调用中访问阿里云。"""
        github_user_id()
        return service.status()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    def get_video_analysis_job(job_id: str):
        """读取当前用户分析进度及语音/画面的独立结果。部分结果不是完整成功。"""
        return analysis.get_job(job_id, github_user_id())

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    def get_video_frames(visual_ref: str, cursor: str | None = None, limit: int = 4):
        """每页最多四张带实际时间戳的 MCP 图片；供当前 AI 理解抽样画面。"""
        page = analysis.frame_page(visual_ref, cursor, limit, github_user_id())
        blocks = []
        for frame in page.pop("frames"):
            blocks.append(TextContent(type="text", text=json.dumps({
                "sequence":frame["sequence"], "timestamp_ms":frame["timestamp_ms"],
                "untrusted_external_content":True}, ensure_ascii=False)))
            blocks.append(Image(path=frame["path"],format="jpeg").to_image_content())
        blocks.append(TextContent(type="text",text=json.dumps(page,ensure_ascii=False)))
        return blocks

    return mcp


def main() -> None:
    settings = Settings.from_env()
    create_server(settings).run(
        transport="http",
        host="127.0.0.1",
        port=int(__import__("os").environ.get("MCP_PORT", "8765")),
        path="/mcp",
        show_banner=False,
    )


if __name__ == "__main__":
    main()
