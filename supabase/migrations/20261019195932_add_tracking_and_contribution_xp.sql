-- Tracking-first XP and approved YouTube contributions. Backend-only earning.
-- Apply after the manual exercise migration and before the new backend.
-- Existing awards, active-plan guards, RLS and historical totals are retained.

alter table public.xp_awards
  drop constraint if exists xp_awards_action_check;

alter table public.xp_awards
  add constraint xp_awards_action_check check (action in (
    'daily_login',
    'training_logged',
    'planned_session_completed',
    'recommended_fighter_content_watched',
    'full_training_week_completed',
    'profile_completed',
    'first_intake_completed',
    'first_plan_ready',
    'first_checkin_completed',
    'readiness_checkin_completed',
    'injury_update_completed',
    'stop_decision_followed',
    'feedback_submitted',
    'feedback_with_comment',
    'first_plan_completed',
    'phase_completed',
    'camp_completed',
    'manual_training_logged',
    'first_session_logged',
    'first_exercise_logged',
    'training_days_10',
    'training_days_25',
    'training_days_50',
    'training_days_100',
    'exercise_contribution_approved'
  ));

alter table public.xp_awards
  drop constraint if exists xp_awards_amount_check;

alter table public.xp_awards
  add constraint xp_awards_amount_check check (
    (action = 'daily_login' and amount = 10)
    or (action = 'training_logged' and amount = 25)
    or (action = 'planned_session_completed' and amount = 50)
    or (action = 'recommended_fighter_content_watched' and amount = 10)
    or (action = 'full_training_week_completed' and amount = 100)
    or (action = 'profile_completed' and amount = 25)
    or (action = 'first_intake_completed' and amount = 50)
    or (action = 'first_plan_ready' and amount = 100)
    or (action = 'first_checkin_completed' and amount = 25)
    or (action = 'readiness_checkin_completed' and amount = 10)
    or (action = 'injury_update_completed' and amount = 10)
    or (action = 'stop_decision_followed' and amount = 15)
    or (action = 'feedback_submitted' and amount = 1)
    or (action = 'feedback_with_comment' and amount = 3)
    or (action = 'first_plan_completed' and amount = 250)
    or (action = 'phase_completed' and amount = 200)
    or (action = 'camp_completed' and amount = 500)
    or (action = 'manual_training_logged' and amount = 25)
    or (action = 'first_session_logged' and amount = 50)
    or (action = 'first_exercise_logged' and amount = 25)
    or (action = 'training_days_10' and amount = 100)
    or (action = 'training_days_25' and amount = 150)
    or (action = 'training_days_50' and amount = 250)
    or (action = 'training_days_100' and amount = 500)
    or (action = 'exercise_contribution_approved' and amount = 100)
  );



-- Derive all evidence from owned, directly reported occurrences; never day credit.
create or replace function public.xp_recorded_training_days(p_athlete_id uuid)
returns table(training_day date) language sql stable security invoker
set search_path = pg_catalog, public as $$
  with today as (
    select ((now() at time zone coalesce(nullif(athlete_timezone,''),'UTC'))
      - interval '3 hours')::date as day from public.profiles where id=p_athlete_id
  )
  select s.training_day from public.external_training_sessions s, today
  where s.athlete_id=p_athlete_id and s.deleted_at is null and s.training_day<=today.day
  union
  select s.training_day from public.session_completions s, today
  where s.athlete_id=p_athlete_id and s.completion_origin='reported'
    and s.status in ('done','modified') and s.training_day<=today.day;
$$;

