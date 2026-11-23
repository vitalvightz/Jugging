-- New accounts no longer wait in an admin approval queue. Signing up is enough
-- to build a first plan, so every profile — existing and future — is approved.
--
-- The column, its check constraint and the service-role-only update guard stay:
-- community and clinical-review RPCs still read access_status = 'approved', and
-- keeping the column lets an operator suspend an account by hand if ever needed.

alter table public.profiles
  alter column access_status set default 'approved';

-- private.prevent_client_access_status_change() only lets service_role change
-- access_status, and a migration runs as the database owner, so the guard is
-- paused for this one backfill and restored straight after.
do $$
declare
  has_guard boolean := exists (
    select 1
    from pg_trigger
    where tgrelid = 'public.profiles'::regclass
      and tgname = 'profiles_prevent_client_access_status_change'
      and not tgisinternal
  );
begin
  if has_guard then
    alter table public.profiles disable trigger profiles_prevent_client_access_status_change;
  end if;

  update public.profiles
  set access_status = 'approved'
  where access_status <> 'approved';

  if has_guard then
    alter table public.profiles enable trigger profiles_prevent_client_access_status_change;
  end if;
end;
$$;
