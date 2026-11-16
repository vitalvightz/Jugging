-- A connected coach may record a fighter's completed session. The row stays the
-- athlete's own History record; these columns say who entered it. The backend
-- checks the accepted connection before every such write.
alter table public.external_training_sessions
  add column if not exists logged_by_coach_id uuid references public.profiles(id) on delete set null,
  add column if not exists logged_by_coach_name text;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'external_training_sessions_coach_name_length'
      and conrelid = 'public.external_training_sessions'::regclass) then
    alter table public.external_training_sessions
      add constraint external_training_sessions_coach_name_length check (char_length(logged_by_coach_name) <= 120);
  end if;
end $$;

-- A coach-entered session is real training load, but the athlete did not report
-- it, so it earns no XP: it is not a recorded training day, its exercises are
-- not a first exercise result, and it can never carry the manual-session award.
-- The name marks such a row for good: the id is cleared if the coach's account
-- is deleted, and the session must not start earning XP then.
create or replace function public.xp_recorded_training_days(p_athlete_id uuid)
returns table(training_day date) language sql stable security invoker
set search_path = pg_catalog, public as $$
  with today as (
    select ((now() at time zone coalesce(nullif(athlete_timezone,''),'UTC'))
      - interval '3 hours')::date as day from public.profiles where id=p_athlete_id
  )
  select s.training_day from public.external_training_sessions s, today
  where s.athlete_id=p_athlete_id and s.deleted_at is null and s.training_day<=today.day
    and s.logged_by_coach_name is null
  union
  select s.training_day from public.session_completions s, today
  where s.athlete_id=p_athlete_id and s.completion_origin='reported'
    and s.status in ('done','modified') and s.training_day<=today.day;
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
      and s.logged_by_coach_name is null
      and s.training_day<=today.day and public.xp_has_measurements(e.actual)
  );
$$;

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
      where s.athlete_id=new.athlete_id and s.deleted_at is null and s.logged_by_coach_name is null
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
