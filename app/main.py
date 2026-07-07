"""FastAPI entrypoint. Run: uvicorn app.main:app --reload"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routers import questions, surveys, templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="Facility Health Survey API", version="0.1.0")

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(questions.router)
app.include_router(questions.domains_router)
app.include_router(surveys.router)
app.include_router(templates.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}
