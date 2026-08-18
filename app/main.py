"""FastAPI entrypoint. Run: uvicorn app.main:app --reload"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routers import questions, survey_content, surveys, templates, translate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# Third-party libraries that log ~150 INFO lines per PDF render (font subsetting)
# or per-request keep-alives. Raise their floor to WARNING so our own logs stay
# readable and debuggable. fpdf2 pulls fontTools; httpx logs every Supabase call.
for _noisy in ("fontTools", "fontTools.subset", "fontTools.ttLib"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

app = FastAPI(title="Facility Health Survey API", version="0.1.0")

settings = get_settings()
# Explicit origins (prod) PLUS a dev regex that allows localhost and any private-LAN
# origin, so a phone/tablet at http://192.168.x.x:3000 can reach the API during
# on-device testing without per-IP config.
_LAN_ORIGIN_RE = (
    r"^http://(localhost|127\.0\.0\.1|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?$"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_origin_regex=_LAN_ORIGIN_RE,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(questions.router)
app.include_router(questions.domains_router)
app.include_router(surveys.router)
app.include_router(survey_content.router)
app.include_router(templates.router)
app.include_router(translate.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}
