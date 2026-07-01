-- Supabase (Postgres) schema for the internship pipeline — Phase 1.
-- Run once in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
-- Idempotent: safe to re-run.

create table if not exists public.jobs (
    dedupe_key    text primary key,         -- stable hash of the normalized URL
    company_name  text not null,
    title         text not null,
    url           text not null,
    locations     jsonb default '[]'::jsonb,
    date_posted   text,                      -- raw feed value (epoch or ISO); parsed later
    active        boolean not null default true,
    source        text,                      -- e.g. "greenhouse:stripe", "Simplify"
    source_feed   text,                      -- greenhouse | lever | ashby | simplify | jsearch
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now()
);

create index if not exists idx_jobs_first_seen on public.jobs (first_seen_at);
create index if not exists idx_jobs_active on public.jobs (active);

create table if not exists public.runs (
    run_id      text primary key,
    started_at  timestamptz not null,
    finished_at timestamptz,
    status      text,                        -- running | success | partial | failed
    counts      jsonb default '{}'::jsonb,
    errors      jsonb default '[]'::jsonb
);

-- This pipeline uses the service-role key (server-side, GitHub Actions secret),
-- which bypasses Row Level Security. If you later expose these tables to the
-- anon/public key, enable RLS and add explicit policies first.
