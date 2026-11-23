-- Time to first usable plan: the headline onboarding metric.
--
-- first_usable_plan_at is the earliest plan an athlete could actually train
-- from (status ready or publishable_with_flags, matching
-- api/services/xp_awards.ACTIVATION_READY_PLAN_STATUSES). Measured against
-- profiles.created_at it is the time from signup to a plan in hand, and it is
-- derived from stored rows so it covers every existing athlete, not just those
-- who sign up after this ships.
--
-- create or replace may only append columns, so the new column goes last.

create or replace view public.admin_athlete_rollups
with (security_invoker = true) as
select
  p.id,
  p.email,
  p.username,
  p.role,
  p.full_name,
  p.technical_style,
  p.tactical_style,
  p.stance,
  p.professional_status,
  p.record_summary,
  p.athlete_timezone,
  p.athlete_locale,
  p.appearance_mode,
  p.onboarding_draft,
  p.nutrition_profile,
  p.created_at,
  p.updated_at,
  count(pl.id)::int as plan_count,
  max(pl.created_at) as latest_plan_created_at,
  p.access_status,
  min(pl.created_at) filter (
    where pl.status in ('ready', 'publishable_with_flags')
  ) as first_usable_plan_at
from public.profiles p
left join public.plans pl on pl.athlete_id = p.id
group by p.id;

revoke all on public.admin_athlete_rollups from anon, authenticated;
grant select on public.admin_athlete_rollups to service_role;
