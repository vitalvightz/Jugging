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

-- Earlier connections agreed to read access only. Logging requires a fresh,
-- explicit athlete choice; neither coaches nor signup rules can enable it.
alter table public.coach_connections add column if not exists allow_session_logging boolean not null default false;
drop function if exists public.coach_connection_command(text,uuid,text,boolean,boolean,boolean);
drop function if exists coach_private.connection_command(text,uuid,text,boolean,boolean,boolean);
drop function if exists public.coach_connection_command(text,uuid,text,boolean,boolean,boolean,boolean);
drop function if exists coach_private.connection_command(text,uuid,text,boolean,boolean,boolean,boolean);
create function coach_private.connection_command(p_action text, p_id uuid, p_email text, p_recovery boolean, p_injuries boolean, p_weight boolean, p_logging boolean)
returns uuid language plpgsql security definer set search_path = '' as $$
declare actor public.profiles; c public.coach_connections; result uuid;
begin
  select * into actor from public.profiles where id = auth.uid() and access_status = 'approved';
  if actor.id is null then raise exception 'Approved account required' using errcode='42501'; end if;
  if p_action = 'invite' then
    if actor.role not in ('coach','admin') then raise exception 'Coach account required' using errcode='42501'; end if;
    if p_email is null or lower(trim(p_email)) !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' or char_length(trim(p_email)) > 254
      or lower(trim(p_email)) = lower(actor.email) then raise exception 'Enter a valid athlete email'; end if;
    if (select count(*) from public.coach_connections where coach_id=actor.id and status in ('pending','accepted')) >= 250
      then raise exception 'Roster invitation limit reached'; end if;
    -- Do not look up the recipient: invitation results cannot enumerate accounts.
    insert into public.coach_connections(coach_id,invited_email) values(actor.id,lower(trim(p_email)))
      on conflict (coach_id,invited_email) where status in ('pending','accepted') do nothing returning id into result;
    if result is null then select id into result from public.coach_connections where coach_id=actor.id and invited_email=lower(trim(p_email)) and status in ('pending','accepted'); end if;
    return result;
  end if;
  select * into c from public.coach_connections where id=p_id for update;
  if c.id is null then raise exception 'Connection unavailable' using errcode='42501'; end if;
  if p_action in ('accept','decline') and c.status='pending' and c.invited_email=coach_private.actor_email() and actor.role='athlete'
    and exists(select 1 from public.profiles where id=c.coach_id and role in ('coach','admin') and access_status='approved') then
    update public.coach_connections set athlete_id=actor.id, status=case when p_action='accept' then 'accepted' else 'declined' end,
      share_recovery=(p_action='accept' and coalesce(p_recovery,false)), share_injuries=(p_action='accept' and coalesce(p_injuries,false)),
      share_weight=(p_action='accept' and coalesce(p_weight,false)),
      allow_session_logging=(p_action='accept' and coalesce(p_logging,false)),
      accepted_at=case when p_action='accept' then now() end, updated_at=now() where id=c.id;
  elsif p_action in ('revoke','consent') and c.status='accepted' and c.athlete_id=actor.id and actor.role='athlete' then
    update public.coach_connections set status=case when p_action='revoke' then 'revoked' else 'accepted' end,
      share_recovery=(p_action='consent' and coalesce(p_recovery,false)), share_injuries=(p_action='consent' and coalesce(p_injuries,false)),
      share_weight=(p_action='consent' and coalesce(p_weight,false)),
      allow_session_logging=(p_action='consent' and coalesce(p_logging,false)), updated_at=now() where id=c.id;
  elsif p_action='remove' and c.coach_id=actor.id and actor.role in ('coach','admin') and c.status in ('pending','accepted') then
    update public.coach_connections set status='removed',share_recovery=false,share_injuries=false,share_weight=false,allow_session_logging=false,updated_at=now() where id=c.id;
  else raise exception 'Connection action not permitted' using errcode='42501'; end if;
  return c.id;
end $$;

