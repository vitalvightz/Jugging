-- Photos and voice notes on logged work.
--
-- Every upload is untrusted. It lands in a private quarantine bucket that no
-- client can read; the process-attachment Edge Function rebuilds it (a fresh
-- JPEG from decoded pixels, or a fresh WAV from raw samples) into a separate
-- approved bucket that no client can read either. Only the API, with the
-- service role, issues 60-second links to rebuilt files, after checking the
-- owner (or the coach's live connection and media-sharing consent).

-- 1. Dedicated consent: a coach sees photos, voice notes and videos only when
-- the fighter shares media with that coach. Off by default and on accept.
alter table public.coach_connections add column share_media boolean not null default false;

drop function if exists public.coach_connection_command(text,uuid,text,boolean,boolean,boolean,boolean);
drop function if exists coach_private.connection_command(text,uuid,text,boolean,boolean,boolean,boolean);
create function coach_private.connection_command(p_action text, p_id uuid, p_email text, p_recovery boolean, p_injuries boolean,
  p_weight boolean, p_logging boolean, p_media boolean)
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
      share_media=(p_action='accept' and coalesce(p_media,false)),
      accepted_at=case when p_action='accept' then now() end, updated_at=now() where id=c.id;
  elsif p_action in ('revoke','consent') and c.status='accepted' and c.athlete_id=actor.id and actor.role='athlete' then
    update public.coach_connections set status=case when p_action='revoke' then 'revoked' else 'accepted' end,
      share_recovery=(p_action='consent' and coalesce(p_recovery,false)), share_injuries=(p_action='consent' and coalesce(p_injuries,false)),
      share_weight=(p_action='consent' and coalesce(p_weight,false)),
      allow_session_logging=(p_action='consent' and coalesce(p_logging,false)),
      share_media=(p_action='consent' and coalesce(p_media,false)), updated_at=now() where id=c.id;
  elsif p_action='remove' and c.coach_id=actor.id and actor.role in ('coach','admin') and c.status in ('pending','accepted') then
    update public.coach_connections set status='removed',share_recovery=false,share_injuries=false,share_weight=false,
      allow_session_logging=false,share_media=false,updated_at=now() where id=c.id;
  else raise exception 'Connection action not permitted' using errcode='42501'; end if;
  return c.id;
end $$;

create function public.coach_connection_command(p_action text,p_id uuid default null,p_email text default null,
  p_recovery boolean default false,p_injuries boolean default false,p_weight boolean default false,p_logging boolean default false,
  p_media boolean default false)
returns uuid language sql security invoker set search_path='' as $$
  select coach_private.connection_command(p_action,p_id,p_email,p_recovery,p_injuries,p_weight,p_logging,p_media); $$;
revoke all on function coach_private.connection_command(text,uuid,text,boolean,boolean,boolean,boolean,boolean) from public,anon;
grant execute on function coach_private.connection_command(text,uuid,text,boolean,boolean,boolean,boolean,boolean) to authenticated;
revoke all on function public.coach_connection_command(text,uuid,text,boolean,boolean,boolean,boolean,boolean) from public,anon;
grant execute on function public.coach_connection_command(text,uuid,text,boolean,boolean,boolean,boolean,boolean) to authenticated;

create or replace function coach_private.connections() returns jsonb language sql stable security definer set search_path='' as $$
  select coalesce(jsonb_agg(jsonb_build_object('id',c.id,'coach_id',c.coach_id,'athlete_id',c.athlete_id,
    'invited_email',c.invited_email,'status',c.status,'share_recovery',c.share_recovery,'share_injuries',c.share_injuries,
    'share_weight',c.share_weight,'allow_session_logging',c.allow_session_logging,'share_media',c.share_media,
    'health_consent',case when c.athlete_id=actor.id then actor.health_data_consent end,
    'created_at',c.created_at,'coach_name',p.full_name) order by c.created_at desc),'[]'::jsonb)
  from public.coach_connections c join public.profiles p on p.id=c.coach_id
  join public.profiles actor on actor.id=auth.uid() and actor.access_status='approved'
  where c.status in ('pending','accepted') and (c.coach_id=actor.id or c.athlete_id=actor.id or
    (c.status='pending' and c.invited_email=coach_private.actor_email()));
$$;

-- 2. Uploaded photos and voice notes. Paths are server-generated; client
-- filenames are never used. Only the service role touches this table.
create table public.attachment_assets (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references public.profiles(id) on delete cascade,
  kind text not null check (kind in ('photo','voice')),
  status text not null default 'reserved' check (status in ('reserved','processing','ready','rejected','deleted')),
  content_type text not null check ((kind='photo' and content_type='image/jpeg') or (kind='voice' and content_type='audio/wav')),
  size_bytes integer not null check (size_bytes > 0 and size_bytes <= case when kind='photo' then 8388608 else 2097152 end),
  intake_path text not null unique,
  ready_path text unique,
  width integer check (width between 1 and 2048),
  height integer check (height between 1 and 2048),
  duration_ms integer check (duration_ms between 1 and 60500),
  reject_reason text check (char_length(reject_reason) <= 120),
  upload_expires_at timestamptz not null default now() + interval '15 minutes',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (status <> 'ready' or (ready_path is not null and (kind <> 'photo' or (width is not null and height is not null))
    and (kind <> 'voice' or duration_ms is not null)))
);
create index attachment_assets_owner_idx on public.attachment_assets(owner_id, created_at desc);
create index attachment_assets_status_idx on public.attachment_assets(status, updated_at);
alter table public.attachment_assets enable row level security;
revoke all on public.attachment_assets from public, anon, authenticated;
grant all on public.attachment_assets to service_role;

create table public.attachment_upload_log (
  owner_id uuid not null references public.profiles(id) on delete cascade,
  created_at timestamptz not null default now()
);
create index attachment_upload_log_owner_idx on public.attachment_upload_log(owner_id, created_at desc);
alter table public.attachment_upload_log enable row level security;
revoke all on public.attachment_upload_log from public, anon, authenticated;
grant all on public.attachment_upload_log to service_role;

-- 3. Storage: quarantine for intake, a separate bucket for rebuilt files.
insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types) values
  ('attachment-intake','attachment-intake',false,8388608,array['image/jpeg','audio/wav']),
  ('attachment-ready','attachment-ready',false,8388608,array['image/jpeg','audio/wav'])
