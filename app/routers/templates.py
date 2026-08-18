"""
Report-template endpoints for the admin PDF editor.

Only two endpoints live here on purpose:
  GET  /templates/presets  -> the built-in themes + curated font list
  POST /templates/preview  -> render a SAMPLE report PDF with a posted template

Template load/save is done by the admin UI directly against Supabase (RLS
`is_admin()`), exactly like the questions editor — so the service-role backend
never becomes a way to bypass admin-only writes.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ..auth import get_current_user
from ..services.render import render_pdf
from ..services.report_template import CURATED_FONTS, PRESETS, merge_config, sample_render_inputs

log = logging.getLogger("templates")
router = APIRouter(prefix="/templates", tags=["templates"], dependencies=[Depends(get_current_user)])


@router.get("/presets")
def list_presets() -> dict:
    """Predefined themes and the curated font list the editor offers."""
    return {"presets": PRESETS, "fonts": CURATED_FONTS}


@router.post("/preview")
async def preview(body: dict) -> Response:
    """Render a sample report with the given template and return the PDF inline.

    Input:  { "config": <ReportTemplate JSON> }
    Output: application/pdf (a fixed synthetic facility, so no survey is needed)."""
    cfg = merge_config((body or {}).get("config")).model_dump()
    s = sample_render_inputs()
    try:
        pdf = await asyncio.to_thread(
            render_pdf, s["content"], s["survey"], s["photos"], s["view"],
            s["health"], s["actions"], s["groups"], cfg, area_order=s.get("area_order"))
    except Exception as e:  # noqa: BLE001 - external render isolated from the API layer
        log.error("[PREVIEW_ERR] %s", e)
        raise HTTPException(status_code=502, detail="preview render failed") from e
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "inline; filename=report-preview.pdf"})