create or replace function coach_private.connections() returns jsonb language sql stable security definer set search_path='' as $$
  select coalesce(jsonb_agg(jsonb_build_object('id',c.id,'coach_id',c.coach_id,'athlete_id',c.athlete_id,
    'invited_email',c.invited_email,'status',c.status,'share_recovery',c.share_recovery,'share_injuries',c.share_injuries,
    'share_weight',c.share_weight,'allow_session_logging',c.allow_session_logging,
    'health_consent',case when c.athlete_id=actor.id then actor.health_data_consent end,
    'created_at',c.created_at,'coach_name',p.full_name) order by c.created_at desc),'[]'::jsonb)
  from public.coach_connections c join public.profiles p on p.id=c.coach_id
  join public.profiles actor on actor.id=auth.uid() and actor.access_status='approved'
  where c.status in ('pending','accepted') and (c.coach_id=actor.id or c.athlete_id=actor.id or
    (c.status='pending' and c.invited_email=coach_private.actor_email()));
$$;

create or replace function coach_private.fighters(p_athlete_id uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
  if not exists(select 1 from public.profiles where id=auth.uid() and role in ('coach','admin') and access_status='approved')
    then raise exception 'Coach account required' using errcode='42501'; end if;
  select coalesce(jsonb_agg(data order by data->>'name'),'[]'::jsonb) into result from (
    select jsonb_build_object('id',a.id,'connection_id',c.id,'name',a.full_name,'avatar_url',a.avatar_url,
      'today',clock.training_day,'timezone',a.athlete_timezone,'share_recovery',(c.share_recovery and a.health_data_consent),'share_injuries',(c.share_injuries and a.health_data_consent),
      'share_weight',(c.share_weight and a.health_data_consent),'allow_session_logging',c.allow_session_logging,
      'sport',coalesce(plan.structured_plan#>>'{plan_metadata,sport}',intake.intake->>'sport',intake.intake->>'sport_profile'),
      'fight_date',coalesce(plan.fight_date,intake.fight_date),
      'plan_id',plan.id,'plan_name',plan.plan_name,
      'schedule',case when p_athlete_id is not null then (
        select coalesce(jsonb_agg(jsonb_build_object('day',d->>'date','id',s->>'session_id','type',s->>'session_type',
          'title',case when (c.share_injuries and a.health_data_consent)
            or not coach_private.health_session(s->>'session_type',s->>'title',s->'blocks') then s->>'title' end) order by d->>'date'),'[]'::jsonb)
        from jsonb_array_elements(coalesce(plan.structured_plan->'weeks','[]'::jsonb)) w
        cross join lateral jsonb_array_elements(coalesce(w->'days','[]'::jsonb)) d
        cross join lateral jsonb_array_elements(coalesce(d->'sessions','[]'::jsonb)) s
        where s->>'session_type' not in ('rehab','recovery') or (c.share_injuries and a.health_data_consent)
      ) else '[]'::jsonb end,
      'sessions',(
        select coalesce(jsonb_agg(row order by row->>'day' desc,row->>'recorded_at' desc),'[]'::jsonb) from (
          select jsonb_build_object('id',sc.id,'session_id',sc.session_id,'plan_id',sc.plan_id,'day',sc.training_day,
            'type',case when meta.kind in ('rehab','recovery') and not (c.share_injuries and a.health_data_consent) then 'programme' else meta.kind end,
            'title',case when p_athlete_id is not null and ((c.share_injuries and a.health_data_consent)
              or not coach_private.health_session(meta.kind,meta.title,meta.blocks)) then meta.title end,
            'status',sc.status,'rpe',sc.session_rpe,'source','unlxck','recorded_at',sc.updated_at) row
          from public.session_completions sc
          left join public.plans historic on historic.id=sc.plan_id and historic.athlete_id=sc.athlete_id
          left join lateral (
            select session from jsonb_array_elements(coalesce(historic.structured_plan->'weeks','[]'::jsonb)) week
            cross join lateral jsonb_array_elements(coalesce(week->'days','[]'::jsonb)) day_row
            cross join lateral jsonb_array_elements(coalesce(day_row->'sessions','[]'::jsonb)) session
            where session->>'session_id'=sc.session_id and day_row->>'date'=sc.training_day::text limit 1
          ) historical on true
          cross join lateral (select coalesce(sc.prescription_snapshot#>>'{session,session_type}',historical.session->>'session_type','programme') kind,
            coalesce(sc.prescription_snapshot#>>'{session,title}',historical.session->>'title') title,
            coalesce(sc.prescription_snapshot#>'{session,blocks}',historical.session->'blocks') blocks) meta
          where sc.athlete_id=a.id and sc.training_day between clock.training_day-55 and clock.training_day
            and sc.completion_origin is distinct from 'day_credit' and sc.status in ('done','modified','skipped')
          union all
          select jsonb_build_object('id',e.id,'day',e.training_day,'type',e.activity,'sport',e.sport,'status','done',
            'effort',e.effort,'duration',e.duration_minutes,'source','external','recorded_at',e.created_at)
          from public.external_training_sessions e where e.athlete_id=a.id and e.deleted_at is null and e.training_day between clock.training_day-55 and clock.training_day
        ) sessions
      ),
      'recovery',case when c.share_recovery and a.health_data_consent then (
        select coalesce(jsonb_agg(row order by row->>'day' desc,row->>'recorded_at' desc),'[]'::jsonb) from (
          select jsonb_build_object('day',t.training_day,'sleep',t.sleep,'body',t.body,'source','today','recorded_at',t.updated_at) row
          from public.today_checkins t where t.athlete_id=a.id and t.training_day between clock.training_day-55 and clock.training_day
          union all
          select jsonb_build_object('day',d.checkin_date,'readiness',d.readiness,'fatigue',d.fatigue,'sleep_quality',d.sleep_quality,'source','daily','recorded_at',d.updated_at)
          from public.daily_checkins d where d.athlete_id=a.id and d.checkin_date between clock.training_day-55 and clock.training_day
        ) checkins
      ) else null end,
      -- Contact reports add detail, never a second workload occurrence.
      'sparring_reports',(
        select coalesce(jsonb_agg(jsonb_build_object('id',s.id,'day',s.training_day,'intensity',s.intensity,
          'rounds',s.rounds_completed,'round_seconds',s.round_seconds,
          'head_contact',case when c.share_injuries and a.health_data_consent then s.head_contact end,
          'rocked',case when c.share_injuries and a.health_data_consent then s.rocked end) order by s.training_day desc,s.created_at desc),'[]'::jsonb)
        from public.sparring_logs s where s.athlete_id=a.id and s.training_day between clock.training_day-55 and clock.training_day
          and (s.external_session_id is null or exists(select 1 from public.external_training_sessions e where e.id=s.external_session_id and e.athlete_id=a.id and e.deleted_at is null))
      ),
      'injuries',case when c.share_injuries and a.health_data_consent then (
        select coalesce(jsonb_agg(jsonb_build_object('id',i.id,'body_area',i.body_area,'severity',i.severity,'status',i.status,'updated_at',i.updated_at)),'[]'::jsonb)
        from public.injury_flags i where i.athlete_id=a.id and i.status in ('open','monitoring')
      ) else null end
    ) data
    from public.coach_connections c join public.profiles a on a.id=c.athlete_id and a.role='athlete' and a.access_status='approved'
    left join public.plans plan on plan.id=a.active_plan_id and plan.athlete_id=a.id
    left join lateral (select i.* from public.athlete_intakes i where i.athlete_id=a.id order by i.created_at desc limit 1) intake on true
    cross join lateral (select ((now() at time zone coalesce((select name from pg_catalog.pg_timezone_names where name=a.athlete_timezone),'UTC')) - interval '4 hours')::date as training_day) clock
    where c.coach_id=auth.uid() and c.status='accepted' and (p_athlete_id is null or c.athlete_id=p_athlete_id)
  ) fighters;
  return result;
end $$;


create function public.coach_connection_command(p_action text,p_id uuid default null,p_email text default null,
  p_recovery boolean default false,p_injuries boolean default false,p_weight boolean default false,p_logging boolean default false)
returns uuid language sql security invoker set search_path='' as $$ select coach_private.connection_command(p_action,p_id,p_email,p_recovery,p_injuries,p_weight,p_logging); $$;
revoke all on function coach_private.connection_command(text,uuid,text,boolean,boolean,boolean,boolean) from public,anon;
grant execute on function coach_private.connection_command(text,uuid,text,boolean,boolean,boolean,boolean) to authenticated;
revoke all on function public.coach_connection_command(text,uuid,text,boolean,boolean,boolean,boolean) from public,anon;
grant execute on function public.coach_connection_command(text,uuid,text,boolean,boolean,boolean,boolean) to authenticated;
