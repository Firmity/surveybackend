"""
Smoke tests for the report renderer (PDF + DOCX).

Idempotent, no DB and no network — everything runs off report_template.sample_render_inputs().
Run from the backend/ directory:

    python -m pytest tests/test_render.py -q

These guard the highest-risk surface (the renderer touches every report) against
regressions the truncating dev-sandbox mount tends to hide.
"""
import io
import random

import pytest
from docx import Document

from app.services import render as R
from app.services import report_template as rt
from app.services.gemini import ReportContent, SectionFinding
from app.services.render import render_docx, render_pdf

random.seed(42)  # deterministic — no unseeded randomness in assertions


def _inputs():
    return rt.sample_render_inputs()


def _args(s, template=None):
    return (s["content"], s["survey"], s["photos"], s["view"],
            s["health"], s["actions"], s["groups"], template)


# ----------------------------- template model -----------------------------
def test_presets_and_merge():
    assert len(rt.PRESETS) == 4
    assert rt.hex_to_rgb("#2f5cff") == (47, 92, 255)
    t = rt.merge_config({"palette": {"blue": "#ff0000"}, "spacing": 1.4})
    assert t.palette.blue == "#ff0000"
    assert t.spacing == 1.4
    # merge_config must never raise on bad user data (fail-open)
    assert rt.merge_config({"palette": {"blue": 123}}) is not None
    assert rt.merge_config(None) is not None


# ----------------------------- PDF -----------------------------
def test_pdf_default_is_valid_pdf():
    pdf = render_pdf(*_args(_inputs()))
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 20_000


@pytest.mark.parametrize("preset", rt.PRESETS, ids=[p["name"] for p in rt.PRESETS])
def test_pdf_each_preset_renders(preset):
    pdf = render_pdf(*_args(_inputs(), preset))
    assert pdf[:4] == b"%PDF"


def test_pdf_with_overlays_and_section_styles():
    cfg = rt.merge_config({
        "palette": {"blue": "#e8590c"},
        "branding": {"brand": "ACME FM"},
        "section_styles": {"buildings": {"bg": "#6c2bd9", "accent": "#f5d90a"}},
        "spacing": 1.3,
        "overlays": [{"id": "t", "page": "cover", "type": "text", "x": 20, "y": 150,
                      "w": 120, "text": "DRAFT", "size": 20, "color": "#dc2626", "opacity": 0.5}],
    }).model_dump()
    pdf = render_pdf(*_args(_inputs(), cfg))
    assert pdf[:4] == b"%PDF"


# ----------------------------- DOCX -----------------------------
def test_docx_default_has_tables():
    dx = render_docx(*_args(_inputs()))
    d = Document(io.BytesIO(dx))
    assert len(d.tables) > 0
    assert len(dx) > 20_000


# ----------------------------- resilience -----------------------------
def test_fallback_shaped_content_renders():
    """Content shaped like the deterministic fallback (empty recommendations) must render."""
    content = ReportContent(
        executive_summary="Generated directly from the on-site survey data.",
        overall_rating="Fair",
        sections=[SectionFinding(area="Tower A", domain="hvac", title="t",
                                 risks=["Flagged during survey: AHU filter"], recommendations=[])],
        key_recommendations=["Replace AHU filters"])
    s = _inputs()
    pdf = render_pdf(content, s["survey"], s["photos"], s["view"], s["health"], s["actions"], s["groups"])
    dx = render_docx(content, s["survey"], s["photos"], s["view"], s["health"], s["actions"], s["groups"])
    assert pdf[:4] == b"%PDF"
    assert len(dx) > 10_000


def test_theme_state_resets_between_renders():
    """Module-global theme state must not leak from one render into the next."""
    cfg = rt.merge_config({"palette": {"blue": "#123456"}, "spacing": 1.5,
                           "section_styles": {"cover": {"bg": "#000000"}}}).model_dump()
    render_pdf(*_args(_inputs(), cfg))
    assert R.SPACING == 1.0
    assert R._SECTION_STYLES == {}
    assert R.BLUE == R._DEFAULT_RGB["BLUE"]
