-- Progress photos: an athlete's own photos that aren't tied to one session
-- (weigh-ins, physique, an injury healing). They use the same rebuilt-photo
-- pipeline and attachment table, under a fourth source, 'progress', whose
-- target is the athlete themself. Only the athlete adds them, photos only;
-- a coach sees them only with the fighter's media sharing and health consent,
-- exactly like every other attachment.
do $$
declare c record;
begin
  for c in select conname from pg_constraint where conrelid = 'public.training_attachments'::regclass and contype = 'c'
    and pg_get_constraintdef(oid) like '%''sparring''::text%' and pg_get_constraintdef(oid) like '%source%'
  loop
    execute format('alter table public.training_attachments drop constraint %I', c.conname);
  end loop;
end $$;
alter table public.training_attachments add constraint training_attachments_source_allowed
  check (source in ('unlxck','external','sparring','progress'));
-- Videos keep to logged records: training_record_available never accepts 'progress'.

create or replace function public.attach_training_asset(p_author_id uuid, p_athlete_id uuid, p_asset_id uuid, p_source text,
  p_target_id uuid, p_exercise_ref text)
returns setof public.training_attachments language plpgsql security invoker set search_path = '' as $$
declare asset public.attachment_assets; ref text := coalesce(p_exercise_ref, '');
begin
  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text, 2833));
  select * into asset from public.attachment_assets where id = p_asset_id and owner_id = p_author_id for share;
  if asset.id is null then raise exception 'File unavailable' using errcode = '42501'; end if;
  if asset.status <> 'ready' then raise exception 'File is not ready' using errcode = '55000'; end if;
  if p_source = 'progress' then
    -- The athlete's own photos, on their own profile, never anyone else's.
    if p_author_id <> p_athlete_id or p_target_id <> p_athlete_id or ref <> '' or asset.kind <> 'photo'
      or not exists (select 1 from public.profiles where id = p_athlete_id and role = 'athlete' and access_status = 'approved') then
      raise exception 'Progress photos are the athlete''s own' using errcode = '42501';
    end if;
    if (select count(*) from public.training_attachments where athlete_id = p_athlete_id and source = 'progress') >= 24 then
      raise exception 'Twenty-four progress photos' using errcode = '54000'; end if;
  else
    if p_author_id <> p_athlete_id then
      if asset.kind not in ('voice','pdf') or not exists (
          select 1 from public.coach_connections c
            join public.profiles coach on coach.id = c.coach_id and coach.role in ('coach','admin') and coach.access_status = 'approved'
            join public.profiles athlete on athlete.id = c.athlete_id and athlete.role = 'athlete'
              and athlete.access_status = 'approved' and athlete.health_data_consent is true
            where c.coach_id = p_author_id and c.athlete_id = p_athlete_id and c.status = 'accepted' and c.share_media) then
        raise exception 'Media sharing unavailable' using errcode = '42501';
      end if;
    end if;
    if not public.training_record_available(p_athlete_id, p_source, p_target_id, ref) then
      raise exception 'Record unavailable' using errcode = '42501';
    end if;
    if (select count(*) from public.training_attachments where athlete_id = p_athlete_id and source = p_source
        and target_id = p_target_id and exercise_ref = ref) >= 6 then
      raise exception 'Six files per record' using errcode = '54000'; end if;
  end if;
  return query insert into public.training_attachments(athlete_id, author_id, asset_id, source, target_id, exercise_ref)
    values (p_athlete_id, p_author_id, asset.id, p_source, p_target_id, ref)
    on conflict (asset_id) do nothing returning *;
end $$;
