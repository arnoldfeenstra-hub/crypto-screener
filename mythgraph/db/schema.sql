-- MythGraph schema. Every content column is copied verbatim from Wikipedia /
-- Wikimedia Commons; provenance columns say exactly where.

create table if not exists ingest_runs (
  id           bigserial primary key,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  seed_count   int,
  node_count   int,
  edge_count   int,
  image_count  int,
  report       jsonb
);

create table if not exists nodes (
  id               text primary key,          -- slug of the canonical title
  title            text not null unique,      -- canonical en.wikipedia title
  type             text not null,
  type_source      text not null,             -- 'seed' (curated) | 'description' (Wikipedia short description)
  description      text,                      -- Wikipedia short description
  extract          text,                      -- lead section, plain text
  fun_fact         text,                      -- verbatim opening of a named section
  fun_fact_section text,
  generation       real,                      -- derived relative chronology; null = unplaced
  wiki_url         text not null,
  revision_id      bigint,                    -- revision the text was taken from (CC BY-SA attribution)
  run_id           bigint references ingest_runs(id),
  fetched_at       timestamptz not null default now()
);

create table if not exists images (
  node_id       text primary key references nodes(id) on delete cascade,
  file_title    text not null,
  thumb_url     text not null,
  thumb_width   int,
  thumb_height  int,
  original_url  text,
  commons_url   text not null,
  artist        text,
  date_text     text,
  license       text not null,                -- public domain / CC0 only
  credit        text,
  object_name   text
);

create table if not exists edges (
  source      text not null references nodes(id) on delete cascade,
  target      text not null references nodes(id) on delete cascade,
  rel         text not null,                  -- parent | consort | sibling | participant | associated
  provenance  text not null,                  -- e.g. 'infobox:parents@Zeus', 'lead-link@Trojan War'
  primary key (source, target, rel)
);

create index if not exists edges_target_idx on edges (target);
