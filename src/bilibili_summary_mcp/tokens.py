from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import MCPError


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SignedTokenService:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("token signing secret must be at least 32 bytes")
        self._secret = secret

    def _issue(self, purpose: str, payload: dict[str, Any]) -> str:
        body = json.dumps(
            {"purpose": purpose, **payload},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded = _b64encode(body)
        signature = _b64encode(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return f"v1.{encoded}.{signature}"

    def _verify(self, token: str, purpose: str) -> dict[str, Any]:
        try:
            version, encoded, signature = token.split(".", 2)
            expected = hmac.new(
                self._secret, encoded.encode("ascii"), hashlib.sha256
            ).digest()
            if version != "v1" or not hmac.compare_digest(
                expected, _b64decode(signature)
            ):
                raise ValueError
            payload = json.loads(_b64decode(encoded))
            if payload.pop("purpose") != purpose:
                raise ValueError
            return payload
        except (
            ValueError,
            KeyError,
            json.JSONDecodeError,
            binascii.Error,
            UnicodeDecodeError,
        ) as exc:
            raise MCPError("INVALID_SIGNED_TOKEN", "签名令牌无效。") from exc

    def issue_context(
        self,
        *,
        github_user_id: int,
        bvid: str,
        cid: int,
        page: int,
        duration_seconds: int,
        now: datetime,
        ttl: timedelta,
    ) -> str:
        now_utc = now.astimezone(timezone.utc)
        return self._issue(
            "context",
            {
                "github_user_id": github_user_id,
                "bvid": bvid,
                "cid": cid,
                "page": page,
                "duration_seconds": duration_seconds,
                "iat": int(now_utc.timestamp()),
                "exp": int((now_utc + ttl).timestamp()),
            },
        )

    def verify_context(
        self, token: str, *, github_user_id: int, now: datetime
    ) -> dict[str, Any]:
        try:
            payload = self._verify(token, "context")
        except MCPError as exc:
            raise MCPError("INVALID_CONTEXT_TOKEN", "转写上下文令牌无效。") from exc
        if payload.get("github_user_id") != github_user_id:
            raise MCPError("INVALID_CONTEXT_TOKEN", "转写上下文令牌不属于当前用户。")
        if int(payload.get("exp", 0)) < int(now.astimezone(timezone.utc).timestamp()):
            raise MCPError("CONTEXT_TOKEN_EXPIRED", "转写上下文令牌已过期。")
        if "platform" not in payload:
            payload.update(platform="bilibili", video_id=payload.get("bvid"), part=str(payload.get("cid")))
        return payload

    def issue_video_context(self, *, github_user_id: int, platform: str,
                            video_id: str, part: str, duration_seconds: int,
                            now: datetime, ttl: timedelta) -> str:
        return self._issue("context", {
            "github_user_id": github_user_id, "platform": platform,
            "video_id": video_id, "part": part, "duration_seconds": duration_seconds,
            "iat": int(now.timestamp()), "exp": int((now + ttl).timestamp()),
        })

    def issue_visual_ref(self, payload: dict[str, Any]) -> str:
        return self._issue("visual", payload)

    def verify_visual_ref(self, ref: str) -> dict[str, Any]:
        return self._verify(ref, "visual")

    def issue_cursor(self, transcript_ref: str, *, offset: int) -> str:
        return self._issue(
            "cursor", {"transcript_ref": transcript_ref, "offset": offset}
        )

    def verify_cursor(self, cursor: str, transcript_ref: str) -> int:
        try:
            payload = self._verify(cursor, "cursor")
            if payload["transcript_ref"] != transcript_ref:
                raise ValueError
            offset = int(payload["offset"])
            if offset < 0:
                raise ValueError
            return offset
        except (MCPError, KeyError, TypeError, ValueError) as exc:
            raise MCPError("INVALID_TRANSCRIPT_CURSOR", "字幕分页游标无效。") from exc

    def issue_transcript_ref(self, payload: dict[str, Any]) -> str:
        return self._issue("transcript", payload)

    def verify_transcript_ref(self, transcript_ref: str) -> dict[str, Any]:
        try:
            return self._verify(transcript_ref, "transcript")
        except MCPError as exc:
            raise MCPError("INVALID_TRANSCRIPT_REF", "字幕引用无效。") from exc

    def issue_media_ref(
        self,
        *,
        job_id: str,
        filename: str,
        content_hash: str,
        now: datetime,
        ttl: timedelta,
    ) -> str:
        now_utc = now.astimezone(timezone.utc)
        return self._issue(
            "media",
            {
                "job_id": job_id,
                "filename": filename,
                "content_hash": content_hash,
                "iat": int(now_utc.timestamp()),
                "exp": int((now_utc + ttl).timestamp()),
            },
        )

    def verify_media_ref(self, token: str, *, now: datetime) -> dict[str, Any]:
        try:
            payload = self._verify(token, "media")
        except MCPError as exc:
            raise MCPError("MEDIA_TOKEN_INVALID", "临时媒体令牌无效。") from exc
        if int(payload.get("exp", 0)) < int(now.astimezone(timezone.utc).timestamp()):
            raise MCPError("MEDIA_TOKEN_EXPIRED", "临时媒体令牌已过期。")
        return payload
