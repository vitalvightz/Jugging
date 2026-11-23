-- Optional exercise results for a manually logged training session
-- (PUT/DELETE /api/training/sessions/{id}/exercises/{entry_id}).
--
-- external_training_sessions stays the record of the occurrence and its
-- workload (activity, duration, effort). An entry here only says which movement
-- was performed and what the athlete chose to record for it, so nothing that
-- reads workload, readiness, XP, rehab exposure or sparring contact reads this
-- table. exercise_logs is untouched: it is keyed by plan/day/prescribed block
-- and its as_prescribed/modified statuses have no meaning for a coach's own
-- programme.
--
-- Identity:
--   exercise_key          a canonical catalogue movement, validated server-side.
--                         Same namespace as exercise_logs.exercise_key, so the
--                         athlete's Back Squat history includes both sources.
--   personal_exercise_id  a private movement the athlete named themselves.
--   Exactly one of the two. Names are never fuzzy-matched across identities.
--
-- Lifecycle:
--   client_entry_id is the retry key: a repeated save never creates a second
--   occurrence, and a tombstone stops a late retry resurrecting a deleted one.
--   revision orders edits: each save names the revision it was based on and the
--   backend applies it only if that is still current, so a delayed retry of an
--   older save can never overwrite a later correction.
--   training_day always mirrors the parent session, and soft-deleting the parent
--   soft-deletes its entries in the same statement.
--
-- Only the backend (service role) writes. Athletes may read their own live rows.
-- Repeatable: every step is guarded.

-- The composite key keeps every link inside the athlete's own records.
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'external_training_sessions_id_athlete_key'
      and conrelid = 'public.external_training_sessions'::regclass) then
    alter table public.external_training_sessions
      add constraint external_training_sessions_id_athlete_key unique (id, athlete_id);
  end if;
end $$;

create table if not exists public.personal_exercises (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 100 and name = btrim(name)),
  -- Case and whitespace only: "back  SQUAT" and "Back squat" are one name;
  -- "Pause back squat" is a different movement.
  name_key text not null check (char_length(name_key) between 1 and 100),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint personal_exercises_name_key unique (athlete_id, name_key),
  constraint personal_exercises_id_athlete_key unique (id, athlete_id)
);

create table if not exists public.manual_exercise_entries (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  external_session_id uuid not null,
  client_entry_id uuid not null,
  training_day date not null,
  exercise_key text check (
    exercise_key is null
    or (char_length(exercise_key) between 1 and 120 and exercise_key ~ '^[a-z0-9]+(-[a-z0-9]+)*$')),
  personal_exercise_id uuid,
  display_name text not null check (char_length(display_name) between 1 and 120),
  measurement_format text not null
    check (measurement_format in ('strength', 'bodyweight', 'isometric', 'intervals', 'distance')),
  -- Only the values the athlete entered, in exercise_logs.actual's shape.
  actual jsonb not null default '{}'::jsonb
    check (jsonb_typeof(actual) = 'object' and octet_length(actual::text) <= 4096),
  notes text not null default '' check (char_length(notes) <= 500),
  revision integer not null default 1 check (revision >= 1),
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint manual_exercise_entries_request_key unique (athlete_id, client_entry_id),
  constraint manual_exercise_entries_one_identity check ((exercise_key is null) <> (personal_exercise_id is null)),
  constraint manual_exercise_entries_session_fkey foreign key (external_session_id, athlete_id)
    references public.external_training_sessions(id, athlete_id) on delete cascade,
  constraint manual_exercise_entries_personal_fkey foreign key (personal_exercise_id, athlete_id)
    references public.personal_exercises(id, athlete_id) on delete cascade
);

create index if not exists manual_exercise_entries_session_idx
  on public.manual_exercise_entries (athlete_id, external_session_id, created_at)
  where deleted_at is null;
create index if not exists manual_exercise_entries_key_idx
  on public.manual_exercise_entries (athlete_id, exercise_key, training_day desc, id desc)
  where deleted_at is null and exercise_key is not null;
create index if not exists manual_exercise_entries_personal_idx
  on public.manual_exercise_entries (athlete_id, personal_exercise_id, training_day desc, id desc)
  where deleted_at is null and personal_exercise_id is not null;

