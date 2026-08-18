"""Survey authoring layer (v2): the per-survey area tree + question instances.

Why this exists (the "recipe folder" model):
    The shared question bank (`questions`) is the master recipe book. When a
    survey is created we *snapshot* the relevant recipes into that survey's own
    folder (`survey_questions`) so every property can diverge without mutating
    the bank — and bank edits never rewrite an in-flight survey.

Layering (kept deliberately flat, per the codebase):
    - input validation ....... Pydantic models (models.py)
    - authorization .......... authorize_survey (surveys.py) — reused as a dep
    - DB I/O ................. Supabase client (get_db)
    - each handler wraps DB calls in try/except and fails loudly ([DB_ERR]).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from supabase import Client

from ..db import get_db
from ..models import (
    AddFromBankIn,
    AreaReorder,
    CustomQuestionIn,
    QuestionReorder,
    SurveyArea,
    SurveyAreaIn,
    SurveyQuestion,
)
from .surveys import authorize_survey

log = logging.getLogger("survey_content")

# Reuse the existing /surveys prefix + per-survey authorization on every route.
router = APIRouter(prefix="/surveys/{survey_id}", tags=["survey-content"])

# Facility-level domains attach at area_id = NULL (site-wide, not per-building).
# Only Site Profile (general) is auto-added; Client Pain / UREST are opt-in via
# the picker, not boilerplate on every survey.
_FACILITY_DOMAINS = ("general",)
# Domains absorbed into the per-building assessment Keys (not shown standalone).
_MERGED_DOMAINS = frozenset({"green_building", "fire_safety"})


# ─────────────────────────── shared helpers ────────────────────────────────
def _fetch_areas(db: Client, survey_id: str) -> list[dict]:
    res = (
        db.table("survey_areas").select("*")
        .eq("survey_id", survey_id).order("sort_order").execute()
    )
    return res.data or []


def _ensure_default_building(db: Client, survey_id: str) -> str:
    """Return the id of the survey's first top-level building, creating one if the
    tree is empty. Every survey always has at least one building so questions and
    answers have a home in the tree."""
    for a in _fetch_areas(db, survey_id):
        if a.get("parent_id") is None:
            return a["id"]
    ins = db.table("survey_areas").insert({
        "survey_id": survey_id, "parent_id": None,
        "name": "Main Building", "kind": "building", "sort_order": 0,
    }).execute()
    if not ins.data:
        raise HTTPException(status_code=502, detail="could not create default building")
    return ins.data[0]["id"]


def _descendant_ids(areas: list[dict], root_id: str) -> set[str]:
    """All area ids in the subtree rooted at root_id (inclusive). Computed in
    Python from the flat list — the tree is small and this avoids a recursive
    SQL round-trip while still deleting the whole branch atomically below."""
    children: dict[Optional[str], list[str]] = {}
    for a in areas:
        children.setdefault(a.get("parent_id"), []).append(a["id"])
    out: set[str] = set()
    stack = [root_id]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(children.get(cur, []))
    return out


def _snapshot_rows(
    bank: list[dict], facility_type: str, survey_id: str, area_id: str | None
) -> list[dict]:
    """Filter bank rows by facility_type and shape them as survey_questions rows
    for one area (or facility-level when area_id is None)."""
    out: list[dict] = []
    for r in bank:
        fts = r.get("facility_types") or []
        if fts and facility_type not in fts:
            continue
        out.append({
            "survey_id": survey_id,
            "area_id": area_id,
            "domain_slug": r["domain_slug"],
            "section": r.get("section"),
            "text": r["text"],
            "answer_type": r["answer_type"],
            "needs_photo": bool(r.get("needs_photo", False)),
            "checklist": r.get("checklist") or [],
            "good_answer": r.get("good_answer"),
            "sort_order": r.get("sort_order", 0),
            "source": "bank",
            "origin_question_id": r["id"],
        })
    return out


def materialize_survey_content(
    db: Client, survey_id: str, facility_type: str, domain_slugs: list[str]
) -> None:
    """Snapshot boilerplate (is_default) bank questions into a new survey.

    Layout mirrors the surveyor UI:
      - one building node per survey block (or a single default building),
      - facility-level domains (Site Profile / Client Pain / UREST) at area_id=NULL,
      - each building gets the assessment Keys (domains.is_key) + the client's
        selected domains.

    Called once at creation (website form OR admin create). Idempotent: skips if
    the survey already has question instances, so a retried create never
    double-inserts. Non-fatal on error — the surveyor can add questions manually.
    """
    try:
        existing = (
            db.table("survey_questions").select("id")
            .eq("survey_id", survey_id).limit(1).execute()
        )
        if existing.data:
            return  # already materialized — do not duplicate

        # Building nodes: one per block name, else a single default building.
        srow = db.table("surveys").select("blocks").eq("id", survey_id).limit(1).execute()
        blocks = (srow.data or [{}])[0].get("blocks") or []
        names = [str((b or {}).get("name") or "").strip() for b in blocks]
        names = [n for n in names if n] or ["Main Building"]
        building_ids: list[str] = []
        for i, name in enumerate(names):
            ins = db.table("survey_areas").insert({
                "survey_id": survey_id, "parent_id": None,
                "name": name, "kind": "building", "sort_order": i,
            }).execute()
            if ins.data:
                building_ids.append(ins.data[0]["id"])

        # Assessment Keys (is_key) + selected domains -> per building.
        dres = db.table("domains").select("slug,is_key").eq("is_active", True).execute()
        key_slugs = [d["slug"] for d in (dres.data or []) if d.get("is_key")]
        selected = [d for d in domain_slugs if d not in _MERGED_DOMAINS]
        building_slugs = list(dict.fromkeys(key_slugs + selected))

        payload: list[dict] = []

        if building_slugs and building_ids:
            # ALL active scope questions (default-N/A model), is_default sorted first.
            bq = (
                db.table("questions").select("*")
                .in_("domain_slug", building_slugs)
                .eq("is_active", True)
                .order("is_default", desc=True).order("sort_order").execute()
            ).data or []
            for bid in building_ids:
                payload += _snapshot_rows(bq, facility_type, survey_id, bid)

        # Facility-level domains at area_id = NULL (site-wide).
        fq = (
            db.table("questions").select("*")
            .in_("domain_slug", list(_FACILITY_DOMAINS))
            .eq("is_active", True)
            .order("is_default", desc=True).order("sort_order").execute()
        ).data or []
        payload += _snapshot_rows(fq, facility_type, survey_id, None)

        if payload:
            db.table("survey_questions").insert(payload).execute()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — creation must not 500 on content seeding
        log.error("[DB_ERR] materialize_survey_content %s: %s", survey_id, e)
        # Non-fatal: the survey row exists; the surveyor can add questions manually.


def _scope_slugs(db: Client, domain_slugs: list[str]) -> list[str]:
    """Assessment Keys (is_key) + the survey's selected scope (minus merged)."""
    dres = db.table("domains").select("slug,is_key").eq("is_active", True).execute()
    key_slugs = [d["slug"] for d in (dres.data or []) if d.get("is_key")]
    selected = [d for d in domain_slugs if d not in _MERGED_DOMAINS]
    return list(dict.fromkeys(key_slugs + selected))


