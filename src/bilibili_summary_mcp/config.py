from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"缺少必需环境变量 {name}")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    oauth_storage_dir: Path
    public_base_url: str
    github_client_id: str
    github_client_secret: str
    github_allowed_ids: frozenset[int]
    jwt_signing_key: str
    oauth_storage_key: bytes
    token_signing_secret: bytes
    bilibili_cookie: str | None
    dashscope_api_key: str
    aliyun_workspace_id: str
    asr_allow_paid: bool
    free_tier_confirmed: bool
    monthly_max_seconds: int = 36_000
    quota_sync_interval_seconds: int = 300
    quota_stale_seconds: int = 1_800
    max_video_seconds: int = 10_800
    cache_ttl_days: int = 30
    enable_transcode: bool = False
    douyin_cookie: str | None = None
    bilibili_credentials_path: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("ZEPHYR_DATA_DIR", "/var/lib/bilibili-summary-mcp"))
        frozen_values = {
            "ASR_PROVIDER": "aliyun",
            "ASR_ENGINE": "paraformer-v2",
            "ASR_REGION": "cn-beijing",
            "FASTMCP_WORKERS": "1",
            "ASR_MAX_CONCURRENCY": "1",
        }
        for name, expected in frozen_values.items():
            if os.environ.get(name, expected) != expected:
                raise RuntimeError(f"首版要求 {name}={expected}")
        allow_paid = _boolean("ASR_ALLOW_PAID", False)
        if allow_paid:
            raise RuntimeError("首版禁止 ASR_ALLOW_PAID=true")
        allowed = frozenset(
            int(value.strip())
            for value in _required("GITHUB_ALLOWED_USER_IDS").split(",")
            if value.strip()
        )
        oauth_key = _required("OAUTH_STORAGE_FERNET_KEY").encode("ascii")
        token_secret = base64.urlsafe_b64decode(
            _required("CONTEXT_TOKEN_SIGNING_KEY") + "=="
        )
        monthly_max = int(os.environ.get("ASR_MONTHLY_MAX_SECONDS", "36000"))
        if monthly_max > 36_000 or monthly_max <= 0:
            raise RuntimeError("ASR_MONTHLY_MAX_SECONDS 必须在 1..36000 之间")
        return cls(
            data_dir=data_dir,
            database_path=data_dir / "mcp.db",
            oauth_storage_dir=data_dir / "oauth",
            public_base_url=os.environ.get(
                "MCP_PUBLIC_BASE_URL", "https://mcp.example.com"
            ).rstrip("/"),
            github_client_id=_required("GITHUB_CLIENT_ID"),
            github_client_secret=_required("GITHUB_CLIENT_SECRET"),
            github_allowed_ids=allowed,
            jwt_signing_key=_required("FASTMCP_JWT_SIGNING_KEY"),
            oauth_storage_key=oauth_key,
            token_signing_secret=token_secret,
            bilibili_cookie=os.environ.get("BILIBILI_COOKIE"),
            bilibili_credentials_path=Path(os.environ['BILIBILI_CREDENTIALS_FILE'])
                if os.environ.get('BILIBILI_CREDENTIALS_FILE') else None,
            douyin_cookie=os.environ.get("DOUYIN_COOKIE"),
            dashscope_api_key=_required("DASHSCOPE_API_KEY"),
            aliyun_workspace_id=_required("ALIYUN_WORKSPACE_ID"),
            asr_allow_paid=allow_paid,
            free_tier_confirmed=_boolean("ALIYUN_FREE_TIER_ONLY_CONFIRMED"),
            monthly_max_seconds=monthly_max,
            quota_sync_interval_seconds=int(
                os.environ.get("ASR_QUOTA_SYNC_INTERVAL_SECONDS", "300")
            ),
            quota_stale_seconds=int(os.environ.get("ASR_QUOTA_STALE_SECONDS", "1800")),
            max_video_seconds=int(os.environ.get("ASR_MAX_VIDEO_SECONDS", "10800")),
            cache_ttl_days=int(os.environ.get("ASR_CACHE_TTL_DAYS", "30")),
            enable_transcode=_boolean("ASR_ENABLE_TRANSCODE", False),
        )
