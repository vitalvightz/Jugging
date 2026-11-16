-- Extend the existing drafts; athlete_id remains the author/owner. A coach
-- draft has one recipient and becomes immutable when sent for consent.
alter table public.custom_training_plans
  add column fighter_id uuid references public.profiles(id) on delete cascade,
  add column coach_name text,
  add column assigned_at timestamptz,
  add column accepted_at timestamptz,
  add column declined_at timestamptz,
  add column published_plan_id uuid references public.plans(id) on delete set null,
  add constraint coach_plan_consent check (
    (fighter_id is null and assigned_at is null and accepted_at is null and declined_at is null and published_plan_id is null)
    or (fighter_id is not null and fighter_id <> athlete_id
      and not (accepted_at is not null and declined_at is not null)
      and (accepted_at is null or assigned_at is not null)
      and (declined_at is null or assigned_at is not null))
  );
create index coach_plans_recipient on public.custom_training_plans(fighter_id, assigned_at desc)
  where fighter_id is not null;
-- No new client grants/policies. Both sides use authenticated backend routes.
-- Each coach mutation locks the live connection, serializing it with revocation.
create function public.coach_plan_command(
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
    insert into public.custom_training_plans(id,athlete_id,fighter_id,name,sessions,incomplete)
      values(p_id,p_actor,p_fighter,p_fields->>'name',p_fields->'sessions',(p_fields->>'incomplete')::boolean)
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
    update public.custom_training_plans set name=p_fields->>'name',sessions=p_fields->'sessions',incomplete=(p_fields->>'incomplete')::boolean
      where id=d.id returning * into d;
  elsif p_action <> 'create' then raise exception 'Invalid action';
  end if;
  return to_jsonb(d);
end $$;

-- Only the server may call this. The HTTP actor must be the recipient. Lock
-- profile first, then draft: concurrent activations use a compare-and-set, and
-- consent, pointer switch and optional archive commit together or roll back.
create function public.respond_coach_plan(
  p_actor uuid, p_id uuid, p_action text, p_plan uuid default null,
  p_expected uuid default null, p_replace boolean default false
) returns boolean language plpgsql security invoker set search_path='' as $$
declare d public.custom_training_plans; current_id uuid;
begin
  select active_plan_id into current_id from public.profiles
    where id=p_actor and role='athlete' and access_status='approved' for update;
  if not found then return false; end if;
  select * into d from public.custom_training_plans where id=p_id and fighter_id=p_actor and assigned_at is not null for update;
  if d.id is null then return false; end if;
  if p_action='decline' then
    if d.accepted_at is not null then return false; end if;
    update public.custom_training_plans set declined_at=coalesce(declined_at,now()) where id=d.id;
    return true;
  end if;
  if p_action <> 'activate' or d.declined_at is not null then return false; end if;
  -- A successful retry never reactivates a subsequently superseded plan.
  if d.accepted_at is not null then return d.published_plan_id=p_plan; end if;
  if current_id is distinct from p_expected then return false; end if;
  -- Revocation ends new actions; already accepted plans remain athlete-owned.
  perform 1 from public.coach_connections where coach_id=d.athlete_id and athlete_id=p_actor and status='accepted' for update;
  if not found then return false; end if;
  perform 1 from public.plans where id=p_plan and athlete_id=p_actor and status='review_required'
    and (planning_brief::jsonb->>'coach_assignment_id')=d.id::text for update;
  if not found then return false; end if;
  update public.plans set status='ready' where id=p_plan;
  update public.custom_training_plans set accepted_at=now(),published_plan_id=p_plan where id=d.id;
  update public.profiles set active_plan_id=p_plan where id=p_actor;
  if p_replace and current_id is not null and current_id <> p_plan then
    update public.plans set status='archived' where id=current_id and athlete_id=p_actor;
  end if;
  return true;
end $$;
revoke all on function public.coach_plan_command(uuid,uuid,uuid,text,jsonb) from public,anon,authenticated;
revoke all on function public.respond_coach_plan(uuid,uuid,text,uuid,uuid,boolean) from public,anon,authenticated;
grant execute on function public.coach_plan_command(uuid,uuid,uuid,text,jsonb) to service_role;
grant execute on function public.respond_coach_plan(uuid,uuid,text,uuid,uuid,boolean) to service_role;
