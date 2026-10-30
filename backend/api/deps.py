"""FastAPI glue for authentication.  All decisions live in services/auth.py (framework-free, unit-tested)."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException

from services.auth import authenticate
from services.persistence import persistence
from services.supabase_service import AuthError, AuthUser


@dataclass
class AuthContext:
    user: AuthUser
    token: str | None

    @property
    def scoped(self) -> bool:
        return not self.user.local


async def current_auth(authorization: str | None = Header(default=None)) -> AuthContext:
    try:
        user, token = await authenticate(authorization, persistence.service, persistence.enabled)
    except AuthError as exc:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        raise HTTPException(exc.status, str(exc), headers=headers)
    return AuthContext(user, token)
