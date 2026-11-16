-- Add authored calendar dates without rewriting existing weekly drafts.
alter table public.custom_training_plans
  add column schedule_mode text not null default 'weekly' check (schedule_mode in ('weekly','dated')),
  add column start_date date,
  add column end_date date,
  add constraint custom_plan_date_range check (
    (schedule_mode='weekly' and start_date is null and end_date is null) or
    (schedule_mode='dated' and start_date is not null and end_date is not null and end_date-start_date between 0 and 83)
  );
alter table public.custom_training_plans drop constraint custom_training_plans_sessions_check;
alter table public.custom_training_plans add constraint custom_training_plans_sessions_check
  check (jsonb_typeof(sessions)='array' and jsonb_array_length(sessions) <= case when schedule_mode='weekly' then 28 else 336 end);

create or replace function public.coach_plan_command(
  p_actor uuid, p_fighter uuid, p_id uuid, p_action text, p_fields jsonb default '{}'::jsonb
) returns jsonb language plpgsql security invoker set search_path='' as $$
declare c public.coach_connections; d public.custom_training_plans; actor public.profiles;
begin
  perform 1 from public.profiles where id=p_fighter for update;
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
  elsif p_action <> 'create' then raise exception 'Invalid action';
  end if;
  return to_jsonb(d);
end $$;

