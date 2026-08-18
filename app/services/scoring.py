"""
Deterministic facility scoring + corrective-action derivation.

Pure functions (no I/O) so they are unit-testable and reproducible: the same
answers always yield the same score and the same action list. Think of this as
the "grading rubric" applied to the surveyor's answers — separate from the LLM's
narrative, which is probabilistic.

Answer vocabulary (from the surveyor UI):
    rating  -> "Good" | "Satisfactory" | "Unsatisfactory"
    yes_no  -> "Yes" | "No" | "N/A" | "<number>"  (Numbered)
    text/number/remarks -> free-form (not graded)

Grading:
    Good / Yes            -> 1.0  (pass)
    Satisfactory          -> 0.5  (watch)
    Unsatisfactory / No   -> 0.0  (fail)
    N/A, numbers, text, "" -> not graded (excluded from the denominator)
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

# value(lowercased) -> points. Only these values contribute to the score.
_GRADE: dict[str, float] = {
    "good": 1.0,
    "yes": 1.0,
    "satisfactory": 0.5,
    "unsatisfactory": 0.0,
    "no": 0.0,
}
_NEGATIVE = {"unsatisfactory", "no"}   # high-severity corrective actions
_WEAK = {"satisfactory"}               # medium-severity (improvement) actions

# Checklist gate: reserved key in the value map holding the parent's Yes/No/N-A
# answer.
#   Yes -> expand the sub-questions.
#   No  -> the asset is ABSENT — a real finding, so it IS scored + reported
#          (a single parent-level unit); the sub-questions are skipped.
#   Not Applicable -> skip the whole checklist (nothing scored or reported).
GATE_KEY = "__gate"
_GATE_NA = {"n/a", "na", "not applicable"}

# NOTE: we deliberately do NOT emit generic filler like "rectify the problem" or
# "schedule preventive maintenance". Deterministic scoring can't know the real fix,
# so the corrective action carries the SPECIFIC finding + the surveyor's on-site
# remark; the actionable remediation comes from the AI recommendations.


def _norm(v: Any) -> str:
    return str(v or "").strip().lower()


def grade_value(v: Any, good: Any = None) -> Optional[float]:
    """Public: grade a single answer value -> 1.0 / 0.5 / 0.0, or None if not
    gradeable (numbers, text, N/A). Used by the report renderer for per-section
    scores and the rating-distribution chart.

    `good` is the question's compliant answer ('yes' | 'no'); for a Yes/No question
    where 'no' is the good answer (e.g. "Is there scrap on the floor?"), the score
    is inverted so a compliant 'No' grades 1.0 (pass), not 0.0. Ratings are
    unaffected (Good/Satisfactory/Unsatisfactory carry their own polarity)."""
    n = _norm(v)
    base = _GRADE.get(n)
    if base is None:
        return None
    if n in ("yes", "no") and str(good or "yes").strip().lower() == "no":
        return 1.0 - base  # invert: compliant No -> 1.0, non-compliant Yes -> 0.0
    return base


def _area_of(answer: dict) -> str:
    return answer.get("area") or "Common Areas"


def _load_map(raw: Any) -> dict:
    """Parse a checklist value/remark JSON map; tolerate bad/empty data."""
    if not raw:
        return {}
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def answer_units(answer: dict, question: dict) -> list[dict[str, Any]]:
    """The gradeable/reportable units of an answer.

    A checklist answer expands into one unit per sub-question (label
    'Parent - Sub', with the sub's own value + remark). Every other answer type
    yields a single unit. This is the ONE place checklist expansion lives, shared
    by scoring and report rendering.
    """
    if question.get("answer_type") == "checklist":
        vmap = _load_map(answer.get("value"))
        gate = _norm(vmap.get(GATE_KEY))
        if gate in _GATE_NA:
            return []  # Not Applicable -> skip the whole checklist
        rmap = _load_map(answer.get("remark"))
        if gate == "no":
            # Asset absent: a single finding (parent = No). "Not present" is inherently
            # a deficiency, so no polarity is applied (good=None -> No grades 0).
            return [{
                "label": question.get("text", ""),
                "value": "No",
                "remark": rmap.get(GATE_KEY) or "",
                "good": None,
            }]
        parent = question.get("text", "")
        units: list[dict[str, Any]] = []
        for sub in question.get("checklist") or []:
            sid = sub.get("id")
            units.append({
                "label": f"{parent} - {sub.get('text', '')}".strip(" -"),
                "value": vmap.get(sid),
                "remark": rmap.get(sid) or "",
                "good": sub.get("good_answer"),  # per sub-question polarity
            })
        return units
    return [{
        "label": question.get("text", ""),
        "value": answer.get("value"),
        "remark": answer.get("remark") or "",
        "good": question.get("good_answer"),  # per-question polarity
    }]


def _grade_label(score: Optional[int]) -> str:
    if score is None:
        return "Not scored"
    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 50:
        return "Fair"
    return "Poor"


def compute_health(
    answers: Iterable[dict],
    questions: dict[str, dict],
    na: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Return an overall 0-100 health score plus a per-domain breakdown.

    na: set of '<area>||<domain>' keys to exclude (not-applicable sections).
    Sections with no gradeable answers are omitted; overall is None if nothing
    could be graded (caller decides how to display that).
    """
    na = na or set()
    per_domain: dict[str, list[float]] = {}

    for a in answers:
        q = questions.get(a.get("question_id"))
        if not q:
            continue
        area, dom = _area_of(a), q["domain_slug"]
        if f"{area}||{dom}" in na:
            continue
        for u in answer_units(a, q):
            pts = grade_value(u["value"], u.get("good"))
            if pts is None:
                continue
            per_domain.setdefault(dom, []).append(pts)

    domains: list[dict[str, Any]] = []
    total_pts = 0.0
    total_n = 0
    for dom, pts in per_domain.items():
        s, n = sum(pts), len(pts)
        domains.append({"domain": dom, "score": round(s / n * 100), "graded": n})
        total_pts += s
        total_n += n

    overall = round(total_pts / total_n * 100) if total_n else None
    domains.sort(key=lambda d: d["score"])  # worst first — most actionable
    return {
        "overall": overall,
        "grade": _grade_label(overall),
        "graded": total_n,
        "domains": domains,
    }


def derive_actions(
    answers: Iterable[dict],
    questions: dict[str, dict],
    na: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """Corrective-action list from failing/weak answers, high-severity first.

    Each item: {area, domain, question, finding, severity, remark, action}.
    Deterministic ordering (severity, then area, then question) so reports are
    reproducible.
    """
    na = na or set()
    out: list[dict[str, Any]] = []

    for a in answers:
        q = questions.get(a.get("question_id"))
        if not q:
            continue
        area, dom = _area_of(a), q["domain_slug"]
        if f"{area}||{dom}" in na:
            continue
        for u in answer_units(a, q):
            pts = grade_value(u["value"], u.get("good"))
            if pts is None or pts >= 1.0:
                continue  # not gradeable, or compliant -> not a corrective action
            severity = "high" if pts <= 0.0 else "medium"
            remark = (u["remark"] or "").strip()
            # Specific, honest action text: lead with the surveyor's on-site remark
            # (the concrete detail) when present; otherwise reference the exact item
            # and its recorded finding. No generic filler.
            action = remark or f"Investigate and correct the cause of '{u['label']}' (recorded: {u['value']})."
            out.append({
                "area": area,
                "domain": dom,
                "question": u["label"],
                "finding": u["value"],
                "severity": severity,
                "remark": remark,
                "action": action,
            })

    order = {"high": 0, "medium": 1}
    out.sort(key=lambda x: (order.get(x["severity"], 9), x["area"], x["question"]))
    return out
