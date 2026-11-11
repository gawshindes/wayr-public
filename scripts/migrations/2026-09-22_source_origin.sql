-- Typed sources: additive, idempotent migration for an EXISTING deployment.
-- Apply to every Supabase project running this branch BEFORE running prework —
-- the mirror writes source_origin at discovery, and PostgREST rejects a row with
-- an unknown column, so until this runs EVERY discovery batch is dropped from
-- the mirror (logged at ERROR, but the pipeline itself carries on).
--
--   Supabase dashboard → SQL editor → paste → Run.  Safe to re-run.
--
-- supabase_schema.sql is the from-scratch schema; this file is the delta.

alter table public.pipeline_articles
    add column if not exists source_origin text;   -- 'press' | 'brand', from the sources file origin= token
