# Editing the report look yourself

All the report's **colours, category names, section titles/descriptions, and
branding** live in one file:

```
backend/app/services/report_theme.py
```

Edit that file, save, **restart the backend**, and generate a new report. No other
file needs changing, and you never have to ask a developer for these tweaks.

### 1. Change a colour
Colours are `(R, G, B)` numbers, 0–255. To use a hex colour (e.g. `#2F5CFF`),
convert it with any "hex to rgb" website → `(47, 92, 255)`.

```python
BLUE = (47, 92, 255)     # change this to recolour headings, the roadmap, links
LIME = (195, 245, 60)    # secondary accent on dark pages
INK  = (11, 11, 11)      # main text + dark section pages
CREAM = (250, 248, 240)  # page background
```
Status colours (`GREEN`, `AMBER`, `RED`) carry meaning — keep green = good,
red = problem, so the report stays clear.

### 2. Rename a category (in the report)
```python
CATEGORY_LABELS = {
    "plumbing": "Water Management",     # left = database slug, right = report title
    "housekeeping": "Housekeeping & Sanitation",
    ...
}
```
The left side is the fixed database slug; edit only the right side.

### 3. Change a section intro slide (title + description)
```python
SECTIONS = {
    "corrective": (
        "Section 02", "Corrective Action Plan",
        "The prioritised remediation plan - what needs fixing, how urgently ...",
    ),
    ...
}
```
Each is `(eyebrow, big title, one-line description)`. The colours come from the
palette above.

### 4. Branding
```python
BRAND = "Firmity"
REPORT_TITLE = "Facility Health Report"
BACK_COVER_LINE = "This report was generated from an on-site facility survey."
```

### Tips
- Keep it valid Python: strings in quotes, colours as `(r, g, b)` with commas.
- If a report fails to generate after an edit, you probably left off a quote or a
  comma — undo your last change and try again.
- Both the **PDF and Word** reports read these colours, so a change applies to both.
