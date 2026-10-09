from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bilibili import BilibiliClient
from .config import Settings
from .db import Database
from .errors import MCPError
from .tokens import SignedTokenService


SHANGHAI = ZoneInfo("Asia/Shanghai")


class ZephyrService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        bilibili: BilibiliClient,
        tokens: SignedTokenService,
    ) -> None:
        self.settings = settings
        self.db = db
        self.bilibili = bilibili
        self.tokens = tokens
        self.started_at = datetime.now(SHANGHAI)

    def status(self) -> dict[str, Any]:
        now = datetime.now(SHANGHAI)
        quota = self.db.get_status(
            now=now, monthly_limit_seconds=self.settings.monthly_max_seconds
        )
        for key in ("quota_observed_at", "autostop_observed_at"):
            value = quota.get(key)
            if value and now - datetime.fromisoformat(value) > timedelta(
                seconds=self.settings.quota_stale_seconds
            ):
                quota["provider_data_stale"] = True
                quota["effective_available_seconds"] = 0
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        quota["reset_at"] = (
            month_start.replace(year=month_start.year + 1, month=1)
            if month_start.month == 12
            else month_start.replace(month=month_start.month + 1)
        ).isoformat()
        return {
            "service": {
                "name": 'bilibili-summary-mcp',
                "version": "0.1.0",
                "status": "running",
                "started_at": self.started_at.isoformat(),
            },
            "bilibili_credentials": "available"
            if self.settings.bilibili_cookie or self.settings.bilibili_credentials_path
            else "unavailable",
            "asr": {
                "provider": "aliyun",
                "engine": "paraformer-v2",
                "region": "cn-beijing",
                "allow_paid": False,
                "free_tier_only": True,
                "operator_guard_confirmed": self.settings.free_tier_confirmed,
                "quota": quota,
            },

        }

    async def prepare_video(
        self, bvid: str, page: int, github_user_id: int
    ) -> dict[str, Any]:
        now = datetime.now(SHANGHAI)
        part, metadata = await self.bilibili.get_part(bvid, page)
        player_info = await self.bilibili.player_info(bvid, part.cid)
        tracks = await self.bilibili.subtitle_tracks(bvid, part.cid)
        ai_summary = await self.bilibili.ai_summary(
            bvid, part.cid, (metadata.get("owner") or {}).get("mid")
        )
        cache = self.db.get_cached_document(bvid=bvid, cid=part.cid, now=now)
        sources: list[dict[str, Any]] = []
        for track in tracks:
            segments = await self.bilibili.subtitle_segments(track["url"])
            ref = self.tokens.issue_transcript_ref(
                {"kind": "bilibili", "url": track["url"], "source": track["source"]}
            )
            sources.append(
                {
                    "source": track["source"],
                    "trust": "higher_external" if track["source"] == "human_subtitle" else "medium",
                    "untrusted_external_content": True,
                    "total_segments": len(segments),
                    "preview": segments[:10],
                    "transcript_ref": ref,
                }
            )
        if cache:
            page_data = self.db.get_transcript_page(
                document_id=cache["document_id"], offset=0, limit=10
            )
            ref = self.tokens.issue_transcript_ref(
                {"kind": "database", "document_id": cache["document_id"]}
            )
            sources.append(
                {
                    "source": "aliyun_paraformer",
                    "trust": "medium",
                    "untrusted_external_content": True,
                    "total_segments": page_data["total_segments"],
                    "preview": page_data["segments"],
                    "transcript_ref": ref,
                }
            )
        result: dict[str, Any] = {
            "video": {
                "bvid": part.bvid,
                "cid": part.cid,
                "page": part.page,
                "title": part.title,
                "part_title": part.part_title,
                "duration_seconds": part.duration_seconds,
                "description": metadata.get("desc"),
                "chapters": player_info.get("view_points") or [],
            },
            "transcripts": sources,
            "bilibili_ai_summary": {
                "source": "bilibili_ai_summary",
                "trust": "medium_low",
                "untrusted_external_content": True,
                "content": ai_summary,
            }
            if ai_summary
            else None,
            "comments_included": False,
            "untrusted_external_content": True,
        }
        if not sources:
            result["estimated_free_quota_seconds"] = part.duration_seconds
            result["context_token"] = self.tokens.issue_context(
                github_user_id=github_user_id,
                bvid=part.bvid,
                cid=part.cid,
                page=part.page,
                duration_seconds=part.duration_seconds,
                now=now,
                ttl=timedelta(minutes=30),
            )
        return result

    def start_transcription(
        self,
        context_token: str,
        confirm_free_quota_usage: bool,
        github_user_id: int,
    ) -> dict[str, Any]:
        if not confirm_free_quota_usage:
            raise MCPError("ASR_CONFIRMATION_REQUIRED", "必须明确确认消耗本月免费额度。")
        if self.settings.asr_allow_paid:
            raise MCPError("ASR_PAID_USAGE_FORBIDDEN", "首版禁止付费 ASR。")
        if not self.settings.free_tier_confirmed:
            raise MCPError(
                "ASR_FREE_TIER_GUARD_NOT_CONFIRMED", "部署人员尚未确认免费额度用完即停。"
            )
        now = datetime.now(SHANGHAI)
        context = self.tokens.verify_context(
            context_token, github_user_id=github_user_id, now=now
        )
        if context.get("platform") != "bilibili":
            raise MCPError("INVALID_CONTEXT_TOKEN", "B站转写工具不能使用抖音上下文。")
        duration = int(context["duration_seconds"])
        if duration > self.settings.max_video_seconds:
            raise MCPError("ASR_VIDEO_TOO_LONG", "首版单视频最多转写 3 小时。")
        job = self.db.reserve_transcription(
            bvid=context["bvid"],
            cid=int(context["cid"]),
            page=int(context["page"]),
            duration_seconds=duration,
            now=now,
            monthly_limit_seconds=self.settings.monthly_max_seconds,
            provider_stale_after=timedelta(seconds=self.settings.quota_stale_seconds),
            transaction_guard=lambda: self.tokens.verify_context(
                context_token, github_user_id=github_user_id, now=datetime.now(SHANGHAI)
            ),
        )
        if job.cached_document_id:
            return {
                "state": "cached",
                "reserved_seconds": 0,
                "created": False,
                "transcript_ref": self.tokens.issue_transcript_ref(
                    {"kind": "database", "document_id": job.cached_document_id}
                ),
            }
        return {
            "job_id": job.job_id,
            "state": "reserved" if job.created else "existing",
            "reserved_seconds": job.reserved_seconds,
            "created": job.created,
        }

    def get_job(self, job_id: str, github_user_id: int | None = None) -> dict[str, Any]:
        stored = self.db.get_job(job_id)
        if stored.get("platform") == "douyin":
            with self.db.connect() as connection:
                owner = connection.execute("SELECT 1 FROM video_analysis_jobs WHERE asr_job_id=? AND github_user_id=?",
                    (job_id,github_user_id)).fetchone()
            if not owner:
                raise MCPError("ASR_JOB_NOT_FOUND", "任务不存在或不属于当前用户。")
        job = {
            key: stored.get(key)
            for key in (
                "job_id",
                "bvid",
                "cid",
                "page",
                "state",
                "reserved_seconds",
                "actual_seconds",
                "error_code",
                "error_summary",
                "created_at",
                "updated_at",
                "total_segments",
            )
        }
        if stored.get("document_id"):
            reference = {"kind": "database", "document_id": stored["document_id"]}
            if stored.get("platform") == "douyin":
                reference["github_user_id"] = github_user_id
            job["transcript_ref"] = self.tokens.issue_transcript_ref(
                reference
            )
            page = self.db.get_transcript_page(
                document_id=self.tokens.verify_transcript_ref(job["transcript_ref"])["document_id"],
                offset=0,
                limit=10,
            )
            job["preview"] = page["segments"]
        return job

    async def transcript_page(
        self, transcript_ref: str, cursor: str | None, limit: int, github_user_id: int | None = None
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        offset = self.tokens.verify_cursor(cursor, transcript_ref) if cursor else 0
        ref = self.tokens.verify_transcript_ref(transcript_ref)
        if "github_user_id" in ref and ref["github_user_id"] != github_user_id:
            raise MCPError("INVALID_TRANSCRIPT_REF", "转写引用不属于当前用户。")
        if ref.get("kind") == "database":
            result = self.db.get_transcript_page(
                document_id=ref["document_id"], offset=offset, limit=limit
            )
        elif ref.get("kind") == "bilibili":
            segments = await self.bilibili.subtitle_segments(ref["url"])
            selected = segments[offset : offset + limit]
            next_offset = offset + len(selected)
            result = {
                "source": ref["source"],
                "total_segments": len(segments),
                "segments": selected,
                "next_offset": next_offset if next_offset < len(segments) else None,
                "truncated": next_offset < len(segments),
            }
        else:
            raise MCPError("INVALID_TRANSCRIPT_REF", "未知字幕来源。")
        character_limit = 24_000
        kept: list[dict[str, Any]] = []
        characters = 0
        for segment in result["segments"]:
            length = len(str(segment.get("text", "")))
            if kept and characters + length > character_limit:
                break
            kept.append(segment)
            characters += length
        if len(kept) < len(result["segments"]):
            result["segments"] = kept
            result["next_offset"] = offset + len(kept)
            result["truncated"] = True
        result["next_cursor"] = (
            self.tokens.issue_cursor(transcript_ref, offset=result["next_offset"])
            if result.get("next_offset") is not None
            else None
        )
        result.pop("next_offset", None)
        result["untrusted_external_content"] = True
        return result
