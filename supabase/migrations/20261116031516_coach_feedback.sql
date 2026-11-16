-- Feedback is separate from athlete-owned notes and survives disconnects.
create table public.coach_feedback (
 id uuid primary key default gen_random_uuid(),
 athlete_id uuid not null references public.profiles(id) on delete cascade,
 coach_id uuid references public.profiles(id) on delete set null,
 coach_name text not null,
 source text not null check(source in ('unlxck','external','sparring')),
 target_id uuid not null,
 exercise_ref text not null default '' check(char_length(exercise_ref)<=200),
 note text not null check(char_length(trim(note)) between 1 and 1500),
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 unique(athlete_id,coach_id,source,target_id,exercise_ref)
);
create index coach_feedback_record on public.coach_feedback(athlete_id,source,target_id);
alter table public.coach_feedback enable row level security;
revoke all on public.coach_feedback from public,anon,authenticated;
grant select on public.coach_feedback to authenticated;
grant all on public.coach_feedback to service_role;
create policy athlete_received_feedback on public.coach_feedback for select to authenticated using(athlete_id=(select auth.uid()));
-- Coaches read only their own feedback while their connection is active.
create function coach_private.can_read_feedback(p_athlete_id uuid) returns boolean
language sql stable security definer set search_path='' as $$
 select exists(select 1 from public.coach_connections c
 join public.profiles coach on coach.id=c.coach_id and coach.role in ('coach','admin') and coach.access_status='approved'
 join public.profiles athlete on athlete.id=c.athlete_id and athlete.role='athlete' and athlete.access_status='approved' and athlete.health_data_consent
 where c.coach_id=auth.uid() and c.athlete_id=p_athlete_id and c.status='accepted');
$$;
revoke all on function coach_private.can_read_feedback(uuid) from public,anon;
grant execute on function coach_private.can_read_feedback(uuid) to authenticated;
create policy coach_own_feedback on public.coach_feedback for select to authenticated using(
 coach_id=(select auth.uid()) and coach_private.can_read_feedback(athlete_id));

create function coach_private.save_feedback(p_athlete_id uuid,p_source text,p_target_id uuid,p_exercise_ref text,p_note text)
returns uuid language plpgsql security definer set search_path='' as $$
declare actor public.profiles; athlete public.profiles; connection public.coach_connections;
 session public.session_completions; external_row public.external_training_sessions;
 block jsonb; result uuid; ref text:=coalesce(p_exercise_ref,'');
