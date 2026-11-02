-- 1. Remove a finished plan session from History in one transaction.
--
-- Deletes that occurrence's exercise results and marks the completion not
-- started together, so a failure (including the rehab reset guard in
-- public.preserve_started_prescription) leaves both exactly as they were.
-- The row and its id stay: earned XP is keyed by that id, so a redo cannot
-- award it twice. A second call on an already-removed session is a no-op.
--
-- 2. Tighten the optional run-detail checks on manual sessions: a distance and
--    its unit are stored together (a NULL half no longer passes the CHECK),
--    and an exact time is roadwork-only, matching the API.
--
-- Repeatable and non-destructive: every step is guarded or replaces in place.

create or replace function public.reset_history_plan_session(p_athlete_id uuid, p_completion_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_row public.session_completions%rowtype;
begin
  select * into v_row from public.session_completions
    where id = p_completion_id and athlete_id = p_athlete_id
    for update;
  if not found then
    return null;
  end if;
  if v_row.status not in ('done', 'modified') then
    return to_jsonb(v_row);
  end if;
  delete from public.exercise_logs
    where athlete_id = p_athlete_id and plan_id = v_row.plan_id
      and session_id = v_row.session_id and training_day = v_row.training_day;
  update public.session_completions
    set status = 'not_started', started_at = null, completed_at = null, session_rpe = null,
        pain_after = null, modification_reason = '', notes = ''
    where id = v_row.id
    returning * into v_row;
  return to_jsonb(v_row);
end;
$$;

revoke all on function public.reset_history_plan_session(uuid, uuid) from public, anon, authenticated;
grant execute on function public.reset_history_plan_session(uuid, uuid) to service_role;

-- Added beside the original checks rather than replacing them (nothing is
-- dropped). Anything these accept also satisfies the originals, which are now
-- redundant and can be removed in a later cleanup.
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'external_training_sessions_run_distance_v2'
      and conrelid = 'public.external_training_sessions'::regclass) then
    alter table public.external_training_sessions
      add constraint external_training_sessions_run_distance_v2 check (
        (distance_value is null and distance_unit is null)
        or (distance_value is not null and distance_unit is not null and activity = 'roadwork'
            and distance_value > 0 and (
              (distance_unit = 'km' and distance_value <= 200)
              or (distance_unit = 'miles' and distance_value <= 125))));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'external_training_sessions_duration_seconds_v2'
      and conrelid = 'public.external_training_sessions'::regclass) then
    alter table public.external_training_sessions
      add constraint external_training_sessions_duration_seconds_v2 check (
        duration_seconds is null
        or (activity = 'roadwork' and duration_seconds between 1 and 36000
            and duration_minutes = ceil(duration_seconds / 60.0)));
  end if;
end $$;
