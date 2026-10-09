from datetime import datetime, timedelta, timezone

import pytest

from bilibili_summary_mcp.errors import MCPError
from bilibili_summary_mcp.tokens import SignedTokenService


def test_context_token_is_user_bound_and_expires():
    service = SignedTokenService(b"a" * 32)
    now = datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc)
    token = service.issue_context(
        github_user_id=123,
        bvid="BV1token",
        cid=7,
        page=1,
        duration_seconds=321,
        now=now,
        ttl=timedelta(minutes=30),
    )

    payload = service.verify_context(token, github_user_id=123, now=now)
    assert payload["bvid"] == "BV1token"
    assert payload["duration_seconds"] == 321

    with pytest.raises(MCPError) as wrong_user:
        service.verify_context(token, github_user_id=456, now=now)
    assert wrong_user.value.code == "INVALID_CONTEXT_TOKEN"

    with pytest.raises(MCPError) as expired:
        service.verify_context(token, github_user_id=123, now=now + timedelta(minutes=31))
    assert expired.value.code == "CONTEXT_TOKEN_EXPIRED"


def test_cursor_is_bound_to_transcript_reference():
    service = SignedTokenService(b"b" * 32)
    cursor = service.issue_cursor("db:document-one", offset=100)

    assert service.verify_cursor(cursor, "db:document-one") == 100
    with pytest.raises(MCPError):
        service.verify_cursor(cursor, "db:document-two")
