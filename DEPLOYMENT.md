# Backend Deployment Guide

FastAPI service that powers the Facility Health Survey: answer sync, deterministic
scoring, and AI report generation (Gemini) rendered to PDF/DOCX and stored in
Supabase Storage. This doc covers env config, database migrations, running the
service, and a post-deploy checklist.

---

## 1. Architecture (one screen)

```
Next.js (Firmity site)  ──HTTPS──▶  FastAPI backend  ──▶ Gemini (report text)
   │  Supabase JS (auth,                │
   │  reads, photo upload)              ├──▶ Supabase Postgres (surveys/answers/...)
   ▼                                    └──▶ Supabase Storage (private: survey-photos, reports)
Supabase Auth  ◀── token validated by FastAPI on every /surveys/* call
```

- The **frontend** talks to Supabase directly for auth, dashboards, and photo
  uploads (service key stays server-side in Next API routes).
- The **backend** owns answer sync, scoring, and report generation. It validates
  the caller's Supabase JWT on every `/surveys/*` request.

---

## 2. Prerequisites

- Python 3.11+
- A Supabase project (Postgres + Auth + Storage)
- A Google Gemini API key
- Node 18+ for the frontend (deployed separately, e.g. Vercel)

---

## 3. Environment variables (backend `.env`)

`app/config.py` reads these **exact** names (aliases are case-sensitive):

| Variable | Required | Example / notes |
|---|---|---|
| `SUPABASE_URL` | yes | `https://xxxx.supabase.co` |
| `SUPABASE_SERVICE_KEY` | yes | service-role key (bypasses RLS — server only) |
| `SUPABASE_ANON_KEY` | yes | anon/publishable key — used to **validate** user tokens |
| `GEMINI_API_KEY` | yes | Google AI Studio key |
| `GEMINI_MODEL` | no | defaults to `gemini-2.5-flash` |
| `GEMINI_TIMEOUT_S` | no | per-attempt LLM timeout (seconds); defaults to `60` |
| `GEMINI_RETRIES` | no | retry attempts on transient LLM errors; defaults to `3` |
| `REPORT_CONCURRENCY` | no | max simultaneous report generations; defaults to `4` |
| `CORS_ORIGINS` | yes (prod) | comma-separated, e.g. `https://firmity.example,https://www.firmity.example` |

> If the LLM is exhausted/slow, report generation **falls back** to deterministic
> content (scores, findings, photos) so the surveyor never re-does the survey; the
> report is flagged `ai_generated:false` and can be regenerated later from the modal.

> **Gotcha:** the variable is `SUPABASE_ANON_KEY`, not `NEXT_PUBLIC_SUPABASE_ANON_KEY`.
> If it's missing, `/surveys/*` returns `500 [CONFIG_ERR] SUPABASE_ANON_KEY missing`.
> After editing `.env`, **restart uvicorn** — settings are cached with `lru_cache`.

Frontend `.env.local` (for reference — deployed with the Next app):

```
NEXT_PUBLIC_SUPABASE_URL=...
NEXT_PUBLIC_SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_KEY=...            # server-only (Next API routes)
NEXT_PUBLIC_API_BASE=https://api.firmity.example   # the FastAPI base URL
```

---

## 4. Database setup (run in Supabase SQL editor, in order)

Migrations are idempotent (`add column if not exists`, etc.) and safe to re-run.
Run the base schema + seed first, then the feature migrations.

**Base**
1. `db/schema.sql` — core tables (surveys, answers, questions, domains, photos, reports)
2. `db/seed.sql` + `db/seed_questions.json` — question bank + domains
   (`db/reseed.sql` re-loads the bank if you change questions)

**Structure & question-bank evolution**
3. `db/migration-areas.sql` — per-building areas
4. `db/migration-profiles.sql` — answer types + profile domains
5. `db/migration-keys.sql` — assessment Keys
6. `db/migration-inventory-sop.sql`
7. `db/migration-quant.sql`
8. `db/migration-merge.sql`
9. `db/migration-security-aids.sql`
10. `db/migration-categories.sql` — `domains.is_key` (admin-addable categories)
11. `db/tweaks.sql` — label/rename tweaks

