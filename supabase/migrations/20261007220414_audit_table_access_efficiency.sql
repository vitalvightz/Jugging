-- Audit follow-up: retain ownership and grants; avoid per-row auth lookups.
-- These tables are currently small (<200 KB each), so ordinary index builds
-- are short. Bound lock acquisition rather than waiting behind live requests.
set lock_timeout = '5s';

alter policy exercise_logs_select_own on public.exercise_logs
  using (athlete_id = (select auth.uid()));
alter policy plan_milestones_select_own on public.plan_milestones
  using (athlete_id = (select auth.uid()));
alter policy week_lifecycle_reconciliations_select_own on public.week_lifecycle_reconciliations
  using (athlete_id = (select auth.uid()));
alter policy rehab_exposures_owner_select on public.rehab_exposures
  using (athlete_id = (select auth.uid()));
alter policy sparring_logs_select_own on public.sparring_logs
  using (athlete_id = (select auth.uid()));

-- Cover parent deletion / FK checks without scanning athlete history.
create index if not exists exercise_logs_plan_id_idx on public.exercise_logs (plan_id);
create index if not exists sparring_logs_plan_id_idx on public.sparring_logs (plan_id);
create index if not exists week_lifecycle_reconciliations_plan_id_idx
  on public.week_lifecycle_reconciliations (plan_id);
create index if not exists injury_episode_events_injury_owner_idx
  on public.injury_episode_events (injury_id, athlete_id);
create index if not exists rehab_exposures_injury_owner_idx
  on public.rehab_exposures (injury_id, athlete_id);

-- Prescription freshness checks and Today history order by athlete/time,
-- independently of injury/episode; existing episode indexes cannot serve that.
create index if not exists injury_episode_events_athlete_created_idx
  on public.injury_episode_events (athlete_id, created_at desc, id desc);
create index if not exists rehab_exposures_athlete_created_idx
  on public.rehab_exposures (athlete_id, created_at desc, id desc);

reset lock_timeout;
