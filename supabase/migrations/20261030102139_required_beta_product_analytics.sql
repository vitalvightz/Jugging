-- Required first-party usage measurement for the agreed private beta.
-- Existing optional choices remain historical evidence, never rewritten as consent.
create table public.product_analytics_rollout (
 singleton boolean primary key default true check(singleton),
 started_at timestamptz not null default now()
);
alter table public.product_analytics_rollout enable row level security;
revoke all on public.product_analytics_rollout from public,anon,authenticated;
grant select on public.product_analytics_rollout to service_role;
insert into public.product_analytics_rollout(singleton) values(true);
alter table public.product_events drop constraint product_events_event_name_check;
alter table public.product_events add constraint product_events_event_name_check check (event_name in (
 'plan_opened','plan_editor_opened','plan_edited','plan_replaced',
 'timer_started','timer_round_completed','timer_completed','timer_exited',
 'history_opened','nutrition_opened','nutrition_saved','feedback_prompt_shown',
 'checkin_started','sparring_log_started',
 'app_opened','page_viewed','operation_failed','frontend_error'));

drop index public.product_event_discovery_daily;
create unique index product_event_discovery_daily on public.product_events
(athlete_id,event_name,feature,(coalesce(flow_id,'00000000-0000-0000-0000-000000000000'::uuid)),((created_at at time zone 'UTC')::date))
where event_name in ('plan_opened','plan_editor_opened','history_opened','nutrition_opened','nutrition_saved','plan_edited','plan_replaced',
 'checkin_started','sparring_log_started');

create or replace function public.ingest_product_event(p_athlete_id uuid,p_event_name text,p_feature text,p_request_id uuid,p_flow_id uuid default null)
returns boolean language plpgsql security invoker set search_path=public,pg_temp as $$
begin
 -- Serialize the cap per athlete, including concurrent requests.
 perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text,0));
 if exists(select 1 from public.profiles p join public.product_analytics_excluded_emails x on x.email=lower(btrim(p.email)) where p.id=p_athlete_id) then return false; end if;
 if not coalesce((public.product_analytics_retention_health()->>'healthy')::boolean,false) then return false; end if;
 if not exists(select 1 from public.profiles where id=p_athlete_id and role='athlete' and not analytics_excluded
   and not exists(select 1 from public.product_analytics_excluded_emails x where x.email=lower(btrim(profiles.email)))) then return false; end if;
 if (select count(*) from public.product_events where athlete_id=p_athlete_id and created_at>now()-interval '1 minute')>=120 then return false; end if;
 if (select count(*) from public.product_events where athlete_id=p_athlete_id and created_at >= date_trunc('day',now() at time zone 'UTC') at time zone 'UTC')>=1000 then return false; end if;
 insert into public.product_events(athlete_id,event_name,feature,request_id,flow_id)
 values(p_athlete_id,p_event_name,p_feature,p_request_id,p_flow_id) on conflict do nothing;
 return true;
end $$;
revoke all on function public.ingest_product_event(uuid,text,text,uuid,uuid) from public,anon,authenticated;
grant execute on function public.ingest_product_event(uuid,text,text,uuid,uuid) to service_role;

