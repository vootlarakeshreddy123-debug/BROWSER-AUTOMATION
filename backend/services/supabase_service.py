"""Supabase access over plain HTTPS (GoTrue + PostgREST) through an injectable transport.

Responsibilities (and ONLY these):
  * validate a user's access token  -> who is calling                      (GoTrue  GET /auth/v1/user)
  * persist task rows / event rows  -> history that survives a restart     (PostgREST, user-scoped, RLS enforces owner)
  * read them back for that user only

Supabase never plans, never chooses actions and never decides success: callers pass in the status the BACKEND decided.

Security model
  * The owner of a row is always the id returned by GoTrue for the presented token - never a value from the request body.
  * Reads and writes carry the USER's JWT plus the publishable key, so Postgres Row Level Security is the final
    isolation layer even if backend code were wrong.  A secret (service) key is optional: it is used only to finish a
    background write after the user's token expired, with ``user_id`` still set from the validated owner.
  * No key, token, password or cookie is ever logged or stored (``safe`` redacts).

Stdlib only; httpx is imported lazily by the default transport.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger("services.supabase")

# (method, url, headers, params, json_body, timeout) -> (status, body, response_headers)
Transport = Callable[[str, str, dict, "dict | None", Any, float], Awaitable["tuple[int, Any, dict]"]]


class SupabaseError(Exception):
    """A persistence call failed.  ``transient`` = worth a bounded retry (network / 5xx / 429)."""

    def __init__(self, message: str, status: int | None = None, transient: bool = False) -> None:
        super().__init__(message)
        self.status, self.transient = status, transient


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


class AuthUnavailable(AuthError):
    """The auth service could not be reached: access is DENIED (fail closed), never granted by default."""

    def __init__(self, message: str = "authentication service unavailable") -> None:
        super().__init__(message, 503)


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str | None = None
    local: bool = False          # True only in local single-user mode (Supabase disabled)


async def httpx_transport(method: str, url: str, headers: dict, params: dict | None, json_body: Any, timeout: float):
    import httpx  # lazy
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method, url, headers=headers, params=params, json=json_body)
    try:
        body = resp.json() if resp.content else None
    except Exception:
        body = resp.text
    return resp.status_code, body, dict(resp.headers)


def _jwt_exp(token: str) -> float | None:
    """The ``exp`` claim, ONLY used to shorten a cache entry.  Never used to establish identity."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except Exception:
        return None


