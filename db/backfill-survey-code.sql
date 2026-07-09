-- One-time backfill: website bookings created before the persistSurvey fix have
-- survey_code = null (the code lived only in form_payload). Recover it where
-- present, otherwise mint a fresh one. Idempotent: only touches null/empty rows.
-- Run once in the Supabase SQL editor.

update surveys
set survey_code = coalesce(
    nullif(form_payload->>'surveyCode', ''),
    'FS-' || upper(substr(md5(random()::text), 1, 6))
)
where survey_code is null or survey_code = '';
