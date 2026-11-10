-- Read-only beta-tester reports over the existing analytics objects. Run each
-- query on its own as the service role (e.g. Supabase SQL editor). UTC days.
--
-- Every query starts from the same eligible population as
-- product_analytics_facts(): role athletes, not analytics_excluded, not in the
-- durable internal-email registry. Put the deployed UNLXCK_ADMIN_EMAILS
-- (lowercase) into each admin_emails array so figures match the admin report.
--
-- "Meaningful" = product_activity.meaningful: saved training, check-ins,
-- sparring logs, custom plan creation/edits, session feedback, timer use and
-- nutrition save-days. Signing in, opening pages and generated plans never
-- count. Those core actions come from existing records and need no optional
-- consent. Opens, starts, timer exits and error events exist only for athletes
-- who opted in: read them as observed lower bounds, never as totals.

-- 1. Sign-ups versus activated users and first meaningful action, by signup week.
with admin_emails(email) as (select unnest('{}'::text[])),
eligible as (
 select p.id, p.created_at signup_at from public.profiles p
 where p.role='athlete' and not p.analytics_excluded
 and not exists(select 1 from public.product_analytics_excluded_emails x where x.email=lower(btrim(p.email)))
 and not exists(select 1 from admin_emails a where a.email=lower(btrim(p.email)))
), first_action as (
 select distinct on (a.athlete_id) a.athlete_id, a.occurred_at, a.feature||':'||a.action first_action
 from public.product_activity a join eligible e on e.id=a.athlete_id
 where a.meaningful and a.occurred_at<=now()
 order by a.athlete_id, a.occurred_at
)
select date_trunc('week', e.signup_at at time zone 'UTC')::date signup_week,
 count(*) signups,
 count(f.athlete_id) activated,
 round(100.0*count(f.athlete_id)/count(*),1) activated_pct,
 count(*) filter (where f.occurred_at<e.signup_at+interval '24 hours') activated_within_24h,
 round((percentile_cont(0.5) within group (order by extract(epoch from f.occurred_at-e.signup_at)/3600))::numeric,1) median_hours_to_first_action,
 mode() within group (order by f.first_action) most_common_first_action
from eligible e left join first_action f on f.athlete_id=e.id
group by 1 order by 1;

-- 2. D1 / D7 / D30 exact-day return after signup. Only cohorts whose return
--    day has fully elapsed enter each denominator.
with admin_emails(email) as (select unnest('{}'::text[])),
eligible as (
 select p.id, (p.created_at at time zone 'UTC')::date signup_day from public.profiles p
 where p.role='athlete' and not p.analytics_excluded
 and not exists(select 1 from public.product_analytics_excluded_emails x where x.email=lower(btrim(p.email)))
 and not exists(select 1 from admin_emails a where a.email=lower(btrim(p.email)))
), active_days as (
 select distinct a.athlete_id, (a.occurred_at at time zone 'UTC')::date as day
 from public.product_activity a join eligible e on e.id=a.athlete_id
 where a.meaningful and a.occurred_at<=now()
)
select n as return_day,
 count(*) filter (where e.signup_day+n < (now() at time zone 'UTC')::date) eligible,
 count(*) filter (where e.signup_day+n < (now() at time zone 'UTC')::date
  and exists(select 1 from active_days d where d.athlete_id=e.id and d.day=e.signup_day+n)) returned
from eligible e cross join (values (1),(7),(30)) r(n)
group by n order by n;

-- 3. Feature adoption and repeat use (2+ distinct meaningful UTC days), all time
--    and last 30 days. "opened_by" counts observed (opted-in) discovery only.
with admin_emails(email) as (select unnest('{}'::text[])),
eligible as (
 select p.id from public.profiles p
 where p.role='athlete' and not p.analytics_excluded
 and not exists(select 1 from public.product_analytics_excluded_emails x where x.email=lower(btrim(p.email)))
 and not exists(select 1 from admin_emails a where a.email=lower(btrim(p.email)))
), per_athlete as (
 select a.feature, a.athlete_id,
  count(distinct (a.occurred_at at time zone 'UTC')::date) filter (where a.meaningful) used_days,
  count(*) filter (where a.meaningful) actions,
  bool_or(a.meaningful and a.occurred_at>=now()-interval '30 days') used_30d,
  bool_or(a.action like '%\_opened') opened
 from public.product_activity a join eligible e on e.id=a.athlete_id
 where a.occurred_at<=now() and a.action not in ('plan_generated','onboarding_started','onboarding_completed')
 group by 1,2
)
select feature,
 count(*) filter (where used_days>0) users,
 round(100.0*count(*) filter (where used_days>0)/(select count(*) from eligible),1) adoption_pct,
 count(*) filter (where used_days>=2) repeat_users,
 count(*) filter (where used_30d) users_30d,
 sum(actions) actions,
 count(*) filter (where opened) opened_by,
 count(*) filter (where opened and used_days=0) opened_never_used
