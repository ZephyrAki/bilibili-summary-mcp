import asyncio

import pytest
from cryptography.fernet import Fernet

from bilibili_summary_mcp.auth import require_allowed_github_user
from bilibili_summary_mcp.oauth_storage import build_oauth_storage


class Token:
    def __init__(self, claims):
        self.claims = claims
        self.subject = None


def test_allowlist_uses_numeric_id_from_verified_github_context():
    token = Token({"github_user_data": {"id": 12345, "login": "renamed-user"}})
    assert require_allowed_github_user(token, frozenset({12345})) == 12345


def test_allowlist_rejects_username_only():
    token = Token({"login": "AkiZephyr"})
    with pytest.raises(PermissionError):
        require_allowed_github_user(token, frozenset({12345}))


def test_allowlist_rejects_different_numeric_id():
    token = Token({"github_user_data": {"id": 99999, "login": "other-user"}})
    with pytest.raises(PermissionError):
        require_allowed_github_user(token, frozenset({12345}))


def test_oauth_storage_is_encrypted_and_survives_reopen(tmp_path):
    key = Fernet.generate_key()

    async def exercise():
        first = build_oauth_storage(tmp_path, key)
        await first.put("client", {"token": "top-secret"}, collection="oauth")
        second = build_oauth_storage(tmp_path, key)
        return await second.get("client", collection="oauth")

    assert asyncio.run(exercise()) == {"token": "top-secret"}
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert files
    assert all(b"top-secret" not in path.read_bytes() for path in files)
