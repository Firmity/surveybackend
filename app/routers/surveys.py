"""Survey lifecycle: create (from website form) -> sync answers -> trigger report."""
from __future__ import annotations

import json
import logging
import secrets
import string
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from supabase import Client

from ..auth import get_current_user
from ..db import get_db
from ..models import AdminSurveyCreate, AnswerSync, ReportOut, SurveyCreate, SurveyOut
from ..services.question_source import load_question_map
from ..services.report import generate_report
from ..services.scoring import compute_health, derive_actions

log = logging.getLogger("surveys")
router = APIRouter(prefix="/surveys", tags=["surveys"], dependencies=[Depends(get_current_user)])

_CODE_ALPHABET = string.ascii_uppercase + string.digits  # no lookalikes stripped; simple + short


def _gen_survey_code(n: int = 6) -> str:
    """On-site code shared with the client (kept server-side; never sent to surveyor)."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n))


def _role_of(db: Client, uid: str, attempts: int = 3) -> str | None:
    """Look up a user's role from `profiles`.

    Retries transient connection drops (the Supabase pooler occasionally resets an
    idle keep-alive with 'Server disconnected'). Without this an admin gets
    fail-closed out of a survey assigned to someone else purely because of a flaky
    read — which is exactly the 403 seen in the logs. Returns None only after all
    attempts fail (callers still treat None as 'not admin/manager')."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            prof = db.table("profiles").select("role").eq("id", uid).limit(1).execute()
            return (prof.data or [{}])[0].get("role")
        except Exception as e:  # noqa: BLE001 - retry boundary for transient drops
            last = e
            log.warning("[AUTHZ] role lookup attempt %d/%d for user %s failed: %s", i + 1, attempts, uid, e)
    log.error("[AUTHZ] role lookup exhausted for user %s: %s", uid, last)
    return None


