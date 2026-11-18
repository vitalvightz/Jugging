-- PDFs on logged work, rebuilt as page images.
--
-- The uploader's browser renders each page of their own PDF to a JPEG and
-- discards the original. Each page lands in the quarantine bucket, is rebuilt
-- from decoded pixels by the process-attachment Edge Function (one page per
-- call), and a final call writes a brand-new PDF that contains only those
-- rebuilt images. No byte of the original file is ever stored or served.
--
-- Fencing follows the photo and voice pipeline: every claim issues a token,
-- nothing is ever reclaimed (a stalled document is rejected by cleanup), and
-- every finish checks the token and a 5-minute lease.

-- 1. Documents are a third kind of asset. The kind, type and size checks were
-- created unnamed, so they are found by definition rather than by name.
do $$
declare c record;
begin
  for c in select conname from pg_constraint where conrelid = 'public.attachment_assets'::regclass and contype = 'c'
    and (pg_get_constraintdef(oid) like '%content_type%' or pg_get_constraintdef(oid) like '%size_bytes%'
      or pg_get_constraintdef(oid) like '%''photo''::text, ''voice''::text%')
  loop
    execute format('alter table public.attachment_assets drop constraint %I', c.conname);
  end loop;
end $$;
alter table public.attachment_assets add constraint attachment_assets_kind_allowed check (kind in ('photo','voice','pdf'));
alter table public.attachment_assets add constraint attachment_assets_content_type_allowed check (
  (kind='photo' and content_type='image/jpeg') or (kind='voice' and content_type='audio/wav')
  or (kind='pdf' and content_type='application/pdf'));
-- A document's size is the total of its page images; it must fit the 8 MB bucket limit as one PDF.
alter table public.attachment_assets add constraint attachment_assets_size_allowed check (
  size_bytes > 0 and size_bytes <= case kind when 'photo' then 8388608 when 'voice' then 2097152 else 8388608 end);
alter table public.attachment_assets add column page_count integer check (page_count between 1 and 30);
-- A display name only: never used in a path, and restricted to plain characters.
alter table public.attachment_assets add column file_name text
  check (file_name ~ '^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,95}\.pdf$');
alter table public.attachment_assets add column assembly_started boolean not null default false;
alter table public.attachment_assets add constraint attachment_assets_pdf_shape check (
  (kind = 'pdf') = (page_count is not null and file_name is not null));

create table public.attachment_pages (
  asset_id uuid not null references public.attachment_assets(id) on delete cascade,
  page integer not null check (page between 1 and 30),
  status text not null default 'reserved' check (status in ('reserved','processing','ready')),
  processing_token uuid,
  width integer check (width between 1 and 1600),
  height integer check (height between 1 and 1600),
  updated_at timestamptz not null default now(),
  primary key (asset_id, page),
  check (status <> 'ready' or (width is not null and height is not null))
);
alter table public.attachment_pages enable row level security;
revoke all on public.attachment_pages from public, anon, authenticated;
grant all on public.attachment_pages to service_role;

-- Rebuilt PDFs live in the approved bucket next to photos and voice notes.
update storage.buckets set allowed_mime_types = array['image/jpeg','audio/wav','application/pdf'] where id = 'attachment-ready';

-- 2. Intake: a document's pages upload to {owner}/{id}/pages/NNN.jpg, only for
-- page numbers that were reserved, only by the owner, only before expiry.
create or replace function public.attachment_intake_allowed(p_name text) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (select 1 from public.attachment_assets a where a.owner_id = (select auth.uid())
    and a.status = 'reserved' and a.upload_expires_at > now()
    and ((a.kind <> 'pdf' and a.intake_path = p_name) or (a.kind = 'pdf'
      and left(p_name, char_length(a.intake_path) + 1) = a.intake_path || '/'
      and substr(p_name, char_length(a.intake_path) + 2) ~ '^[0-9]{3}\.jpg$'
      and substr(p_name, char_length(a.intake_path) + 2, 3)::integer between 1 and a.page_count)));
$$;

-- The single-file claim never touches documents.
create or replace function public.claim_attachment_processing(p_asset_id uuid)
returns setof public.attachment_assets language plpgsql security invoker set search_path = '' as $$
begin
  return query update public.attachment_assets a
    set status='processing', processing_token=gen_random_uuid(), updated_at=now(),
      ready_path=a.owner_id::text || '/' || a.id::text || case when a.kind='photo' then '/clean.jpg' else '/clean.wav' end
    where a.id=p_asset_id and a.status='reserved' and a.kind in ('photo','voice')
      and exists(select 1 from storage.objects o where o.bucket_id='attachment-intake' and o.name=a.intake_path)
    returning a.*;
end $$;

-- 3. Reserve a document: same quotas and lock as photos and voice notes.
create function public.reserve_attachment_document(p_owner_id uuid, p_size_bytes integer, p_page_count integer, p_file_name text)
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
  insert into public.attachment_assets(id,owner_id,kind,content_type,size_bytes,intake_path,page_count,file_name)
    values (asset_uuid,p_owner_id,'pdf','application/pdf',p_size_bytes,
      p_owner_id::text || '/' || asset_uuid::text || '/pages',p_page_count,p_file_name);
  insert into public.attachment_pages(asset_id,page) select asset_uuid, n from generate_series(1,p_page_count) n;
  return query select * from public.attachment_assets where id = asset_uuid;
end $$;