create or replace function public.product_analytics_facts(p_excluded_emails text[] default '{}', p_athlete_id uuid default null)
returns jsonb language sql stable security invoker set search_path=public,pg_temp as $$
 with athletes as (
 select p.id, p.full_name, p.email, p.created_at, p.professional_status, p.technical_style, p.starting_path,
 (select min(created_at) from public.plans where athlete_id=p.id and status in ('ready','publishable_with_flags')) first_usable_plan_at
 from public.profiles p where (p_athlete_id is null and p.role='athlete' and not p.analytics_excluded
 and (p.email is null or not (lower(btrim(p.email))=any(p_excluded_emails)))
 and not exists(select 1 from public.product_analytics_excluded_emails x where x.email=lower(btrim(p.email)))) or p.id=p_athlete_id
 ), facts as (
 select a.athlete_id, (a.occurred_at at time zone 'UTC')::date as day, a.feature, a.action, a.meaningful,
 count(*) as count, min(a.occurred_at) as first_at, max(a.occurred_at) as last_at
 from public.product_activity a join athletes p on p.id=a.athlete_id
 where a.occurred_at <= now() group by 1,2,3,4,5
 ) select jsonb_build_object('athletes',coalesce((select jsonb_agg(to_jsonb(p)) from athletes p),'[]'::jsonb),
 'facts',coalesce((select jsonb_agg(to_jsonb(f)) from facts f),'[]'::jsonb),
 'timer_flows',coalesce((select jsonb_agg(x) from (
 select e.athlete_id,e.flow_id,min(e.created_at) filter(where event_name='timer_started') started_at,
 max(e.created_at) filter(where event_name='timer_completed') completed_at,
 max(e.created_at) filter(where event_name='timer_exited') exited_at
 from public.product_events e join athletes p on p.id=e.athlete_id where feature='timer' and flow_id is not null
 group by 1,2 having bool_or(event_name='timer_started')
 ) x),'[]'::jsonb),
 'tracking_health',public.product_analytics_retention_health(),
 'tracking_since',(select started_at from public.product_analytics_rollout where singleton),
 'optional_coverage',coalesce((select jsonb_agg(jsonb_build_object('athlete_id',p.id,'since',greatest(p.created_at,r.started_at)))
 from athletes p cross join public.product_analytics_rollout r
 where coalesce((public.product_analytics_retention_health()->>'healthy')::boolean,false)),'[]'::jsonb),
 'plans_used',coalesce((select jsonb_agg(x) from (
 select pl.id,pl.athlete_id,pl.created_at,pl.status,
 -- Keep the old private fields through migration-first deployment/rollback.
 -- The corrected calculator never uses these unbounded legacy booleans.
 exists(select 1 from public.product_activity a where a.athlete_id=pl.athlete_id and a.flow_id=pl.id::text
  and a.action<>'plan_generated' and a.occurred_at>=pl.created_at and a.occurred_at<=now()) interacted,
 exists(select 1 from public.session_completions c where c.plan_id=pl.id and c.athlete_id=pl.athlete_id
  and c.status in ('done','modified') and c.completed_at>=pl.created_at and c.completed_at<=now()) used,
 (select min(e.created_at) from public.product_events e where e.athlete_id=pl.athlete_id
  and e.flow_id=pl.id and e.event_name='plan_opened' and e.created_at>=pl.created_at and e.created_at<=now()) opened_at,
 exists(select 1 from public.session_completions c where c.plan_id=pl.id and c.athlete_id=pl.athlete_id
  and c.status in ('done','modified') and c.completed_at>=pl.created_at
  and c.completed_at<pl.created_at+interval '7 days' and c.completed_at<=now()) completed_within_7d
 from public.plans pl join athletes p on p.id=pl.athlete_id where pl.created_at<=now()
 ) x),'[]'::jsonb),
 'sessions',coalesce((select jsonb_agg(x) from (
 select c.id,c.athlete_id,c.plan_id,c.started_at,c.completed_at,c.status from public.session_completions c
 join athletes p on p.id=c.athlete_id
 ) x),'[]'::jsonb),
 -- Stable identities, never a same-day/same-athlete guess. A linked sparring
 -- detail and its persisted training session represent one training record.
 'training_records',coalesce((select jsonb_agg(x) from (
 select 'planned:'||c.id::text id,c.athlete_id,c.completed_at occurred_at
 from public.session_completions c join athletes p on p.id=c.athlete_id
 where c.status in ('done','modified') and c.completed_at<=now()
 union all
 select 'manual:'||m.id::text,m.athlete_id,m.created_at from public.external_training_sessions m
 join athletes p on p.id=m.athlete_id where m.deleted_at is null and m.created_at<=now()
 union all
 select 'sparring:'||s.id::text,s.athlete_id,s.created_at from public.sparring_logs s
 join athletes p on p.id=s.athlete_id where s.created_at<=now()
 and not exists(select 1 from public.external_training_sessions m where m.id=s.external_session_id and m.athlete_id=s.athlete_id and m.deleted_at is null)
 and not exists(select 1 from public.session_completions c where c.athlete_id=s.athlete_id and c.plan_id=s.plan_id
  and c.session_id=s.session_id and c.training_day=s.training_day and c.status in ('done','modified') and c.completed_at<=now())
 ) x),'[]'::jsonb),
 'error_returns',coalesce((select jsonb_agg(jsonb_build_object('athlete_id',e.athlete_id,'feature',e.feature,
 'action',e.action,'error_at',e.last_at,'returned',exists(select 1 from public.product_activity a
 where a.athlete_id=e.athlete_id and a.meaningful and a.occurred_at>e.last_at
 and a.occurred_at<=e.last_at+interval '7 days' and a.occurred_at<=now())))
 from facts e where e.action in ('operation_failed','frontend_error')),'[]'::jsonb),
 'generation_errors',coalesce((select jsonb_agg(x) from (
 select j.athlete_id, (j.created_at at time zone 'UTC')::date as day, count(*) as count
 from public.generation_jobs j join athletes p on p.id=j.athlete_id where j.status='failed' group by 1,2
 ) x),'[]'::jsonb))
 $$;
revoke all on function public.product_analytics_facts(text[],uuid) from public, anon, authenticated;
grant execute on function public.product_analytics_facts(text[],uuid) to service_role;

