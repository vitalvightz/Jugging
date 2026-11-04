-- list_claimable_generation_jobs_v2 ORed a parameter-gated branch into its
-- filter. Inside a SQL function the plan is generic, so Postgres could not drop
-- that branch and seq-scanned generation_jobs on every worker poll (~5.5k polls
-- a day). The same rows now come from UNION ALL branches that each use a status
-- index; the legacy branch is a one-time filter skipped entirely when
-- p_include_legacy_blank is false. The branches select disjoint statuses, so no
-- row can appear twice, and the outer order/limit are unchanged.

create or replace function public.list_claimable_generation_jobs_v2(
  p_limit integer,
  p_stale_before timestamptz,
  p_include_legacy_blank boolean default false
)
returns table (
  id uuid,
  status text,
  created_at timestamptz,
  started_at timestamptz,
  heartbeat_at timestamptz,
  completed_at timestamptz,
  progress_milestones jsonb,
  stage1_result jsonb,
  final_result jsonb
)
language sql
stable
set search_path to 'pg_catalog', 'public'
as $function$
  select
    job.id,
    job.status,
    job.created_at,
    job.started_at,
    job.heartbeat_at,
    job.completed_at,
    coalesce(job.progress_milestones, '[]'::jsonb),
    case when job.stage1_result is null then null else '{}'::jsonb end,
    case when job.final_result is null then null else '{}'::jsonb end
  from (
    select queued.* from public.generation_jobs as queued
    where queued.status = 'queued'
    union all
    select blank.* from public.generation_jobs as blank
    where p_include_legacy_blank and (blank.status is null or blank.status = '')
    union all
    select stale.* from public.generation_jobs as stale
    where stale.status = 'running'
      and stale.completed_at is null
      and stale.stage1_result is null
      and stale.final_result is null
      and coalesce(stale.progress_milestones, '[]'::jsonb) = '[]'::jsonb
      and coalesce(stale.heartbeat_at, stale.started_at) <= p_stale_before
  ) as job
  order by job.created_at asc
  limit least(greatest(coalesce(p_limit, 1), 1), 100);
$function$;

revoke all on function public.list_claimable_generation_jobs_v2(integer, timestamptz, boolean)
  from public, anon, authenticated;
grant execute on function public.list_claimable_generation_jobs_v2(integer, timestamptz, boolean)
  to service_role;
