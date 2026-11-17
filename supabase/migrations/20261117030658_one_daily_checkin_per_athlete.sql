-- Readiness is athlete-local daily state, independent of the active programme.
begin;
lock table public.today_checkins in share row exclusive mode;
-- Keep the latest answers where plan switches produced multiple check-ins.
with ranked as (
  select id, row_number() over (
    partition by athlete_id, training_day order by updated_at desc nulls last, created_at desc, id desc
  ) as position from public.today_checkins
)
delete from public.today_checkins where id in (select id from ranked where position > 1);
create unique index if not exists today_checkins_athlete_day_key
  on public.today_checkins (athlete_id, training_day);
commit;

create or replace function public.upsert_plan_free_today_checkin(
  p_athlete_id uuid,
  p_fields jsonb
)
returns public.today_checkins
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_row public.today_checkins%rowtype;
begin
  insert into public.today_checkins (
    athlete_id, plan_id, phase, training_day, athlete_timezone,
    sleep, body, pain, active_injury, previous_session,
    sharp_pain, instability, swelling, neurological_symptoms, illness_symptoms,
    cannot_warm_into_movement, worse_next_day_pain,
    recommendation_state, recommendation_reason, recommendation_triggers
  ) values (
    p_athlete_id, null, null,
    (p_fields->>'training_day')::date,
    coalesce(p_fields->>'athlete_timezone', ''),
    p_fields->>'sleep', p_fields->>'body', p_fields->>'pain',
    coalesce(p_fields->>'active_injury', 'none'),
    coalesce(p_fields->>'previous_session', 'none'),
    coalesce((p_fields->>'sharp_pain')::boolean, false),
    coalesce((p_fields->>'instability')::boolean, false),
    coalesce((p_fields->>'swelling')::boolean, false),
    coalesce((p_fields->>'neurological_symptoms')::boolean, false),
    coalesce((p_fields->>'illness_symptoms')::boolean, false),
    coalesce((p_fields->>'cannot_warm_into_movement')::boolean, false),
    coalesce((p_fields->>'worse_next_day_pain')::boolean, false),
    p_fields->>'recommendation_state',
    coalesce(p_fields->>'recommendation_reason', ''),
    coalesce(p_fields->'recommendation_triggers', '[]'::jsonb)
  )
  on conflict (athlete_id, training_day) do update set
    plan_id = null, phase = null,
    athlete_timezone = excluded.athlete_timezone,
    sleep = excluded.sleep,
    body = excluded.body,
    pain = excluded.pain,
    active_injury = excluded.active_injury,
    previous_session = excluded.previous_session,
    sharp_pain = excluded.sharp_pain,
    instability = excluded.instability,
    swelling = excluded.swelling,
    neurological_symptoms = excluded.neurological_symptoms,
    illness_symptoms = excluded.illness_symptoms,
    cannot_warm_into_movement = excluded.cannot_warm_into_movement,
    worse_next_day_pain = excluded.worse_next_day_pain,
    recommendation_state = excluded.recommendation_state,
    recommendation_reason = excluded.recommendation_reason,
    recommendation_triggers = excluded.recommendation_triggers
  returning * into v_row;
  return v_row;
end;
$$;

revoke all on function public.upsert_plan_free_today_checkin(uuid, jsonb) from public, anon, authenticated;
grant execute on function public.upsert_plan_free_today_checkin(uuid, jsonb) to service_role;
