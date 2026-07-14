"""Pydantic schemas = the frontend<->backend contract. Stable keys, validated I/O."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

FacilityType = Literal[
    "manufacturing", "educational", "residential",
    "commercial", "healthcare", "hotel", "mixed_use",
]
AnswerType = Literal["choice", "text", "number", "yes_no", "rating", "remarks", "checklist"]

# The 9 form-selectable domains. 'general' (Site Profile) is always-on and is
# NOT chosen by the client, so it is intentionally excluded here.
SELECTABLE_DOMAINS: frozenset[str] = frozenset({
    "security", "fire_safety", "hvac", "electrical", "plumbing",
    "civil", "horticulture", "housekeeping", "green_building",
    "technology",
})


# ---------- Domains ----------
class Domain(BaseModel):
    slug: str
    name: str
    is_per_building: bool = False
    is_key: bool = False   # shown under every building (data-driven assessment Key)
    is_active: bool = True  # inactive categories are hidden from the surveyor
    sort_order: int = 0


# ---------- Questions ----------
class ChecklistItem(BaseModel):
    id: str
    text: str
    answer_type: Literal["yes_no", "rating", "number", "text"] = "yes_no"


class Question(BaseModel):
    id: str
    domain_slug: str
    section: Optional[str] = None
    text: str
    answer_type: AnswerType
    needs_photo: bool
    facility_types: list[str] = Field(default_factory=list)
    sort_order: int = 0
    checklist: list[ChecklistItem] = Field(default_factory=list)  # only for answer_type='checklist'


# ---------- Survey creation (from the website client form) ----------
class Block(BaseModel):
    name: str
    notes: Optional[str] = None


class PreferredDate(BaseModel):
    date: date
    window: Optional[str] = None  # e.g. "10:00-13:00"


class Contact(BaseModel):
    first_name: str
    last_name: str
    designation: Optional[str] = None
    email: EmailStr
    phone: str
    alternate_phone: Optional[str] = None


class SurveyCreate(BaseModel):
    facility_type: FacilityType
    domain_slugs: list[str] = Field(min_length=1)
    facility_name: Optional[str] = None
    facility_address: Optional[str] = None
    total_area: Optional[float] = Field(default=None, gt=0)
    area_unit: Optional[Literal["sqft", "acres"]] = None
    blocks: list[Block] = Field(default_factory=list)
    preferred_dates: list[PreferredDate] = Field(default_factory=list)
    contact: Contact
    form_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("domain_slugs")
    @classmethod
    def _validate_domains(cls, v: list[str]) -> list[str]:
        """Reject unknown/typo'd slugs early, and de-duplicate while preserving order.
        Fails loudly (422) instead of silently storing a slug that yields no questions."""
        bad = [s for s in v if s not in SELECTABLE_DOMAINS]
        if bad:
            raise ValueError(
                f"unknown domain_slugs: {bad}. allowed: {sorted(SELECTABLE_DOMAINS)}"
            )
        return list(dict.fromkeys(v))  # dedupe, keep order


class SurveyOut(BaseModel):
    # Pydantic ignores extra columns (form_payload, updated_at, ...) by default.
    id: str
    facility_type: str
    domain_slugs: list[str]
    facility_name: Optional[str] = None
    facility_address: Optional[str] = None
    total_area: Optional[float] = None
    area_unit: Optional[str] = None
    blocks: list[dict[str, Any]] = Field(default_factory=list)      # [{name, ...}] towers/blocks
    preferred_dates: list[dict[str, Any]] = Field(default_factory=list)
    contact: dict[str, Any] = Field(default_factory=dict)           # {first_name, designation, ...}
    deployment_plan: dict[str, Any] = Field(default_factory=dict)   # Staff Profile grids (manager-entered)
    progress: dict[str, Any] = Field(default_factory=dict)          # { section name: true } completion
    na_sections: list[str] = Field(default_factory=list)            # ['<area>||<domain>'] excluded from AI/report
    gate_located_at: Optional[datetime] = None                      # on-site GPS confirmed (cross-device gate)
    gate_verified_at: Optional[datetime] = None                     # survey code confirmed (cross-device gate)
    status: str
    created_at: datetime


# ---------- Answers (surveyor; offline-sync friendly) ----------
class AnswerIn(BaseModel):
    question_id: str
    area: str = "Common Areas"          # tower name, or 'Common Areas' for site-wide
    value: Optional[str] = None
    remark: Optional[str] = None
    client_uuid: Optional[str] = None  # device idempotency key


class AnswerSync(BaseModel):
    answers: list[AnswerIn] = Field(min_length=1)


# ---------- Report ----------
class ReportOut(BaseModel):
    id: str
    survey_id: str
    pdf_url: Optional[str] = None
    docx_url: Optional[str] = None
    share_token: Optional[str] = None
    generated_at: datetime
    ai_generated: bool = True  # False => produced from the deterministic fallback (LLM was unavailable)
