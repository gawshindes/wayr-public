-- WAYR Supabase schema — runtime tables only. FROM-SCRATCH: `create table if not
-- exists` does not add columns to a table that already exists. For an existing
-- project, apply scripts/migrations/*.sql in date order instead.
-- Run this on a fresh Supabase project (prod or staging) to provision the
-- tables the pipeline and eval tooling read/write.
--
-- Tables:
--   pipeline_articles           — one row per article, upserted stage-by-stage
--                                 by SupabasePipelineStore.
--   eval_industry_classifier    — hand-labeled eval set for industry classifier
--   eval_news_classifier        — hand-labeled eval set for news classifier
--   eval_category_classifier    — hand-labeled eval set for category classifier
--   eval_author                 — hand-labeled eval set for author agent
--
-- Historical tables (post_records, source_documents, eval_labels) are
-- intentionally omitted — they were artifacts of earlier labeling flows and
-- the current codebase does not reference them.

-- ─────────────────────────────────────────────────────────────────────────
-- Parent table first — every eval_* table FK-references this.
-- ─────────────────────────────────────────────────────────────────────────
create table if not exists public.pipeline_articles (
    url_hash text primary key,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    published_at timestamptz,
    rss_published_at timestamptz,

    region text,                   -- 'us' | 'india' — set at discovery from pipeline region
    title text,
    source_url text,
    source_tag text,
    source_origin text,                -- 'press' | 'brand', from the sources file origin= token
    rss_raw jsonb,
    raw_content text,

    status text,
    last_stage text,

    industry_classification boolean,
    news_classification boolean,
    article_type text,
    categories jsonb,

    -- Newsletter mode (docs/newsletter-mode-plan.md)
    importance jsonb,              -- {"score": 0-1, "reasoning": "...", "scored_at": "..."}
    eligible_at timestamptz,       -- the watermark key: when the story became publishable

    author_output jsonb,
    summary_output jsonb,        -- written by record_edited() — bundles summary-agent output and (when enabled) proofreader output

    wordpress_post_id bigint,
    wordpress_url text
);

create index if not exists pipeline_articles_status_idx
    on public.pipeline_articles (status);
create index if not exists pipeline_articles_last_stage_idx
    on public.pipeline_articles (last_stage);
create index if not exists pipeline_articles_created_at_idx
    on public.pipeline_articles (created_at desc);
create index if not exists pipeline_articles_source_tag_idx
    on public.pipeline_articles (source_tag);
create index if not exists pipeline_articles_region_idx
    on public.pipeline_articles (region);
create index if not exists pipeline_articles_eligible_at_idx
    on public.pipeline_articles (eligible_at desc);


-- ─────────────────────────────────────────────────────────────────────────
-- Newsletter run log — mirror of data/newsletter_runs.json (which is
-- authoritative). The watermark is max(window_end) where status = 'success'.
-- ─────────────────────────────────────────────────────────────────────────
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
    mail_id text,                  -- what the mailer did (NEWSLETTER_MAILER), if any
    mail_status text,
    mail_error text,
    region text,
    created_at timestamptz not null default now()
);
create index if not exists newsletter_runs_status_end_idx
    on public.newsletter_runs (status, window_end desc);


-- ─────────────────────────────────────────────────────────────────────────
-- Eval label tables — each keyed by (url_hash, prompt_version) so the same
-- article can be re-labeled under different prompt versions without
-- overwriting.
-- ─────────────────────────────────────────────────────────────────────────

create table if not exists public.eval_industry_classifier (
    url_hash text not null,
    prompt_version text not null,
    expected_result boolean,
    reviewer_comment text,
    reviewed_at timestamptz,
    created_at timestamptz not null default now(),
    constraint eval_industry_classifier_pkey primary key (url_hash, prompt_version),
    constraint fk_eval_ic_pipeline foreign key (url_hash)
        references public.pipeline_articles (url_hash) on delete cascade
);


create table if not exists public.eval_news_classifier (
    url_hash text not null,
    prompt_version text not null,
    expected_result boolean,
    reviewer_comment text,
    reviewed_at timestamptz,
    created_at timestamptz not null default now(),
    constraint eval_news_classifier_pkey primary key (url_hash, prompt_version),
    constraint fk_eval_nc_pipeline foreign key (url_hash)
        references public.pipeline_articles (url_hash) on delete cascade
);


create table if not exists public.eval_category_classifier (
    url_hash text not null,
    prompt_version text not null,
    expected_primary_category text,
    expected_secondary_category text,
    reviewer_comment text,
    reviewed_at timestamptz,
    created_at timestamptz not null default now(),
    constraint eval_category_classifier_pkey primary key (url_hash, prompt_version),
    constraint fk_eval_cc_pipeline foreign key (url_hash)
        references public.pipeline_articles (url_hash) on delete cascade
);


create table if not exists public.eval_author (
    url_hash text not null,
    prompt_version text not null,
    structure_followed text,
    source_accuracy text,
    made_up_content text,
    reviewer_comment text,
    reviewed_at timestamptz,
    created_at timestamptz not null default now(),
    constraint eval_author_pkey primary key (url_hash, prompt_version),
    constraint fk_eval_au_pipeline foreign key (url_hash)
        references public.pipeline_articles (url_hash) on delete cascade
);
