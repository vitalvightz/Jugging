-- The active pointer and "only unarchived plan" decision must be one transaction.
-- Lock the owner profile first: plans.athlete_id has an FK to profiles.id,
-- so concurrent plan inserts cannot commit past this lock.
create function public.try_activate_only_unarchived_plan(
  p_athlete_id uuid,
  p_plan_id uuid,
  p_expected_id uuid,
  p_training_day date
)
returns boolean
language plpgsql security invoker
set search_path = pg_catalog, public
as $$
declare
  v_current uuid;
  v_row record;
  v_count integer := 0;
begin
  select active_plan_id into v_current
  from public.profiles where id = p_athlete_id for update;
  if not found or v_current is distinct from p_expected_id then
    return false;
  end if;

  -- Lock candidate/status rows so a concurrent archive cannot invalidate
  -- the decision before this function commits.
  for v_row in
    select id, status, fight_date from public.plans
    where athlete_id = p_athlete_id and status <> 'archived'
    order by id for update
  loop
    v_count := v_count + 1;
    if v_count > 1 or v_row.id <> p_plan_id
       or v_row.status not in ('ready', 'publishable_with_flags')
       or (v_row.fight_date is not null and v_row.fight_date < p_training_day) then
      return false;
    end if;
  end loop;

  if v_count <> 1 then
    return false;
  end if;
  update public.profiles set active_plan_id = p_plan_id where id = p_athlete_id;
  return true;
end $$;

revoke all on function public.try_activate_only_unarchived_plan(uuid, uuid, uuid, date)
  from public, anon, authenticated;
grant execute on function public.try_activate_only_unarchived_plan(uuid, uuid, uuid, date)
  to service_role;
