"""
Report template model — the single contract shared by the admin PDF editor
(frontend) and the fpdf2 renderer (backend).

Flow:  admin editor  ->  ReportTemplate (JSON)  ->  report_templates table
                     ->  render.apply_template()  ->  themed PDF/DOCX

Everything the editor can change lives here: palette, fonts, branding text,
section intro copy, category label overrides, and free-form overlay elements
(text / shapes / images / icons) positioned in PDF millimetres.

Coordinates are LANDSCAPE A4 millimetres (297 x 210), origin top-left — the
same coordinate space the renderer draws in, so what the editor shows maps 1:1
to the generated PDF.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

PAGE_W_MM = 297.0
PAGE_H_MM = 210.0

# Curated Google-Fonts set exposed in the editor. Only fonts whose .ttf files are
# bundled under app/assets/fonts get embedded in the PDF; others fall back to the
# default family (Work Sans) in the PDF while still previewing correctly on screen.
CURATED_FONTS = [
    "Work Sans", "Inter", "Roboto", "Open Sans", "Lato", "Montserrat", "Poppins",
    "Merriweather", "Playfair Display", "Archivo", "Source Sans 3", "Nunito",
    "Raleway", "DM Sans", "Space Grotesk",
]


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    """'#2f5cff' | '2f5cff' -> (47, 92, 255). Falls back to black on bad input."""
    try:
        s = h.lstrip("#")
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (ValueError, IndexError, TypeError):
        return (0, 0, 0)


class Palette(BaseModel):
    ink: str = "#0b0b0b"        # main text + dark panels
    blue: str = "#2f5cff"       # primary accent
    lime: str = "#c3f53c"       # secondary accent (dark pages)
    cream: str = "#faf8f0"      # page background
    card: str = "#ffffff"       # cards / tiles
    line: str = "#dfdbcd"       # separator lines
    muted: str = "#6b6b6b"      # captions / secondary text
    green: str = "#16a34a"      # Good
    lime_g: str = "#65a30d"     # good-ish (70-84)
    amber: str = "#f59e0b"      # Satisfactory / medium
    red: str = "#dc2626"        # Unsatisfactory / high
    slate: str = "#94a3b8"      # N/A


class Fonts(BaseModel):
    heading: str = "Work Sans"
    body: str = "Work Sans"


class Branding(BaseModel):
    brand: str = "Firmity"
    report_title: str = "Facility Health Report"
    back_cover_line: str = "This report was generated from an on-site facility survey."


class SectionText(BaseModel):
    eyebrow: str
    title: str
    description: str


class Sections(BaseModel):
    buildings: SectionText = SectionText(
        eyebrow="Section 01", title="Assessment by Building",
        description=("A detailed walkthrough of every building and category surveyed: the status "
                     "of each item, key facts, risks, recommendations and supporting photographs."))
    corrective: SectionText = SectionText(
        eyebrow="Section 02", title="Corrective Action Plan",
        description=("The prioritised remediation plan - what needs fixing, how urgently, a "
                     "recommended timeline, and the specific action for every deficiency found."))
    key_recs: SectionText = SectionText(
        eyebrow="Section 03", title="Key Recommendations",
        description="The highest-impact actions to improve this facility's health, in priority order.")


# Canonical section keys — used for per-section styling AND overlay anchoring.
SECTION_KEYS = ["cover", "overview", "exec_summary", "buildings", "facility_overview",
                "building", "corrective", "key_recs", "back"]

OverlayType = Literal["text", "rect", "line", "image"]
OverlayPage = Literal["cover", "overview", "exec_summary", "buildings", "facility_overview",
                      "building", "corrective", "key_recs", "back", "all"]


class SectionStyle(BaseModel):
    """Per-section colour overrides. None = inherit the section's palette default."""
    accent: Optional[str] = None    # eyebrow / bar / highlight
    bg: Optional[str] = None        # full-page background (divider/cover/back sections)
    text: Optional[str] = None      # primary text colour on that section


class Overlay(BaseModel):
    """A free-form element the editor drops on the canvas. mm coordinates."""
    id: str
    page: OverlayPage = "cover"
    type: OverlayType = "text"
    x: float = 20.0
    y: float = 20.0
    w: float = 60.0
    h: float = 16.0
    # text
    text: Optional[str] = None
    font: Optional[str] = None
    size: float = 16.0          # pt
    color: str = "#0b0b0b"
    align: Literal["left", "center", "right"] = "left"
    bold: bool = False
    # rect / line
    fill: Optional[str] = None
    stroke: Optional[str] = None
    stroke_w: float = 0.6
    radius: float = 0.0
    # image / icon (data URL 'data:image/png;base64,...' or https URL)
    src: Optional[str] = None
    opacity: float = 1.0        # 0..1


class ReportTemplate(BaseModel):
    name: str = "Modern Editorial"
    palette: Palette = Field(default_factory=Palette)
    fonts: Fonts = Field(default_factory=Fonts)
    branding: Branding = Field(default_factory=Branding)
    sections: Sections = Field(default_factory=Sections)
    section_styles: dict[str, SectionStyle] = Field(default_factory=dict)  # per-section colour overrides
    spacing: float = 1.0            # vertical spacing multiplier for responses (0.8..1.6)
    category_labels: dict[str, str] = Field(default_factory=dict)
    overlays: list[Overlay] = Field(default_factory=list)


