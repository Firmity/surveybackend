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

# Default remediation copy per severity; the surveyor's remark (if any) is
# appended so the action stays specific.
_DEFAULT_ACTION = {
    "high": "Rectify the deficiency and re-inspect; assign an owner and target date.",
    "medium": "Schedule preventive maintenance to bring this up to standard.",
}


def _norm(v: Any) -> str:
    return str(v or "").strip().lower()


def grade_value(v: Any) -> Optional[float]:
    """Public: grade a single answer value -> 1.0 / 0.5 / 0.0, or None if not
    gradeable (numbers, text, N/A). Used by the report renderer for per-section
    scores and the rating-distribution chart."""
    return _GRADE.get(_norm(v))


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
            # Asset absent: a single finding (parent = No), scored + reported.
            # Sub-questions are meaningless when the item isn't there, so skip them.
            return [{
                "label": question.get("text", ""),
                "value": "No",
                "remark": rmap.get(GATE_KEY) or "",
            }]
        parent = question.get("text", "")
        units: list[dict[str, Any]] = []
        for sub in question.get("checklist") or []:
            sid = sub.get("id")
            units.append({
                "label": f"{parent} - {sub.get('text', '')}".strip(" -"),
                "value": vmap.get(sid),
                "remark": rmap.get(sid) or "",
            })
        return units
    return [{
        "label": question.get("text", ""),
        "value": answer.get("value"),
        "remark": answer.get("remark") or "",
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
            pts = _GRADE.get(_norm(u["value"]))
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
            v = _norm(u["value"])
            if v in _NEGATIVE:
                severity = "high"
            elif v in _WEAK:
                severity = "medium"
            else:
                continue
            remark = (u["remark"] or "").strip()
            out.append({
                "area": area,
                "domain": dom,
                "question": u["label"],
                "finding": u["value"],
                "severity": severity,
                "remark": remark,
                "action": f"{_DEFAULT_ACTION[severity]}"
                          + (f" Note: {remark}" if remark else ""),
            })

    order = {"high": 0, "medium": 1}
    out.sort(key=lambda x: (order.get(x["severity"], 9), x["area"], x["question"]))
    return out
