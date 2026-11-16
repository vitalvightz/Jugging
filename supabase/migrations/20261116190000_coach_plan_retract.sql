-- Let a coach take back a sent plan the fighter has not answered, and clear
-- answered plans out of their own list. An accepted plan the fighter is
-- training on stays the fighter's: it is never retracted or removed here.
--
-- coach_removed_at hides a declined or superseded assignment from the coach
-- only. The row, its consent timestamps and the fighter's view are untouched.
alter table public.custom_training_plans add column coach_removed_at timestamptz;

create or replace function public.coach_plan_command(
  p_actor uuid, p_fighter uuid, p_id uuid, p_action text, p_fields jsonb default '{}'::jsonb
) returns jsonb language plpgsql security invoker set search_path='' as $$
declare c public.coach_connections; d public.custom_training_plans; actor public.profiles; active_id uuid;
begin
  -- The fighter's profile lock serializes this with their accept / decline.
  select active_plan_id into active_id from public.profiles where id=p_fighter for update;
  select * into c from public.coach_connections where coach_id=p_actor and athlete_id=p_fighter and status='accepted' for update;
  select * into actor from public.profiles where id=p_actor and role in ('coach','admin') and access_status='approved';
  if c.id is null or actor.id is null or not exists (
    select 1 from public.profiles where id=p_fighter and role='athlete' and access_status='approved'
  ) then raise exception 'Fighter not found' using errcode='42501'; end if;
  if p_action='create' then
    insert into public.custom_training_plans(id,athlete_id,fighter_id,name,sessions,incomplete,schedule_mode,start_date,end_date)
      values(p_id,p_actor,p_fighter,p_fields->>'name',p_fields->'sessions',(p_fields->>'incomplete')::boolean,coalesce(p_fields->>'schedule_mode','weekly'),(p_fields->>'start_date')::date,(p_fields->>'end_date')::date)
      on conflict(id) do nothing;
  end if;
  select * into d from public.custom_training_plans where id=p_id and athlete_id=p_actor and fighter_id=p_fighter for update;
  if d.id is null then raise exception 'Plan not found' using errcode='42501'; end if;
  if p_action='assign' then
    if d.assigned_at is null then
      if d.incomplete then raise exception 'Finish the plan before assigning' using errcode='23514'; end if;
      update public.custom_training_plans set assigned_at=now(),coach_name=actor.full_name where id=d.id returning * into d;
    end if;
  elsif p_action='update' then
    if d.assigned_at is not null then raise exception 'Assigned plans cannot be edited' using errcode='42501'; end if;
    update public.custom_training_plans set name=p_fields->>'name',sessions=p_fields->'sessions',incomplete=(p_fields->>'incomplete')::boolean,
      schedule_mode=coalesce(p_fields->>'schedule_mode','weekly'),start_date=(p_fields->>'start_date')::date,end_date=(p_fields->>'end_date')::date
      where id=d.id returning * into d;
  elsif p_action='retract' then
    -- Only while unanswered. A retry after a successful retract is a no-op.
    if d.accepted_at is not null or d.declined_at is not null then
      raise exception 'The fighter has already answered this plan' using errcode='42501';
    end if;
    if d.assigned_at is not null then
      -- An interrupted acceptance can leave an inert compiled copy; a retracted
      -- draft may be edited, so that copy must not be reused by a later send.
      -- CASE fixes the evaluation order: a brief is only parsed once the row
      -- is known to be an inert copy, so an older non-JSON brief cannot fail this.
      delete from public.plans where athlete_id=p_fighter
        and (case when status='review_required' and planning_brief ~ '^\s*\{'
                  then planning_brief::jsonb->>'coach_assignment_id' end)=d.id::text;
      update public.custom_training_plans set assigned_at=null,coach_name=null where id=d.id returning * into d;
    end if;
  elsif p_action='remove' then
    -- Declined, or accepted and no longer the fighter's active plan.
    if d.assigned_at is null or (d.accepted_at is null and d.declined_at is null) then
      raise exception 'Retract or delete this plan instead' using errcode='42501';
    end if;
    if d.accepted_at is not null and d.published_plan_id is not null and d.published_plan_id=active_id then
      raise exception 'The fighter is training on this plan' using errcode='42501';
    end if;
    update public.custom_training_plans set coach_removed_at=coalesce(coach_removed_at,now()) where id=d.id returning * into d;
  elsif p_action <> 'create' then raise exception 'Invalid action';
  end if;
  return to_jsonb(d);
end $$;
