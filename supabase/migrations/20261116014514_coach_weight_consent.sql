-- Weight history and fight-weight targets are their own sharing choice.
-- Connections accepted before this existed agreed to recovery and injury
-- sharing only, so they start without weight until the athlete agrees to it.
alter table public.coach_connections add column share_weight boolean not null default false;

-- A plan session can reveal injury work without being typed rehab or recovery:
-- a strength session carrying rehab blocks, or a title naming the injury work.
create function coach_private.health_session(p_kind text, p_title text, p_blocks jsonb)
returns boolean language sql immutable set search_path='' as $$
  select coalesce(p_kind, '') in ('rehab','recovery')
    or coalesce(p_title, '') ~* '\m(rehab|prehab|injur|physio|pain|recover|sore|strain|sprain|tendon|tendin|concuss)'
    or exists(select 1 from jsonb_array_elements(case when jsonb_typeof(p_blocks)='array' then p_blocks else '[]'::jsonb end) b
              where b->>'block_type'='rehab');
$$;

drop function public.coach_connection_command(text,uuid,text,boolean,boolean);
drop function coach_private.connection_command(text,uuid,text,boolean,boolean);

create function coach_private.connection_command(p_action text, p_id uuid, p_email text, p_recovery boolean, p_injuries boolean, p_weight boolean)
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
      accepted_at=case when p_action='accept' then now() end, updated_at=now() where id=c.id;
  elsif p_action in ('revoke','consent') and c.status='accepted' and c.athlete_id=actor.id and actor.role='athlete' then
    update public.coach_connections set status=case when p_action='revoke' then 'revoked' else 'accepted' end,
      share_recovery=(p_action='consent' and coalesce(p_recovery,false)), share_injuries=(p_action='consent' and coalesce(p_injuries,false)),
      share_weight=(p_action='consent' and coalesce(p_weight,false)), updated_at=now() where id=c.id;
  elsif p_action='remove' and c.coach_id=actor.id and actor.role in ('coach','admin') and c.status in ('pending','accepted') then
    update public.coach_connections set status='removed',share_recovery=false,share_injuries=false,share_weight=false,updated_at=now() where id=c.id;
  else raise exception 'Connection action not permitted' using errcode='42501'; end if;
  return c.id;
end $$;

-- The athlete sees each category exactly as it stands, including whether their
-- own health-data consent currently withholds the health categories.
create or replace function coach_private.connections() returns jsonb language sql stable security definer set search_path='' as $$
  select coalesce(jsonb_agg(jsonb_build_object('id',c.id,'coach_id',c.coach_id,'athlete_id',c.athlete_id,
    'invited_email',c.invited_email,'status',c.status,'share_recovery',c.share_recovery,'share_injuries',c.share_injuries,
    'share_weight',c.share_weight,
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
      'share_weight',(c.share_weight and a.health_data_consent),
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
  p_recovery boolean default false,p_injuries boolean default false,p_weight boolean default false)
returns uuid language sql security invoker set search_path='' as $$ select coach_private.connection_command(p_action,p_id,p_email,p_recovery,p_injuries,p_weight); $$;
-- Grant only the functions created here: other coach_private functions (the
-- signup invitation trigger) are deliberately not callable by clients.
revoke all on function coach_private.connection_command(text,uuid,text,boolean,boolean,boolean),
  coach_private.health_session(text,text,jsonb) from public,anon;
grant execute on function coach_private.connection_command(text,uuid,text,boolean,boolean,boolean),
  coach_private.health_session(text,text,jsonb) to authenticated;
revoke all on function public.coach_connection_command(text,uuid,text,boolean,boolean,boolean) from public,anon;
grant execute on function public.coach_connection_command(text,uuid,text,boolean,boolean,boolean) to authenticated;
