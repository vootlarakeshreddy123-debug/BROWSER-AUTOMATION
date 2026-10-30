"""Framework-free authentication/authorisation core (the FastAPI glue in api/deps.py only calls these).

* Supabase DISABLED -> local single-user mode: everyone is the local user, nothing is scoped (exactly the old behaviour).
* Supabase ENABLED  -> every protected call needs ``Authorization: Bearer <access token>``, validated by the auth server.
  The user id comes from that validation; a ``user_id`` / ``owner`` in a body, query or header is never consulted.
"""
from __future__ import annotations

from typing import Any

from services.supabase_service import AuthError, AuthUnavailable, AuthUser, SupabaseService

LOCAL_USER = AuthUser(id="local", email=None, local=True)


def extract_bearer(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def authenticate(authorization: str | None, service: SupabaseService | None, enabled: bool) -> tuple[AuthUser, str | None]:
    """(user, token).  Raises AuthError(401) / AuthUnavailable(503)."""
    if not enabled:
        return LOCAL_USER, None
    if service is None:
        raise AuthUnavailable("authentication is enabled but not configured")
    token = extract_bearer(authorization)
    if token is None:
        raise AuthError("authentication required: send 'Authorization: Bearer <access token>'")
    return await service.validate_token(token), token


def owns(task: Any, user: AuthUser) -> bool:
    """Local mode: everything is visible.  Auth mode: only the owner's tasks (an unowned task is nobody's)."""
    if user.local:
        return True
    owner = getattr(task, "owner_id", None)
    return owner is not None and owner == user.id
