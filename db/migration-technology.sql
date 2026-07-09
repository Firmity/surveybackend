-- ============================================================
-- Migration: "Technology Readiness" category (domain) + dummy questions.
-- A client can select ONLY this domain; the surveyor then sees only these
-- questions and the AI report is naturally technology-centric (it only ever
-- receives technology answers).
-- Run once. Idempotent (safe to re-run).
-- ============================================================

-- 1) The category itself. is_active=true default, is_key=false default,
--    is_per_building=false default (a normal selectable domain, like Security).
insert into domains (slug, name, sort_order) values
  ('technology', 'Technology Readiness', 10)
on conflict (slug) do update set name = excluded.name, sort_order = excluded.sort_order;

-- 2) Dummy questions (edit/replace with the real bank later).
--    Answer types in use: 'rating' (Good/Satisfactory/Unsatisfactory),
--    'yes_no', 'number', 'choice', 'text'. facility_types '{}' = applies to all.
insert into questions (domain_slug, section, text, answer_type, needs_photo, facility_types, sort_order)
select v.domain_slug, v.section, v.text, v.answer_type, v.needs_photo, v.facility_types, v.sort_order
from (values
  ('technology','Technology Readiness','Building Management System (BMS) installed and operational','yes_no',false,'{}'::text[],1),
  ('technology','Technology Readiness','IoT sensor coverage across critical assets (pumps, DG, HVAC)','rating',false,'{}'::text[],2),
  ('technology','Technology Readiness','Preventive-maintenance software (CMMS) adoption','rating',false,'{}'::text[],3),
  ('technology','Technology Readiness','CCTV coverage with video analytics / AI surveillance','rating',false,'{}'::text[],4),
  ('technology','Technology Readiness','Access-control system type','choice',false,'{}'::text[],5),
  ('technology','Technology Readiness','Number of digital control / automation panels','number',false,'{}'::text[],6),
  ('technology','Technology Readiness','Wi-Fi / network coverage in common areas','rating',false,'{}'::text[],7),
  ('technology','Technology Readiness','Energy-monitoring dashboard availability','yes_no',false,'{}'::text[],8),
  ('technology','Technology Readiness','Data backup & cybersecurity posture','rating',false,'{}'::text[],9),
  ('technology','Technology Readiness','Resident / tenant mobile app for service requests','yes_no',false,'{}'::text[],10)
) as v(domain_slug, section, text, answer_type, needs_photo, facility_types, sort_order)
where not exists (
  select 1 from questions q where q.domain_slug = v.domain_slug and q.text = v.text
);