from per_athlete group by 1 order by users desc;

-- 4. Workflow abandonment. Training starts come from saved session records
--    (every athlete); the rest are opted-in observations. Excludes the
--    last 24h so in-progress work is not called abandoned.
with admin_emails(email) as (select unnest('{}'::text[])),
eligible as (
 select p.id from public.profiles p
 where p.role='athlete' and not p.analytics_excluded
 and not exists(select 1 from public.product_analytics_excluded_emails x where x.email=lower(btrim(p.email)))
 and not exists(select 1 from admin_emails a where a.email=lower(btrim(p.email)))
), ev as (
 select e.* from public.product_events e join eligible p on p.id=e.athlete_id
 where e.created_at<now()-interval '24 hours'
)
select 'training session' workflow, count(*) started,
 count(*) filter (where c.status in ('done','modified')) completed,
 count(*) filter (where c.status is distinct from 'done' and c.status is distinct from 'modified') unresolved
from public.session_completions c join eligible p on p.id=c.athlete_id
where c.started_at is not null and c.started_at<now()-interval '24 hours'
union all
select 'round timer', count(*),
 count(*) filter (where completed),
 count(*) filter (where not completed)
from (select flow_id, bool_or(event_name='timer_completed') completed
 from ev where feature='timer' and flow_id is not null group by flow_id
 having bool_or(event_name='timer_started')) t
union all
select 'round timer (explicit early exit)', count(distinct flow_id) filter (where event_name='timer_started'),
 null, count(distinct flow_id) filter (where event_name='timer_exited')
from ev where feature='timer'
union all
-- One check-in / sparring start per athlete per UTC day; completed when a saved
-- record exists for that athlete on that day.
select 'recovery check-in', count(*),
 count(*) filter (where done), count(*) filter (where not done)
from (select s.athlete_id, (s.created_at at time zone 'UTC')::date as day,
 exists(select 1 from public.product_activity a where a.athlete_id=s.athlete_id and a.feature='recovery'
  and a.action='checkin_completed' and (a.occurred_at at time zone 'UTC')::date=(s.created_at at time zone 'UTC')::date) done
 from ev s where s.event_name='checkin_started') x
union all
select 'sparring log', count(*),
 count(*) filter (where done), count(*) filter (where not done)
from (select s.athlete_id,
 exists(select 1 from public.sparring_logs l where l.athlete_id=s.athlete_id
  and (l.created_at at time zone 'UTC')::date=(s.created_at at time zone 'UTC')::date) done
 from ev s where s.event_name='sparring_log_started') x
union all
-- Plans: generated usable plan with no completed linked session within 7 days.
select 'generated plan (7-day use)', count(*),
 count(*) filter (where used), count(*) filter (where not used)
from (select exists(select 1 from public.session_completions c where c.plan_id=pl.id and c.athlete_id=pl.athlete_id
  and c.status in ('done','modified') and c.completed_at<pl.created_at+interval '7 days') used
 from public.plans pl join eligible p on p.id=pl.athlete_id
 where pl.status in ('ready','publishable_with_flags') and pl.created_at<now()-interval '7 days') x;

