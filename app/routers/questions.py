"""Question bank endpoints. Drives the surveyor UI (filtered) + admin CRUD."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from supabase import Client

from ..auth import get_current_user
from ..db import get_db
from ..models import Domain, Question

log = logging.getLogger("questions")
_auth = [Depends(get_current_user)]
router = APIRouter(prefix="/questions", tags=["questions"], dependencies=_auth)
domains_router = APIRouter(prefix="/domains", tags=["domains"], dependencies=_auth)


@domains_router.get("", response_model=list[Domain])
def list_domains(db: Client = Depends(get_db)) -> list[Domain]:
    """Active domains for the surveyor (inactive categories are hidden)."""
    try:
        res = db.table("domains").select("*").eq("is_active", True).order("sort_order").execute()
    except Exception as e:
        log.error("[DB_ERR] list_domains: %s", e)
        raise HTTPException(status_code=502, detail="domain lookup failed") from e
    return [Domain(**d) for d in (res.data or [])]


@router.get("/batch", response_model=list[Question])
def list_questions_batch(
    domains: str = Query(..., description="comma-separated domain slugs"),
    facility_type: str = Query(...),
    db: Client = Depends(get_db),
) -> list[Question]:
    """All active questions for several domains in ONE call (cuts survey load time)."""
    slugs = [d for d in domains.split(",") if d]
    if not slugs:
        return []
    try:
        res = (
            db.table("questions").select("*")
            .in_("domain_slug", slugs).eq("is_active", True)
            .order("section").order("sort_order").execute()
        )
    except Exception as e:
        log.error("[DB_ERR] list_questions_batch: %s", e)
        raise HTTPException(status_code=502, detail="question lookup failed") from e
    rows = res.data or []
    return [
        Question(**r) for r in rows
        if not r.get("facility_types") or facility_type in r["facility_types"]
    ]


@router.get("", response_model=list[Question])
def list_questions(
    domain: str = Query(..., description="domain slug, e.g. 'electrical'"),
    facility_type: str = Query(..., description="e.g. 'residential'"),
    db: Client = Depends(get_db),
) -> list[Question]:
    """
    Return active questions for a domain, filtered by facility type.
    Rule: a question applies if facility_types is empty (all) OR contains facility_type.
    Filtering done in Python for clarity; small bank, negligible cost.
    """
    try:
        res = (
            db.table("questions")
            .select("*")
            .eq("domain_slug", domain)
            .eq("is_active", True)
            .order("section")
            .order("sort_order")
            .execute()
        )
    except Exception as e:  # external call isolated from logic errors
        log.error("[DB_ERR] list_questions: %s", e)
        raise HTTPException(status_code=502, detail="question lookup failed") from e

    rows = res.data or []
    out = [
        r for r in rows
        if not r.get("facility_types") or facility_type in r["facility_types"]
    ]
    return [Question(**r) for r in out]