begin
 select * into actor from public.profiles where id=auth.uid() and access_status='approved' and role in ('coach','admin') for share;
 select * into athlete from public.profiles where id=p_athlete_id and access_status='approved' and role='athlete' for share;
 select * into connection from public.coach_connections where coach_id=actor.id and athlete_id=athlete.id and status='accepted' for share;
 if connection.id is null or athlete.health_data_consent is not true then
   raise exception 'Feedback access unavailable' using errcode='42501'; end if;
 if p_note is null or char_length(trim(p_note)) not between 1 and 1500 or char_length(ref)>200 then
   raise exception 'Enter a note of 1–1500 characters' using errcode='22023'; end if;
 if p_source='external' then
   select * into external_row from public.external_training_sessions where id=p_target_id and athlete_id=athlete.id and deleted_at is null;
   if external_row.id is null then raise exception 'Record unavailable' using errcode='42501'; end if;
   if ref<>'' and not connection.share_injuries and exists(select 1 from public.manual_exercise_entries e
     where e.id::text=ref and e.external_session_id=p_target_id and e.athlete_id=athlete.id
     and coach_private.health_session('',e.display_name,'[]'::jsonb)) then
     raise exception 'Injury sharing required for this exercise' using errcode='42501'; end if;
   if ref<>'' and not exists(select 1 from public.manual_exercise_entries e where e.id::text=ref
      and e.external_session_id=p_target_id and e.athlete_id=athlete.id and e.deleted_at is null) then
     raise exception 'Exercise unavailable' using errcode='42501'; end if;
 elsif p_source='unlxck' then
   select * into session from public.session_completions where id=p_target_id and athlete_id=athlete.id
     and status in ('done','modified','skipped') and completion_origin is distinct from 'day_credit';
   if session.id is null then raise exception 'Record unavailable' using errcode='42501'; end if;
   if not connection.share_injuries and (session.prescription_snapshot is null or coach_private.health_session(
      session.prescription_snapshot#>>'{session,session_type}',session.prescription_snapshot#>>'{session,title}',session.prescription_snapshot#>'{session,blocks}')) then
     raise exception 'Injury sharing required for this record' using errcode='42501'; end if;
   if ref<>'' then
     select b into block from jsonb_array_elements(coalesce(session.prescription_snapshot#>'{session,blocks}','[]'::jsonb)) b where b->>'block_id'=ref limit 1;
     if block is null and not exists(select 1 from public.exercise_logs e where e.athlete_id=athlete.id
       and e.plan_id is not distinct from session.plan_id and e.session_id=session.session_id and e.training_day=session.training_day and e.block_id=ref) then
       raise exception 'Exercise unavailable' using errcode='42501'; end if;
   end if;
 elsif p_source='sparring' then
   if ref<>'' or not exists(select 1 from public.sparring_logs s where s.id=p_target_id and s.athlete_id=athlete.id
     and (s.external_session_id is null or exists(select 1 from public.external_training_sessions e where e.id=s.external_session_id and e.athlete_id=s.athlete_id and e.deleted_at is null))) then
     raise exception 'Report unavailable' using errcode='42501'; end if;
 else raise exception 'Record unavailable' using errcode='42501'; end if;
 insert into public.coach_feedback(athlete_id,coach_id,coach_name,source,target_id,exercise_ref,note)
 values(athlete.id,actor.id,left(coalesce(nullif(actor.full_name,''),'Coach'),120),p_source,p_target_id,ref,trim(p_note))
 on conflict(athlete_id,coach_id,source,target_id,exercise_ref) do update set note=excluded.note,updated_at=now()
 returning id into result;
 return result;
end $$;
revoke all on function coach_private.save_feedback(uuid,text,uuid,text,text) from public,anon;
grant execute on function coach_private.save_feedback(uuid,text,uuid,text,text) to authenticated;
create function public.save_coach_feedback(p_athlete_id uuid,p_source text,p_target_id uuid,p_exercise_ref text,p_note text)
returns uuid language sql security invoker set search_path='' as $$ select coach_private.save_feedback(p_athlete_id,p_source,p_target_id,p_exercise_ref,p_note); $$;
revoke all on function public.save_coach_feedback(uuid,text,uuid,text,text) from public,anon;
grant execute on function public.save_coach_feedback(uuid,text,uuid,text,text) to authenticated;

-- Small profile addition: existing fields and recorded sessions only.
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
      -- Only derived age leaves the database; never the birth date.
      'can_leave_feedback',a.health_data_consent,
      'age',case when a.date_of_birth is not null and a.date_of_birth <= current_date
        then extract(year from age(current_date,a.date_of_birth))::integer end,
      'technical_style',a.technical_style,'tactical_style',a.tactical_style,'stance',nullif(trim(a.stance),''),'record',nullif(trim(a.record_summary),''),
      'last_training',(
        select jsonb_build_object('day',latest_record.training_day,'activity',latest_record.activity) from (
          (select sc.training_day,sc.updated_at as recorded_at,
            case when not (c.share_injuries and a.health_data_consent) and coach_private.health_session(
              sc.prescription_snapshot#>>'{session,session_type}',sc.prescription_snapshot#>>'{session,title}',sc.prescription_snapshot#>'{session,blocks}')
              then 'programme' else coalesce(sc.prescription_snapshot#>>'{session,session_type}','programme') end activity
          from public.session_completions sc where sc.athlete_id=a.id and sc.status in ('done','modified')
            and sc.completion_origin is distinct from 'day_credit' and sc.training_day<=clock.training_day
          order by sc.training_day desc,sc.updated_at desc limit 1)
          union all
          (select e.training_day,e.created_at,e.activity from public.external_training_sessions e
          where e.athlete_id=a.id and e.deleted_at is null and e.training_day<=clock.training_day
          order by e.training_day desc,e.created_at desc limit 1)
        ) latest_record order by latest_record.training_day desc,latest_record.recorded_at desc limit 1
      ),
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

