"""Resolve answer references to question metadata + area names (v1/v2 bridge).

Why this exists:
    v2 answers reference `survey_questions.id` (the survey's own snapshot folder),
    while legacy answers reference `questions.id` (the shared bank). Scoring and
    the report only need the question's metadata (domain_slug, text, answer_type,
    checklist), so we union both sources — survey_questions winning — and callers
    never care which era an answer came from.

    Area nodes are stored by UUID; answers/na-keys reference those ids in v2.
    For a human-readable report we map id -> name here in one place.

Pure-ish I/O helpers (Supabase reads only); no router imports, so any layer can
use them without an import cycle.
"""
from __future__ import annotations

import logging
from typing import Iterable

from supabase import Client

log = logging.getLogger("question_source")


def load_question_map(
    db: Client, survey_id: str, question_ids: Iterable[str]
) -> dict[str, dict]:
    """Map each answer's question_id -> its question dict.

    Looks in `survey_questions` first (v2 snapshots), then fills any misses from
    the shared `questions` bank (legacy surveys). Returns {} for no ids.
    """
    ids = list({q for q in question_ids if q})
    if not ids:
        return {}
    out: dict[str, dict] = {}
    try:
        # Fetch by survey_id (one short URL) instead of a giant id=in.(...) list —
        # a survey has a bounded number of question instances, so this is cheaper and
        # avoids URL-length limits when a survey has hundreds of answers.
        sq = (
            db.table("survey_questions").select("*")
            .eq("survey_id", survey_id).execute()
        )
        for r in sq.data or []:
            out[r["id"]] = r
    except Exception as e:  # noqa: BLE001 - fall through to bank on any read error
        log.warning("[QSRC] survey_questions read failed %s: %s", survey_id, e)

    missing = [i for i in ids if i not in out]
    if missing:
        try:
            bank = db.table("questions").select("*").in_("id", missing).execute()
            for r in bank.data or []:
                out[r["id"]] = r
        except Exception as e:  # noqa: BLE001
            log.warning("[QSRC] bank read failed: %s", e)
    return out


def load_area_names(db: Client, survey_id: str) -> dict[str, str]:
    """Map survey_areas.id -> name for display translation. Empty on error."""
    try:
        res = (
            db.table("survey_areas").select("id,name")
            .eq("survey_id", survey_id).execute()
        )
        return {r["id"]: r["name"] for r in (res.data or [])}
    except Exception as e:  # noqa: BLE001
        log.warning("[QSRC] area name read failed %s: %s", survey_id, e)
        return {}


def load_area_paths(db: Client, survey_id: str) -> tuple[dict[str, str], list[str]]:
    """Return (paths, dfs_order) for the survey's area tree.

    paths:     area_id -> breadcrumb label, e.g. "Main Building > 1st Floor > Cabin 1".
    dfs_order: area ids in tree order (building, then its floors, then their rooms),
               so the report groups rooms under their building/floor instead of
               sorting names alphabetically (and duplicate names like "Bathroom" on
               every floor no longer collide, since the full path is unique).
    """
    try:
        rows = (
            db.table("survey_areas").select("id,parent_id,name,sort_order")
            .eq("survey_id", survey_id).execute()
        ).data or []
    except Exception as e:  # noqa: BLE001
        log.warning("[QSRC] area tree read failed %s: %s", survey_id, e)
        return {}, []

    by_id = {r["id"]: r for r in rows}
    children: dict[str | None, list[dict]] = {}
    for r in rows:
        children.setdefault(r.get("parent_id"), []).append(r)
    for arr in children.values():
        arr.sort(key=lambda r: (r.get("sort_order") or 0, str(r.get("name") or "")))

    def path_of(r: dict) -> str:
        parts, cur, seen = [], r, set()
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            parts.append(str(cur.get("name") or ""))
            cur = by_id.get(cur.get("parent_id"))
        return " > ".join(reversed(parts))

    paths: dict[str, str] = {}
    order: list[str] = []

    def walk(parent: str | None) -> None:
        for r in children.get(parent, []):
            paths[r["id"]] = path_of(r)
            order.append(r["id"])
            walk(r["id"])

    walk(None)
    return paths, order


def translate_area(area: str | None, names: dict[str, str], default: str = "Common Areas") -> str:
    """id -> display name. Legacy areas are already names (not in `names`) and pass
    through unchanged; unknown/blank -> default."""
    a = area or default
    return names.get(a, a)


def translate_na(na: Iterable[str], names: dict[str, str]) -> set[str]:
    """Translate '<area>||<domain>' keys' area part id -> name so na-filtering
    matches answers whose area has also been translated to a name."""
    out: set[str] = set()
    for key in na or ():
        if "||" in key:
            area, dom = key.split("||", 1)
            out.add(f"{translate_area(area, names)}||{dom}")
        else:
            out.add(key)
    return out