create or replace function public.xp_has_measurements(p_values jsonb)
returns boolean language sql immutable security invoker
set search_path = pg_catalog, public as $$
  select jsonb_typeof(p_values)='object' and not exists (
    select 1 from jsonb_each(p_values) item
    where (item.key in ('sets','reps','rounds') and item.value='0'::jsonb)
      or (item.key in ('duration','work','distance') and item.value->'value'='0'::jsonb)
  ) and exists (
    select 1 from jsonb_each(p_values) item
    where item.key in ('sets','reps','rounds','load','duration','work','distance')
      and case when jsonb_typeof(item.value)='number' then (item.value::text)::numeric>0
        when jsonb_typeof(item.value)='object' and jsonb_typeof(item.value->'value')='number'
          then (item.value->>'value')::numeric>0 else false end
  );
$$;

create or replace function public.xp_has_exercise_result(p_athlete_id uuid)
returns boolean language sql stable security invoker
set search_path = pg_catalog, public as $$
  with today as (
    select ((now() at time zone coalesce(nullif(athlete_timezone,''),'UTC'))
      - interval '3 hours')::date as day from public.profiles where id=p_athlete_id
  )
  select exists (
    select 1 from public.exercise_logs e, today
    where e.athlete_id=p_athlete_id and e.training_day<=today.day
      and e.status in ('as_prescribed','modified')
      and coalesce(e.prescribed->>'block_type','') not in ('rehab','mindset','nutrition','sparring')
      and public.xp_has_measurements(
        (case when e.status='as_prescribed' then e.prescribed else '{}'::jsonb end) || e.actual)
    union all
    select 1 from public.manual_exercise_entries e
    join public.external_training_sessions s on s.id=e.external_session_id and s.athlete_id=e.athlete_id
    cross join today
    where e.athlete_id=p_athlete_id and e.deleted_at is null and s.deleted_at is null
      and s.training_day<=today.day and public.xp_has_measurements(e.actual)
  );
$$;

-- Source validation also protects direct service-role inserts. Account locks
-- serialize the shared daily credit across manual and prescribed save races.
create or replace function public.enforce_tracking_xp_source()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
declare v_day date; v_days integer; v_threshold integer;
begin
  if new.action not in ('manual_training_logged','first_session_logged','first_exercise_logged',
    'training_days_10','training_days_25','training_days_50','training_days_100',
    'exercise_contribution_approved','training_logged') then return new; end if;
  perform pg_advisory_xact_lock(hashtextextended(new.athlete_id::text,0));
  if new.action='training_logged' then
    if exists(select 1 from public.xp_awards where athlete_id=new.athlete_id
      and action='manual_training_logged' and calendar_date=new.calendar_date) then return null; end if;
    return new;
  end if;
  if new.action='manual_training_logged' then
    if not exists (
      select 1 from public.external_training_sessions s
      where s.athlete_id=new.athlete_id and s.deleted_at is null
        and new.idempotency_key='manual-training:'||s.id::text and s.training_day=new.calendar_date
        and s.training_day in (select training_day from public.xp_recorded_training_days(new.athlete_id))
    ) then raise exception 'manual XP requires an owned live training occurrence' using errcode='23514'; end if;
    if exists(select 1 from public.xp_awards where athlete_id=new.athlete_id
      and action in ('manual_training_logged','training_logged') and calendar_date=new.calendar_date)
      then return null; end if;
    return new;
  end if;
  if new.action='exercise_contribution_approved' then
    if not exists(
      select 1 from public.community_exercises e
      join public.community_exercise_revisions r on r.id=e.published_revision_id and r.exercise_id=e.id
      join public.private_exercise_media m on m.id=r.media_id
      where e.creator_id=new.athlete_id and e.attribution_consent and e.withdrawn_at is null
        and r.status='published' and r.confirmed_at is not null and r.verified_at is not null
        and m.provider='youtube' and r.verified_proof->>'youtube_id'=m.video_id
        and r.verified_proof->>'youtube_verified'='true'
        and public.community_revision_available(r.id)
        and new.idempotency_key='exercise-contribution:'||e.id::text||':'||m.video_id
    ) then raise exception 'contribution XP requires an approved public YouTube demo' using errcode='23514'; end if;
    return new;
  end if;
  if new.idempotency_key<>new.action||':'||new.athlete_id::text then
    raise exception 'invalid tracking milestone key' using errcode='23514'; end if;
  if new.action='first_exercise_logged' then
    if not public.xp_has_exercise_result(new.athlete_id) then
      raise exception 'first exercise XP requires recorded measurements' using errcode='23514'; end if;
  else
    select count(*) into v_days from public.xp_recorded_training_days(new.athlete_id);
    v_threshold:=case when new.action='first_session_logged' then 1
      else split_part(new.action,'_',3)::integer end;
    if v_days<v_threshold then raise exception 'training day milestone not reached' using errcode='23514'; end if;
  end if;
  return new;