-- 5. Inactive testers: last meaningful action, what it was, and what they did
--    afterwards that was not meaningful (opt-in observations only).
with admin_emails(email) as (select unnest('{}'::text[])),
eligible as (
 select p.id, p.created_at from public.profiles p
 where p.role='athlete' and not p.analytics_excluded
 and not exists(select 1 from public.product_analytics_excluded_emails x where x.email=lower(btrim(p.email)))
 and not exists(select 1 from admin_emails a where a.email=lower(btrim(p.email)))
), last_action as (
 select distinct on (a.athlete_id) a.athlete_id, a.occurred_at, a.feature||':'||a.action action
 from public.product_activity a join eligible e on e.id=a.athlete_id
 where a.meaningful and a.occurred_at<=now()
 order by a.athlete_id, a.occurred_at desc
)
select e.id athlete_id, (e.created_at at time zone 'UTC')::date signed_up,
 l.occurred_at last_meaningful_at, l.action last_meaningful_action,
 ((now() at time zone 'UTC')::date-(l.occurred_at at time zone 'UTC')::date) days_inactive,
 (select count(*) from public.product_events x where x.athlete_id=e.id
  and x.event_name in ('operation_failed','frontend_error')) observed_errors,
 (select max(x.created_at) from public.product_events x where x.athlete_id=e.id) last_observed_event_at
from eligible e left join last_action l on l.athlete_id=e.id
where l.occurred_at is null or l.occurred_at<now()-interval '7 days'
order by l.occurred_at nulls first;

-- 6. Errors by feature (opt-in observations; Sentry holds technical detail),
--    plus failed plan generations from existing job records (every athlete).
with admin_emails(email) as (select unnest('{}'::text[])),
eligible as (
 select p.id from public.profiles p
 where p.role='athlete' and not p.analytics_excluded
 and not exists(select 1 from public.product_analytics_excluded_emails x where x.email=lower(btrim(p.email)))
 and not exists(select 1 from admin_emails a where a.email=lower(btrim(p.email)))
)
select e.event_name source, e.feature, count(*) errors, count(distinct e.athlete_id) athletes
from public.product_events e join eligible p on p.id=e.athlete_id
where e.event_name in ('operation_failed','frontend_error') and e.created_at>=now()-interval '30 days'
group by 1,2
union all
select 'generation_job_failed', 'plans', count(*), count(distinct j.athlete_id)
from public.generation_jobs j join eligible p on p.id=j.athlete_id
where j.status='failed' and j.created_at>=now()-interval '30 days'
order by errors desc;

-- 7. Consent reach: how much of the population optional events can describe.
with admin_emails(email) as (select unnest('{}'::text[])),
eligible as (
 select p.id, p.date_of_birth from public.profiles p
 where p.role='athlete' and not p.analytics_excluded
 and not exists(select 1 from public.product_analytics_excluded_emails x where x.email=lower(btrim(p.email)))
 and not exists(select 1 from admin_emails a where a.email=lower(btrim(p.email)))
)
select count(*) eligible_athletes,
 count(*) filter (where e.date_of_birth<=((now() at time zone 'UTC')::date-interval '18 years')::date) adults,
 count(c.athlete_id) made_a_choice,
 count(*) filter (where c.enabled) opted_in,
 public.product_analytics_retention_health()->>'healthy' collection_healthy
from eligible e left join public.product_analytics_preferences c on c.athlete_id=e.id;