def materialize_node_questions(db: Client, survey_id: str, area_id: str) -> list[dict]:
    """Snapshot the survey's full scope question set onto ONE new node (default-N/A
    model: every building/floor/room inherits the scope). Returns the inserted rows
    so the caller can hand them back to the client without a refetch. Non-fatal."""
    try:
        srow = (db.table("surveys").select("facility_type,domain_slugs")
                .eq("id", survey_id).limit(1).execute())
        if not srow.data:
            return []
        facility_type = srow.data[0].get("facility_type") or ""
        slugs = _scope_slugs(db, srow.data[0].get("domain_slugs") or [])
        if not slugs:
            return []
        bq = (
            db.table("questions").select("*")
            .in_("domain_slug", slugs).eq("is_active", True)
            .order("is_default", desc=True).order("sort_order").execute()
        ).data or []
        payload = _snapshot_rows(bq, facility_type, survey_id, area_id)
        if not payload:
            return []
        res = db.table("survey_questions").insert(payload).execute()
        return res.data or []
    except Exception as e:  # noqa: BLE001 — node creation must not fail on seeding
        log.error("[DB_ERR] materialize_node_questions %s/%s: %s", survey_id, area_id, e)
        return []


# ─────────────────────────────── areas ─────────────────────────────────────
@router.get("/areas", response_model=list[SurveyArea])
def list_areas(survey_id: str, db: Client = Depends(get_db),
               _sv: dict = Depends(authorize_survey)) -> list[SurveyArea]:
    """Flat list of every node; the client assembles the tree from parent_id."""
    try:
        return [SurveyArea(**a) for a in _fetch_areas(db, survey_id)]
    except Exception as e:
        log.error("[DB_ERR] list_areas %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not load areas") from e


