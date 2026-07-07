"""Supabase Storage helpers for PRIVATE buckets (Phase 6b).

Uploads use the service-role key; reads are served via short-lived signed URLs.
Buckets ('reports', 'survey-photos') are private — see db/migration-private-buckets.sql.
"""
from __future__ import annotations

import logging
from typing import Any

from supabase import Client

log = logging.getLogger("storage")

SIGNED_URL_TTL = 60 * 60 * 24 * 7  # 7 days


def _signed_url(res: Any) -> str | None:
    """supabase-py returns the signed URL under one of these keys depending on version."""
    if isinstance(res, dict):
        return res.get("signedURL") or res.get("signedUrl") or res.get("signed_url")
    return getattr(res, "signed_url", None)


def upload_and_sign(db: Client, bucket: str, path: str, data: bytes, content_type: str,
                    expires: int = SIGNED_URL_TTL) -> str | None:
    """Upload bytes to a private bucket and return a time-limited signed URL."""
    try:
        db.storage.from_(bucket).upload(
            path, data, {"content-type": content_type, "upsert": "true"}
        )
    except Exception as e:
        log.error("[STORAGE_ERR] upload %s/%s: %s", bucket, path, e)
        raise
    return _signed_url(db.storage.from_(bucket).create_signed_url(path, expires))


def download(db: Client, bucket: str, path: str) -> bytes:
    """Download bytes from a private bucket using the service-role key."""
    return db.storage.from_(bucket).download(path)
