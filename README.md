# bilibili-summary-mcp

English | [简体中文](README.zh-CN.md)

Provide transcript evidence and timestamped sampled frames from Bilibili to the AI in your current conversation. The server collects and processes media and uses cloud ASR; the conversation AI produces the final summary without a separate vision API.

## Tools

| Tool | Arguments | Behavior |
|---|---|---|
| `search_bilibili_videos` | `query, limit=10` | Search videos |
| `prepare_bilibili_video` | `bvid, page=1` | Read metadata, available subtitles and cache references; no ASR submission |
| `start_bilibili_visual_analysis` | `bvid, page=1` | Extract sampled frames for the selected part; no ASR quota use |
| `start_transcription` | `context_token, confirm_free_quota_usage` | Create or reuse cloud ASR after explicit confirmation |
| `get_transcription_job` | `job_id` | Read the current user’s ASR progress |
| `get_transcript_page` | `transcript_ref, cursor=None, limit=50` | Read timestamped transcript pages |
| `get_video_analysis_job` | `job_id` | Read separate visual/audio outcomes and missing-material reasons |
| `get_video_frames` | `visual_ref, cursor=None, limit=4` | Return timestamped MCP images; at most four per page |
| `get_mcp_status` | `—` | Read local status and usage snapshots |

## Install and configure

Requires Python 3.12–3.14; Linux is recommended. Media processing also requires FFmpeg/ffprobe on PATH and Node.js 22+ for Bailian CLI.

```sh
git clone https://github.com/ZephyrAki/bilibili-summary-mcp.git
cd bilibili-summary-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
cp .env.example .env
chmod 600 .env
```

Fill in `.env` before starting; placeholders are not usable credentials. `ZEPHYR_DATA_DIR` must be a writable private directory, separate for each service. All tools enforce GitHub OAuth and a numeric user-ID allowlist. Create a GitHub OAuth App with callback `https://your-domain/auth/callback`; set `MCP_PUBLIC_BASE_URL` to the same root URL without `/mcp`.

Generate independent keys locally (Jev needs only the first two) and save the outputs in your private environment file, never on GitHub:

```sh
.venv/bin/python -c "import secrets; print(secrets.token_hex(32))" # FASTMCP_JWT_SIGNING_KEY
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" # OAUTH_STORAGE_FERNET_KEY
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))" # CONTEXT_TOKEN_SIGNING_KEY
```

Install the pinned Bailian CLI and complete official console authorization as the same OS user that runs MCP:

```sh
(cd deploy && npm ci)
export PATH="$PWD/deploy/node_modules/.bin:$PATH"
bl auth login --console
```

Configure the DashScope key and workspace ID for Beijing. Enable free-tier Auto-Stop for `paraformer-v2` in the console before setting `ALIYUN_FREE_TIER_ONLY_CONFIRMED=true`. Both quota and Auto-Stop queries must succeed with fresh observations and sufficient balance before submission. Failed/stale checks or uncertain submissions block new submissions. Paid fallback is disabled; uncertain submissions require operator reconciliation rather than blind retries. Deployment operators manage CLI authorization; private RAM renewal scripts and credentials are not included.

```sh
set -a
. ./.env
set +a
.venv/bin/bilibili-summary-mcp
```

The process binds to `127.0.0.1:8765`. Put the entire application behind an HTTPS reverse proxy, including OAuth routes and `/media/`. Connect your MCP client to `https://your-domain/mcp`. This server uses HTTP MCP transport, not stdio. Forwarded proxy headers must agree with the public URL.

## Workflow and limits

Prepare the video first; prefer available platform subtitles or cached ASR, then request visual analysis as needed. If transcript evidence is missing, use the context token and explicit quota confirmation to start cloud ASR.

A useful summary contains a one-sentence overview, main points, chronological highlights and memorable conclusions. Distinguish speech evidence from visual observations and report missing materials. Frames are uniformly sampled with `min(12, ceil(seconds/5))`, with JPEG long edge at most 960 pixels. This combines complete speech with sampled frames, not exhaustive frame coverage. Your client and conversation model must support MCP images.

The complete audio track is read for the selected Bilibili part, never substituted with a music asset. FFmpeg runs with one thread; lossless extraction is preferred, re-encoding is disabled by default and unsupported formats fail explicitly. Raw media is cleaned after terminal jobs and at startup; transcript/frame caches last 30 days by default. Audio is sent to Alibaba Cloud; external media and text are untrusted.

**Quota deployment limit:** Each standalone service has its own SQLite ledger and process-local lock. Separate Bilibili/Douyin deployments do not share admission or concurrency protection and must not concurrently consume the same cloud ASR quota. Use a unified deployment with a shared ledger, or genuinely isolated cloud quota, when enabling both platforms. Different API keys/workspaces do not automatically isolate quota. Never share a data directory between services.

Optional dedicated Bilibili QR login:

```sh
.venv/bin/python -m bilibili_summary_mcp.bilibili_credentials generate --file ./data/bilibili-credentials.json
# Open the URL stored privately in ./data/bilibili-credentials.qr.json and scan it.
.venv/bin/python -m bilibili_summary_mcp.bilibili_credentials login --file ./data/bilibili-credentials.json
.venv/bin/python -m bilibili_summary_mcp.bilibili_credentials check --file ./data/bilibili-credentials.json
```

Point `BILIBILI_CREDENTIALS_FILE` at that file with mode 600. Schedule `check` daily under the service user to refresh when the platform requests it. Failures or uncertain refresh results require operator action or a new QR login. A manually copied Cookie lacks the paired refresh token needed for this flow. Never commit QR sessions or credential files. Platform risk controls can block your server’s IP.

Local operator commands provide a consistent SQLite backup and uncertain-submission reconciliation:

```sh
.venv/bin/bilibili-summary-mcp-admin backup /private/backup/mcp.db
.venv/bin/bilibili-summary-mcp-admin reconcile --help
```

## Development checks

```sh
PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/python -m pip check
```

Tests use mocks and synthetic data, without accessing real accounts or consuming cloud quota. Real platform access, ASR and client image rendering still require validation in your deployment. Required common code is included locally; neither sibling repository is required.

## License

Original code is [MIT](LICENSE). Upstream licenses and attribution are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