-- 8. Plan-to-training activation. One row per usable generated plan (a
--    completed generation job linked to a ready/publishable plan), so the
--    first-ever plan and later regenerations are measured separately:
--    cohort = 'first_plan' when the athlete had no earlier usable plan (ready,
--    publishable or since archived; failed builds don't count), 'regenerated'
--    otherwise. Segments: fight = scheduled_fight (fight_date
--    set) vs open_camp (no fight date); plan_source = the job's source
--    (quick_build / self_serve / ...); starting_path = current saved profile
--    value. Training intent itself is not captured; these are proxies.
--    Every window starts at available_at: when the plan's generation job
--    completed (the earliest completed job, so a retry never restarts the
--    clock), falling back to plans.created_at only when no completion time
--    exists. Metrics only count plans old enough for their window, so in-progress
--    windows never read as failures. Session evidence is session_completions
--    rows for that exact plan: an observed start (started_at) and a logged
--    done/modified completion (completed_at). First scheduled session = the
--    earliest structured-plan day on/after the available day that schedules at
--    least one session; open (undated) plans have none and are reported as
--    null there, not as misses. week1_return = a meaningful action on any
--    later UTC day within 7 days of availability (generating is never one).
with admin_emails(email) as (select unnest('{}'::text[])),
eligible as (
 select p.id, p.starting_path from public.profiles p
 where p.role='athlete' and not p.analytics_excluded
 and not exists(select 1 from public.product_analytics_excluded_emails x where x.email=lower(btrim(p.email)))
 and not exists(select 1 from admin_emails a where a.email=lower(btrim(p.email)))
), generated as (
 select distinct on (pl.id) pl.id plan_id, pl.athlete_id, pl.created_at,
  coalesce(j.completed_at, pl.created_at) available_at, pl.fight_date, pl.structured_plan,
  coalesce(nullif(j.source,''),'unknown') plan_source, coalesce(e.starting_path,'unknown') starting_path,
  case when exists(select 1 from public.plans prior where prior.athlete_id=pl.athlete_id
   and prior.status in ('ready','publishable_with_flags','archived')
   and (prior.created_at, prior.id) < (pl.created_at, pl.id)) then 'regenerated' else 'first_plan' end cohort
 from public.plans pl
 join eligible e on e.id=pl.athlete_id
 join public.generation_jobs j on j.plan_id=pl.id and j.status='completed'
 where pl.status in ('ready','publishable_with_flags')
 order by pl.id, j.completed_at asc nulls last
), facts as (
 select g.*,
  (select min(c.started_at) from public.session_completions c
   where c.plan_id=g.plan_id and c.athlete_id=g.athlete_id and c.started_at>=g.available_at) first_start_at,
  (select min(c.completed_at) from public.session_completions c
   where c.plan_id=g.plan_id and c.athlete_id=g.athlete_id and c.status in ('done','modified')
   and c.completed_at>=g.available_at) first_logged_at,
  (select min((d->>'date')::date)
   from jsonb_array_elements(case when jsonb_typeof(g.structured_plan->'weeks')='array' then g.structured_plan->'weeks' else '[]'::jsonb end) w,
    jsonb_array_elements(case when jsonb_typeof(w->'days')='array' then w->'days' else '[]'::jsonb end) d
   where (d->>'date') ~ '^\d{4}-\d{2}-\d{2}$'
   and jsonb_typeof(d->'sessions')='array' and jsonb_array_length(d->'sessions')>0
   and (d->>'date')::date>=(g.available_at at time zone 'UTC')::date) first_scheduled_day
 from generated g
), scored as (
 select f.*,
  f.available_at<now()-interval '48 hours' mature_48h,
  f.available_at<now()-interval '8 days' mature_7d,
  f.first_scheduled_day is not null and f.first_scheduled_day<(now() at time zone 'UTC')::date-1 scheduled_elapsed,
  exists(select 1 from public.session_completions c where c.plan_id=f.plan_id and c.athlete_id=f.athlete_id
   and c.training_day=f.first_scheduled_day and c.status in ('done','modified')) first_scheduled_completed,
  exists(select 1 from public.product_activity a where a.athlete_id=f.athlete_id and a.meaningful
   and (a.occurred_at at time zone 'UTC')::date>(f.available_at at time zone 'UTC')::date
   and a.occurred_at<f.available_at+interval '7 days') week1_return
 from facts f
)
select cohort,
 case when fight_date is null then 'open_camp' else 'scheduled_fight' end fight,
 plan_source, starting_path,
 count(*) plans,
 count(*) filter (where first_start_at is not null) first_session_started_ever,
 count(*) filter (where mature_48h) plans_48h_mature,
 count(*) filter (where mature_48h and first_start_at<available_at+interval '48 hours') started_within_48h,
 count(*) filter (where mature_48h and first_logged_at<available_at+interval '48 hours') logged_within_48h,
 count(*) filter (where scheduled_elapsed) dated_first_session_elapsed,
 count(*) filter (where scheduled_elapsed and first_scheduled_completed) first_scheduled_session_completed,
 count(*) filter (where mature_7d) plans_7d_mature,
 count(*) filter (where mature_7d and week1_return) returned_within_7d,
 round((percentile_cont(0.5) within group (order by extract(epoch from first_logged_at-available_at)/3600)
  filter (where first_logged_at is not null))::numeric,1) median_hours_to_first_log
from scored
group by grouping sets ((cohort), (cohort, fight), (cohort, fight, plan_source, starting_path))
order by cohort, fight nulls first, plan_source nulls first, starting_path nulls first;
