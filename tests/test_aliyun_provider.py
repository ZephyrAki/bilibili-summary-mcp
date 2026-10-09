import json

import pytest

from bilibili_summary_mcp.aliyun import AliyunQuotaProvider, ProviderError


@pytest.mark.asyncio
async def test_quota_and_autostop_are_separate_cli_operations():
    calls: list[list[str]] = []

    async def runner(command: list[str], timeout: float):
        calls.append(command)
        if command[1:3] == ["usage", "free"]:
            return 0, json.dumps(
                [
                    {
                        "model": "paraformer-v2",
                        "total": 36_000,
                        "remaining": 23_400,
                        "expires": "2026-10-01",
                    }
                ]
            ), ""
        return 0, json.dumps(
            {"data": {"freeTierOnlyStatuses": [
                {"model": "paraformer-v2", "freeTierOnly": True}
            ]}}
        ), ""

    provider = AliyunQuotaProvider(runner=runner)
    quota = await provider.fetch_quota()
    autostop = await provider.fetch_autostop()

    assert quota.quota_used_seconds == 12_600
    assert quota.quota_remaining_seconds == 23_400
    assert autostop.free_tier_autostop is True
    assert calls[0][2] == "free"
    assert calls[1][1:3] == ["console", "call"]
    assert any("queryFreeTierOnlyStatus" in argument for argument in calls[1])
    assert all("--output" in command and "json" in command for command in calls)


@pytest.mark.asyncio
async def test_provider_rejects_wrong_model_and_redacts_errors():
    async def runner(command: list[str], timeout: float):
        return 1, "", "Authorization: Bearer secret-value requestId=req-123"

    provider = AliyunQuotaProvider(runner=runner)
    with pytest.raises(ProviderError) as exc:
        await provider.fetch_quota()

    assert exc.value.code == "CLI_FAILED"
    assert "secret-value" not in exc.value.summary
    assert "req-123" in exc.value.summary


@pytest.mark.asyncio
async def test_autostop_parser_accepts_nested_console_gateway_shape():
    async def runner(command: list[str], timeout: float):
        return 0, json.dumps(
            {"data": {"DataV2": {"data": {"data": {
                "freeTierOnlyStatuses": [
                    {"model": "paraformer-v2", "freeTierOnly": True}
                ]
            }}}}}
        ), ""

    state = await AliyunQuotaProvider(runner=runner).fetch_autostop()
    assert state.free_tier_autostop is True


@pytest.mark.asyncio
async def test_expired_console_session_has_actionable_error():
    async def runner(command,timeout):
        return 3, '{"error":{"code":3,"message":"Console session is not logged in or has expired."}}', ''
    provider = AliyunQuotaProvider(runner=runner)
    with pytest.raises(ProviderError) as caught:
        await provider.fetch_quota()
    assert caught.value.code == 'CLI_AUTH_EXPIRED'
    assert '授权' in caught.value.summary
