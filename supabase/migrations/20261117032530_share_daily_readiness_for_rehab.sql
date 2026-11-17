-- Daily readiness belongs to the athlete and local day, independent of plan.
-- Keep record/revision freshness and all prescription, injury and allocation guards.
create or replace function public.preserve_started_prescription()
returns trigger language plpgsql set search_path = public, pg_temp as $$
declare
  v_block jsonb;
  v_limit int;
  v_used int;
begin
  if new.prescription_snapshot is not null or (tg_op = 'UPDATE' and old.prescription_snapshot is not null) then
    perform pg_advisory_xact_lock(hashtextextended('injury:' || new.athlete_id::text, 0));
  end if;
  -- INSERT ... ON CONFLICT runs the insert trigger before the update trigger.
  -- The existing occurrence is validated by the UPDATE branch with its real id.
  if tg_op = 'INSERT' and exists (select 1 from public.session_completions s
     where s.athlete_id = new.athlete_id and s.session_id = new.session_id and s.training_day = new.training_day) then
    return new;
  end if;
  if tg_op = 'UPDATE' and old.prescription_snapshot is not null then
    if old.status in ('done','modified') and new.status not in ('done','modified')
       and exists (select 1 from jsonb_array_elements(old.prescription_snapshot->'session'->'blocks') b
                   where b ? 'policy_id' or b ? 'clinical_review_pin') then
      raise exception 'rehab_exposure_cannot_be_reset' using errcode = '23514';
    end if;
    if new.prescription_snapshot is not null and new.prescription_snapshot <> old.prescription_snapshot then
      raise exception 'prescription_revision_conflict' using errcode = '23514';
    end if;
    new.prescription_snapshot := old.prescription_snapshot;
  end if;
  if new.prescription_snapshot is not null then
    if new.prescription_snapshot->>'plan_id' is distinct from new.plan_id::text
       or new.prescription_snapshot->>'training_day' is distinct from new.training_day::text
       or new.prescription_snapshot->'session'->>'session_id' is distinct from new.session_id
       or coalesce(new.prescription_snapshot->>'revision', '') !~ '^[a-f0-9]{64}$' then
      raise exception 'invalid prescription occurrence' using errcode = '23514';
    end if;
    if tg_op = 'INSERT' or old.prescription_snapshot is null or old.status not in ('started','done','modified') then
      perform pg_advisory_xact_lock(hashtextextended('injury:' || new.athlete_id::text, 0));
      if new.prescription_snapshot->'evidence_context'->>'exposure_id' is distinct from
         (select id::text from public.rehab_exposures where athlete_id = new.athlete_id order by created_at desc, id desc limit 1)
         or new.prescription_snapshot->'evidence_context'->>'event_id' is distinct from
         (select id::text from public.injury_episode_events where athlete_id = new.athlete_id
           and event_type in ('injury_checkin','delayed_rehab_response','clinician_clearance_report',
             'rehab_progression_assessment','clinical_progression_review','clinical_progression_review_lifecycle') order by created_at desc, id desc limit 1) then
        raise exception 'prescription_revision_conflict' using errcode = '23514';
      end if;
      if jsonb_typeof(new.prescription_snapshot->'readiness_context') is distinct from 'object'
         or not exists (select 1 from public.today_checkins c
           where c.athlete_id = new.athlete_id
             and c.training_day = new.training_day
             and c.id = (new.prescription_snapshot->'readiness_context'->>'id')::uuid
             and c.updated_at is not distinct from
               (new.prescription_snapshot->'readiness_context'->>'updated_at')::timestamptz) then
        raise exception 'prescription_revision_conflict' using errcode = '23514';
      end if;
      if jsonb_typeof(new.prescription_snapshot->'injury_context') is distinct from 'array'
         or (select count(*) from public.injury_flags where athlete_id = new.athlete_id and status in ('open','monitoring'))
            <> jsonb_array_length(new.prescription_snapshot->'injury_context')
         or exists (select 1 from jsonb_array_elements(new.prescription_snapshot->'injury_context') c
             where not exists (select 1 from public.injury_flags i
               where i.athlete_id = new.athlete_id and i.id = (c->>'id')::uuid
                 and i.episode_id = (c->>'episode_id')::uuid and i.status in ('open','monitoring')
                 and i.updated_at is not distinct from (c->>'updated_at')::timestamptz)) then
        raise exception 'prescription_revision_conflict' using errcode = '23514';
      end if;
    end if;
    if new.status in ('started','done','modified') then
      v_limit := coalesce((new.prescription_snapshot->>'allocation_limit')::int, 2);
      if v_limit not between 1 and 2 then raise exception 'invalid rehab allocation' using errcode = '23514'; end if;
      -- Server-owned bundle identity groups drills without grouping legacy blocks.
      select count(distinct (s.id, coalesce(nullif(b->>'rehab_allocation_id', ''),
                                           'block:' || ordinal::text)))
        into v_used from public.session_completions s,
        lateral jsonb_array_elements(s.prescription_snapshot->'session'->'blocks')
          with ordinality as items(b, ordinal)
        where s.athlete_id = new.athlete_id and s.training_day = new.training_day
          and s.status in ('started','done','modified') and s.id <> new.id and b->>'block_type' = 'rehab';
      if exists (select 1 from jsonb_array_elements(new.prescription_snapshot->'session'->'blocks') b where b->>'block_type' = 'rehab')
         and v_used + (select count(distinct coalesce(nullif(b->>'rehab_allocation_id', ''),
                                                     'block:' || ordinal::text))
                       from jsonb_array_elements(new.prescription_snapshot->'session'->'blocks')
                         with ordinality as items(b, ordinal)
                       where b->>'block_type' = 'rehab') > v_limit then
        raise exception 'rehab_daily_allocation_conflict' using errcode = '23514';
      end if;
      for v_block in select b from jsonb_array_elements(new.prescription_snapshot->'session'->'blocks') b where b ? 'policy_id' loop
        if exists (select 1 from public.session_completions s,
             lateral jsonb_array_elements(s.prescription_snapshot->'session'->'blocks') b
             where s.athlete_id = new.athlete_id and s.id <> new.id and s.status in ('started','done','modified')
               and b->>'injury_id' = v_block->>'injury_id' and b->>'injury_episode_id' = v_block->>'injury_episode_id'
               and (s.training_day = new.training_day or
                 (s.status in ('done','modified') and b->>'rehab_drill_id' = v_block->>'rehab_drill_id'
                   and s.training_day <= new.training_day
                   and s.training_day + greatest(coalesce((b->>'minimum_gap_days')::int,1), (v_block->>'minimum_gap_days')::int) > new.training_day))) then
          raise exception 'rehab_daily_allocation_conflict' using errcode = '23514';
        end if;
      end loop;
    end if;
  end if;
  return new;
end;
$$;
