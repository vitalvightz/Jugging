-- Recognize only persisted, work-bearing headline days and validated frozen
-- rehab occurrences. Retain all original ownership, active-plan and daily caps.
set lock_timeout = '5s';

create or replace function public.xp_day_session_ids(p_day jsonb, p_training_day date)
returns table(session_id text)
language sql immutable security invoker set search_path = pg_catalog, public as $$
  select btrim(s->>'session_id')
  from jsonb_array_elements(case when jsonb_typeof(p_day->'sessions') = 'array'
    then p_day->'sessions' else '[]'::jsonb end) s
  where nullif(btrim(s->>'session_id'),'') is not null
    and coalesce(s->'optional','false'::jsonb) <> 'true'::jsonb
  union all
  select p_training_day::text
  where p_training_day is not null
    and (p_day->'sessions' is null or p_day->'sessions' = '[]'::jsonb)
    and btrim(coalesce(p_day->'today_card'->>'headline','')) !~* '^(?:full\s+|complete\s+|total\s+)?(?:rest|off|no\s+training|day\s+off|travel)\y'
    and btrim(coalesce(p_day->'today_card'->>'headline','')) ~* '\y(coach(?:ing|ed)?|spar(?:r(?:ing|ed)|s)?|technical|combat|boxing|pad\s?work|pads|mitts?|skills?|drill(?:s|ing)?|primer|strength|conditioning|mobility|reset|fight\s+day|protocol|rhythm|flush|breath(?:ing)?|downshift|(?:easy|recovery)\s+walk|visuali[sz](?:e|ation)|cue\s+card|mindset\s+(?:reset|work)|movement\s+quality|shadowbox(?:ing|es)?|footwork|tactical\s+(?:watch|review)|film\s+(?:review|study))\y';
$$;
revoke all on function public.xp_day_session_ids(jsonb,date) from public,anon,authenticated;
grant execute on function public.xp_day_session_ids(jsonb,date) to service_role;

create or replace function public.xp_completion_is_frozen_rehab(p_completion public.session_completions)
returns boolean
language sql immutable security invoker set search_path = pg_catalog, public as $$
  select coalesce(
    p_completion.status in ('done','modified')
    and p_completion.rehab_performance in ('done_as_shown','changed')
    and p_completion.session_id ~ ('^rehab-' || p_completion.training_day::text || '(-[a-f0-9]{24})?$')
    and p_completion.prescription_snapshot->>'plan_id' = p_completion.plan_id::text
    and p_completion.prescription_snapshot->>'training_day' = p_completion.training_day::text
    and p_completion.prescription_snapshot->>'revision' ~ '^[a-f0-9]{64}$'
    and p_completion.prescription_snapshot->'session'->>'session_id' = p_completion.session_id
    and p_completion.prescription_snapshot->'session'->>'session_type' = 'rehab'
    and jsonb_typeof(p_completion.prescription_snapshot->'session'->'blocks') = 'array'
    and exists (
      select 1 from jsonb_array_elements(case
        when jsonb_typeof(p_completion.prescription_snapshot->'session'->'blocks')='array'
        then p_completion.prescription_snapshot->'session'->'blocks' else '[]'::jsonb end) b
      where b->>'block_type'='rehab' and nullif(b->>'policy_id','') is not null)
    and not exists (
      select 1 from jsonb_array_elements(case
        when jsonb_typeof(p_completion.prescription_snapshot->'session'->'blocks')='array'
        then p_completion.prescription_snapshot->'session'->'blocks' else '[]'::jsonb end) b
      where b->>'block_type' is distinct from 'rehab'), false);
$$;
revoke all on function public.xp_completion_is_frozen_rehab(public.session_completions)
  from public,anon,authenticated;
grant execute on function public.xp_completion_is_frozen_rehab(public.session_completions) to service_role;

-- Replace the source-integrity trigger so renewable open-plan full-week awards
-- are validated against deterministic projected calendar dates.

create or replace function public.enforce_xp_award_source_integrity()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_completion_id uuid;
  v_completion_plan_id uuid;
  v_completion_session_id text;
  v_completion_day date;
  v_completion_status text;
  v_plan_id uuid;
  v_week_id text;
  v_plan_type text;
  v_active_plan_id uuid;
  v_timezone text;
