from __future__ import annotations

from typing import Any


def require_allowed_github_user(token: Any, allowed_ids: frozenset[int]) -> int:
    """Authorize only immutable numeric GitHub IDs from verified FastMCP claims."""
    if token is None:
        raise PermissionError("需要 GitHub 登录。")
    claims = getattr(token, "claims", None)
    if not isinstance(claims, dict):
        raise PermissionError("登录身份缺少已验证的 GitHub 信息。")
    user_data = claims.get("github_user_data")
    candidates = [
        user_data.get("id") if isinstance(user_data, dict) else None,
        claims.get("sub"),
        getattr(token, "subject", None),
    ]
    github_id: int | None = None
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool):
            github_id = value
            break
        if isinstance(value, str) and value.isdecimal():
            github_id = int(value)
            break
    if github_id is None or github_id not in allowed_ids:
        raise PermissionError("该 GitHub 数字用户 ID 不在允许列表中。")
    return github_id