end; $$;

drop trigger if exists xp_awards_00_tracking_source on public.xp_awards;
create trigger xp_awards_00_tracking_source before insert on public.xp_awards
for each row execute function public.enforce_tracking_xp_source();

create unique index if not exists xp_awards_tracking_one_time
on public.xp_awards(athlete_id,action) where action in (
  'first_session_logged','first_exercise_logged','training_days_10','training_days_25','training_days_50','training_days_100');
create unique index if not exists xp_awards_manual_daily
on public.xp_awards(athlete_id,calendar_date) where action='manual_training_logged';
-- Revision changes/reapproval cannot earn twice for the same exercise.
create unique index if not exists xp_awards_contribution_exercise
on public.xp_awards(athlete_id,split_part(idempotency_key,':',2)) where action='exercise_contribution_approved';
-- A duplicate YouTube video, including another clip or account, is not another contribution.
create unique index if not exists xp_awards_contribution_video
on public.xp_awards(split_part(idempotency_key,':',3)) where action='exercise_contribution_approved';

alter table public.xp_awards drop constraint if exists xp_awards_calendar_scope_check;
alter table public.xp_awards add constraint xp_awards_calendar_scope_check check (
  (action in ('daily_login','training_logged','manual_training_logged','planned_session_completed',
    'full_training_week_completed','readiness_checkin_completed','injury_update_completed',
    'stop_decision_followed','feedback_submitted','feedback_with_comment')) = (calendar_date is not null));

create or replace function public.reconcile_training_engagement_xp(
  p_athlete_id uuid, p_manual_session_id uuid default null
) returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare v_days integer; v_item record; v_day date; v_results jsonb:='[]'::jsonb; v_result jsonb;
begin
  if coalesce(auth.role(),'')<>'service_role' then raise exception 'backend only' using errcode='42501'; end if;
  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text,0));
  if p_manual_session_id is not null then
    select training_day into v_day from public.external_training_sessions
    where id=p_manual_session_id and athlete_id=p_athlete_id and deleted_at is null;
    if v_day is not null then
      v_result:=public.award_athlete_xp(p_athlete_id,'manual_training_logged','manual-training:'||p_manual_session_id::text,v_day);
      v_results:=v_results||jsonb_build_array(v_result);
    end if;
  end if;
  select count(*) into v_days from public.xp_recorded_training_days(p_athlete_id);
  for v_item in select * from (values ('first_session_logged',1),('training_days_10',10),
    ('training_days_25',25),('training_days_50',50),('training_days_100',100)) rewards(action,threshold)
  loop
    if v_days>=v_item.threshold and not exists(select 1 from public.xp_awards where athlete_id=p_athlete_id and action=v_item.action) then
      v_results:=v_results||jsonb_build_array(public.award_athlete_xp(p_athlete_id,v_item.action,v_item.action||':'||p_athlete_id::text));
    end if;
  end loop;
  if not exists(select 1 from public.xp_awards where athlete_id=p_athlete_id and action='first_exercise_logged')
    and public.xp_has_exercise_result(p_athlete_id) then
    v_results:=v_results||jsonb_build_array(public.award_athlete_xp(p_athlete_id,'first_exercise_logged','first_exercise_logged:'||p_athlete_id::text));
  end if;
  -- The existing athlete-wide award covers either route to a usable plan.
  if not exists(select 1 from public.xp_awards where athlete_id=p_athlete_id and action='first_plan_ready')
    and exists(select 1 from public.plans where athlete_id=p_athlete_id and status in ('ready','publishable_with_flags')) then
    v_results:=v_results||jsonb_build_array(public.award_athlete_xp(p_athlete_id,'first_plan_ready','first-plan-ready:'||p_athlete_id::text));
  end if;
  return v_results;
