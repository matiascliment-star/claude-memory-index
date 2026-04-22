-- Schema for claude-memory-index V2.
-- Apply once per Supabase project.

create extension if not exists vector;

create or replace function memory_immutable_unaccent(text)
returns text
language sql
immutable
parallel safe
strict
as $$ select public.unaccent('public.unaccent'::regdictionary, $1) $$;

create table if not exists memory_turns (
  id bigserial primary key,
  turn_uuid text unique not null,
  session_id text not null,
  project text not null,
  machine_id text not null,
  role text not null check (role in ('user', 'assistant')),
  content text not null,
  "timestamp" timestamptz not null,
  cwd text,
  git_branch text,
  embedding vector(384),
  content_tsv tsvector generated always as (
    to_tsvector('spanish', memory_immutable_unaccent(coalesce(content, '')))
  ) stored,
  created_at timestamptz default now()
);

create index if not exists memory_turns_session_idx on memory_turns(session_id);
create index if not exists memory_turns_timestamp_idx on memory_turns("timestamp" desc);
create index if not exists memory_turns_project_idx on memory_turns(project);
create index if not exists memory_turns_machine_idx on memory_turns(machine_id);
create index if not exists memory_turns_fts_idx on memory_turns using gin(content_tsv);
create index if not exists memory_turns_embedding_idx on memory_turns using hnsw (embedding vector_cosine_ops);

create table if not exists memory_sessions (
  session_id text primary key,
  project text not null,
  machine_id text not null,
  cwd text,
  first_ts timestamptz not null,
  last_ts timestamptz not null,
  turn_count integer not null default 0,
  first_user_prompt text,
  jsonl_storage_path text,
  jsonl_mtime double precision,
  jsonl_size bigint,
  updated_at timestamptz default now()
);

create index if not exists memory_sessions_last_ts_idx on memory_sessions(last_ts desc);
create index if not exists memory_sessions_machine_idx on memory_sessions(machine_id);

create table if not exists memory_files_indexed (
  path text not null,
  machine_id text not null,
  mtime double precision not null,
  size bigint not null,
  last_line integer not null default 0,
  indexed_at timestamptz default now(),
  primary key (path, machine_id)
);

alter table memory_turns enable row level security;
alter table memory_sessions enable row level security;
alter table memory_files_indexed enable row level security;

-- RPC functions

create or replace function memory_search_fts(q text, lim int default 50)
returns table (id bigint, score real, session_id text)
language sql stable parallel safe as $$
  select id,
         ts_rank(content_tsv, plainto_tsquery('spanish', memory_immutable_unaccent(q)))::real as score,
         session_id
  from memory_turns
  where content_tsv @@ plainto_tsquery('spanish', memory_immutable_unaccent(q))
  order by score desc
  limit lim
$$;

create or replace function memory_search_vec(q_emb vector(384), lim int default 50)
returns table (id bigint, score real, session_id text)
language sql stable parallel safe as $$
  select id,
         (1 - (embedding <=> q_emb))::real as score,
         session_id
  from memory_turns
  where embedding is not null
  order by embedding <=> q_emb
  limit lim
$$;

create or replace function memory_refresh_all_sessions(mid text)
returns int
language plpgsql as $$
declare
  n int;
begin
  with agg as (
    select
      t.session_id,
      max(t.project) as project,
      t.machine_id,
      max(t.cwd) as cwd,
      min(t."timestamp") as first_ts,
      max(t."timestamp") as last_ts,
      count(*)::int as turn_count,
      (array_agg(t.content order by t."timestamp" asc) filter (where t.role = 'user'))[1] as first_user_prompt
    from memory_turns t
    where t.machine_id = mid
    group by t.session_id, t.machine_id
  )
  insert into memory_sessions as s
    (session_id, project, machine_id, cwd, first_ts, last_ts, turn_count, first_user_prompt, updated_at)
  select
    session_id, project, machine_id, cwd, first_ts, last_ts, turn_count,
    substring(coalesce(first_user_prompt, '') from 1 for 400),
    now()
  from agg
  on conflict (session_id) do update set
    project = excluded.project,
    machine_id = excluded.machine_id,
    cwd = excluded.cwd,
    first_ts = excluded.first_ts,
    last_ts = excluded.last_ts,
    turn_count = excluded.turn_count,
    first_user_prompt = excluded.first_user_prompt,
    updated_at = now();

  get diagnostics n = row_count;
  return n;
end
$$;

create or replace function memory_set_jsonl_path(sid text, path text, mtime double precision, size bigint)
returns void
language sql as $$
  update memory_sessions
  set jsonl_storage_path = path,
      jsonl_mtime = mtime,
      jsonl_size = size,
      updated_at = now()
  where session_id = sid
$$;

-- Storage bucket (run via UI or insert directly):
-- insert into storage.buckets (id, name, public, file_size_limit)
-- values ('claude-memory-jsonl', 'claude-memory-jsonl', false, 104857600)
-- on conflict (id) do nothing;