def merge_config(cfg: Optional[dict]) -> ReportTemplate:
    """Validate a partial/stored config into a full ReportTemplate (defaults fill gaps).

    Never raises on user data — invalid fields fall back to defaults so a bad
    template can never break report generation."""
    if not cfg:
        return ReportTemplate()
    try:
        return ReportTemplate.model_validate(cfg)
    except Exception:  # noqa: BLE001 - be permissive; defaults keep reports rendering
        base = ReportTemplate()
        for key in ("name", "category_labels"):
            if key in cfg:
                setattr(base, key, cfg[key])
        return base


# --------------------------------------------------------------------------- presets
def _preset(name: str, pal: dict, heading: str, body: str, brand: str = "Firmity") -> dict:
    t = ReportTemplate(name=name, palette=Palette(**pal), fonts=Fonts(heading=heading, body=body),
                       branding=Branding(brand=brand))
    return t.model_dump()


PRESETS: list[dict] = [
    _preset("Modern Editorial",
            {"ink": "#0b0b0b", "blue": "#2f5cff", "lime": "#c3f53c", "cream": "#faf8f0",
             "muted": "#6b6b6b"},
            heading="Work Sans", body="Work Sans"),
    _preset("Corporate Professional",
            {"ink": "#1a2332", "blue": "#1e4e8c", "lime": "#c8a15a", "cream": "#ffffff",
             "card": "#f5f7fa", "line": "#d9dee7", "muted": "#5a6472", "green": "#2e7d47",
             "lime_g": "#3f8f5a", "amber": "#c98a1a", "red": "#b03a3a", "slate": "#8592a6"},
            heading="Merriweather", body="Inter"),
    _preset("Minimal Mono",
            {"ink": "#111111", "blue": "#111111", "lime": "#111111", "cream": "#ffffff",
             "card": "#fafafa", "line": "#e5e5e5", "muted": "#8a8a8a", "green": "#111111",
             "lime_g": "#444444", "amber": "#7a7a7a", "red": "#111111", "slate": "#bdbdbd"},
            heading="Inter", body="Inter"),
    _preset("Bold Vibrant",
            {"ink": "#161032", "blue": "#6c2bd9", "lime": "#f5d90a", "cream": "#fff9f0",
             "card": "#ffffff", "line": "#efe0d8", "muted": "#6b5f7a",
             "green": "#0ca678", "lime_g": "#37b24d", "amber": "#f59f00", "red": "#e8590c",
             "slate": "#adb5bd"},
            heading="Archivo", body="DM Sans"),
]


def sample_render_inputs() -> dict:
    """Synthetic report inputs for the live-preview endpoint (no DB needed).

    Returns kwargs for render_pdf: content, survey, photos, view, health, actions, groups.
    Import ReportContent lazily to avoid a heavy import at module load."""
    from .gemini import ReportContent, SectionFinding

    sec = SectionFinding(
        area="Tower A", domain="hvac", title="HVAC & Mechanical - Tower A",
        observations=["Two of three AHUs operational."],
        risks=["Cooling tower scaling reduces efficiency."],
        recommendations=["Schedule chemical descaling within two weeks."])
    content = ReportContent(
        executive_summary=("The facility is in good overall condition with strong housekeeping and "
                           "security. HVAC and water management need near-term attention."),
        overall_rating="Good",
        sections=[sec],
        key_recommendations=["Descale cooling tower", "Add perimeter lighting", "Replace AHU filters"])
    survey = {"facility_name": "Sample Facility", "facility_type": "commercial",
              "total_area": 50000, "area_unit": "sqft", "facility_address": "Sample Address",
              "contact": {"first_name": "Sample", "last_name": "Manager", "email": "manager@example.com"},
              "deployment_plan": {}}
    health = {"overall": 78, "grade": "Good",
              "domains": [{"domain": "hvac", "score": 68, "graded": 6},
                          {"domain": "security", "score": 88, "graded": 5},
                          {"domain": "housekeeping", "score": 91, "graded": 7},
                          {"domain": "plumbing", "score": 61, "graded": 4}]}
    actions = [
        {"severity": "high", "domain": "hvac", "area": "Tower A", "finding": "AHU filters clogged",
         "question": "AHU filter condition?", "action": "Replace all AHU filters"},
        {"severity": "high", "domain": "plumbing", "area": "Tower A", "finding": "Leak at riser",
         "question": "Riser condition?", "action": "Seal and re-pressure-test riser"},
        {"severity": "medium", "domain": "security", "area": "Tower B", "finding": "Weak lighting",
         "question": "Perimeter lighting adequate?", "action": "Add two flood fixtures"}]
    groups = [
        {"area": "Site Profile", "domain": "general",
         "answers": [{"question": "Total built-up area", "value": "50000 sqft", "remark": ""},
                     {"question": "Number of floors", "value": "12", "remark": ""}]},
        {"area": "Tower A", "domain": "hvac",
         "answers": [{"question": "AHU filter condition?", "value": "unsatisfactory", "remark": ""},
                     {"question": "Chiller status?", "value": "good", "remark": ""},
                     {"question": "Rated tonnage", "value": "200 TR", "remark": "2 units"}]},
        {"area": "Tower A", "domain": "plumbing",
         "answers": [{"question": "Riser condition?", "value": "unsatisfactory", "remark": ""},
                     {"question": "Pump room housekeeping?", "value": "satisfactory", "remark": ""}]},
        {"area": "Tower B", "domain": "security",
         "answers": [{"question": "Perimeter lighting adequate?", "value": "satisfactory", "remark": ""},
                     {"question": "CCTV coverage?", "value": "good", "remark": ""}]},
    ]
    return {"content": content, "survey": survey, "photos": {}, "view": "domain",
            "health": health, "actions": actions, "groups": groups}