-- An entry belongs to one live parent session for life: its day comes from the
-- parent, it cannot move between sessions or owners, and it is never undeleted.
-- New entries need a live parent and fewer than 30 live siblings; a retry of an
-- existing entry passes through to ON CONFLICT DO NOTHING untouched.
create or replace function public.guard_manual_exercise_entry()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  parent record;
  live_count integer;
begin
  if tg_op = 'INSERT' then
    if exists (select 1 from public.manual_exercise_entries
               where athlete_id = new.athlete_id and client_entry_id = new.client_entry_id) then
      return new;
    end if;
    select training_day, deleted_at into parent from public.external_training_sessions
      where id = new.external_session_id and athlete_id = new.athlete_id for update;
    if not found or parent.deleted_at is not null then
      raise exception 'manual training session not found' using errcode = 'P0002';
    end if;
    select count(*) into live_count from public.manual_exercise_entries
      where athlete_id = new.athlete_id and external_session_id = new.external_session_id and deleted_at is null;
    if live_count >= 30 then
      raise exception 'manual exercise limit reached' using errcode = 'P0001';
    end if;
    new.training_day := parent.training_day;
    new.deleted_at := null;
    new.revision := 1;
    return new;
  end if;
  new.athlete_id := old.athlete_id;
  new.external_session_id := old.external_session_id;
  new.client_entry_id := old.client_entry_id;
  new.created_at := old.created_at;
  new.deleted_at := coalesce(old.deleted_at, new.deleted_at);
  -- Revisions only move forward, one edit at a time.
  if new.revision is distinct from old.revision and new.revision <> old.revision + 1 then
    new.revision := old.revision;
  end if;
  select training_day into new.training_day from public.external_training_sessions
    where id = old.external_session_id and athlete_id = old.athlete_id;
  return new;
end
$$;

drop trigger if exists guard_manual_exercise_entry on public.manual_exercise_entries;
create trigger guard_manual_exercise_entry
  before insert or update on public.manual_exercise_entries
  for each row execute function public.guard_manual_exercise_entry();

drop trigger if exists set_manual_exercise_entries_updated_at on public.manual_exercise_entries;
create trigger set_manual_exercise_entries_updated_at
  before update on public.manual_exercise_entries
  for each row execute function public.set_updated_at();

drop trigger if exists set_personal_exercises_updated_at on public.personal_exercises;
create trigger set_personal_exercises_updated_at
  before update on public.personal_exercises
  for each row execute function public.set_updated_at();

-- A parent's date change or soft delete reaches its entries atomically, so no
-- visible performance outlives its session or sits on a stale date.
create or replace function public.sync_manual_exercise_entries()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.training_day is distinct from old.training_day or new.deleted_at is distinct from old.deleted_at then
    update public.manual_exercise_entries
      set training_day = new.training_day,
          deleted_at = case when new.deleted_at is not null then coalesce(deleted_at, new.deleted_at) else deleted_at end
      where external_session_id = new.id and athlete_id = new.athlete_id;
  end if;
  return new;
end
$$;

drop trigger if exists sync_manual_exercise_entries on public.external_training_sessions;
create trigger sync_manual_exercise_entries
  after update of training_day, deleted_at on public.external_training_sessions
  for each row execute function public.sync_manual_exercise_entries();

revoke all on function public.guard_manual_exercise_entry() from public, anon, authenticated;
revoke all on function public.sync_manual_exercise_entries() from public, anon, authenticated;

alter table public.personal_exercises enable row level security;
revoke all on public.personal_exercises from anon, authenticated;
grant select on public.personal_exercises to authenticated;
grant all on public.personal_exercises to service_role;
drop policy if exists personal_exercises_select_own on public.personal_exercises;
create policy personal_exercises_select_own on public.personal_exercises
  for select to authenticated using (athlete_id = (select auth.uid()));

alter table public.manual_exercise_entries enable row level security;
revoke all on public.manual_exercise_entries from anon, authenticated;
grant select on public.manual_exercise_entries to authenticated;
grant all on public.manual_exercise_entries to service_role;
drop policy if exists manual_exercise_entries_select_own on public.manual_exercise_entries;
create policy manual_exercise_entries_select_own on public.manual_exercise_entries
  for select to authenticated using (athlete_id = (select auth.uid()) and deleted_at is null);