-- 4. Pages are claimed once each, and only after every page has been uploaded,
-- so a half-uploaded document never starts.
create function public.claim_attachment_page(p_asset_id uuid, p_page integer)
returns table(id uuid, owner_id uuid, intake_path text, page integer, page_token uuid)
language plpgsql security invoker set search_path = '' as $$
declare asset public.attachment_assets; token uuid := gen_random_uuid();
begin
  select * into asset from public.attachment_assets a where a.id = p_asset_id and a.kind = 'pdf'
    and a.status in ('reserved','processing') and not a.assembly_started for update;
  if asset.id is null then return; end if;
  if asset.status = 'reserved' then
    if (select count(*) from storage.objects o where o.bucket_id = 'attachment-intake'
        and o.name in (select asset.intake_path || '/' || lpad(n::text, 3, '0') || '.jpg' from generate_series(1, asset.page_count) n))
        <> asset.page_count then
      return;
    end if;
    update public.attachment_assets a set status = 'processing', processing_token = gen_random_uuid(),
      ready_path = a.owner_id::text || '/' || a.id::text || '/clean.pdf' where a.id = asset.id;
  end if;
  update public.attachment_assets a set updated_at = now() where a.id = asset.id;
  update public.attachment_pages p set status = 'processing', processing_token = token, updated_at = now()
    where p.asset_id = asset.id and p.page = p_page and p.status = 'reserved';
  if not found then return; end if;
  return query select asset.id, asset.owner_id, asset.intake_path, p_page, token;
end $$;

create function public.finish_attachment_page(p_asset_id uuid, p_page integer, p_token uuid, p_width integer, p_height integer)
returns void language plpgsql security invoker set search_path = '' as $$
begin
  update public.attachment_pages p set status = 'ready', width = p_width, height = p_height, updated_at = now()
    where p.asset_id = p_asset_id and p.page = p_page and p.status = 'processing' and p.processing_token = p_token
      and p.updated_at > now() - interval '5 minutes'
      and exists (select 1 from public.attachment_assets a where a.id = p_asset_id and a.status = 'processing');
  if not found then raise exception 'Processing lease expired' using errcode = '55000'; end if;
end $$;

-- One bad page rejects the whole document; nothing of it is ever released.
create function public.reject_attachment_page(p_asset_id uuid, p_page integer, p_token uuid, p_reason text)
returns void language sql security invoker set search_path = '' as $$
  update public.attachment_assets a set status = 'rejected', reject_reason = left(p_reason, 120), updated_at = now()
    where a.id = p_asset_id and a.status = 'processing' and exists (select 1 from public.attachment_pages p
      where p.asset_id = p_asset_id and p.page = p_page and p.status = 'processing' and p.processing_token = p_token);
$$;

-- 5. Assembly is claimed once, only with every page rebuilt. It issues a fresh
-- asset token, so finish_attachment / reject_attachment fence it as usual.
create function public.claim_attachment_assembly(p_asset_id uuid)
returns setof public.attachment_assets language plpgsql security invoker set search_path = '' as $$
begin
  return query update public.attachment_assets a set assembly_started = true, processing_token = gen_random_uuid(), updated_at = now()
    where a.id = p_asset_id and a.kind = 'pdf' and a.status = 'processing' and not a.assembly_started
      and not exists (select 1 from public.attachment_pages p where p.asset_id = a.id and p.status <> 'ready')
    returning a.*;
end $$;

create function public.attachment_document_pages(p_asset_id uuid)
returns setof public.attachment_pages language sql stable security invoker set search_path = '' as $$
  select * from public.attachment_pages where asset_id = p_asset_id order by page;
$$;

-- 6. Coaches may share PDFs (plans, nutrition guides) as well as voice notes,
-- under the same connection, health-consent and media-sharing checks.
create or replace function public.attach_training_asset(p_author_id uuid, p_athlete_id uuid, p_asset_id uuid, p_source text,
  p_target_id uuid, p_exercise_ref text)
returns setof public.training_attachments language plpgsql security invoker set search_path = '' as $$
declare asset public.attachment_assets; ref text := coalesce(p_exercise_ref, '');
begin
  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text, 2833));
  select * into asset from public.attachment_assets where id = p_asset_id and owner_id = p_author_id for share;
  if asset.id is null then raise exception 'File unavailable' using errcode = '42501'; end if;
  if asset.status <> 'ready' then raise exception 'File is not ready' using errcode = '55000'; end if;
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
  return query insert into public.training_attachments(athlete_id, author_id, asset_id, source, target_id, exercise_ref)
    values (p_athlete_id, p_author_id, asset.id, p_source, p_target_id, ref)
    on conflict (asset_id) do nothing returning *;
end $$;

revoke all on function public.reserve_attachment_document(uuid,integer,integer,text), public.claim_attachment_page(uuid,integer),
  public.finish_attachment_page(uuid,integer,uuid,integer,integer), public.reject_attachment_page(uuid,integer,uuid,text),
  public.claim_attachment_assembly(uuid), public.attachment_document_pages(uuid) from public, anon, authenticated;
grant execute on function public.reserve_attachment_document(uuid,integer,integer,text), public.claim_attachment_page(uuid,integer),
  public.finish_attachment_page(uuid,integer,uuid,integer,integer), public.reject_attachment_page(uuid,integer,uuid,text),
  public.claim_attachment_assembly(uuid), public.attachment_document_pages(uuid) to service_role;