@router.post("/areas", response_model=SurveyArea, status_code=201)
def create_area(survey_id: str, body: SurveyAreaIn, db: Client = Depends(get_db),
                _sv: dict = Depends(authorize_survey)) -> SurveyArea:
    """Add a building (parent_id null) or a nested area. id is client-supplied
    (UUID) so the surveyor's device can key offline answers to it immediately."""
    if body.parent_id:
        parents = {a["id"] for a in _fetch_areas(db, survey_id)}
        if body.parent_id not in parents:
            raise HTTPException(status_code=400, detail="parent_id not in this survey")
    row = {
        "survey_id": survey_id,
        "parent_id": body.parent_id,
        "name": body.name.strip(),
        "kind": body.kind,
        "sort_order": body.sort_order if body.sort_order is not None else 0,
    }
    if body.id:
        row["id"] = body.id
    try:
        res = db.table("survey_areas").insert(row).execute()
    except Exception as e:
        log.error("[DB_ERR] create_area %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not create area") from e
    if not res.data:
        raise HTTPException(status_code=500, detail="insert returned no row")
    # Every new node inherits the survey's full scope question set (default-N/A).
    materialize_node_questions(db, survey_id, res.data[0]["id"])
    return SurveyArea(**res.data[0])


@router.patch("/areas/{area_id}", response_model=SurveyArea)
def update_area(survey_id: str, area_id: str, body: SurveyAreaIn,
                db: Client = Depends(get_db),
                _sv: dict = Depends(authorize_survey)) -> SurveyArea:
    """Rename or move an area. Move guards against reparenting a node under its
    own descendant (which would orphan a cycle)."""
    areas = _fetch_areas(db, survey_id)
    if area_id not in {a["id"] for a in areas}:
        raise HTTPException(status_code=404, detail="area not found")
    patch: dict = {"name": body.name.strip()}
    if body.sort_order is not None:
        patch["sort_order"] = body.sort_order
    if body.parent_id is not None or body.kind == "building":
        # Explicit reparent (including promotion to building via parent_id=None).
        new_parent = body.parent_id
        if new_parent and new_parent in _descendant_ids(areas, area_id):
            raise HTTPException(status_code=400, detail="cannot move an area under itself")
        patch["parent_id"] = new_parent
    try:
        res = db.table("survey_areas").update(patch).eq("id", area_id).eq("survey_id", survey_id).execute()
    except Exception as e:
        log.error("[DB_ERR] update_area %s: %s", area_id, e)
        raise HTTPException(status_code=502, detail="could not update area") from e
    if not res.data:
        raise HTTPException(status_code=404, detail="area not found")
    return SurveyArea(**res.data[0])