def authorize_survey(
    survey_id: str,
    db: Client = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    """Authorize access to one survey AND return its row (so handlers reuse it).

    Closes the IDOR where any authenticated user could read/modify ANY survey by
    id. Policy:
      - the assigned surveyor  -> allow
      - admin / manager role   -> allow (any survey)
      - survey still unassigned -> allow (any surveyor may pick it up; matches the
        Surveys board UX and keeps enforcement fail-safe before assignment)
      - otherwise              -> 403
    Fail-safe: a missing `assigned_to` column reads as None -> unassigned -> allow,
    so enabling this never locks out existing surveys before the schema catches up.
    """
    res = db.table("surveys").select("*").eq("id", survey_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="survey not found")
    survey = res.data[0]
    uid = user.get("id")
    assigned = survey.get("assigned_to")
    if not assigned or assigned == uid:
        return survey
    if _role_of(db, uid) in ("admin", "manager"):
        return survey
    log.warning("[AUTHZ] user %s denied survey %s (assigned=%s)", uid, survey_id, assigned)
    raise HTTPException(status_code=403, detail="not authorized for this survey")


@router.post("", response_model=SurveyOut, status_code=201)
def create_survey(body: SurveyCreate, db: Client = Depends(get_db)) -> SurveyOut:
    """Called by the website client form. Creates the row pages 1-2 read from."""
    row = {
        "facility_type": body.facility_type,
        "domain_slugs": body.domain_slugs,
        "facility_name": body.facility_name,
        "facility_address": body.facility_address,
        "total_area": body.total_area,
        "area_unit": body.area_unit,
        "blocks": [b.model_dump() for b in body.blocks],
        "preferred_dates": json.loads(body.model_dump_json())["preferred_dates"],
        "contact": body.contact.model_dump(),
        "form_payload": body.form_payload,
        "status": "submitted",
        "survey_code": _gen_survey_code(),  # on-site code for the client to share
    }
    try:
        res = db.table("surveys").insert(row).execute()
    except Exception as e:
        log.error("[DB_ERR] create_survey: %s", e)
        raise HTTPException(status_code=502, detail="could not create survey") from e

    if not res.data:
        raise HTTPException(status_code=500, detail="insert returned no row")

    # Materialize the survey's own question folder (boilerplate snapshots) +
    # default building. Lazy import breaks the surveys<->survey_content cycle.
    created = res.data[0]
    from .survey_content import materialize_survey_content
    materialize_survey_content(db, created["id"], body.facility_type, body.domain_slugs)
    return SurveyOut(**created)


@router.post("/admin", response_model=SurveyOut, status_code=201)
def admin_create_survey(
    body: AdminSurveyCreate,
    db: Client = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> SurveyOut:
    """Admin/manager creates a survey directly (no client website booking needed)
    and optionally assigns it to a surveyor. Same materialization as the public
    create path. Role-gated: only admin/manager may call it."""
    if _role_of(db, user.get("id")) not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="admin or manager role required")

    row = {
        "facility_type": body.facility_type,
        "domain_slugs": body.domain_slugs,
        "facility_name": body.facility_name,
        "facility_address": body.facility_address,
        "total_area": body.total_area,
        "area_unit": body.area_unit,
        "blocks": [b.model_dump() for b in body.blocks],
        "preferred_dates": json.loads(body.model_dump_json())["preferred_dates"],
        "contact": body.contact.model_dump() if body.contact else {},
        "form_payload": body.form_payload,
        "assigned_to": body.assigned_to,
        "status": "submitted",
        "survey_code": _gen_survey_code(),
    }
    try:
        res = db.table("surveys").insert(row).execute()
    except Exception as e:
        log.error("[DB_ERR] admin_create_survey: %s", e)
        raise HTTPException(status_code=502, detail="could not create survey") from e
    if not res.data:
        raise HTTPException(status_code=500, detail="insert returned no row")

    created = res.data[0]
    from .survey_content import materialize_survey_content
    materialize_survey_content(db, created["id"], body.facility_type, body.domain_slugs)
    return SurveyOut(**created)


@router.post("/{survey_id}/verify-code", status_code=200)
def verify_code(survey_id: str, body: dict, db: Client = Depends(get_db),
                _sv: dict = Depends(authorize_survey)) -> dict:
    """Check the on-site code the surveyor entered against the survey's code.
    The code itself is never returned — only a boolean."""
    code = str((body or {}).get("code") or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="code required")
    try:
        res = db.table("surveys").select("survey_code").eq("id", survey_id).limit(1).execute()
    except Exception as e:  # noqa: BLE001
        log.error("[DB_ERR] verify_code %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="verification failed") from e
    if not res.data:
        raise HTTPException(status_code=404, detail="survey not found")
    expected = str(res.data[0].get("survey_code") or "").strip().upper()
    ok = bool(expected) and secrets.compare_digest(code, expected)
    # Persist the pass so any device the surveyor uses skips the code prompt.
    if ok:
        try:
            db.table("surveys").update(
                {"gate_verified_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", survey_id).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("[GATE] could not set gate_verified_at %s: %s", survey_id, e)
    return {"ok": ok}


@router.post("/{survey_id}/visit", status_code=201)
def record_visit(survey_id: str, body: dict, db: Client = Depends(get_db),
                 user: dict = Depends(get_current_user),
                 _sv: dict = Depends(authorize_survey)) -> dict:
    """Record the surveyor's GPS location for this survey (internal audit only)."""
    try:
        lat = float(body["lat"])
        lng = float(body["lng"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="lat and lng are required numbers")
    acc = body.get("accuracy")
    meta = user.get("user_metadata") or {}
    name = meta.get("full_name") or meta.get("name") or user.get("email")
    row = {
        "survey_id": survey_id,
        "surveyor_id": user.get("id"),
        "surveyor_name": name,
        "surveyor_email": user.get("email"),
        "lat": lat,
        "lng": lng,
        "accuracy": float(acc) if isinstance(acc, (int, float)) else None,
    }
    try:
        res = db.table("survey_visits").insert(row).execute()
    except Exception as e:  # noqa: BLE001
        log.error("[DB_ERR] record_visit %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not save location") from e
    # Cross-device gate flag: another device (e.g. the surveyor's phone) reads this
    # off the survey so it won't re-prompt for location. Best-effort — never fail the
    # visit if the flag write hiccups.
    try:
        db.table("surveys").update(
            {"gate_located_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", survey_id).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("[GATE] could not set gate_located_at %s: %s", survey_id, e)
    return {"ok": True, "id": (res.data or [{}])[0].get("id")}


@router.get("/{survey_id}", response_model=SurveyOut)
def get_survey(survey: dict = Depends(authorize_survey)) -> SurveyOut:
    """Surveyor screen loads this to know facility_type + chosen domains.
    authorize_survey both authorizes the caller and returns the row."""
    return SurveyOut(**survey)


@router.get("/{survey_id}/answers", status_code=200)
def get_answers(survey_id: str, db: Client = Depends(get_db),
                _sv: dict = Depends(authorize_survey)) -> dict:
    """Resume support: return saved answers so the surveyor can continue later."""
    try:
        res = (
            db.table("answers")
            .select("question_id,area,value,remark")
            .eq("survey_id", survey_id)
            .execute()
        )
    except Exception as e:
        log.error("[DB_ERR] get_answers %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="answer lookup failed") from e
    return {"answers": res.data or []}


@router.put("/{survey_id}/deployment", status_code=200)
def save_deployment(survey_id: str, body: dict, db: Client = Depends(get_db),
                    _sv: dict = Depends(authorize_survey)) -> dict:
    """Save the Staff Profile deployment grids (whole object replace)."""
    try:
        db.table("surveys").update({"deployment_plan": body}).eq("id", survey_id).execute()
    except Exception as e:
        log.error("[DB_ERR] save_deployment %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not save deployment") from e
    return {"ok": True}


@router.put("/{survey_id}/status", status_code=200)
def set_status(survey_id: str, body: dict, db: Client = Depends(get_db),
               _sv: dict = Depends(authorize_survey)) -> dict:
    """Set survey status (submitted|in_progress|ready|reported)."""
    status = body.get("status")
    if status not in {"submitted", "in_progress", "ready", "reported"}:
        raise HTTPException(status_code=400, detail="invalid status")
    try:
        db.table("surveys").update({"status": status}).eq("id", survey_id).execute()
    except Exception as e:
        log.error("[DB_ERR] set_status %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not set status") from e
    return {"ok": True}


@router.put("/{survey_id}/progress", status_code=200)
def save_progress(survey_id: str, body: dict, db: Client = Depends(get_db),
                  _sv: dict = Depends(authorize_survey)) -> dict:
    """Save section-completion map { section: true } for the sidebar (whole object replace)."""
    try:
        db.table("surveys").update({"progress": body}).eq("id", survey_id).execute()
    except Exception as e:
        log.error("[DB_ERR] save_progress %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not save progress") from e
    return {"ok": True}


@router.put("/{survey_id}/na", status_code=200)
def save_na_sections(survey_id: str, body: dict, db: Client = Depends(get_db),
                     _sv: dict = Depends(authorize_survey)) -> dict:
    """Replace the not-applicable section list.

    Body: { "na_sections": ["<area>||<domain>", ...] }. These sections are excluded
    from the AI prompt and the generated report (whole-list replace, not merge).
    """
    na = body.get("na_sections")
    if not isinstance(na, list) or not all(isinstance(x, str) for x in na):
        raise HTTPException(status_code=400, detail="na_sections must be a list of strings")
    try:
        db.table("surveys").update({"na_sections": na}).eq("id", survey_id).execute()
    except Exception as e:
        log.error("[DB_ERR] save_na_sections %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not save na_sections") from e
    return {"ok": True}


@router.post("/{survey_id}/answers", status_code=200)
def sync_answers(survey_id: str, body: AnswerSync, db: Client = Depends(get_db),
                 _sv: dict = Depends(authorize_survey)) -> dict:
    """
    Idempotent bulk upsert from the surveyor device (online or after offline sync).
    Conflict target (survey_id, question_id) -> re-sending never duplicates.
    """
    rows = [
        {
            "survey_id": survey_id,
            "question_id": a.question_id,
            "area": a.area,
            "value": a.value,
            "remark": a.remark,
            "client_uuid": a.client_uuid,
        }
        for a in body.answers
    ]
    # Start the survey timer on the first-ever answer. authorize_survey already
    # loaded the row, so we avoid an extra read; set-once guarded by the column
    # being null. Best-effort — a timer hiccup must never fail an answer sync.
    patch: dict = {"status": "in_progress"}
    if not _sv.get("first_answer_at"):
        patch["first_answer_at"] = datetime.now(timezone.utc).isoformat()
    try:
        db.table("answers").upsert(rows, on_conflict="survey_id,area,question_id").execute()
        db.table("surveys").update(patch).eq("id", survey_id).execute()
    except Exception as e:
        log.error("[DB_ERR] sync_answers survey=%s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="answer sync failed") from e
    return {"synced": len(rows)}


def _load_scoring_inputs(survey_id: str, db: Client) -> tuple[list[dict], dict[str, dict], set[str]]:
    """Shared loader for /health and /actions: answers, question map, na set.

    Question metadata is resolved via the v1/v2 union source (survey_questions
    snapshots, falling back to the shared bank), so both eras score. Areas stay as
    stored ids here — the frontend maps ids -> names for live display; only the
    server-rendered report translates them.
    """
    survey = db.table("surveys").select("na_sections").eq("id", survey_id).limit(1).execute()
    if not survey.data:
        raise HTTPException(status_code=404, detail="survey not found")
    na = set(survey.data[0].get("na_sections") or [])
    ans = db.table("answers").select("*").eq("survey_id", survey_id).execute().data or []
    q_ids = list({a["question_id"] for a in ans})
    questions = load_question_map(db, survey_id, q_ids) if q_ids else {}
    return ans, questions, na


@router.get("/{survey_id}/health", status_code=200)
def get_health(survey_id: str, db: Client = Depends(get_db),
               _sv: dict = Depends(authorize_survey)) -> dict:
    """Live deterministic health score (0-100) + per-domain breakdown."""
    try:
        answers, questions, na = _load_scoring_inputs(survey_id, db)
    except HTTPException:
        raise
    except Exception as e:
        log.error("[DB_ERR] get_health %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not compute health") from e
    return compute_health(answers, questions, na)


@router.get("/{survey_id}/actions", status_code=200)
def get_actions(survey_id: str, db: Client = Depends(get_db),
                _sv: dict = Depends(authorize_survey)) -> dict:
    """Live corrective-action list derived from failing/weak answers."""
    try:
        answers, questions, na = _load_scoring_inputs(survey_id, db)
    except HTTPException:
        raise
    except Exception as e:
        log.error("[DB_ERR] get_actions %s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="could not compute actions") from e
    return {"actions": derive_actions(answers, questions, na)}


@router.post("/{survey_id}/report", response_model=ReportOut)
async def make_report(
    survey_id: str,
    view: str = Query("domain", pattern="^(domain|area|both)$"),
    db: Client = Depends(get_db),
    _sv: dict = Depends(authorize_survey),
) -> ReportOut:
    """Generate the facility health report. view = domain | area | both."""
    try:
        report = await generate_report(db, survey_id, view)
    except ValueError as e:                 # internal logic / not-found
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:                  # external (Gemini/render) isolated
        log.error("[REPORT_ERR] survey=%s: %s", survey_id, e)
        raise HTTPException(status_code=502, detail="report generation failed") from e
    return report
