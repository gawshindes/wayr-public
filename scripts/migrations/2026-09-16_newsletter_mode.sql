-- Newsletter mode: additive, idempotent migration for an EXISTING deployment.
-- Apply to every Supabase project running this branch BEFORE deploying it in
-- any mode — the pipeline's Supabase mirror writes eligible_at at discovery and
-- fails (best-effort, silently) until these columns exist.
--
--   Supabase dashboard → SQL editor → paste → Run.  Safe to re-run.
--
-- supabase_schema.sql is the from-scratch schema; this file is the delta.

alter table public.pipeline_articles
    add column if not exists importance jsonb,          -- {"score": 0-1, "reasoning": "...", "scored_at": "..."}
    add column if not exists eligible_at timestamptz;   -- the watermark key: when the story became publishable

-- Backfill: for existing rows the story was publishable when we first saw it.
update public.pipeline_articles
   set eligible_at = created_at
 where eligible_at is null;

create index if not exists pipeline_articles_eligible_at_idx
    on public.pipeline_articles (eligible_at desc);

create table if not exists public.newsletter_runs (
    id bigserial primary key,
    window_start timestamptz not null,
    window_end timestamptz not null,
    status text not null,          -- success | failed | empty | preview
    story_count int not null default 0,
    publisher text,
    external_id text,
    draft_url text,
    error text,
    region text,
    created_at timestamptz not null default now()
);
create index if not exists newsletter_runs_status_end_idx
    on public.newsletter_runs (status, window_end desc);