end; $$;

create or replace function public.reconcile_approved_contribution_xp(p_revision_id uuid)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare v_creator uuid; v_exercise uuid; v_video text; v_zone text; v_result jsonb;
begin
  if coalesce(auth.role(),'')<>'service_role' then raise exception 'backend only' using errcode='42501'; end if;
  select e.creator_id,e.id,m.video_id,p.athlete_timezone into v_creator,v_exercise,v_video,v_zone
  from public.community_exercises e
  join public.community_exercise_revisions r on r.id=e.published_revision_id and r.exercise_id=e.id
  join public.private_exercise_media m on m.id=r.media_id
  join public.profiles p on p.id=e.creator_id and p.role::text='athlete'
  where r.id=p_revision_id and r.status='published' and m.provider='youtube';
  if v_creator is null then return null; end if;
  v_result:=public.award_athlete_xp(v_creator,'exercise_contribution_approved',
    'exercise-contribution:'||v_exercise::text||':'||v_video);
  return jsonb_build_object('athlete_id',v_creator,'timezone',coalesce(v_zone,'UTC'),'results',jsonb_build_array(v_result));
end; $$;

-- Discover missed approvals from durable publication state. Keyset pagination
-- lets the worker pass a failing row and revisit it on the next full scan.
create or replace function public.list_unrewarded_contribution_revisions(
  p_after_id uuid default null, p_limit integer default 50
)
returns table(exercise_id uuid, revision_id uuid)
language plpgsql stable security invoker set search_path=pg_catalog,public as $$
begin
  if coalesce(auth.role(),'')<>'service_role' then raise exception 'backend only' using errcode='42501'; end if;
  return query
  select e.id,r.id
  from public.community_exercises e
  join public.community_exercise_revisions r on r.id=e.published_revision_id and r.exercise_id=e.id
  join public.private_exercise_media m on m.id=r.media_id
  join public.profiles p on p.id=e.creator_id and p.role::text='athlete'
  where (p_after_id is null or e.id>p_after_id)
    and e.attribution_consent and e.withdrawn_at is null
    and r.status='published' and r.confirmed_at is not null and r.verified_at is not null
    and m.provider='youtube' and r.verified_proof->>'youtube_id'=m.video_id
    and r.verified_proof->>'youtube_verified'='true'
    and public.community_revision_available(r.id)
    and not exists (
      select 1 from public.xp_awards a where a.action='exercise_contribution_approved'
        and ((a.athlete_id=e.creator_id and split_part(a.idempotency_key,':',2)=e.id::text)
          or split_part(a.idempotency_key,':',3)=m.video_id)
    )
  order by e.id limit greatest(1,least(coalesce(p_limit,50),100));
end; $$;

create or replace function public.award_athlete_xp(
  p_athlete_id uuid,
  p_action text,
  p_idempotency_key text,
  p_calendar_date date default null
)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_action text := btrim(coalesce(p_action, ''));
  v_key text := btrim(coalesce(p_idempotency_key, ''));
  v_amount integer;
  v_previous bigint;
  v_total bigint;
  v_last_login date;
  v_award public.xp_awards%rowtype;
  v_awarded boolean := false;
  v_recent jsonb;
  v_calendar_scoped boolean;
  v_calendar_date date := p_calendar_date;
