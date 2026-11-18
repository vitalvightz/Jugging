-- No processing reclaim: a stalled invocation is rejected by cleanup after 24h.
-- A reservation must be uploaded again rather than racing the original worker.
alter table public.attachment_assets add column processing_token uuid;
alter table public.attachment_assets add column intake_removed boolean not null default false;
create or replace function public.claim_attachment_processing(p_asset_id uuid)
returns setof public.attachment_assets language plpgsql security invoker set search_path = '' as $$
begin
  return query update public.attachment_assets a
    set status='processing', processing_token=gen_random_uuid(), updated_at=now(),
      ready_path=a.owner_id::text || '/' || a.id::text || case when a.kind='photo' then '/clean.jpg' else '/clean.wav' end
    where a.id=p_asset_id and a.status='reserved'
      and exists(select 1 from storage.objects o where o.bucket_id='attachment-intake' and o.name=a.intake_path)
    returning a.*;
end $$;
-- Remove unfenced entry points, including service-role access.
drop function public.finish_attachment(uuid,text,integer,integer,integer);
drop function public.reject_attachment(uuid,text);
create function public.finish_attachment(p_asset_id uuid, p_token uuid, p_ready_path text, p_width integer, p_height integer, p_duration_ms integer)
returns void language plpgsql security invoker set search_path = '' as $$
begin
  update public.attachment_assets set status='ready', width=p_width, height=p_height,
    duration_ms=p_duration_ms, updated_at=now()
    where id=p_asset_id and status='processing' and processing_token=p_token and ready_path=p_ready_path
      and updated_at > now()-interval '5 minutes';
  if not found then raise exception 'Processing lease expired' using errcode='55000'; end if;
end $$;
create function public.reject_attachment(p_asset_id uuid, p_token uuid, p_reason text)
returns void language sql security invoker set search_path = '' as $$
  update public.attachment_assets set status='rejected', reject_reason=left(p_reason,120), updated_at=now()
    where id=p_asset_id and status='processing' and processing_token=p_token;
$$;
create or replace function public.attachment_cleanup_candidates() returns setof public.attachment_assets
language plpgsql security invoker set search_path = '' as $$
begin
  -- Fencing happens before removal; failed removals remain eligible on every run.
  update public.attachment_assets a set status='rejected'
    where a.updated_at < now()-interval '24 hours' and
      (a.status in ('reserved','processing') or
       (a.status='ready' and not exists(select 1 from public.training_attachments t where t.asset_id=a.id)));
  return query select a.* from public.attachment_assets a
    where a.status='rejected' or (a.status='ready' and not a.intake_removed)
    order by a.updated_at limit 50;
end $$;
create function public.ack_attachment_cleanup(p_asset_id uuid, p_all_removed boolean)
returns void language sql security invoker set search_path = '' as $$
  update public.attachment_assets set intake_removed=true,
    status=case when p_all_removed and status='rejected' then 'deleted' else status end
    where id=p_asset_id and status in ('ready','rejected');
$$;
revoke all on function public.finish_attachment(uuid,uuid,text,integer,integer,integer),
  public.reject_attachment(uuid,uuid,text), public.ack_attachment_cleanup(uuid,boolean) from public,anon,authenticated;
grant execute on function public.finish_attachment(uuid,uuid,text,integer,integer,integer),
  public.reject_attachment(uuid,uuid,text), public.ack_attachment_cleanup(uuid,boolean) to service_role;