on conflict(id) do update set public=false,file_size_limit=excluded.file_size_limit,allowed_mime_types=excluded.allowed_mime_types;

-- Restrictive guards hold even if another feature adds broad permissive policies.
create policy attachment_no_client_read on storage.objects as restrictive for select to anon,authenticated
  using (bucket_id not in ('attachment-intake','attachment-ready'));
create policy attachment_no_client_update on storage.objects as restrictive for update to anon,authenticated
  using (bucket_id not in ('attachment-intake','attachment-ready'))
  with check (bucket_id not in ('attachment-intake','attachment-ready'));
create policy attachment_no_client_delete on storage.objects as restrictive for delete to anon,authenticated
  using (bucket_id not in ('attachment-intake','attachment-ready'));
-- Clients get no access to attachment_assets; this narrow definer answers one
-- question: may the signed-in user upload to exactly this reserved path now?
create function public.attachment_intake_allowed(p_name text) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (select 1 from public.attachment_assets a where a.intake_path = p_name
    and a.owner_id = (select auth.uid()) and a.status = 'reserved' and a.upload_expires_at > now());
$$;
revoke all on function public.attachment_intake_allowed(text) from public, anon;
grant execute on function public.attachment_intake_allowed(text) to authenticated;
create policy attachment_intake_guard on storage.objects as restrictive for insert to anon,authenticated
  with check (bucket_id <> 'attachment-ready' and (bucket_id <> 'attachment-intake' or public.attachment_intake_allowed(name)));
create policy attachment_intake_insert on storage.objects for insert to authenticated
  with check (bucket_id = 'attachment-intake' and public.attachment_intake_allowed(name));

-- 4. Reserve: quotas and rate limits under the owner's lock.
create function public.reserve_attachment_upload(p_owner_id uuid, p_kind text, p_size_bytes integer, p_content_type text)
returns setof public.attachment_assets language plpgsql security invoker set search_path = '' as $$
declare asset_uuid uuid := gen_random_uuid();
begin
  perform pg_advisory_xact_lock(hashtextextended(p_owner_id::text, 2834));
  if (select count(*) from public.attachment_upload_log where owner_id=p_owner_id and created_at > now() - interval '1 hour') >= 20
    or (select count(*) from public.attachment_upload_log where owner_id=p_owner_id and created_at > now() - interval '1 day') >= 60
    or (select count(*) from public.attachment_assets where owner_id=p_owner_id and status in ('reserved','processing')) >= 3
    or (select count(*) from public.attachment_assets where owner_id=p_owner_id and status <> 'deleted') >= 300
    or (select coalesce(sum(size_bytes),0) from public.attachment_assets where owner_id=p_owner_id and status <> 'deleted')
       + p_size_bytes > 524288000 then
    raise exception 'Upload limit reached' using errcode = '54000';
  end if;
  insert into public.attachment_upload_log(owner_id) values (p_owner_id);
  return query insert into public.attachment_assets(id,owner_id,kind,content_type,size_bytes,intake_path)
    values (asset_uuid,p_owner_id,p_kind,p_content_type,p_size_bytes,
      p_owner_id::text || '/' || asset_uuid::text || case when p_kind='photo' then '/source.jpg' else '/source.wav' end)
    returning *;
