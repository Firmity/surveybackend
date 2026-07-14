"""
Report pipeline: load survey -> group answers by (area, domain) -> Gemini ->
render (domain/area/both view) -> upload -> store.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random

from supabase import Client

from ..models import ReportOut
from .gemini import ReportContent, SectionFinding, generate_report_content
from .render import render_docx, render_pdf
from .report_template import ContentConfig, merge_config
from .scoring import answer_units, compute_health, derive_actions
from .storage import download, upload_and_sign

log = logging.getLogger("report")

REPORTS_BUCKET = "reports"
PHOTOS_BUCKET = "survey-photos"
VALID_VIEWS = {"domain", "area", "both"}
COMMON = "Common Areas"


def _photo_path(stored: str) -> str:
    """photos.storage_url now holds the object path; legacy rows hold a full public URL."""
    marker = f"/{PHOTOS_BUCKET}/"
    if marker in stored:
        return stored.split(marker, 1)[1].split("?")[0]
    return stored


def _build_groups(
    answers: list[dict], questions: dict[str, dict], na: set[str] | None = None
) -> list[dict]:
    """[{area, domain, answers:[{question,value,remark}]}] — only answered questions.

    Sections the surveyor marked "not applicable" (na set of '<area>||<domain>')
    are excluded entirely so the AI never sees them and they never reach the report.
    """
    na = na or set()
    g: dict[tuple[str, str], list[dict]] = {}
    for a in answers:
        q = questions.get(a["question_id"])
        if not q:
            continue
        area, dom = a.get("area") or COMMON, q["domain_slug"]
        if f"{area}||{dom}" in na:
            continue
        key = (area, dom)
        # Expand checklist answers into one line per sub-question; skip empty units
        # and anything the surveyor marked Not Applicable (also excluded from scoring).
        for u in answer_units(a, q):
            if not (u["value"] or u["remark"]):
                continue
            if str(u.get("value") or "").strip().lower() in ("n/a", "na", "not applicable"):
                continue
            g.setdefault(key, []).append({
                "question": u["label"],
                "value": u["value"],
                "remark": u["remark"],
            })
    return [{"area": area, "domain": dom, "answers": ans} for (area, dom), ans in g.items()]


async def _load_photos(
    db: Client, answers: list[dict], questions: dict[str, dict], na: set[str] | None = None
) -> dict[str, list[dict]]:
    """Download photo bytes concurrently, keyed by '<area>||<domain>' for embedding.

    Photos in na sections are skipped so they don't appear in the rendered report.
    """
    na = na or set()
    aid_meta = {a["id"]: (a["question_id"], a.get("area") or COMMON) for a in answers}
    if not aid_meta:
        return {}
    try:
        ph = db.table("photos").select("answer_id,storage_url,sub_id").in_("answer_id", list(aid_meta)).execute()
    except Exception as e:
        log.warning("[PHOTO_LOAD_ERR] %s", e)
        return {}
    rows = ph.data or []
    if not rows:
        return {}

    async def fetch(r: dict) -> tuple[dict, bytes | None]:
        try:  # download from the PRIVATE bucket via the service-role key
            data = await asyncio.to_thread(download, db, PHOTOS_BUCKET, _photo_path(r["storage_url"]))
            return r, data
        except Exception as e:
            log.warning("[PHOTO_DL_ERR] %s: %s", r.get("storage_url"), e)
            return r, None

    results = await asyncio.gather(*(fetch(r) for r in rows))

    out: dict[str, list[dict]] = {}
    for r, data in results:
        if not data:
            continue
        qid, area = aid_meta.get(r["answer_id"], (None, None))
        q = questions.get(qid)
        if not q:
            continue
        key = f"{area}||{q['domain_slug']}"
        if key in na:
            continue
        # Caption sub-question photos with the parent + sub label.
        caption = q.get("text")
        sub_id = r.get("sub_id")
        if sub_id and q.get("answer_type") == "checklist":
            sub = next((s for s in (q.get("checklist") or []) if s.get("id") == sub_id), None)
            if sub:
                caption = f"{q.get('text')} - {sub.get('text')}"
        out.setdefault(key, []).append({"question": caption, "data": data})
    return out


GEMINI_TIMEOUT_S = int(os.getenv("GEMINI_TIMEOUT_S", "60") or 60)
GEMINI_RETRIES = int(os.getenv("GEMINI_RETRIES", "3") or 3)
# Bound concurrent report generations so a burst can't exhaust the box or the LLM quota
# all at once. Each report does an LLM call + render + uploads. Tune via env.
_REPORT_SEM = asyncio.Semaphore(int(os.getenv("REPORT_CONCURRENCY", "4") or 4))


def _is_quota_error(e: Exception) -> bool:
    """Daily/rate quota (429 RESOURCE_EXHAUSTED). Retrying within seconds only burns
    MORE of the same quota, so we fail fast to the deterministic fallback instead."""
    s = str(e).upper()
    return "429" in s or "RESOURCE_EXHAUSTED" in s or "QUOTA" in s


async def _gemini_content(
    survey: dict, groups: list[dict], health: dict, content: ContentConfig | None = None
) -> ReportContent:
    """Call Gemini with a per-attempt timeout + bounded exponential backoff.

    - Timeout or quota (429) -> raise immediately (caller falls back fast; retrying a
      daily quota just wastes more of it).
    - Other errors (transient 5xx / parse) -> retry with jittered backoff.
    Raises the last error if every attempt fails, so the caller can fall back."""
    last: Exception | None = None
    for attempt in range(GEMINI_RETRIES):
        try:
            return await asyncio.wait_for(
                generate_report_content(survey, groups, health, content), timeout=GEMINI_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise
        except Exception as e:  # noqa: BLE001 - transient LLM/network errors are retryable
            last = e
            if _is_quota_error(e):
                log.warning("[GEMINI_QUOTA] rate/daily limit hit — failing fast to fallback (no retry)")
                raise
            log.warning("[GEMINI_RETRY] attempt %d/%d failed: %s", attempt + 1, GEMINI_RETRIES, e)
            if attempt < GEMINI_RETRIES - 1:
                await asyncio.sleep(0.8 * (2 ** attempt) + random.uniform(0, 0.4))
    raise last if last else RuntimeError("gemini failed")


def _fallback_content(survey: dict, groups: list[dict], health: dict, actions: list[dict]) -> ReportContent:
    """Deterministic report content used when the LLM is unavailable (quota/timeout/error).

    The survey data, scores, corrective actions and photos are already saved and fully
    usable — this just fills the narrative fields so a complete report can still be
    produced without the surveyor having to redo anything."""
    grade = (health or {}).get("grade") or "Needs Attention"
    overall = (health or {}).get("overall")
    acts = actions or []
    n_high = sum(1 for a in acts if a.get("severity") == "high")
    score_txt = f"an overall health score of {overall}/100 ({grade})" if overall is not None else f"an overall rating of {grade}"
    summary = (
        f"This report was generated directly from the on-site survey data. The facility has "
        f"{score_txt}. {len(acts)} corrective action(s) were identified"
        + (f", {n_high} of them high priority" if n_high else "")
        + ". An automated narrative summary was temporarily unavailable when this report was "
        "produced; the scores, findings, corrective actions and photographs below are complete "
        "and were computed directly from the surveyor's answers."
    )
    sections: list[SectionFinding] = []
    for g in groups:
        area, dom = g.get("area") or COMMON, g.get("domain") or ""
        flagged = [a.get("question") for a in g.get("answers", [])
                   if str(a.get("value") or "").strip().lower() in ("unsatisfactory", "no") and a.get("question")]
        sections.append(SectionFinding(
            area=area, domain=dom, title=f"{dom} - {area}",
            observations=[], risks=[f"Flagged during survey: {q}" for q in flagged[:8]], recommendations=[]))
    key_recs = [a.get("action") for a in acts if a.get("severity") == "high" and a.get("action")][:6]
    if not key_recs:
        key_recs = [a.get("action") for a in acts if a.get("action")][:6]
    return ReportContent(executive_summary=summary, overall_rating=grade,
                         sections=sections, key_recommendations=key_recs)


def _load_default_template(db: Client) -> dict | None:
    """Load the admin's default PDF template config. Returns None on any failure so
    report generation always proceeds with the built-in theme (fail-open)."""
    try:
        res = db.table("report_templates").select("config").eq("is_default", True).limit(1).execute()
        if res.data:
            return res.data[0].get("config") or None
    except Exception as e:  # noqa: BLE001 - template table may not exist yet
        log.warning("[TEMPLATE_LOAD_ERR] %s", e)
    return None


async def generate_report(db: Client, survey_id: str, view: str = "domain") -> ReportOut:
    """Public entrypoint. Bounds concurrent generations so bursts don't overwhelm
    the box or the LLM quota; the heavy work runs in _generate_report_impl."""
    async with _REPORT_SEM:
        return await _generate_report_impl(db, survey_id, view)


async def _generate_report_impl(db: Client, survey_id: str, view: str = "domain") -> ReportOut:
    view = view if view in VALID_VIEWS else "domain"

    survey_res = db.table("surveys").select("*").eq("id", survey_id).limit(1).execute()
    if not survey_res.data:
        raise ValueError(f"survey {survey_id} not found")
    survey = survey_res.data[0]

    ans_res = db.table("answers").select("*").eq("survey_id", survey_id).execute()
    answers = ans_res.data or []
    if not answers:
        raise ValueError("no answers to report on yet")

    q_ids = list({a["question_id"] for a in answers})
    q_res = db.table("questions").select("*").in_("id", q_ids).execute()
    questions = {q["id"]: q for q in (q_res.data or [])}

    na = set(survey.get("na_sections") or [])
    groups = _build_groups(answers, questions, na)
    if not groups:
        raise ValueError("all answered sections are marked not-applicable; nothing to report")

    # Deterministic score + corrective actions (computed here, not by the LLM, so
    # they are reproducible and auditable).
    health = compute_health(answers, questions, na)
    actions = derive_actions(answers, questions, na)

    # Load the admin's default report template up-front: its ContentConfig (focus/tone/
    # length/audience or a free-form prompt) steers the AI narrative, and its styling
    # themes the PDF. None -> built-in defaults (current output unchanged).
    template = _load_default_template(db)
    content_cfg: ContentConfig = merge_config(template).content

    # LLM narrative is best-effort. If Gemini is exhausted/slow/errors, fall back to
    # deterministic content so the surveyor never has to redo the survey. Answers and
    # photos are already persisted, so nothing is lost.
    try:
        content = await _gemini_content(survey, groups, health, content_cfg)
        ai_ok = True
    except Exception as e:  # noqa: BLE001 - quota/timeout/parse/any -> graceful fallback
        log.error("[GEMINI_FALLBACK] survey=%s: %s", survey_id, e)
        content = _fallback_content(survey, groups, health, actions)
        ai_ok = False

    photos = await _load_photos(db, answers, questions, na)

    pdf_bytes = await asyncio.to_thread(
        render_pdf, content, survey, photos, view, health, actions, groups, template)
    docx_bytes = await asyncio.to_thread(
        render_docx, content, survey, photos, view, health, actions, groups, template)

    base = f"{survey_id}/report-{view}"
    pdf_url = docx_url = None
    try:
        pdf_url = upload_and_sign(db, REPORTS_BUCKET, f"{base}.pdf", pdf_bytes, "application/pdf")
        docx_url = upload_and_sign(
            db, REPORTS_BUCKET, f"{base}.docx", docx_bytes,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except Exception as e:
        log.error("[REPORT_UPLOAD_ERR] survey=%s: %s", survey_id, e)

    # Don't persist a "ready" report the user can't open. If BOTH files failed to
    # upload, fail loudly so the caller returns an error instead of a report row
    # with null URLs that the UI would auto-download into nothing.
    if pdf_url is None and docx_url is None:
        raise RuntimeError("[REPORT_UPLOAD_ERR] both PDF and DOCX uploads failed — not saving a fileless report")

    rep_res = db.table("reports").insert({
        "survey_id": survey_id,
        "payload": {"view": view, "health": health, "actions": actions,
                    "ai_generated": ai_ok, **content.model_dump()},
        "pdf_url": pdf_url,
        "docx_url": docx_url,
    }).execute()
    if not rep_res.data:
        raise RuntimeError("[DB_ERR] report insert returned no row")

    db.table("surveys").update({"status": "reported"}).eq("id", survey_id).execute()
    log.info("[REPORT] survey=%s view=%s sections=%d pdf=%s ai=%s",
             survey_id, view, len(content.sections), bool(pdf_url), ai_ok)
    row = {k: v for k, v in rep_res.data[0].items() if k != "payload"}
    return ReportOut(**row, ai_generated=ai_ok)
# na-sections exclusion active
