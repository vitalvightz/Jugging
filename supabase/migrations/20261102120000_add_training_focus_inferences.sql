-- Inferred training focus: one row per note (sparring log notes, manual
-- session takeaway, plan completion notes), stored apart from the note itself.
--
-- source_revision is a hash of the note the row was generated for. Claiming a
-- row for a new revision replaces the old inference; a result is written only
-- while the row still holds its revision, so a slow call never overwrites a
-- newer note. The same revision is never generated twice (a stuck pending row
-- is reclaimable after 5 minutes, a failed one after an hour).
--
-- Written by the API's service role only; athletes have no direct access.
-- Repeatable: every step is guarded.

create table if not exists public.training_focus_inferences (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  source_kind text not null check (source_kind in ('sparring_log', 'manual_takeaway', 'plan_completion')),
  source_id uuid not null,
  training_day date not null,
  source_revision text not null,
  status text not null default 'pending' check (status in ('pending', 'ready', 'none', 'failed')),
  reason text not null default '',
  focus text check (focus is null or char_length(focus) <= 90),
  scope text check (scope is null or scope in ('specific_exercise', 'movement_pattern', 'general_quality', 'sport_technique')),
  category text,
  sport text,
  exercise_targets jsonb not null default '[]'::jsonb,
  movement_patterns jsonb not null default '[]'::jsonb,
  model text,
  input_tokens integer,
  output_tokens integer,
  generated_at timestamptz,
  dismissed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint training_focus_inferences_source_key unique (athlete_id, source_kind, source_id)
);

create index if not exists training_focus_inferences_ready_idx
  on public.training_focus_inferences (athlete_id, training_day desc)
  where status = 'ready' and dismissed_at is null;
create index if not exists training_focus_inferences_generated_idx
  on public.training_focus_inferences (athlete_id, generated_at)
  where generated_at is not null;

drop trigger if exists set_training_focus_inferences_updated_at on public.training_focus_inferences;
create trigger set_training_focus_inferences_updated_at
  before update on public.training_focus_inferences
  for each row execute function public.set_updated_at();

alter table public.training_focus_inferences enable row level security;
revoke all on public.training_focus_inferences from anon, authenticated;
grant all on public.training_focus_inferences to service_role;

create or replace function public.claim_training_focus_inference(
  p_athlete_id uuid, p_source_kind text, p_source_id uuid, p_training_day date, p_source_revision text)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_id uuid;
begin
  insert into public.training_focus_inferences as t
    (athlete_id, source_kind, source_id, training_day, source_revision, status)
  values (p_athlete_id, p_source_kind, p_source_id, p_training_day, p_source_revision, 'pending')
  on conflict (athlete_id, source_kind, source_id) do update
    set source_revision = excluded.source_revision, training_day = excluded.training_day,
        status = 'pending', reason = '', focus = null, scope = null, category = null, sport = null,
        exercise_targets = '[]'::jsonb, movement_patterns = '[]'::jsonb, dismissed_at = null
    where t.source_revision <> excluded.source_revision
       or (t.status = 'pending' and t.updated_at < now() - interval '5 minutes')
       or (t.status = 'failed' and t.updated_at < now() - interval '1 hour')
  returning t.id into v_id;
  return v_id is not null;
end;
$$;

revoke all on function public.claim_training_focus_inference(uuid, text, uuid, date, text) from public, anon, authenticated;
grant execute on function public.claim_training_focus_inference(uuid, text, uuid, date, text) to service_role;