end $$;

-- Processing claims a complete upload once; a stalled claim can be retried.
create function public.claim_attachment_processing(p_asset_id uuid)
returns setof public.attachment_assets language plpgsql security invoker set search_path = '' as $$
begin
  return query update public.attachment_assets a set status='processing', updated_at=now()
    where a.id=p_asset_id
      and (a.status='reserved' or (a.status='processing' and a.updated_at < now() - interval '5 minutes'))
      and exists (select 1 from storage.objects o where o.bucket_id='attachment-intake' and o.name=a.intake_path)
    returning a.*;
end $$;

create function public.finish_attachment(p_asset_id uuid, p_ready_path text, p_width integer, p_height integer, p_duration_ms integer)
returns void language plpgsql security invoker set search_path = '' as $$
begin
  update public.attachment_assets set status='ready', ready_path=p_ready_path, width=p_width, height=p_height,
    duration_ms=p_duration_ms, updated_at=now()
    where id=p_asset_id and status='processing' and p_ready_path = owner_id::text || '/' || id::text
      || case when kind='photo' then '/clean.jpg' else '/clean.wav' end;
  if not found then raise exception 'Attachment not processing' using errcode='55000'; end if;
end $$;

create function public.reject_attachment(p_asset_id uuid, p_reason text)
returns void language sql security invoker set search_path = '' as $$
  update public.attachment_assets set status='rejected', reject_reason=left(p_reason,120), updated_at=now()
    where id=p_asset_id and status in ('reserved','processing');
$$;

-- 5. Attach rebuilt files to logged work. The athlete attaches photos and voice
-- notes to their own records; a coach attaches voice notes only, and only while
-- connected with the fighter's health consent and media sharing on.
alter table public.training_attachments alter column media_id drop not null;
alter table public.training_attachments add column asset_id uuid unique references public.attachment_assets(id) on delete cascade;
alter table public.training_attachments add column author_id uuid references public.profiles(id) on delete cascade;
update public.training_attachments set author_id = athlete_id where author_id is null;
alter table public.training_attachments alter column author_id set not null;
alter table public.training_attachments add constraint training_attachments_one_file check (num_nonnulls(media_id, asset_id) = 1);

