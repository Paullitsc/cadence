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
    source_feed   text,                      -- greenhouse | lever | ashby | simplify | jsearch | github_readme | landedhq
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

-- Phase 2: prepared (never auto-submitted) applications, keyed by job dedupe_key.
create table if not exists public.applications (
    dedupe_key           text primary key,   -- references public.jobs (dedupe_key)
    company_name         text not null,
    title                text not null,
    url                  text not null,
    fit_score            numeric not null default 0,
    keywords             jsonb default '[]'::jsonb,
    tailored_resume_path text,               -- rendered PDF path (nullable)
    tailored_resume_yaml text,               -- RenderCV YAML (auditable)
    drafted_answers      jsonb default '{}'::jsonb,  -- question -> answer
    human_review         boolean not null default false,
    status               text not null default 'pending_review',
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

create index if not exists idx_applications_status on public.applications (status);

-- Phase 3: drafted (never auto-sent) cold-outreach messages, one row per (job, channel).
-- channel='linkedin' is DRAFT-ONLY (LinkedIn automation is a ban-risk red zone).
create table if not exists public.outreach (
    outreach_id          text primary key,   -- make_outreach_id(dedupe_key, channel)
    dedupe_key           text not null,       -- references public.jobs (dedupe_key)
    company_name         text not null,
    title                text not null,
    url                  text not null,
    channel              text not null,       -- email | linkedin
    contact_name         text,
    contact_email        text,
    contact_title        text,
    contact_source       text not null default 'none',   -- hunter | apollo | pattern_guess | none
    contact_confidence   integer,             -- 0-100 (null for a pattern guess)
    contact_verified     boolean not null default false,
    contact_note         text,
    subject              text,                -- email only
    body                 text not null default '',
    status               text not null default 'pending_review',
    suppressed           boolean not null default false,
    human_review         boolean not null default false,
    used_llm             boolean not null default false,
    sent_at              text,
    provider_message_id  text,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

create index if not exists idx_outreach_status on public.outreach (status);
create index if not exists idx_outreach_dedupe on public.outreach (dedupe_key);

-- Phase 3: do-not-contact list, enforced before any send. An entry is a full email
-- address or a bare domain (opt out an entire company).
create table if not exists public.suppressions (
    entry       text primary key,   -- email address OR bare domain, lowercased
    reason      text,
    created_at  timestamptz not null default now()
);

-- Phase 5: durable CV artifacts + Gmail outreach drafts. `add column if not exists`
-- upgrades a database created in an earlier phase; the whole file stays idempotent.
alter table public.applications add column if not exists cv_drive_link text;
alter table public.outreach add column if not exists gmail_draft_id text;
alter table public.outreach add column if not exists gmail_draft_link text;

-- Phase 5: cross-run CV cache. cache_key = hash(selected bullet ids + keyword set);
-- a hit reuses the stored CV outright (no LLM tailoring call, no render, no upload).
create table if not exists public.cv_cache (
    cache_key            text primary key,
    tailored_resume_yaml text not null,
    cv_drive_link        text,
    drive_file_id        text,
    pdf_path             text,
    created_at           timestamptz not null default now()
);

-- CV review workflow (assignment 2026-07): the AI recommends which experience/
-- project bullets to keep; the human confirms in the local review app, and only
-- reviewed applications reach the tracker sheet. Re-run this file in the Supabase
-- SQL editor to pick these up (idempotent).
alter table public.applications add column if not exists recommended_bullets jsonb default '[]'::jsonb;
alter table public.applications add column if not exists final_bullets jsonb default '[]'::jsonb;
alter table public.applications add column if not exists reviewed_at text;
alter table public.cv_cache add column if not exists recommended_bullets jsonb default '[]'::jsonb;

-- This pipeline uses the service-role key (server-side, GitHub Actions secret),
-- which bypasses Row Level Security. If you later expose these tables to the
-- anon/public key, enable RLS and add explicit policies first.