**Staff, progress, status, not-applicable**
12. `db/migration-staff.sql` — `surveys.deployment_plan`
13. `db/migration-progress.sql` — `surveys.progress`  ← **required or Save fails silently**
14. `db/migration-status.sql` — allow `ready` status
15. `db/migration-na-sections.sql` — `surveys.na_sections`

**Auth, roles, awards, storage, scheduling**
16. `db/migration-roles-awards.sql` — `profiles`, `awards`, RLS, `is_admin()`, `handle_new_user` trigger
17. `db/migration-admin-rls.sql` — admin RLS on questions/domains
18. `db/storage-setup.sql` + `db/migration-private-buckets.sql` — buckets `survey-photos`, `reports` (private)
19. `db/migration-scheduling.sql` — `surveys.assigned_to`, `surveys.scheduled_at`

**Checklists, categories, report editor (latest)**
20. `db/migration-domain-active.sql` — `domains.is_active` (hide categories)
21. `db/migration-checklist.sql` — checklist sub-questions
22. `db/migration-report-templates.sql` — `report_templates` table  ← **required for the PDF Report Editor "Save"**
23. `db/migration-surveyor-location.sql` — `surveys.survey_code` + `survey_visits` table  ← **required for on-site GPS + code gating**

> If you're bringing an existing deployment up to date, at minimum run any of
> steps 13–23 you haven't applied yet, then **restart the backend** (settings are
> cached, and new routes/renderer only load on boot).

**Latest backend features that need a restart (no schema change):**
> The PDF-template renderer, Gemini retry/backoff + concurrency limits, and the
> `/templates/*` routes all ship in code — a plain **backend restart** activates them.

**Storage buckets:** ensure both `survey-photos` and `reports` exist and are
**private**. Signed URLs are minted by the backend (7-day expiry).

---

## 5. Install & run

**Install**
```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-report.txt              # fpdf2, python-docx, Pillow
```

**Dev**
```bash
uvicorn app.main:app --reload --port 8000
```

**Production** (bounded workers; report rendering is CPU-bound, so keep workers
modest and rely on async for I/O):
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 3
# or behind gunicorn:
gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 3 -b 0.0.0.0:8000 --timeout 60
```

Put it behind a reverse proxy (nginx/Caddy) terminating TLS. Health check:
`GET /health` → `{"status":"ok"}`.

**Container sketch**
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-report.txt
COPY app ./app
EXPOSE 8000
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--workers","3"]
```

---

## 6. CORS

Set `CORS_ORIGINS` to your exact frontend origin(s), comma-separated, **no
trailing slash**. Missing/incorrect origins show up as browser CORS errors on
`/surveys/*` calls from the site.

---

## 7. Post-deploy smoke test

1. `GET /health` → `200 {"status":"ok"}`.
2. From the site, sign in as staff and open a survey → answers load (validates
   `SUPABASE_ANON_KEY` token check).
3. Enter an answer → within ~1.5s it syncs (survey status flips to `in_progress`).
4. Open **Health** on a survey → score + corrective actions render (validates
   `/surveys/{id}/health` and `/actions`).
5. Generate a report → PDF + DOCX download links appear and open (validates
   Gemini + rendering + Storage signed URLs).

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `500 [CONFIG_ERR] SUPABASE_ANON_KEY missing` | Wrong env var name. Use `SUPABASE_ANON_KEY`. Restart uvicorn. |
| Save shows an error / checkmarks vanish on reopen | `migration-progress.sql` not applied. Run it, restart. |
| `503` on `/surveys/*` | Backend can't reach Supabase Auth — check `SUPABASE_URL` + network egress. |
| Report generation `502` | Gemini key/quota or Storage bucket missing/not private. Check logs `[REPORT_ERR]`, `[REPORT_UPLOAD_ERR]`. |
| Photos absent from report | `survey-photos` bucket missing or path mismatch. Check `[PHOTO_DL_ERR]`. |
| CORS errors in browser | `CORS_ORIGINS` doesn't match the site origin exactly. |
| `"all answered sections are marked not-applicable"` | Every answered section was flagged N/A; nothing to report. Expected. |

Logs use a structured taxonomy (`[DB_ERR]`, `[REPORT_ERR]`, `[PHOTO_DL_ERR]`,
`[CONFIG_ERR]`, …) — grep these to trace failures quickly.
