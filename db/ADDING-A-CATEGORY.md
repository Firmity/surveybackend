# How to add a survey category (domain) end-to-end

A "category" = a **domain** (slug + questions). Adding one touches **4 required
files** + **1 optional polish file**. Do them in this order. Every step is
copy-paste from the Technology Readiness example (`migration-technology.sql`).

Think of a domain like a new aisle in a shop: you (1) build the aisle and stock
it (DB), (2) add it to the master list of aisles the shop is allowed to have
(backend gate), (3) put it on the customer's pick-list (client form), and
(4) make sure the till knows the aisle's barcode (id→slug map).

---

## 1. Database — create the domain + its questions  *(required)*

New file `db/migration-<slug>.sql`, then run it in the **Supabase SQL editor**.

```sql
insert into domains (slug, name, sort_order) values
  ('technology', 'Technology Readiness', 10)
on conflict (slug) do update set name = excluded.name, sort_order = excluded.sort_order;

insert into questions (domain_slug, section, text, answer_type, needs_photo, facility_types, sort_order)
select v.* from (values
  ('technology','Technology Readiness','BMS installed and operational','yes_no',false,'{}'::text[],1),
  ('technology','Technology Readiness','IoT sensor coverage','rating',false,'{}'::text[],2)
  -- ...more rows
) as v(domain_slug, section, text, answer_type, needs_photo, facility_types, sort_order)
where not exists (select 1 from questions q where q.domain_slug=v.domain_slug and q.text=v.text);
```

Field rules:
- **slug**: lowercase, no spaces (this is the join key everywhere).
- **answer_type**: `rating` (Good/Satisfactory/Unsatisfactory), `yes_no`,
  `number`, `choice`, `text`, `checklist`. Only `rating`/`yes_no` feed the
  health score; the rest are descriptive.
- **facility_types**: `'{}'` = all facility types. Otherwise
  `'{residential,mixed_use}'`.
- The `where not exists` guard makes the migration **idempotent** (safe to re-run).

Add columns only if the domain is special:
- shown under every building → `update domains set is_key=true where slug='...';`
- hidden from surveyors for now → `update domains set is_active=false where slug='...';`

## 2. Backend gate — `app/models.py`  *(required)*

Add the slug to `SELECTABLE_DOMAINS`. **If you skip this, the client form POST
is rejected with 422** ("unknown domain_slugs").

```python
SELECTABLE_DOMAINS: frozenset[str] = frozenset({
    "security", "fire_safety", ..., "technology",
})
```

## 3. Client form — `src/app/facility-survey/book/page.tsx`  *(required)*

Add one row to `SURVEY_TYPES` (and import the icon at the top):

```tsx
{ id: "technology", label: "Technology Readiness", Icon: Cpu, desc: "BMS, IoT, CCTV, automation" },
```

`id` here is the **form id** (step 4 maps it to the slug). It can equal the slug.

## 4. id → slug map — `src/lib/survey-mapping.ts`  *(required)*

Add the form id → backend slug pair in `DOMAIN_MAP`:

```ts
technology: "technology",
```

If `id` already equals the slug this is a mirror line, but it is **still
required** — `mapDomains()` drops any id not present in this map.

## 5. Report polish — backend  *(optional but recommended)*

Without this the report shows the raw slug and no icon.

- `app/services/report_theme.py` → `CATEGORY_LABELS`: `"technology": "Technology Readiness",`
- `app/services/render.py` → `_ICON_FOR`: `"technology": "bolt",`
  (glyph keys: info, shield, flame, fan, bolt, drop, building, leaf, spark, doc, box, wrench)

---

## Why the report is automatically category-centric

You do **not** wire the report per category. The surveyor only ever loads
questions for `survey.domain_slugs` (`/questions/batch`), so only those get
answered; the report and AI prompt are built purely from the answers that exist.
Select **only** Technology Readiness → the survey shows only tech questions →
the report contains only tech findings. No extra step.

## Verify

```bash
python -m py_compile app/models.py          # backend gate
npm run build                                # frontend (from Firmity-Website-)
```
Then in Supabase run the migration, create a test survey with only the new
category, and confirm the surveyor sees only its questions.
