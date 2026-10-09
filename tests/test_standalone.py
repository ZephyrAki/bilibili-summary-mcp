import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from bilibili_summary_mcp.config import Settings
from bilibili_summary_mcp import server as server_module

PROJECT = 'bilibili-summary-mcp'
EXPECTED_TOOLS = ['search_bilibili_videos', 'prepare_bilibili_video', 'start_bilibili_visual_analysis', 'start_transcription', 'get_transcription_job', 'get_transcript_page', 'get_video_analysis_job', 'get_video_frames', 'get_mcp_status']

def _settings(tmp_path):
    values = dict(data_dir=tmp_path, database_path=tmp_path/'mcp.db', oauth_storage_dir=tmp_path/'oauth',
        public_base_url='https://mcp.example.com', github_client_id='client', github_client_secret='secret',
        github_allowed_ids=frozenset({1}), jwt_signing_key='j'*64, oauth_storage_key=Fernet.generate_key())
    if PROJECT == 'jev-mcp':
        values.update(typesafe_api_key='ts-test-key', typesafe_model='jev-latest', typesafe_timeout_seconds=30,
            typesafe_max_concurrency=4, typesafe_max_request_bytes=1048576, typesafe_usage_warning_tokens=10000000)
    else:
        values.update(token_signing_secret=b't'*32, bilibili_cookie=None, dashscope_api_key='dashscope',
            aliyun_workspace_id='workspace', asr_allow_paid=False, free_tier_confirmed=True)
    return Settings(**values)

def test_environment_has_no_unrelated_backend_requirement(tmp_path, monkeypatch):
    values = dict(ZEPHYR_DATA_DIR=str(tmp_path), MCP_PUBLIC_BASE_URL='https://mcp.example.com',
        GITHUB_CLIENT_ID='client', GITHUB_CLIENT_SECRET='secret', GITHUB_ALLOWED_USER_IDS='1',
        FASTMCP_JWT_SIGNING_KEY='j'*64, OAUTH_STORAGE_FERNET_KEY=Fernet.generate_key().decode())
    for name in ['TYPESAFE_API_KEY', 'DASHSCOPE_API_KEY', 'ALIYUN_WORKSPACE_ID', 'CONTEXT_TOKEN_SIGNING_KEY']:
        monkeypatch.delenv(name, raising=False)
    if PROJECT == 'jev-mcp':
        values['TYPESAFE_API_KEY'] = 'ts-test-key'
    else:
        values.update(DASHSCOPE_API_KEY='dashscope', ALIYUN_WORKSPACE_ID='workspace',
            CONTEXT_TOKEN_SIGNING_KEY=base64.urlsafe_b64encode(b't'*32).decode().rstrip('='))
    for name, value in values.items(): monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.database_path == tmp_path/'mcp.db'
    assert not hasattr(settings, 'dashscope_api_key' if PROJECT == 'jev-mcp' else 'typesafe_api_key')

@pytest.mark.asyncio
async def test_exact_tool_catalog_and_read_only_status(tmp_path, monkeypatch):
    mcp = server_module.create_server(_settings(tmp_path))
    catalog = await mcp.list_tools()
    assert {tool.name for tool in catalog} == set(EXPECTED_TOOLS)
    status = await mcp.get_tool('get_mcp_status')
    assert status.annotations.read_only_hint is True
    monkeypatch.setattr(server_module, 'get_access_token', lambda: None)
    with pytest.raises(PermissionError): status.fn()
    monkeypatch.setattr(server_module, 'get_access_token', lambda: SimpleNamespace(claims={'sub': '1'}))
    result = status.fn()
    assert result['service']['name'] == PROJECT
    assert ('asr' in result) == (PROJECT != 'jev-mcp')
    assert ('typesafe' in result) == (PROJECT == 'jev-mcp')

@pytest.mark.asyncio
async def test_lifespan_closes_own_clients_and_cancels_workers(tmp_path, monkeypatch):
    if PROJECT == 'jev-mcp':
        clients = []
        class FakeClient:
            def __init__(self, **kwargs): self.closed = False; clients.append(self)
            async def aclose(self): self.closed = True
        monkeypatch.setattr(server_module, 'AsyncTypeSafeClient', FakeClient)
    else:
        async def idle(self, stop): await stop.wait()
        for cls in [server_module.QuotaSynchronizer, server_module.ASRCoordinator, server_module.VideoAnalysis]:
            monkeypatch.setattr(cls, 'run', idle)
    mcp = server_module.create_server(_settings(tmp_path))
    async with mcp._lifespan_manager():
        if PROJECT == 'jev-mcp': assert len(clients) == 1 and not clients[0].closed
    if PROJECT == 'jev-mcp': assert clients[0].closed