@router.delete("/areas/{area_id}", status_code=200)
def delete_area(survey_id: str, area_id: str, db: Client = Depends(get_db),
                _sv: dict = Depends(authorize_survey)) -> dict:
    """Delete an area and its whole subtree, its question instances, and their
    answers. Explicit deletes (not FK cascade alone) so orphaned answers can't
    linger and skew scoring."""
    areas = _fetch_areas(db, survey_id)
    if area_id not in {a["id"] for a in areas}:
        raise HTTPException(status_code=404, detail="area not found")
    victims = _descendant_ids(areas, area_id)
    try:
        sq = (db.table("survey_questions").select("id")
              .eq("survey_id", survey_id).in_("area_id", list(victims)).execute())
        q_ids = [r["id"] for r in (sq.data or [])]
        if q_ids:
            db.table("answers").delete().eq("survey_id", survey_id).in_("question_id", q_ids).execute()
            db.table("survey_questions").delete().eq("survey_id", survey_id).in_("id", q_ids).execute()
        # Delete children before parents to satisfy the self-referential FK.
        db.table("survey_areas").delete().eq("survey_id", survey_id).in_("id", list(victims)).execute()
    except Exception as e:
        log.error("[DB_ERR] delete_area %s: %s", area_id, e)
        raise HTTPException(status_code=502, detail="could not delete area") from e
    return {"ok": True, "deleted_areas": len(victims)}


@router.put("/areas/reorder", status_code=200)
def reorder_areas(survey_id: str, body: AreaReorder, db: Client = Depends(get_db),
                  _sv: dict = Depends(authorize_survey)) -> dict:
    """Persist a new sibling order (ordered_ids are siblings under one parent)."""
    valid = {a["id"] for a in _fetch_areas(db, survey_id)}
    try:
        for i, aid in enumerate(body.ordered_ids):
            if aid in valid:
                db.table("survey_areas").update({"sort_order": i}).eq("id", aid).eq("survey_id", survey_id).execute()
    except Exception as e:
        log.error("[DB_ERR] reorder_areas %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not reorder areas") from e
    return {"ok": True}


# ────────────────────────── survey questions ───────────────────────────────
@router.get("/questions", response_model=list[SurveyQuestion])
def list_survey_questions(survey_id: str, db: Client = Depends(get_db),
                          _sv: dict = Depends(authorize_survey)) -> list[SurveyQuestion]:
    """Every question instance for this survey; the client groups by area+domain."""
    try:
        res = (db.table("survey_questions").select("*")
               .eq("survey_id", survey_id).order("sort_order").execute())
        return [SurveyQuestion(**r) for r in (res.data or [])]
    except Exception as e:
        log.error("[DB_ERR] list_survey_questions %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not load questions") from e


@router.post("/questions/from-bank", response_model=list[SurveyQuestion], status_code=201)
def add_from_bank(survey_id: str, body: AddFromBankIn, db: Client = Depends(get_db),
                  _sv: dict = Depends(authorize_survey)) -> list[SurveyQuestion]:
    """Snapshot one or more bank questions into an area. Snapshot (not reference)
    so later bank edits never mutate this survey."""
    area_id = body.area_id or _ensure_default_building(db, survey_id)
    try:
        bank = (db.table("questions").select("*")
                .in_("id", body.question_ids).execute()).data or []
        by_id = {r["id"]: r for r in bank}
        base = (db.table("survey_questions").select("sort_order")
                .eq("survey_id", survey_id).order("sort_order", desc=True).limit(1).execute())
        nxt = ((base.data or [{}])[0].get("sort_order") or 0) + 1
        payload = []
        for qid in body.question_ids:  # preserve caller order
            r = by_id.get(qid)
            if not r:
                continue
            payload.append({
                "survey_id": survey_id, "area_id": area_id,
                "domain_slug": r["domain_slug"], "section": r.get("section"),
                "text": r["text"], "answer_type": r["answer_type"],
                "needs_photo": bool(r.get("needs_photo", False)),
                "checklist": r.get("checklist") or [], "good_answer": r.get("good_answer"),
                "sort_order": nxt, "source": "bank", "origin_question_id": r["id"],
            })
            nxt += 1
        if not payload:
            raise HTTPException(status_code=400, detail="no valid question_ids")
        res = db.table("survey_questions").insert(payload).execute()
    except HTTPException:
        raise
    except Exception as e:
        log.error("[DB_ERR] add_from_bank %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not add questions") from e
    return [SurveyQuestion(**r) for r in (res.data or [])]