begin
  if coalesce(auth.role(), '') <> 'service_role' then
    raise exception 'award_athlete_xp is restricted to the backend service role'
      using errcode = '42501';
  end if;
  if p_athlete_id is null or not exists (
    select 1
    from public.profiles
    where id = p_athlete_id
      and role::text = 'athlete'
  ) then
    raise exception 'xp athlete profile not found' using errcode = '23503';
  end if;
  if char_length(v_key) not between 1 and 200 then
    raise exception 'invalid XP idempotency key' using errcode = '22023';
  end if;

  v_amount := case v_action
    when 'daily_login' then 0
    when 'training_logged' then 25
    when 'planned_session_completed' then 50
    when 'recommended_fighter_content_watched' then 10
    when 'full_training_week_completed' then 100
    when 'profile_completed' then 25
    when 'first_intake_completed' then 50
    when 'first_plan_ready' then 100
    when 'first_checkin_completed' then 25
    when 'readiness_checkin_completed' then 10
    when 'injury_update_completed' then 10
    when 'stop_decision_followed' then 15
    when 'feedback_submitted' then 1
    when 'feedback_with_comment' then 3
    when 'first_plan_completed' then 250
    when 'phase_completed' then 200
    when 'camp_completed' then 500
    when 'manual_training_logged' then 25
    when 'first_session_logged' then 50
    when 'first_exercise_logged' then 25
    when 'training_days_10' then 100
    when 'training_days_25' then 150
    when 'training_days_50' then 250
    when 'training_days_100' then 500
    when 'exercise_contribution_approved' then 100
  end;
  if v_amount is null then
    raise exception 'unknown XP action' using errcode = '22023';
  end if;
  if v_action in ('feedback_submitted', 'feedback_with_comment') then
    raise exception 'feedback XP must use reconcile_feedback_xp'
      using errcode = '22023';
  end if;

  v_calendar_scoped := v_action in (
    'daily_login',
    'training_logged',
    'manual_training_logged',
    'planned_session_completed',
    'full_training_week_completed',
    'readiness_checkin_completed',
    'injury_update_completed',
    'stop_decision_followed'
  );

  if v_calendar_scoped and v_calendar_date is null then
    v_calendar_date := public.xp_legacy_calendar_date(
      p_athlete_id,
      v_action,
      v_key
    );
  end if;

  -- Daily login is retired and writes no row, so an old caller with no date may
  -- still receive the zero-XP compatibility response.
  if v_action <> 'daily_login'
    and v_calendar_scoped
    and v_calendar_date is null then
    raise exception 'invalid calendar scope for XP action' using errcode = '22023';
  end if;
  if not v_calendar_scoped and v_calendar_date is not null then
    raise exception 'invalid calendar scope for XP action' using errcode = '22023';
  end if;

  if v_action = 'profile_completed' and not exists (
    select 1
    from public.profiles p
    where p.id = p_athlete_id
      and char_length(btrim(coalesce(p.full_name, ''))) > 0
      and exists (
        select 1
        from unnest(coalesce(p.technical_style, array[]::text[])) sport(value)
        where lower(btrim(sport.value)) in ('boxing', 'kickboxing', 'mma')
      )
  ) then
    raise exception 'profile activation milestone is not complete'
      using errcode = '23514';
  end if;

  if v_action = 'first_intake_completed' and not exists (
    select 1
    from public.athlete_intakes
    where athlete_id = p_athlete_id
  ) then
    raise exception 'intake activation milestone is not complete'
      using errcode = '23514';
  end if;

  if v_action = 'first_plan_ready' and not exists (
    select 1
    from public.plans
    where athlete_id = p_athlete_id
      and status in ('ready', 'publishable_with_flags')
  ) then
    raise exception 'plan activation milestone is not complete'
      using errcode = '23514';
  end if;

  if v_action = 'first_checkin_completed' and not exists (
    select 1
    from public.today_checkins
    where athlete_id = p_athlete_id
  ) then
    raise exception 'first check-in milestone is not complete'
      using errcode = '23514';
  end if;

  if v_action = 'readiness_checkin_completed' and not exists (
    select 1
    from public.today_checkins
    where athlete_id = p_athlete_id
      and training_day = v_calendar_date
  ) then
    raise exception 'daily check-in milestone is not complete'
      using errcode = '23514';
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text, 0));

  insert into public.xp_accounts (athlete_id)
  values (p_athlete_id)
  on conflict (athlete_id) do nothing;

  select total_xp, last_daily_login_date
  into v_previous, v_last_login
  from public.xp_accounts
  where athlete_id = p_athlete_id
  for update;

  if v_action <> 'daily_login' then
    insert into public.xp_awards (
      athlete_id,
      action,
      amount,
      idempotency_key,
      calendar_date
    ) values (
      p_athlete_id,
      v_action,
      v_amount,
      v_key,
      v_calendar_date
    )
    on conflict do nothing
    returning * into v_award;
    v_awarded := v_award.id is not null;
  end if;

  if v_awarded then
    update public.xp_accounts
    set
      total_xp = total_xp + v_amount,
      updated_at = clock_timestamp()
    where athlete_id = p_athlete_id
    returning total_xp, last_daily_login_date
    into v_total, v_last_login;
  else
    v_total := v_previous;
  end if;

  select coalesce(
    jsonb_agg(item order by awarded_at desc, id desc),
    '[]'::jsonb
  )
  into v_recent
  from (
    select
      award.id,
      award.awarded_at,
      jsonb_strip_nulls(jsonb_build_object(
        'id', award.id,
        'action', award.action,
        'amount', award.amount,
        'awarded_at', award.awarded_at,
        'calendar_date', award.calendar_date
      )) item
    from public.xp_awards award
    where award.athlete_id = p_athlete_id
    order by award.awarded_at desc, award.id desc
    limit 20
  ) recent;

  return jsonb_build_object(
    'state', jsonb_build_object(
      'total_xp', v_total,
      'last_daily_login_date', v_last_login,
      'recent_awards', v_recent
    ),
    'previous_total_xp', v_previous,
    'awarded', v_awarded,
    'award', case
      when v_awarded then jsonb_strip_nulls(jsonb_build_object(
        'id', v_award.id,
        'action', v_award.action,
        'amount', v_award.amount,
        'awarded_at', v_award.awarded_at,
        'calendar_date', v_award.calendar_date
      ))
      else 'null'::jsonb
    end
  );
