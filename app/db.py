"""Supabase client provider (dependency-injected). Service-role key = backend only."""
from __future__ import annotations

import logging
from functools import lru_cache

from supabase import Client, create_client

from .config import get_settings

log = logging.getLogger("db")


@lru_cache
def get_db() -> Client:
    """Reused singleton client (connection pooling under the hood)."""
    s = get_settings()
    if not s.supabase_url or not s.supabase_service_key:
        raise RuntimeError("[CONFIG_ERR] SUPABASE_URL / SUPABASE_SERVICE_KEY missing")
    log.info("[DB] Supabase client initialized")
    return create_client(s.supabase_url, s.supabase_service_key)