@router.post("/questions/custom", response_model=SurveyQuestion, status_code=201)
def add_custom_question(survey_id: str, body: CustomQuestionIn, db: Client = Depends(get_db),
                        _sv: dict = Depends(authorize_survey)) -> SurveyQuestion:
    """Survey-scoped one-off question. Never touches the shared bank."""
    area_id = body.area_id or _ensure_default_building(db, survey_id)
    base = (db.table("survey_questions").select("sort_order")
            .eq("survey_id", survey_id).order("sort_order", desc=True).limit(1).execute())
    nxt = ((base.data or [{}])[0].get("sort_order") or 0) + 1
    row = {
        "survey_id": survey_id, "area_id": area_id,
        "domain_slug": body.domain_slug, "section": body.section,
        "text": body.text.strip(), "answer_type": body.answer_type,
        "needs_photo": body.needs_photo, "good_answer": body.good_answer,
        "checklist": [c.model_dump() for c in body.checklist],
        "sort_order": nxt, "source": "custom", "origin_question_id": None,
    }
    if body.id:
        row["id"] = body.id
    try:
        res = db.table("survey_questions").insert(row).execute()
    except Exception as e:
        log.error("[DB_ERR] add_custom_question %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not add question") from e
    if not res.data:
        raise HTTPException(status_code=500, detail="insert returned no row")
    return SurveyQuestion(**res.data[0])


@router.patch("/questions/{sq_id}", response_model=SurveyQuestion)
def update_survey_question(survey_id: str, sq_id: str, body: CustomQuestionIn,
                           db: Client = Depends(get_db),
                           _sv: dict = Depends(authorize_survey)) -> SurveyQuestion:
    """Edit a question instance in place (text/type/section/area/checklist)."""
    patch = {
        "area_id": body.area_id, "domain_slug": body.domain_slug,
        "section": body.section, "text": body.text.strip(),
        "answer_type": body.answer_type, "needs_photo": body.needs_photo,
        "checklist": [c.model_dump() for c in body.checklist],
    }
    try:
        res = (db.table("survey_questions").update(patch)
               .eq("id", sq_id).eq("survey_id", survey_id).execute())
    except Exception as e:
        log.error("[DB_ERR] update_survey_question %s: %s", sq_id, e)
        raise HTTPException(status_code=502, detail="could not update question") from e
    if not res.data:
        raise HTTPException(status_code=404, detail="question not found")
    return SurveyQuestion(**res.data[0])


@router.delete("/questions/{sq_id}", status_code=200)
def delete_survey_question(survey_id: str, sq_id: str, db: Client = Depends(get_db),
                           _sv: dict = Depends(authorize_survey)) -> dict:
    """Delete a question instance and any answers recorded against it."""
    try:
        db.table("answers").delete().eq("survey_id", survey_id).eq("question_id", sq_id).execute()
        db.table("survey_questions").delete().eq("id", sq_id).eq("survey_id", survey_id).execute()
    except Exception as e:
        log.error("[DB_ERR] delete_survey_question %s: %s", sq_id, e)
        raise HTTPException(status_code=502, detail="could not delete question") from e
    return {"ok": True}


@router.put("/questions/reorder", status_code=200)
def reorder_survey_questions(survey_id: str, body: QuestionReorder,
                             db: Client = Depends(get_db),
                             _sv: dict = Depends(authorize_survey)) -> dict:
    """Persist a new question order within an area/domain group."""
    try:
        for i, qid in enumerate(body.ordered_ids):
            db.table("survey_questions").update({"sort_order": i}).eq("id", qid).eq("survey_id", survey_id).execute()
    except Exception as e:
        log.error("[DB_ERR] reorder_survey_questions %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not reorder questions") from e
    return {"ok": True}