end;
$$;

revoke all on function public.award_athlete_xp(uuid, text, text, date)
  from public, anon, authenticated;
grant execute on function public.award_athlete_xp(uuid, text, text, date)
  to service_role;

revoke all on function public.xp_recorded_training_days(uuid) from public,anon,authenticated;
grant execute on function public.xp_recorded_training_days(uuid) to service_role;
revoke all on function public.xp_has_measurements(jsonb) from public,anon,authenticated;
grant execute on function public.xp_has_measurements(jsonb) to service_role;
revoke all on function public.xp_has_exercise_result(uuid) from public,anon,authenticated;
grant execute on function public.xp_has_exercise_result(uuid) to service_role;
revoke all on function public.enforce_tracking_xp_source() from public,anon,authenticated;
grant execute on function public.enforce_tracking_xp_source() to service_role;
revoke all on function public.reconcile_training_engagement_xp(uuid,uuid) from public,anon,authenticated;
grant execute on function public.reconcile_training_engagement_xp(uuid,uuid) to service_role;
revoke all on function public.reconcile_approved_contribution_xp(uuid) from public,anon,authenticated;
grant execute on function public.reconcile_approved_contribution_xp(uuid) to service_role;

revoke all on function public.list_unrewarded_contribution_revisions(uuid,integer) from public,anon,authenticated;
grant execute on function public.list_unrewarded_contribution_revisions(uuid,integer) to service_role;
