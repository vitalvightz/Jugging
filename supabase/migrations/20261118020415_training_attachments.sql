-- Form-check videos on logged work: a session, one exercise in it, or a
-- sparring report. Reuses the private video pipeline (owner-only upload,
-- isolated processing, sanitized rendition). Every read and write goes through
-- the API with the service role; no client role can read or write this table.
create table public.training_attachments (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  media_id uuid not null unique,
  source text not null check (source in ('unlxck','external','sparring')),
  target_id uuid not null,
  exercise_ref text not null default '' check (char_length(exercise_ref) <= 200),
  created_at timestamptz not null default now(),
  foreign key (media_id) references public.private_exercise_media(id) on delete cascade
);
create index training_attachments_record_idx on public.training_attachments(athlete_id, source, target_id, exercise_ref);
alter table public.training_attachments enable row level security;
revoke all on public.training_attachments from public, anon, authenticated;
grant all on public.training_attachments to service_role;

-- One write path. The record must be the athlete's own, the video the
-- athlete's own verified upload, and a record holds at most six videos.
create function public.attach_training_media(p_athlete_id uuid, p_media_id uuid, p_source text,
  p_target_id uuid, p_exercise_ref text)
returns setof public.training_attachments language plpgsql security invoker set search_path = '' as $$
declare media public.private_exercise_media; asset public.private_video_assets;
  session public.session_completions; ref text := coalesce(p_exercise_ref, '');
begin
  -- Shares the private-media owner lock, so cleanup cannot remove the asset mid-attach.
  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text, 2833));
  if char_length(ref) > 200 then raise exception 'Exercise unavailable' using errcode = '22023'; end if;
  select * into media from public.private_exercise_media
    where id = p_media_id and athlete_id = p_athlete_id and provider = 'uploaded' for share;
  if media.id is null then raise exception 'Video unavailable' using errcode = '42501'; end if;
  select * into asset from public.private_video_assets
    where id = media.asset_id and athlete_id = p_athlete_id and status = 'verified' for share;
  if asset.id is null then raise exception 'Video is not ready' using errcode = '55000'; end if;
  if p_source = 'external' then
    if not exists (select 1 from public.external_training_sessions
        where id = p_target_id and athlete_id = p_athlete_id and deleted_at is null) then
      raise exception 'Record unavailable' using errcode = '42501'; end if;
    if ref <> '' and not exists (select 1 from public.manual_exercise_entries e where e.id::text = ref
        and e.external_session_id = p_target_id and e.athlete_id = p_athlete_id and e.deleted_at is null) then
      raise exception 'Exercise unavailable' using errcode = '42501'; end if;
  elsif p_source = 'unlxck' then
    select * into session from public.session_completions where id = p_target_id and athlete_id = p_athlete_id
      and status in ('done','modified') and completion_origin is distinct from 'day_credit';
    if session.id is null then raise exception 'Record unavailable' using errcode = '42501'; end if;
    if ref <> '' and not exists (select 1 from jsonb_array_elements(
          coalesce(session.prescription_snapshot #> '{session,blocks}', '[]'::jsonb)) b where b ->> 'block_id' = ref)
       and not exists (select 1 from public.exercise_logs e where e.athlete_id = p_athlete_id
          and e.plan_id is not distinct from session.plan_id and e.session_id = session.session_id
          and e.training_day = session.training_day and e.block_id = ref) then
      raise exception 'Exercise unavailable' using errcode = '42501'; end if;
  elsif p_source = 'sparring' then
    if ref <> '' or not exists (select 1 from public.sparring_logs s where s.id = p_target_id and s.athlete_id = p_athlete_id
        and (s.external_session_id is null or exists (select 1 from public.external_training_sessions e
          where e.id = s.external_session_id and e.athlete_id = s.athlete_id and e.deleted_at is null))) then
      raise exception 'Report unavailable' using errcode = '42501'; end if;
  else
    raise exception 'Record unavailable' using errcode = '42501';
  end if;
  if (select count(*) from public.training_attachments where athlete_id = p_athlete_id and source = p_source
      and target_id = p_target_id and exercise_ref = ref) >= 6 then
    raise exception 'Six videos per record' using errcode = '54000'; end if;
  return query insert into public.training_attachments(athlete_id, media_id, source, target_id, exercise_ref)
    values (p_athlete_id, media.id, p_source, p_target_id, ref)
    on conflict (media_id) do nothing returning *;
end $$;
revoke all on function public.attach_training_media(uuid,uuid,text,uuid,text) from public, anon, authenticated;
grant execute on function public.attach_training_media(uuid,uuid,text,uuid,text) to service_role;

-- An attached video is retained like a plan demo: cleanup skips it.
create or replace function public.private_video_cleanup_candidates() returns setof public.private_video_assets
language plpgsql security invoker set search_path = '' as $$
declare candidate public.private_video_assets;
begin
  for candidate in select a.* from public.private_video_assets a where
      (a.status in ('reserved','queued','processing','rejected','deleted') and a.updated_at < now() - interval '24 hours')
      or (a.status='verified' and a.created_at < now() - interval '7 days')
      order by a.created_at for update skip locked limit 100
  loop
    -- Recheck references after acquiring the row lock shared with the plan trigger.
    if not exists (select 1 from public.private_exercise_media m where m.asset_id=candidate.id and public.community_media_retained(m.id))
      and not exists (select 1 from public.private_exercise_media m join public.training_attachments t on t.media_id=m.id
        where m.asset_id=candidate.id)
      and not exists (
        select 1 from public.private_exercise_media m join public.custom_training_plans p on p.athlete_id=m.athlete_id
        cross join lateral jsonb_array_elements(p.sessions) s
        cross join lateral jsonb_array_elements(s->'exercises') e
        where m.asset_id=candidate.id and e->>'media_id'=m.id::text) then
      update public.private_video_assets set status='deleted' where id=candidate.id;
      candidate.status := 'deleted';
      return next candidate;
    end if;
  end loop;
end;
$$;
revoke all on function public.private_video_cleanup_candidates() from public,anon,authenticated;
grant execute on function public.private_video_cleanup_candidates() to service_role;