begin
  if new.action in ('recommended_fighter_content_watched', 'stop_decision_followed') then
    raise exception 'XP action has no live authoritative earning hook'
      using errcode = '23514';
  end if;

  if new.action in ('training_logged', 'planned_session_completed') then
    if split_part(new.idempotency_key, ':', 1) <> new.action then
      raise exception 'invalid session XP source key' using errcode = '22023';
    end if;
    begin
      v_completion_id := split_part(new.idempotency_key, ':', 2)::uuid;
    exception when invalid_text_representation then
      raise exception 'invalid session completion source id' using errcode = '22023';
    end;

    select
      completion.plan_id,
      completion.session_id,
      completion.training_day,
      completion.status
    into
      v_completion_plan_id,
      v_completion_session_id,
      v_completion_day,
      v_completion_status
    from public.session_completions as completion
    where completion.id = v_completion_id
      and completion.athlete_id = new.athlete_id;

    if v_completion_plan_id is null
      or v_completion_day is distinct from new.calendar_date
      or v_completion_status not in ('done', 'modified') then
      raise exception 'session XP source is not a terminal athlete completion'
        using errcode = '23514';
    end if;

    v_active_plan_id := public.xp_resolved_active_plan_id(
      new.athlete_id,
      new.calendar_date
    );
    if v_active_plan_id is null or v_completion_plan_id <> v_active_plan_id then
      raise exception 'inactive plan cannot earn session XP'
        using errcode = '23514';
    end if;

    select lower(coalesce(plan.structured_plan -> 'plan_metadata' ->> 'plan_type', ''))
    into v_plan_type
    from public.plans as plan
    where plan.id = v_completion_plan_id
      and plan.athlete_id = new.athlete_id;

    if not (
      exists (
        select 1 from public.plans plan
        cross join lateral jsonb_array_elements(case
          when jsonb_typeof(plan.structured_plan->'weeks')='array'
          then plan.structured_plan->'weeks' else '[]'::jsonb end) week(item)
        cross join lateral jsonb_array_elements(case
          when jsonb_typeof(week.item->'days')='array'
          then week.item->'days' else '[]'::jsonb end) day(item)
        cross join lateral public.xp_day_session_ids(day.item,new.calendar_date) session
        where plan.id=v_completion_plan_id and plan.athlete_id=new.athlete_id
          and lower(coalesce(day.item->>'day_type','')) <> 'rest'
          and session.session_id=v_completion_session_id
          and (day.item->>'date'=new.calendar_date::text or
            (v_plan_type='open_ongoing_system' and day.item->>'weekday'=to_char(new.calendar_date,'Dy')))
      )
      or (new.action='training_logged' and exists (
        select 1 from public.session_completions completion
        where completion.id=v_completion_id and completion.athlete_id=new.athlete_id
          and public.xp_completion_is_frozen_rehab(completion)))
    ) then
      raise exception 'session XP source is not scheduled in the active plan'
        using errcode = '23514';
    end if;
  end if;

  if new.action = 'full_training_week_completed' then
    if split_part(new.idempotency_key, ':', 1) <> 'full-week' then
      raise exception 'invalid full-week XP source key' using errcode = '22023';
    end if;
    begin
      v_plan_id := split_part(new.idempotency_key, ':', 2)::uuid;
    exception when invalid_text_representation then
      raise exception 'invalid full-week plan source id' using errcode = '22023';
    end;
    v_week_id := btrim(split_part(new.idempotency_key, ':', 3));
    if v_week_id = '' then
      raise exception 'full-week source is missing the week id' using errcode = '22023';
    end if;

    v_active_plan_id := public.xp_resolved_active_plan_id(
      new.athlete_id,
      new.calendar_date
    );
    if v_active_plan_id is null or v_plan_id <> v_active_plan_id then
      raise exception 'inactive plan cannot earn full-week XP'
        using errcode = '23514';
    end if;

    if not exists (
      select 1
      from public.xp_full_week_planned_sessions(
        v_plan_id,
        v_week_id,
        new.calendar_date
      )
    ) then
      raise exception 'full-week source is not part of the active structured plan'
        using errcode = '23514';
    end if;
  end if;

  if new.action = 'injury_update_completed' then
    select coalesce(nullif(profile.athlete_timezone, ''), 'UTC')
    into v_timezone
    from public.profiles as profile
    where profile.id = new.athlete_id;

    if not exists (
      select 1
      from public.injury_flags as injury
      where injury.athlete_id = new.athlete_id
        and (
          injury.updated_at at time zone coalesce(v_timezone, 'UTC')
        )::date = new.calendar_date
    ) then
      raise exception 'injury XP requires an injury record updated on the training day'
        using errcode = '23514';
    end if;
  end if;

  if new.action in ('phase_completed', 'first_plan_completed', 'camp_completed')
    and not exists (
      select 1
      from public.plan_milestones as milestone
      join public.plans as plan on plan.id = milestone.plan_id
      where milestone.athlete_id = new.athlete_id
        and (
          (
            new.action = 'phase_completed'
            and milestone.milestone_type = 'phase_completed'
            and new.idempotency_key =
              'phase-completed:' || new.athlete_id::text || ':' ||
              public.xp_plan_reward_scope(milestone.plan_id) || ':' ||
              lower(milestone.phase_label)
          )
          or (
            new.action = 'first_plan_completed'
            and milestone.milestone_type = 'plan_completed'
            and new.idempotency_key =
              'first-plan-completed:' || new.athlete_id::text
          )
          or (
            new.action = 'camp_completed'
            and milestone.milestone_type = 'camp_completed'
            and lower(coalesce(plan.structured_plan -> 'plan_metadata' ->> 'plan_type', '')) = 'fight_camp'
            and plan.fight_date is not null
            and new.idempotency_key =
              'camp-completed:' || new.athlete_id::text || ':' ||
              public.xp_plan_reward_scope(milestone.plan_id)
          )
        )
    ) then
      raise exception 'lifecycle XP requires a matching persisted plan milestone'
        using errcode = '23514';
  end if;

  return new;
