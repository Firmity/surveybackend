"""Unit tests for the Survey v2 pure logic (no DB / network).

Run:  pytest -s tests/test_survey_v2.py
These cover the deterministic helpers that back the per-property model:
area-id <-> name translation, subtree collection, boilerplate row shaping, and
report duration math. The DB-backed endpoints are exercised via integration
tests against a test Supabase project (out of scope here).
"""
from __future__ import annotations

from app.services.question_source import translate_area, translate_na
from app.services.report import _duration_seconds, _parse_ts
from app.routers.survey_content import _descendant_ids, _snapshot_rows


# ---- area id <-> name translation (report display) --------------------------
def test_translate_area_maps_id_to_name():
    names = {"uuid-1": "Main Building", "uuid-2": "Auditorium"}
    assert translate_area("uuid-1", names) == "Main Building"
    assert translate_area("uuid-2", names) == "Auditorium"


def test_translate_area_passthrough_for_legacy_and_blank():
    names = {"uuid-1": "Main Building"}
    # legacy answers already store a name -> unchanged
    assert translate_area("Site Profile", names) == "Site Profile"
    # blank/None -> default
    assert translate_area(None, names) == "Common Areas"
    assert translate_area("", names, default="X") == "X"


def test_translate_na_translates_only_area_part():
    names = {"uuid-1": "Main Building"}
    out = translate_na({"uuid-1||electrical", "Site Profile||general", "weird"}, names)
    assert "Main Building||electrical" in out
    assert "Site Profile||general" in out
    assert "weird" in out  # keys without '||' pass through


# ---- subtree collection (area delete cascade) -------------------------------
def _area(id_, parent):
    return {"id": id_, "parent_id": parent}


def test_descendant_ids_collects_whole_subtree():
    areas = [
        _area("b1", None),
        _area("a1", "b1"),
        _area("a2", "b1"),
        _area("a1a", "a1"),   # nested area within an area (arbitrary depth)
        _area("b2", None),    # unrelated sibling building
    ]
    got = _descendant_ids(areas, "b1")
    assert got == {"b1", "a1", "a2", "a1a"}
    assert "b2" not in got


def test_descendant_ids_leaf_is_just_itself():
    areas = [_area("b1", None), _area("a1", "b1")]
    assert _descendant_ids(areas, "a1") == {"a1"}


# ---- boilerplate row shaping (facility_type filter) -------------------------
def _bankq(text, fts, domain="electrical"):
    return {
        "id": f"q-{text}", "domain_slug": domain, "section": None, "text": text,
        "answer_type": "rating", "needs_photo": False, "facility_types": fts,
        "checklist": [], "sort_order": 1,
    }


def test_snapshot_rows_filters_by_facility_type():
    bank = [
        _bankq("all", []),                       # empty => applies to all
        _bankq("mfg-only", ["manufacturing"]),
        _bankq("res-only", ["residential"]),
    ]
    rows = _snapshot_rows(bank, "manufacturing", "survey-1", "area-1")
    texts = {r["text"] for r in rows}
    assert texts == {"all", "mfg-only"}          # res-only excluded
    # shape is a survey_questions insert row, snapshotting the origin id
    r = next(r for r in rows if r["text"] == "all")
    assert r["survey_id"] == "survey-1" and r["area_id"] == "area-1"
    assert r["source"] == "bank" and r["origin_question_id"] == "q-all"


def test_snapshot_rows_facility_level_uses_null_area():
    rows = _snapshot_rows([_bankq("x", [], domain="general")], "residential", "s1", None)
    assert rows[0]["area_id"] is None


# ---- report duration math ---------------------------------------------------
def test_duration_seconds_basic():
    start = "2026-07-16T10:00:00+00:00"
    end = "2026-07-16T12:30:00+00:00"
    assert _duration_seconds(start, end) == 2 * 3600 + 30 * 60


def test_duration_seconds_handles_z_suffix_and_missing():
    assert _duration_seconds("2026-07-16T10:00:00Z", "2026-07-16T10:01:00Z") == 60
    assert _duration_seconds(None, "2026-07-16T10:00:00Z") is None
    assert _duration_seconds("2026-07-16T10:00:00Z", None) is None
    # never negative even if clocks disagree
    assert _duration_seconds("2026-07-16T10:05:00Z", "2026-07-16T10:00:00Z") == 0


def test_parse_ts_bad_value_returns_none():
    assert _parse_ts("not-a-date") is None
    assert _parse_ts(None) is None