class SupabaseService:
    def __init__(self, url: str, publishable_key: str | None, secret_key: str | None = None, *,
                 transport: Transport | None = None, timeout_s: float = 8.0, cache_ttl_s: float = 30.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.url = (url or "").rstrip("/")
        self._pub, self._secret = publishable_key, secret_key
        self._transport: Transport = transport or httpx_transport
        self.timeout_s, self.cache_ttl_s, self.clock = timeout_s, cache_ttl_s, clock
        self._cache: dict[str, tuple[AuthUser, float]] = {}

    # ---- configuration ------------------------------------------------------------------
    def problems(self) -> list[str]:
        out = []
        if not self.url.startswith(("https://", "http://")):
            out.append("SUPABASE_URL is not set to your project URL")
        if not self._pub:
            out.append("SUPABASE_PUBLISHABLE_KEY is not set")
        return out

    @property
    def configured(self) -> bool:
        return not self.problems()

    def safe(self, text: Any) -> str:
        from providers.http import redact
        return redact(text, [self._pub, self._secret])[:300]

    # ---- auth ---------------------------------------------------------------------------
    async def validate_token(self, token: str | None) -> AuthUser:
        """Resolve a bearer token to its user by asking the auth server.  Raises AuthError (401) / AuthUnavailable (503)."""
        token = (token or "").strip()
        if not token or token.count(".") != 2 or len(token) > 8192:
            raise AuthError("missing or malformed access token")
        if not self.configured:
            raise AuthUnavailable("Supabase is enabled but not configured")
        key = hashlib.sha256(token.encode()).hexdigest()
        now = self.clock()
        hit = self._cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
        self._cache.pop(key, None)
        try:
            status, body, _ = await self._transport("GET", f"{self.url}/auth/v1/user",
                                                    {"apikey": self._pub or "", "Authorization": f"Bearer {token}"},
                                                    None, None, self.timeout_s)
        except Exception as exc:
            raise AuthUnavailable(self.safe(f"auth service unreachable: {type(exc).__name__}")) from None
        if status in (401, 403):
            raise AuthError("invalid or expired access token")
        if status != 200 or not isinstance(body, dict) or not body.get("id"):
            raise AuthUnavailable(f"auth service returned HTTP {status}")
        user = AuthUser(id=str(body["id"]), email=body.get("email"))
        ttl = self.cache_ttl_s
        exp = _jwt_exp(token)
        if exp is not None:
            ttl = min(ttl, max(0.0, exp - now))          # never cache past the token's own expiry
        if ttl > 0:
            self._cache[key] = (user, now + ttl)
            if len(self._cache) > 512:
                self._cache.pop(next(iter(self._cache)))
        return user

    def forget_token(self, token: str) -> None:
        self._cache.pop(hashlib.sha256((token or "").encode()).hexdigest(), None)

    # ---- low-level REST -----------------------------------------------------------------
    def _headers(self, token: str | None, *, privileged: bool = False, prefer: str | None = None) -> dict:
        h = {"Content-Type": "application/json"}
        if privileged:
            h.update({"apikey": self._secret or "", "Authorization": f"Bearer {self._secret}"})
        else:
            h.update({"apikey": self._pub or "", "Authorization": f"Bearer {token}"})
        if prefer:
            h["Prefer"] = prefer
        return h

    async def _call(self, method: str, path: str, token: str | None, *, params: dict | None = None, body: Any = None,
                    prefer: str | None = None, allow_privileged: bool = False) -> Any:
        """One REST call as the user; on an EXPIRED user token fall back to the secret key only if one is configured
        (the caller still scopes the row to the validated owner).  Raises SupabaseError."""
        for privileged in ((False, True) if (self._secret and allow_privileged) else (False,)):
            if privileged is False and not token:
                continue
            try:
                status, resp, _ = await self._transport(method, f"{self.url}/rest/v1/{path}", self._headers(token, privileged=privileged, prefer=prefer),
                                                        params, body, self.timeout_s)
            except Exception as exc:
                raise SupabaseError(self.safe(f"network error: {type(exc).__name__}"), None, True) from None
            if status in (401, 403) and not privileged and self._secret and allow_privileged:
                continue                                    # expired user token: retry privileged, owner already enforced by caller
            if status in (200, 201, 204, 206):
                return resp
            if status == 409:
                return None                                 # duplicate primary key: the row is already there (idempotent)
            detail = self.safe(resp if isinstance(resp, str) else json.dumps(resp, default=str) if resp else "")
            raise SupabaseError(f"HTTP {status}: {detail}", status, status >= 500 or status in (408, 429))
        raise SupabaseError("no usable credential for this call (token missing/expired" + ("" if allow_privileged else "; reads never use a privileged key") + ")", 401, False)

    # ---- tasks / events -----------------------------------------------------------------
    async def insert_task(self, owner_id: str, token: str | None, row: dict) -> None:
        row = dict(row, user_id=owner_id)                    # owner ALWAYS from the validated identity
        await self._call("POST", "tasks", token, params={"on_conflict": "id"}, body=row,
                         prefer="resolution=ignore-duplicates,return=minimal", allow_privileged=True)

    async def update_task(self, owner_id: str, token: str | None, task_id: str, patch: dict) -> None:
        patch = {k: v for k, v in patch.items() if k not in ("id", "user_id")}
        await self._call("PATCH", "tasks", token, params={"id": f"eq.{task_id}", "user_id": f"eq.{owner_id}"}, body=patch,
                         prefer="return=minimal", allow_privileged=True)

    async def insert_events(self, owner_id: str, token: str | None, rows: list[dict]) -> None:
        if not rows:
            return
        rows = [dict(r, user_id=owner_id) for r in rows]
        await self._call("POST", "task_events", token, params={"on_conflict": "id"}, body=rows,
                         prefer="resolution=ignore-duplicates,return=minimal", allow_privileged=True)

    async def list_tasks(self, token: str, limit: int = 50) -> list[dict]:
        r = await self._call("GET", "tasks", token, params={"select": "*", "order": "created_at.desc", "limit": str(max(1, min(limit, 200)))})
        return r if isinstance(r, list) else []

    async def get_task(self, token: str, task_id: str) -> dict | None:
        r = await self._call("GET", "tasks", token, params={"select": "*", "id": f"eq.{task_id}", "limit": "1"})
        return r[0] if isinstance(r, list) and r else None

    async def list_events(self, token: str, task_id: str, limit: int = 500) -> list[dict]:
        r = await self._call("GET", "task_events", token, params={"select": "*", "task_id": f"eq.{task_id}",
                                                                   "order": "created_at.asc,seq.asc", "limit": str(max(1, min(limit, 1000)))})
        return r if isinstance(r, list) else []
