-- Signup is self-service for athlete/coach only. Runtime permissions continue
-- to use the stored profile role, never mutable Auth user metadata.
alter table public.profiles add column signup_account_type_selected_at timestamptz;

create function private.guard_signup_account_type_choice() returns trigger
language plpgsql security invoker set search_path='' as $$
begin
  if auth.role() is distinct from 'service_role' and
    ((tg_op='INSERT' and new.signup_account_type_selected_at is not null) or
     (tg_op='UPDATE' and new.signup_account_type_selected_at is distinct from old.signup_account_type_selected_at)) then
    raise exception 'Signup account type is server-owned' using errcode='42501';
  end if;
  return new;
end $$;
revoke all on function private.guard_signup_account_type_choice() from public,anon,authenticated;
create trigger profiles_guard_signup_account_type before insert or update on public.profiles
  for each row execute function private.guard_signup_account_type_choice();

create function private.select_signup_account_type(p_athlete_id uuid,p_account_type text) returns text
language plpgsql security definer set search_path='' as $$
declare profile public.profiles;
begin
  if auth.role() is distinct from 'service_role' then
    raise exception 'Server access required' using errcode='42501';
  end if;
  if p_account_type is null or p_account_type not in ('athlete','coach') then
    raise exception 'Invalid signup account type' using errcode='22023';
  end if;
  select * into profile from public.profiles where id=p_athlete_id for update;
  if profile.id is null or profile.access_status<>'approved' then
    raise exception 'Account unavailable' using errcode='42501';
  end if;
  -- Staff provisioned by an operator never lose privileges through signup.
  if profile.role='admin' then return 'admin'; end if;
  if profile.signup_account_type_selected_at is not null then
    if profile.role::text=p_account_type then return p_account_type; end if;
    raise exception 'Signup account type is already selected' using errcode='42501';
  end if;
  if profile.role='coach' and p_account_type='coach' then return 'coach'; end if;
  if profile.role<>'athlete' or profile.created_at < now()-interval '24 hours'
    or exists(select 1 from public.plans where athlete_id=p_athlete_id)
    or exists(select 1 from public.athlete_intakes where athlete_id=p_athlete_id)
    or exists(select 1 from public.session_logs where athlete_id=p_athlete_id)
    or exists(select 1 from public.session_completions where athlete_id=p_athlete_id)
    or exists(select 1 from public.external_training_sessions where athlete_id=p_athlete_id)
    or exists(select 1 from public.today_checkins where athlete_id=p_athlete_id)
    or exists(select 1 from public.daily_checkins where athlete_id=p_athlete_id)
    or exists(select 1 from public.injury_flags where athlete_id=p_athlete_id) then
    raise exception 'Signup account type is locked for this account' using errcode='42501';
  end if;
  update public.profiles set role=p_account_type::public.app_role,
    signup_account_type_selected_at=now() where id=p_athlete_id;
  if p_account_type='coach' then
    -- A Tuta signup may have been invited before choosing its account type.
    -- It is now a coach, so close incoming athlete invitations without sharing.
    update public.coach_connections set status='declined',updated_at=now()
      where status='pending' and invited_email=(select lower(email) from auth.users where id=p_athlete_id);
  end if;
  return p_account_type;
end $$;
revoke all on function private.select_signup_account_type(uuid,text) from public,anon,authenticated;
grant usage on schema private to service_role;
grant execute on function private.select_signup_account_type(uuid,text) to service_role;
create function public.select_signup_account_type(p_athlete_id uuid,p_account_type text) returns text
language sql security invoker set search_path='' as $$ select private.select_signup_account_type(p_athlete_id,p_account_type); $$;
revoke all on function public.select_signup_account_type(uuid,text) from public,anon,authenticated;
grant execute on function public.select_signup_account_type(uuid,text) to service_role;
