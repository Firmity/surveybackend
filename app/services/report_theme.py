"""
========================  REPORT THEME  —  EDIT THIS FILE  ========================
Change how the generated PDF/Word report LOOKS and READS from this ONE file.
After editing, restart the backend and generate a new report to see the change.
You do not need to touch any other file.

Colours are (Red, Green, Blue) tuples, each 0-255.
  To convert a hex colour like #2F5CFF -> R=0x2F=47, G=0x5C=92, B=0xFF=255 -> (47, 92, 255).
  (Any "hex to rgb" website does this in a second.)
==================================================================================
"""

# ---------------------------- 1. PALETTE ----------------------------
INK = (11, 11, 11)         # main text + dark panels/dividers
BLUE = (47, 92, 255)       # primary accent (heading bars, roadmap, links)
LIME = (195, 245, 60)      # secondary accent (eyebrows on dark pages)
CREAM = (250, 248, 240)    # page background
CARD = (255, 255, 255)     # cards / stat tiles
LINE = (223, 219, 205)     # thin separator lines
MUTED = (107, 107, 107)    # captions / secondary text

# Status colours — these carry MEANING. Change the hues if you like, but keep
# GREEN = good, RED = problem, so the report stays readable.
GREEN = (22, 163, 74)      # Good / healthy score
LIME_G = (101, 163, 13)    # good-ish score (70-84)
AMBER = (245, 158, 11)     # Satisfactory / medium priority
RED = (220, 38, 38)        # Unsatisfactory / high priority
SLATE = (148, 163, 184)    # Not Applicable

# Area-tree node colours — building / floor / room are colour-coded so the
# spatial hierarchy is distinct at a glance, in the report AND on the frontend.
# Depth 0 = building, 1 = floor, 2+ = room/area (deepest colour repeats for
# any extra nesting). Keep these in sync with the frontend palette (survey-api).
AREA_BUILDING = (30, 58, 95)    # #1E3A5F deep navy
AREA_FLOOR = (15, 118, 110)     # #0F766E teal
AREA_ROOM = (180, 83, 9)        # #B45309 amber

# ---------------------------- 2. BRANDING ----------------------------
BRAND = "Firmity"
REPORT_TITLE = "Facility Health Report"
BACK_COVER_LINE = "This report was generated from an on-site facility survey."

# ---------------------------- 3. CATEGORY NAMES (report only) ----------------------------
# How each category (domain slug) is TITLED in the report. Rename freely.
# A slug that isn't listed here just shows its raw slug.
CATEGORY_LABELS = {
    "general": "Site Details",
    "security": "Security Assessment",
    "fire_safety": "Fire Safety Audit",
    "hvac": "HVAC & Mechanical",
    "electrical": "Electrical Systems",
    "plumbing": "Water Management",
    "civil": "Civil & Structural",
    "horticulture": "Horticulture / Landscaping",
    "housekeeping": "Housekeeping & Sanitation",
    "green_building": "Green Building Survey",
    "technology": "Technology Readiness",
    "maintenance_manager": "Maintenance Manager",
    "technical_key": "Technical Key",
    "general_maintenance": "General Maintenance",
    "green_building_key": "Green Building Key",
    "client_pain_areas": "Client Pain Areas",
    "urest_suggestion": "UREST Suggestion",
    "sop_registers": "SOP, Logbook & Registers",
    "inventory": "Inventory",
}

# ---------------------------- 4. SECTION INTRO SLIDES ----------------------------
# Each section's full-page divider = (eyebrow, big title, one-line description).
# Edit the wording here; the colours come from the PALETTE above.
SECTIONS = {
    "buildings": (
        "Section 01", "Assessment by Building",
        "A detailed walkthrough of every building and category surveyed: the status of each "
        "item, key facts, risks, recommendations and supporting photographs.",
    ),
    "corrective": (
        "Section 02", "Corrective Action Plan",
        "The prioritised remediation plan - what needs fixing, how urgently, a recommended "
        "timeline, and the specific action for every deficiency found.",
    ),
    "key_recs": (
        "Section 03", "URest Recommendations",
        "The highest-impact actions to improve this facility's health, in priority order.",
    ),
    "appendix": (
        "Appendix", "Submitted Survey Form",
        "The complete record of every question and the response recorded on site, grouped by "
        "area - including any custom questions and checklist sub-questions the surveyor added.",
    ),
}
