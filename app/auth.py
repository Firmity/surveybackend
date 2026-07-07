"""Phase 6b: validate the caller's Supabase access token.

Used as a FastAPI dependency on the data routers. Validates by asking Supabase
who the token belongs to (works across HS256/asymmetric JWT projects without
needing the signing secret). Raises 401 if missing/invalid.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from fastapi import Header, HTTPException

from .config import get_settings

log = logging.getLogger("auth")


async def get_current_user(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    s = get_settings()
    if not s.supabase_anon_key:
        raise HTTPException(status_code=500, detail="[CONFIG_ERR] SUPABASE_ANON_KEY missing")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")

    token = authorization.split(" ", 1)[1]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{s.supabase_url}/auth/v1/user",
                headers={"Authorization": f"Bearer {token}", "apikey": s.supabase_anon_key},
            )
    except Exception as e:  # network/Supabase down
        log.error("[AUTH_ERR] token check failed: %s", e)
        raise HTTPException(status_code=503, detail="auth check failed") from e

    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return r.json()
