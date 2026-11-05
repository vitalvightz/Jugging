-- planning_brief_runtime parsed the brief up to four times (validity check,
-- type check, key test, removal): ~33 ms per active plan against ~4.5 ms for
-- the raw column. Parse once, and not at all when the text has no
-- candidate_pools key. Same results: briefs without the key, non-objects and
-- invalid JSON are returned unchanged.
begin;

create or replace function public.planning_brief_runtime(p public.plans)
returns text
language plpgsql
stable
set search_path = pg_catalog, pg_temp
as $$
declare
  brief jsonb;
begin
  if p.planning_brief is null or strpos(p.planning_brief, '"candidate_pools"') = 0 then
    return p.planning_brief;
  end if;
  begin
    brief := p.planning_brief::jsonb;
  exception when data_exception then
    return p.planning_brief;
  end;
  if jsonb_typeof(brief) <> 'object' or not brief ? 'candidate_pools' then
    return p.planning_brief;
  end if;
  return (brief - 'candidate_pools')::text;
end
$$;

revoke all on function public.planning_brief_runtime(public.plans) from public, anon, authenticated;
grant execute on function public.planning_brief_runtime(public.plans) to service_role;

commit;
