"""
Translation with a persistent DB cache — powered by a FREE translation provider
(Google's public endpoint via `deep-translator`), NOT Gemini.

Why not Gemini: the LLM free tier is ~20 requests/day and is reserved for report
generation. A dedicated translator is free, has excellent Indian-language support,
and is order-preserving (we map results by POSITION, so nothing is dropped).

Flow (a phrasebook you keep adding to): for each string we look up `translations`
by sha256(source)+lang. Hits return instantly; misses are translated in ONE batch,
stored, and reused forever after. Any failure falls back to the English source.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

from deep_translator import GoogleTranslator

from ..db import get_db

log = logging.getLogger("translate")

# code -> English name. English is a passthrough (no translation).
LANGUAGES: dict[str, str] = {
    "en": "English",
    "hi": "Hindi",
    "bn": "Bengali",
    "te": "Telugu",
    "mr": "Marathi",
    "ta": "Tamil",
    "gu": "Gujarati",
    "kn": "Kannada",
    "ml": "Malayalam",
}

# Google truncates very long inputs; keep each string well under the limit.
_MAX_LEN = 4500


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_supported(lang: str) -> bool:
    return lang in LANGUAGES


def _provider_batch(texts: list[str], lang: str) -> dict[str, str]:
    """Translate a batch via GoogleTranslator (sync). Order-preserving -> map by index."""
    translator = GoogleTranslator(source="en", target=lang)
    safe = [t[:_MAX_LEN] for t in texts]
    try:
        out = translator.translate_batch(safe)
    except Exception as e:  # noqa: BLE001 - provider is external
        log.error("[TRANSLATE_PROVIDER_FAIL] lang=%s n=%d: %s", lang, len(texts), e)
        return {t: t for t in texts}
    result: dict[str, str] = {}
    for i, src in enumerate(texts):
        val = out[i] if isinstance(out, list) and i < len(out) else None
        result[src] = val if isinstance(val, str) and val.strip() else src
    return result


async def translate_batch(texts: list[str], lang: str) -> dict[str, str]:
    """
    Return {source: translated} for every input string.
    English/unsupported -> identity. DB cache first, provider for misses. Never raises.
    """
    uniq = [t for t in dict.fromkeys(texts) if t and t.strip()]
    if not uniq or lang == "en" or not is_supported(lang):
        return {t: t for t in texts}

    result: dict[str, str] = {}
    by_hash = {_hash(t): t for t in uniq}

    # 1) cache lookup
    try:
        rows = (
            get_db()
            .table("translations")
            .select("source_hash,translated_text")
            .in_("source_hash", list(by_hash.keys()))
            .eq("lang", lang)
            .execute()
            .data
            or []
        )
        for r in rows:
            src = by_hash.get(r["source_hash"])
            if src is not None:
                result[src] = r["translated_text"]
    except Exception as e:  # noqa: BLE001 - cache is best-effort
        log.warning("[TRANSLATE_CACHE_ERR] %s", e)

    # 2) translate misses off the event loop, then persist
    missing = [t for t in uniq if t not in result]
    if missing:
        fresh = await asyncio.to_thread(_provider_batch, missing, lang)
        result.update(fresh)
        _persist(fresh, lang)

    # 3) map back over the ORIGINAL list (including duplicates)
    return {t: result.get(t, t) for t in texts}


def _persist(mapping: dict[str, str], lang: str) -> None:
    """Best-effort upsert of new translations. Skips identity (untranslated) rows."""
    rows = [
        {"source_hash": _hash(src), "lang": lang, "source_text": src, "translated_text": tr}
        for src, tr in mapping.items()
        if tr and tr != src
    ]
    if not rows:
        return
    try:
        get_db().table("translations").upsert(rows, on_conflict="source_hash,lang").execute()
    except Exception as e:  # noqa: BLE001 - non-fatal
        log.warning("[TRANSLATE_PERSIST_FAIL] %s", e)
