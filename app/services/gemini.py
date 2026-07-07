"""
Gemini 2.5 Flash wrapper. Returns STRUCTURED JSON, not prose, so the report
layout stays deterministic. Findings are produced per (area, domain) group, which
lets the renderer organize the same content domain-wise OR area-wise.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from ..config import get_settings

log = logging.getLogger("gemini")


class SectionFinding(BaseModel):
    area: str        # tower name, or 'Common Areas'
    domain: str      # domain slug
    title: str       # human label, e.g. "Electrical Systems - Tower 1"
    observations: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class ReportContent(BaseModel):
    executive_summary: str
    overall_rating: str  # "Good" | "Fair" | "Needs Attention"
    sections: list[SectionFinding] = Field(default_factory=list)
    key_recommendations: list[str] = Field(default_factory=list)


_SYSTEM = (
    "You are a facilities engineering auditor. Produce a factual facility health "
    "report ONLY from the survey data provided. For EACH (area, domain) group given, "
    "output exactly one section with its `area` and `domain` set to match that group. "
    "Do NOT invent equipment, counts, or findings. If a group's data is sparse, say so "
    "rather than guessing. An objective health score (0-100) is provided; make your "
    "`overall_rating` and executive summary CONSISTENT with it (roughly: >=85 Good, "
    ">=50 Fair, else Needs Attention). Return strictly valid JSON matching the schema."
)


async def generate_report_content(
    survey: dict[str, Any], groups: list[dict], health: dict | None = None
) -> ReportContent:
    """
    groups: [{ "area": str, "domain": str, "answers": [{question,value,remark}] }, ...]
    health: optional {overall, grade, domains} so the AI's rating aligns with the
            deterministic score.
    Raises RuntimeError on unusable output.
    """
    s = get_settings()
    if not s.gemini_api_key:
        raise RuntimeError("[CONFIG_ERR] GEMINI_API_KEY missing")

    client = genai.Client(api_key=s.gemini_api_key)
    prompt = json.dumps({
        "facility": {
            "name": survey.get("facility_name"),
            "type": survey.get("facility_type"),
            "area": survey.get("total_area"),
            "unit": survey.get("area_unit"),
        },
        "health_score": health,  # objective score to align the narrative with
        "groups": groups,
    }, ensure_ascii=False)

    def _call() -> str:
        resp = client.models.generate_content(
            model=s.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM,
                response_mime_type="application/json",
                response_schema=ReportContent,
                temperature=0.3,
            ),
        )
        return resp.text or ""

    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(_call), timeout=s.gemini_timeout_s)
            if not raw.strip():
                raise RuntimeError("[LLM_RESPONSE_EMPTY]")
            log.info("[LLM_CALL] ok attempt=%d bytes=%d", attempt, len(raw))
            return ReportContent.model_validate_json(raw)
        except Exception as e:  # noqa: BLE001 - retry boundary
            last_err = e
            log.warning("[LLM_CALL] attempt=%d failed: %s", attempt, e)
    raise RuntimeError(f"[LLM_FAILED] {last_err}")
