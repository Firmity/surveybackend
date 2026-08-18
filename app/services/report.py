"""
Report pipeline: load survey -> group answers by (area, domain) -> Gemini ->
render (domain/area/both view) -> upload -> store.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re

from supabase import Client

from datetime import datetime, timezone

from ..models import ReportOut
from .gemini import ReportContent, SectionFinding, generate_report_content
from .question_source import load_area_names, load_area_paths, load_question_map, translate_area, translate_na
from .render import render_docx, render_pdf
from .report_template import ContentConfig, merge_config
from .scoring import answer_units, compute_health, derive_actions, grade_value
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
            pts = grade_value(u["value"], u.get("good"))
            g.setdefault(key, []).append({
                "question": u["label"],
                "value": u["value"],
                "remark": u["remark"],
                "good_answer": u.get("good"),        # polarity for the PDF renderer
                "compliant": pts is not None and pts >= 1.0,  # for the AI narrative
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
    # Fetch photos via an RPC so the answer-id list travels in the POST *body*, not
    # the querystring. `in_(<hundreds of ids>)` produced a multi-KB URL (URL-length
    # risk + unreadable logs); the RPC eliminates the querystring entirely.
    # Requires the `get_photos_for_answers(uuid[])` SQL function (see db/migrations).
    try:
        ph = db.rpc("get_photos_for_answers", {"ids": list(aid_meta)}).execute()
        rows = ph.data or []
    except Exception as e:  # noqa: BLE001 - no photos rather than failing the report
        log.warning("[PHOTO_LOAD_ERR] %s", e)
        return {}
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

    # Collapse identical images: the SAME picture attached to several questions (or
    # re-uploaded) previously rendered once per row, so a single floor photo appeared
    # 3-4 times in one grid. Group by image content within each area+category and print
    # it ONCE, captioned with every question it was actually attached to.
    #   key -> image digest -> {"data": bytes, "captions": [question labels]}
    grouped: dict[str, dict[str, dict]] = {}
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
        digest = hashlib.md5(data).hexdigest()  # noqa: S324 - dedupe key, not security
        slot = grouped.setdefault(key, {}).setdefault(digest, {"data": data, "captions": []})
        if caption and caption not in slot["captions"]:
            slot["captions"].append(caption)

    out: dict[str, list[dict]] = {}
    for key, by_digest in grouped.items():
        out[key] = [
            {"question": "  ·  ".join(v["captions"]) or "Photo", "data": v["data"]}
            for v in by_digest.values()
        ]
    return out


# A bare uuid area key = an area that no longer exists (deleted after being marked N/A).
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

GEMINI_TIMEOUT_S = int(os.getenv("GEMINI_TIMEOUT_S", "120") or 120)
GEMINI_RETRIES = int(os.getenv("GEMINI_RETRIES", "3") or 3)
# Bound concurrent report generations so a burst can't exhaust the box or the LLM quota
# all at once. Each report does an LLM call + render + uploads. Tune via env.
_REPORT_SEM = asyncio.Semaphore(int(os.getenv("REPORT_CONCURRENCY", "4") or 4))


def _retry_after_seconds(e: Exception) -> int | None:
    """Parse Gemini's suggested retry delay from a 429 error so the UI can tell the
    user how long to wait. Tolerant of both 'retry in 17.0s' and 'retryDelay: 16s'."""
    s = str(e)
    m = re.search(r"retry in (\d+(?:\.\d+)?)s", s, re.IGNORECASE)
    if m:
        return int(float(m.group(1))) + 1
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+)", s)
    return int(m.group(1)) if m else None


def _last_ai_content(db: Client, survey_id: str) -> ReportContent | None:
    """The most recent AI-generated narrative for this survey, to reuse when the LLM
    quota is exhausted (so re-generating doesn't downgrade a report that previously had
    real AI text). None if none exists or on any read/parse error."""
    try:
        res = (db.table("reports").select("payload")
               .eq("survey_id", survey_id).order("generated_at", desc=True)
               .limit(5).execute())
    except Exception as e:  # noqa: BLE001 - reuse is best-effort
        log.warning("[GEMINI_REUSE] lookup failed for %s: %s", survey_id, e)
        return None
    for row in res.data or []:
        p = row.get("payload") or {}
        if not p.get("ai_generated"):
            continue
        try:
            return ReportContent(
                executive_summary=p.get("executive_summary", ""),
                overall_rating=p.get("overall_rating", ""),
                sections=p.get("sections", []),
                key_recommendations=p.get("key_recommendations", []),
            )
        except Exception:  # noqa: BLE001 - payload shape drift -> try the next one
            continue
    return None


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
                   if a.get("compliant") is False and a.get("question")]
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
    area_order: list[str] = []  # bound up-front so a stale file can never NameError

    survey_res = db.table("surveys").select("*").eq("id", survey_id).limit(1).execute()
    if not survey_res.data:
        raise ValueError(f"survey {survey_id} not found")
    survey = survey_res.data[0]

    ans_res = db.table("answers").select("*").eq("survey_id", survey_id).execute()
    answers = ans_res.data or []
    if not answers:
        raise ValueError("no answers to report on yet")

    # v2 answers reference survey_questions.id (the survey's snapshot folder);
    # legacy answers reference questions.id. Union both so both eras score/report.
    q_ids = list({a["question_id"] for a in answers})
    questions = load_question_map(db, survey_id, q_ids)

    # Areas are stored by id in v2. Translate answers' area + the na-keys to human
    # names ONCE here, so grouping, scoring, photo-matching and headings all key on
    # names — no changes needed downstream (render.py untouched). Legacy surveys
    # already store names and pass through unchanged.
    area_names = load_area_names(db, survey_id)
    area_paths, area_dfs = load_area_paths(db, survey_id)

    # Label an area by its breadcrumb PATH ("Building > Floor > Room") when it's a
    # tree node id — unique + ordered — falling back to the plain name for legacy
    # surveys and facility-level sections.
    def _area_label(raw: str | None) -> str:
        key = raw or "Common Areas"
        return area_paths.get(key) or translate_area(raw, area_names)

    for a in answers:
        a["area"] = _area_label(a.get("area"))
    na = set()
    for k in (survey.get("na_sections") or []):
        if "||" in k:
            ar, dom = k.split("||", 1)
            na.add(f"{_area_label(ar)}||{dom}")
        else:
            na.add(k)
    groups = _build_groups(answers, questions, na)
    # Tree order (Building > Floor > Room) for the renderer to sort sections by.
    area_order = [area_paths[i] for i in area_dfs if i in area_paths]
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
    retry_after: int | None = None
    try:
        content = await _gemini_content(survey, groups, health, content_cfg)
        ai_ok = True
    except Exception as e:  # noqa: BLE001 - quota/timeout/parse/any -> graceful fallback
        ai_ok = False
        if _is_quota_error(e):
            retry_after = _retry_after_seconds(e)
            prev = _last_ai_content(db, survey_id)
            if prev is not None:  # keep the AI narrative from the last good report
                log.warning("[GEMINI_REUSE] survey=%s quota hit — reusing last AI narrative (retry_after=%ss)",
                            survey_id, retry_after)
                content, ai_ok = prev, True
            else:
                log.warning("[GEMINI_FALLBACK] survey=%s quota, no prior AI content (retry_after=%ss)",
                            survey_id, retry_after)
                content = _fallback_content(survey, groups, health, actions)
        else:
            log.error("[GEMINI_FALLBACK] survey=%s: %s", survey_id, e)
            content = _fallback_content(survey, groups, health, actions)

    photos = await _load_photos(db, answers, questions, na)

    # Enrich the survey dict with cover metadata (surveyor, dates, duration, generated
    # stamp) so the renderer can print it without a second DB round-trip in the render
    # layer. Best-effort — a lookup failure degrades to None, never breaks the report.
    survey["_report_meta"] = _report_meta(db, survey)
    # Auditable list of sections the surveyor marked not-applicable (area||domain keys,
    # already translated to labels above). Entries whose area is STILL a bare uuid are
    # stale na_sections rows pointing at areas that were since deleted — they'd print as
    # meaningless ids, so drop them. Staff-self ('__self') markers are dropped too.
    survey["_excluded"] = sorted(
        k for k in na
        if "||" in k and not _UUID_RE.match(k.split("||", 1)[0].strip())
    )

    pdf_bytes = await asyncio.to_thread(
        render_pdf, content, survey, photos, view, health, actions, groups, template, area_order=area_order)
    docx_bytes = await asyncio.to_thread(
        render_docx, content, survey, photos, view, health, actions, groups, template, area_order=area_order)

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
    duration = _duration_seconds(survey.get("first_answer_at"), row.get("generated_at"))
    return ReportOut(**row, ai_generated=ai_ok, duration_seconds=duration, retry_after_seconds=retry_after)


def _report_meta(db: Client, survey: dict) -> dict:
    """Cover metadata for the report: surveyor name, survey start, generated stamp,
    and total duration (first answer -> now). Best-effort; each field independently
    degrades to None so a profiles miss never blocks report generation."""
    now = datetime.now(timezone.utc)
    surveyor: str | None = None
    uid = survey.get("assigned_to")
    if uid:
        try:
            r = db.table("profiles").select("*").eq("id", uid).limit(1).execute()
            if r.data:
                p = r.data[0]
                surveyor = p.get("full_name") or p.get("name") or p.get("email")
        except Exception as e:  # noqa: BLE001 - metadata is non-critical
            log.warning("[REPORT_META] surveyor lookup failed: %s", e)
    start = _parse_ts(survey.get("first_answer_at"))
    dur = int((now - start).total_seconds()) if start else None
    return {
        "surveyor": surveyor,
        "started_at": survey.get("first_answer_at"),
        "generated_at": now.isoformat(),
        "duration_seconds": dur,
    }


def _parse_ts(v) -> datetime | None:
    """Parse a Supabase ISO timestamp (tolerates a trailing 'Z'). None on failure."""
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(first_answer_at, generated_at) -> int | None:
    """Whole seconds from first answer -> report generated. None if either is unknown."""
    start, end = _parse_ts(first_answer_at), _parse_ts(generated_at)
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds()))
# na-sections exclusion active