create function public.training_record_available(p_athlete_id uuid, p_source text, p_target_id uuid, p_ref text)
returns boolean language plpgsql stable security invoker set search_path = '' as $$
declare session public.session_completions; ref text := coalesce(p_ref, '');
begin
  if char_length(ref) > 200 then return false; end if;
  if p_source = 'external' then
    return exists (select 1 from public.external_training_sessions where id = p_target_id and athlete_id = p_athlete_id and deleted_at is null)
      and (ref = '' or exists (select 1 from public.manual_exercise_entries e where e.id::text = ref
        and e.external_session_id = p_target_id and e.athlete_id = p_athlete_id and e.deleted_at is null));
  elsif p_source = 'unlxck' then
    select * into session from public.session_completions where id = p_target_id and athlete_id = p_athlete_id
      and status in ('done','modified') and completion_origin is distinct from 'day_credit';
    if session.id is null then return false; end if;
    return ref = '' or exists (select 1 from jsonb_array_elements(
          coalesce(session.prescription_snapshot #> '{session,blocks}', '[]'::jsonb)) b where b ->> 'block_id' = ref)
       or exists (select 1 from public.exercise_logs e where e.athlete_id = p_athlete_id
          and e.plan_id is not distinct from session.plan_id and e.session_id = session.session_id
          and e.training_day = session.training_day and e.block_id = ref);
  elsif p_source = 'sparring' then
    return ref = '' and exists (select 1 from public.sparring_logs s where s.id = p_target_id and s.athlete_id = p_athlete_id
        and (s.external_session_id is null or exists (select 1 from public.external_training_sessions e
          where e.id = s.external_session_id and e.athlete_id = s.athlete_id and e.deleted_at is null)));
  end if;
  return false;
end $$;

create function public.attach_training_asset(p_author_id uuid, p_athlete_id uuid, p_asset_id uuid, p_source text,
  p_target_id uuid, p_exercise_ref text)
returns setof public.training_attachments language plpgsql security invoker set search_path = '' as $$
declare asset public.attachment_assets; ref text := coalesce(p_exercise_ref, '');
begin
  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text, 2833));
  select * into asset from public.attachment_assets where id = p_asset_id and owner_id = p_author_id for share;
  if asset.id is null then raise exception 'File unavailable' using errcode = '42501'; end if;
  if asset.status <> 'ready' then raise exception 'File is not ready' using errcode = '55000'; end if;
  if p_author_id <> p_athlete_id then
    if asset.kind <> 'voice' or not exists (
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
  return query insert into public.training_attachments(athlete_id, author_id, asset_id, source, target_id, exercise_ref)
    values (p_athlete_id, p_author_id, asset.id, p_source, p_target_id, ref)
    on conflict (asset_id) do nothing returning *;
end $$;

-- Videos attach through the same record check and record the author.
create or replace function public.attach_training_media(p_athlete_id uuid, p_media_id uuid, p_source text,
  p_target_id uuid, p_exercise_ref text)
returns setof public.training_attachments language plpgsql security invoker set search_path = '' as $$
declare media public.private_exercise_media; asset public.private_video_assets; ref text := coalesce(p_exercise_ref, '');
begin
  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text, 2833));
  if char_length(ref) > 200 then raise exception 'Exercise unavailable' using errcode = '22023'; end if;
  select * into media from public.private_exercise_media
    where id = p_media_id and athlete_id = p_athlete_id and provider = 'uploaded' for share;
  if media.id is null then raise exception 'Video unavailable' using errcode = '42501'; end if;
  select * into asset from public.private_video_assets
    where id = media.asset_id and athlete_id = p_athlete_id and status = 'verified' for share;
  if asset.id is null then raise exception 'Video is not ready' using errcode = '55000'; end if;
  if not public.training_record_available(p_athlete_id, p_source, p_target_id, ref) then
    raise exception 'Record unavailable' using errcode = '42501';
  end if;
  if (select count(*) from public.training_attachments where athlete_id = p_athlete_id and source = p_source
      and target_id = p_target_id and exercise_ref = ref) >= 6 then
    raise exception 'Six videos per record' using errcode = '54000'; end if;
  return query insert into public.training_attachments(athlete_id, author_id, media_id, source, target_id, exercise_ref)
    values (p_athlete_id, p_athlete_id, media.id, p_source, p_target_id, ref)
    on conflict (media_id) do nothing returning *;
end $$;

-- 6. Cleanup: unfinished or rejected uploads after a day, and rebuilt files
-- nobody attached after a day. Attached files stay until removed.
create function public.attachment_cleanup_candidates() returns setof public.attachment_assets
language plpgsql security invoker set search_path = '' as $$
declare candidate public.attachment_assets;
begin
  for candidate in select a.* from public.attachment_assets a where
      (a.status in ('reserved','processing','rejected') and a.updated_at < now() - interval '24 hours')
      or (a.status = 'ready' and a.updated_at < now() - interval '24 hours'
          and not exists (select 1 from public.training_attachments t where t.asset_id = a.id))
      order by a.updated_at for update skip locked limit 50
  loop
    update public.attachment_assets set status='deleted', updated_at=now() where id=candidate.id;
    candidate.status := 'deleted';
    return next candidate;
  end loop;
end $$;

-- Removing an attachment releases its file for cleanup right away.
create function public.release_attachment_asset() returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  if old.asset_id is not null then
    update public.attachment_assets set updated_at = now() - interval '25 hours' where id = old.asset_id and status = 'ready';
  end if;
  return old;
end $$;
create trigger training_attachments_release_asset after delete on public.training_attachments
  for each row execute function public.release_attachment_asset();

revoke all on function public.reserve_attachment_upload(uuid,text,integer,text), public.claim_attachment_processing(uuid),
  public.finish_attachment(uuid,text,integer,integer,integer), public.reject_attachment(uuid,text),
  public.training_record_available(uuid,text,uuid,text),
  public.attach_training_asset(uuid,uuid,uuid,text,uuid,text), public.attachment_cleanup_candidates(),
  public.release_attachment_asset() from public, anon, authenticated;
grant execute on function public.reserve_attachment_upload(uuid,text,integer,text), public.claim_attachment_processing(uuid),
  public.finish_attachment(uuid,text,integer,integer,integer), public.reject_attachment(uuid,text),
  public.training_record_available(uuid,text,uuid,text),
  public.attach_training_asset(uuid,uuid,uuid,text,uuid,text), public.attachment_cleanup_candidates() to service_role;
