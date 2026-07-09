"""
Translation endpoint for the surveyor UI (questions, options, labels).

POST /translate  { "texts": [...], "lang": "hi" }  ->  { "translations": {src: dst} }

Auth-gated (same as the rest of the survey app) to prevent open translation abuse.
English / unsupported languages return the input unchanged.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth import get_current_user
from ..services.translate import LANGUAGES, translate_batch

log = logging.getLogger("translate_router")
router = APIRouter(prefix="/translate", tags=["translate"], dependencies=[Depends(get_current_user)])

MAX_TEXTS = 400  # generous ceiling for one survey page


class TranslateIn(BaseModel):
    texts: list[str] = Field(default_factory=list)
    lang: str = "en"


class TranslateOut(BaseModel):
    lang: str
    translations: dict[str, str]


@router.get("/languages")
def languages() -> dict[str, str]:
    """Supported language codes -> English name (for building the picker)."""
    return LANGUAGES


@router.post("", response_model=TranslateOut)
async def translate(body: TranslateIn) -> TranslateOut:
    texts = [t for t in body.texts if isinstance(t, str)][:MAX_TEXTS]
    mapping = await translate_batch(texts, body.lang)
    return TranslateOut(lang=body.lang, translations=mapping)
