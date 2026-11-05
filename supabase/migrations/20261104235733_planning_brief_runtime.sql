-- Athlete runtime reads (Today, XP, streaks, exercise logs) never use the
-- planner's candidate_pools, which are ~75% of planning_brief (avg ~400 KB of
-- ~536 KB on recent plans). This computed field lets PostgREST return the brief
-- without them: select=planning_brief:planning_brief_runtime keeps the key and
-- the text type. Plan detail still reads the full column (exercise identity
-- stamping uses the pools). Read-only; no schema or data change.
begin;

create or replace function public.planning_brief_runtime(p public.plans)
returns text
language sql
stable
set search_path = pg_catalog, pg_temp
as $$
  select case
    when p.planning_brief is not null
      and pg_input_is_valid(p.planning_brief, 'jsonb')
      and jsonb_typeof(p.planning_brief::jsonb) = 'object'
      and p.planning_brief::jsonb ? 'candidate_pools'
    then (p.planning_brief::jsonb - 'candidate_pools')::text
    else p.planning_brief
  end
$$;

revoke all on function public.planning_brief_runtime(public.plans) from public, anon, authenticated;
grant execute on function public.planning_brief_runtime(public.plans) to service_role;

commit;