end;
$$;

revoke all on function public.enforce_xp_award_source_integrity()
  from public, anon, authenticated;
grant execute on function public.enforce_xp_award_source_integrity()
  to service_role;

create or replace function public.xp_full_week_planned_sessions(
  p_plan_id uuid,
  p_week_id text,
  p_week_start date
)
returns table(session_id text, training_day date)
language plpgsql
stable
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_plan_type text;
  v_week jsonb;
begin
  select lower(btrim(coalesce(plan.structured_plan -> 'plan_metadata' ->> 'plan_type', '')))
  into v_plan_type
  from public.plans as plan
  where plan.id = p_plan_id;

  if v_plan_type = 'open_ongoing_system' then
    v_week := public.xp_open_plan_week_item(p_plan_id, p_week_start, p_week_id);
    if v_week is null then
      return;
    end if;

    return query
    select
      session.session_id,
      p_week_start + weekday.offset_days
    from jsonb_array_elements(
      case
        when jsonb_typeof(v_week -> 'days') = 'array'
          then v_week -> 'days'
        else '[]'::jsonb
      end
    ) as day(item)
    cross join lateral (
      select public.xp_weekday_offset(day.item ->> 'weekday') as offset_days
    ) as weekday
    cross join lateral public.xp_day_session_ids(day.item,p_week_start + weekday.offset_days) session
    where lower(coalesce(day.item ->> 'day_type', '')) <> 'rest'
      and weekday.offset_days is not null
      and nullif(btrim(session.session_id), '') is not null;
    return;
  end if;

  return query
  select
    session.session_id,
    (day.item ->> 'date')::date
  from public.plans as plan
  cross join lateral jsonb_array_elements(
    case
      when jsonb_typeof(plan.structured_plan -> 'weeks') = 'array'
        then plan.structured_plan -> 'weeks'
      else '[]'::jsonb
    end
  ) as week(item)
  cross join lateral jsonb_array_elements(
    case
      when jsonb_typeof(week.item -> 'days') = 'array'
        then week.item -> 'days'
      else '[]'::jsonb
    end
  ) as day(item)
  cross join lateral public.xp_day_session_ids(day.item,
    case when coalesce(day.item->>'date','') ~ '^\d{4}-\d{2}-\d{2}$'
      then (day.item->>'date')::date else null end) session
  where plan.id = p_plan_id
    and week.item ->> 'week_id' = p_week_id
    and week.item ->> 'start_date' = p_week_start::text
    and lower(coalesce(day.item ->> 'day_type', '')) <> 'rest'
    and coalesce(day.item ->> 'date', '') ~ '^\d{4}-\d{2}-\d{2}$'
    and nullif(btrim(session.session_id), '') is not null;
end;
$$;

revoke all on function public.xp_full_week_planned_sessions(uuid,text,date) from public,anon,authenticated;
grant execute on function public.xp_full_week_planned_sessions(uuid,text,date) to service_role;
reset lock_timeout;

