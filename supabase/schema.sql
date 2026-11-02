create extension if not exists pgcrypto;

-- Internal helpers that must never be reachable as client RPCs live here, per
-- 20260726162809_harden_internal_functions_and_search_paths. Note that this file
-- still declares the older guard helpers (prevent_self_role_escalation,
-- prevent_username_policy_bypass, is_admin) under `public`; that migration moved
-- them to `private` on live databases, so those declarations are stale. New
-- guards are written against the live convention.
create schema if not exists private authorization postgres;
revoke all on schema private from public;
grant usage on schema private to anon, authenticated, service_role;

-- Role foundation for the single Unlxck app. `athlete` and `admin` are live in
-- private beta; `coach` and `gym_owner` are reserved for public beta (not yet
-- selectable at sign-up). On databases created before these values existed, the
-- 20260611130000 migration backfills `coach` and `gym_owner` into the enum.
do $$
begin
  create type public.app_role as enum ('athlete', 'coach', 'gym_owner', 'admin');
exception
  when duplicate_object then null;
end
$$;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = timezone('utc', now());
  return new;
end;
$$;

-- Retained for backend/service-role introspection and historical migrations
-- only. It intentionally trusts profiles.role = 'admin' alone, so it must NOT be
-- used in any browser-facing RLS policy or mutation guard: the env allowlist
-- (UNLXCK_ADMIN_EMAILS) is the real admin kill-switch and lives only in the
-- backend, so cross-athlete access is granted exclusively via service-role
-- endpoints. See migration 20260710120000_revoke_client_admin_cross_athlete_rls.sql.
create or replace function public.is_admin()
returns boolean
language sql
security definer
stable
set search_path = public
as $$
  select exists(
    select 1
    from public.profiles
    where id = auth.uid()
      and role = 'admin'
  );
$$;

create or replace function public.validate_generation_job_active_lock()
returns boolean
language sql
stable
as $$
select exists (
  select 1
  from pg_indexes
  where schemaname = 'public'
    and tablename = 'generation_jobs'
    and indexname = 'generation_jobs_one_active_job_per_athlete'
    and lower(indexdef) like 'create unique index%'
    and lower(indexdef) like '%(athlete_id)%'
    and lower(indexdef) like '%where%'
    and lower(indexdef) like '%queued%'
    and lower(indexdef) like '%running%'
);
$$;

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null unique,
  username text unique
    constraint profiles_username_length
      check (username is null or char_length(username) between 3 and 24)
    constraint profiles_username_lowercase
      check (username is null or username = lower(username))
    constraint profiles_username_format
      check (username is null or username ~ '^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$'),
  username_change_history jsonb not null default '[]'::jsonb,
  role public.app_role not null default 'athlete',
  access_status text not null default 'approved'
    constraint profiles_access_status_check check (access_status in ('pending', 'approved')),
  full_name text not null default '',
  technical_style text[] not null default '{}',
  tactical_style text[] not null default '{}',
  stance text not null default '',
  professional_status text not null default '',
  record_summary text not null default '',
  athlete_timezone text not null default '',
  athlete_locale text not null default '',
  appearance_mode text not null default 'dark',
  avatar_url text,
  onboarding_draft jsonb,
  nutrition_profile jsonb not null default '{}'::jsonb,
  -- When the athlete confirmed they read the private trial instructions. The
  -- gate lives on the profile, not in browser storage, so it survives a device
  -- change, a reinstall, and a cleared cache.
  private_trial_ack_at timestamptz,
  -- Compliance evidence. The athlete's age band is derived from date_of_birth
  -- server-side (never a client-supplied is_minor flag), and Terms acceptance is
  -- recorded separately from health-data consent because UK GDPR Art. 9(2)(a)
  -- consent must be specific, separate and withdrawable. private_trial_ack_at
  -- is not reusable for either: it evidences a different thing and has no
  -- document version. See docs/health-data-lawful-basis-dpia.md.
  date_of_birth date
    constraint profiles_date_of_birth_plausible
      check (date_of_birth is null or date_of_birth > date '1900-01-01'),
  terms_version text,
  terms_accepted_at timestamptz,
  health_consent_version text,
  health_data_consent boolean not null default false,
  health_consent_at timestamptz,
  health_consent_withdrawn_at timestamptz,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

comment on column public.profiles.health_data_consent is
  'Current explicit health-data consent choice. Server-owned and stored with the grant or withdrawal timestamp.';


-- Role and username changes must flow through the service-role backend (which
-- records the admin audit trail and enforces the username-change policy). There
-- is deliberately no is_admin() bypass here: a stale DB-role admin whose email
-- was removed from UNLXCK_ADMIN_EMAILS must not be able to self-escalate or
-- bypass the username policy directly from the browser. See migration
-- 20260710120000_revoke_client_admin_cross_athlete_rls.sql.
create or replace function public.prevent_self_role_escalation()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if auth.role() <> 'service_role' then
    if (tg_op = 'INSERT' and new.role <> 'athlete')
      or (tg_op = 'UPDATE' and new.role is distinct from old.role)
      or (tg_op = 'UPDATE' and new.access_status is distinct from old.access_status) then
      raise exception 'Only the backend service role can change profile roles.';
    end if;
  end if;

  return new;
end;
$$;

create or replace function public.prevent_username_policy_bypass()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if auth.role() <> 'service_role' then
    if new.username is distinct from old.username
      or new.username_change_history is distinct from old.username_change_history then
      raise exception 'Use the username change endpoint.';
    end if;
  end if;

  return new;
end;
$$;

-- Consent evidence is only evidence if the subject cannot write it themselves.
-- The live UPDATE policy (profiles_self_or_admin_update) lets a browser client
-- write its own row and a DB-role admin write anyone's, so without this guard an
-- athlete could stamp their own terms_accepted_at, backdate a health-data
-- consent, or edit their date of birth out of the under-18 band.
--
-- Lives in `private` with a pinned search_path, per
-- 20260726162809_harden_internal_functions_and_search_paths. Not SECURITY
-- DEFINER: it reads auth.role() and the row, then raises, so it needs no
-- elevated privilege.
create or replace function private.prevent_client_compliance_writes()
returns trigger
language plpgsql
set search_path = pg_catalog, pg_temp
as $$
begin
  if auth.role() <> 'service_role' then
    if tg_op = 'INSERT' then
      if new.date_of_birth is not null
        or new.terms_version is not null
        or new.terms_accepted_at is not null
        or new.health_data_consent is true
        or new.health_consent_version is not null
        or new.health_consent_at is not null
        or new.health_consent_withdrawn_at is not null then
        raise exception 'Consent and date of birth are set by the backend only.';
      end if;
    elsif tg_op = 'UPDATE' then
      if new.date_of_birth is distinct from old.date_of_birth
        or new.terms_version is distinct from old.terms_version
        or new.terms_accepted_at is distinct from old.terms_accepted_at
        or new.health_data_consent is distinct from old.health_data_consent
        or new.health_consent_version is distinct from old.health_consent_version
        or new.health_consent_at is distinct from old.health_consent_at
        or new.health_consent_withdrawn_at is distinct from old.health_consent_withdrawn_at then
        raise exception 'Consent and date of birth are set by the backend only.';
      end if;
    end if;
  end if;

  return new;
end;
$$;

-- The 13+ floor from docs/children-age-appropriate-use-policy.md, applied to
-- every writer including the service role. A CHECK constraint cannot express it
-- (check expressions must be immutable, and this needs current_date).
create or replace function private.enforce_profile_minimum_age()
returns trigger
language plpgsql
set search_path = pg_catalog, pg_temp
as $$
begin
  if new.date_of_birth is not null
    and new.date_of_birth > (current_date - interval '13 years') then
    raise exception 'under_minimum_age'
      using detail = 'UNLXCK accounts require an athlete aged 13 or over.';
  end if;

  return new;
end;
$$;

create table if not exists public.athlete_intakes (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  fight_date date,
  technical_style text[] not null default '{}',
  intake jsonb not null,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

create table if not exists public.plans (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  intake_id uuid references public.athlete_intakes(id) on delete set null,
  fight_date date,
  technical_style text[] not null default '{}',
  full_name text not null default '',
  plan_name text not null default '',
  status text not null default 'generated',
  plan_text text not null default '',
  draft_plan_text text not null default '',
  final_plan_text text not null default '',
  coach_notes text not null default '',
  pdf_url text,
  why_log jsonb not null default '{}'::jsonb,
  planning_brief text,
  stage2_payload jsonb,
  stage2_handoff_text text not null default '',
  stage2_retry_text text not null default '',
  stage2_validator_report jsonb not null default '{}'::jsonb,
  stage2_status text not null default '',
  stage2_attempt_count integer not null default 0,
  structured_plan jsonb,
  schema_version text,
  created_at timestamptz not null default timezone('utc', now())
);

alter table public.profiles
add column if not exists active_plan_id uuid references public.plans(id) on delete set null;

create table if not exists public.generation_jobs (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  client_request_id text not null,
  source text not null default 'self_serve',
  request_payload jsonb not null default '{}'::jsonb,
  payload_hash text,
  status text not null default 'queued',
  error text,
  intake_id uuid references public.athlete_intakes(id) on delete set null,
  stage1_result jsonb,
  final_result jsonb,
  plan_id uuid references public.plans(id) on delete set null,
  attempt_count integer not null default 0,
  heartbeat_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,
  failed_at timestamptz,
  -- Worker ownership for the current running attempt (set by
  -- claim_generation_job; checked by the terminal RPCs). Lock expiry rides on
  -- heartbeat_at staleness, so there is no separate lock_expires_at.
  claimed_by text,
  claimed_at timestamptz,
  -- Stage 2 token/cost telemetry (nullable; populated best-effort after Stage 2
  -- finalization so cost can be audited per athlete/job from the database).
  stage2_model text,
  stage2_input_tokens integer,
  stage2_output_tokens integer,
  stage2_total_tokens integer,
  stage2_estimated_cost_usd numeric(14, 6),
  stage2_attempt_count integer,
  stage2_response_id text,
  stage2_cost_recorded_at timestamptz,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  constraint generation_jobs_athlete_client_request_key unique (athlete_id, client_request_id)
);

create table if not exists public.plan_generation_rate_limits (
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  created_at timestamptz not null default timezone('utc', now())
);

alter table public.plans add column if not exists draft_plan_text text not null default '';
alter table public.plans add column if not exists final_plan_text text not null default '';
alter table public.plans add column if not exists plan_name text not null default '';
alter table public.plans add column if not exists stage2_retry_text text not null default '';
alter table public.plans add column if not exists stage2_validator_report jsonb not null default '{}'::jsonb;
alter table public.plans add column if not exists stage2_status text not null default '';
alter table public.plans add column if not exists stage2_attempt_count integer not null default 0;
alter table public.plans add column if not exists parsing_metadata jsonb not null default '{}'::jsonb;
alter table public.generation_jobs add column if not exists source text not null default 'self_serve';
alter table public.generation_jobs add column if not exists request_payload jsonb not null default '{}'::jsonb;
alter table public.generation_jobs add column if not exists payload_hash text;
alter table public.generation_jobs add column if not exists status text not null default 'queued';
alter table public.generation_jobs add column if not exists error text;
alter table public.generation_jobs add column if not exists intake_id uuid references public.athlete_intakes(id) on delete set null;
alter table public.generation_jobs add column if not exists stage1_result jsonb;
alter table public.generation_jobs add column if not exists final_result jsonb;
alter table public.generation_jobs add column if not exists plan_id uuid references public.plans(id) on delete set null;
alter table public.generation_jobs add column if not exists attempt_count integer not null default 0;
alter table public.generation_jobs add column if not exists heartbeat_at timestamptz;
alter table public.generation_jobs add column if not exists started_at timestamptz;
alter table public.generation_jobs add column if not exists completed_at timestamptz;
alter table public.generation_jobs add column if not exists failed_at timestamptz;
alter table public.generation_jobs add column if not exists claimed_by text;
alter table public.generation_jobs add column if not exists claimed_at timestamptz;
alter table public.generation_jobs add column if not exists updated_at timestamptz not null default timezone('utc', now());
alter table public.generation_jobs add column if not exists progress_milestones jsonb not null default '[]'::jsonb;
alter table public.generation_jobs add column if not exists stage2_model text;
alter table public.generation_jobs add column if not exists stage2_input_tokens integer;
alter table public.generation_jobs add column if not exists stage2_output_tokens integer;
alter table public.generation_jobs add column if not exists stage2_total_tokens integer;
alter table public.generation_jobs add column if not exists stage2_estimated_cost_usd numeric(14, 6);
alter table public.generation_jobs add column if not exists stage2_attempt_count integer;
alter table public.generation_jobs add column if not exists stage2_response_id text;
alter table public.generation_jobs add column if not exists stage2_cost_recorded_at timestamptz;
alter table public.profiles add column if not exists appearance_mode text not null default 'dark';
alter table public.profiles add column if not exists avatar_url text;
alter table public.profiles add column if not exists nutrition_profile jsonb not null default '{}'::jsonb;
alter table public.profiles add column if not exists username text;
alter table public.profiles add column if not exists username_change_history jsonb not null default '[]'::jsonb;

do $$
begin
  update public.generation_jobs
  set status = case
    when status is null or btrim(status) = '' then 'queued'
    when lower(btrim(status)) in ('queued', 'running', 'completed', 'review_required', 'failed') then lower(btrim(status))
    when lower(btrim(status)) in ('held_for_review', 'needs_review', 'medical_hold', 'restricted_rehab_only') then 'review_required'
    when lower(btrim(status)) in ('generated', 'ready', 'publishable_with_flags', 'triage_blocked', 'archived') then 'completed'
    else 'review_required'
  end
  where status is null
     or status <> lower(btrim(status))
     or lower(btrim(status)) not in ('queued', 'running', 'completed', 'review_required', 'failed');

  update public.plans
  set status = case
    when status is null or btrim(status) = '' then 'generated'
    when lower(btrim(status)) in (
      'generated',
      'ready',
      'review_required',
      'held_for_review',
      'publishable_with_flags',
      'triage_blocked',
      'medical_hold',
      'restricted_rehab_only',
      'needs_review',
      'archived'
    ) then lower(btrim(status))
    else 'review_required'
  end
  where status is null
     or status <> lower(btrim(status))
     or lower(btrim(status)) not in (
      'generated',
      'ready',
      'review_required',
      'held_for_review',
      'publishable_with_flags',
      'triage_blocked',
      'medical_hold',
      'restricted_rehab_only',
      'needs_review',
      'archived'
     );

  if not exists (
    select 1
    from pg_constraint
    where conname = 'generation_jobs_status_check'
      and conrelid = 'public.generation_jobs'::regclass
  ) then
    alter table public.generation_jobs
      add constraint generation_jobs_status_check
      check (status in ('queued', 'running', 'completed', 'review_required', 'failed'));
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conname = 'plans_status_check'
      and conrelid = 'public.plans'::regclass
  ) then
    alter table public.plans
      add constraint plans_status_check
      check (
        status in (
          'generated',
          'ready',
          'review_required',
          'held_for_review',
          'publishable_with_flags',
          'triage_blocked',
          'medical_hold',
          'restricted_rehab_only',
          'needs_review',
          'archived'
        )
      )
      not valid;
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conname = 'profiles_username_key'
      and conrelid = 'public.profiles'::regclass
  ) then
    alter table public.profiles add constraint profiles_username_key unique (username);
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conname = 'profiles_username_length'
      and conrelid = 'public.profiles'::regclass
  ) then
    alter table public.profiles
      add constraint profiles_username_length
      check (username is null or char_length(username) between 3 and 24);
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conname = 'profiles_username_lowercase'
      and conrelid = 'public.profiles'::regclass
  ) then
    alter table public.profiles
      add constraint profiles_username_lowercase
      check (username is null or username = lower(username));
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conname = 'profiles_username_format'
      and conrelid = 'public.profiles'::regclass
  ) then
    alter table public.profiles
      add constraint profiles_username_format
      check (username is null or username ~ '^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$');
  end if;
end
$$;

alter table public.plans validate constraint plans_status_check;

alter table public.athlete_intakes add column if not exists updated_at timestamptz not null default timezone('utc', now());

create index if not exists profiles_email_idx on public.profiles (email);
create index if not exists profiles_username_idx on public.profiles (username);
create index if not exists profiles_active_plan_id_idx on public.profiles(active_plan_id);
create index if not exists athlete_intakes_athlete_id_created_at_idx on public.athlete_intakes (athlete_id, created_at desc);
create index if not exists plans_athlete_id_created_at_idx on public.plans (athlete_id, created_at desc);
create index if not exists generation_jobs_athlete_id_created_at_idx on public.generation_jobs (athlete_id, created_at desc);
create index if not exists generation_jobs_status_heartbeat_at_idx on public.generation_jobs (status, heartbeat_at);
create unique index if not exists generation_jobs_athlete_client_request_uidx on public.generation_jobs (athlete_id, client_request_id);
create unique index if not exists generation_jobs_one_active_job_per_athlete on public.generation_jobs (athlete_id) where status in ('queued', 'running');
-- Supports admin/dev "highest-cost jobs" lookups.
create index if not exists generation_jobs_stage2_estimated_cost_idx on public.generation_jobs (stage2_estimated_cost_usd desc nulls last);
create index if not exists plan_generation_rate_limits_athlete_created_idx on public.plan_generation_rate_limits (athlete_id, created_at);

drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at
before update on public.profiles
for each row
execute function public.set_updated_at();


drop trigger if exists profiles_prevent_self_role_escalation on public.profiles;
create trigger profiles_prevent_self_role_escalation
before insert or update on public.profiles
for each row
execute function public.prevent_self_role_escalation();

drop trigger if exists profiles_prevent_username_policy_bypass on public.profiles;
create trigger profiles_prevent_username_policy_bypass
before update on public.profiles
for each row
execute function public.prevent_username_policy_bypass();

drop trigger if exists profiles_prevent_client_compliance_writes on public.profiles;
create trigger profiles_prevent_client_compliance_writes
before insert or update on public.profiles
for each row
execute function private.prevent_client_compliance_writes();

drop trigger if exists profiles_enforce_minimum_age on public.profiles;
create trigger profiles_enforce_minimum_age
before insert or update of date_of_birth on public.profiles
for each row
execute function private.enforce_profile_minimum_age();

create or replace function public.try_parse_timestamptz(p_value text)
returns timestamptz
language plpgsql
stable
as $$
begin
  return p_value::timestamptz;
exception when others then
  return null;
end;
$$;

create or replace function public.change_profile_username(
  p_profile_id uuid,
  p_username text
)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_profile public.profiles%rowtype;
  v_now timestamptz := now();
  v_cutoff timestamptz := v_now - interval '30 days';
  v_recent jsonb := '[]'::jsonb;
  v_next_available timestamptz;
begin
  select *
  into v_profile
  from public.profiles
  where id = p_profile_id
  for update;

  if not found then
    raise exception 'profile_not_found';
  end if;

  if v_profile.username is not null and v_profile.username = p_username then
    return;
  end if;

  select coalesce(jsonb_agg(to_jsonb(parsed_at)), '[]'::jsonb),
         min(parsed_at)
  into v_recent
       ,v_next_available
  from (
    select candidate.parsed_at
    from (
      select public.try_parse_timestamptz(value_text) as parsed_at
      from jsonb_array_elements_text(
        case
          when jsonb_typeof(v_profile.username_change_history) = 'array'
            then v_profile.username_change_history
          else '[]'::jsonb
        end
      ) as value_text
    ) candidate
    where candidate.parsed_at is not null and candidate.parsed_at >= v_cutoff
  ) filtered;

  if jsonb_array_length(v_recent) >= 4 then
    raise exception 'username_rate_limit_exceeded:%',
      coalesce((v_next_available + interval '30 days')::text, '');
  end if;

  update public.profiles
  set
    username = p_username,
    username_change_history = v_recent || to_jsonb(v_now),
    updated_at = v_now
  where id = p_profile_id;
end;
$$;

revoke execute on function public.change_profile_username(uuid, text) from public;
revoke execute on function public.change_profile_username(uuid, text) from anon;
revoke execute on function public.change_profile_username(uuid, text) from authenticated;
grant execute on function public.change_profile_username(uuid, text) to service_role;

create or replace function public.check_plan_generation_short_window_limit(
  p_athlete_id uuid,
  p_max_requests integer,
  p_window_seconds double precision
)
returns table (allowed boolean, retry_after_seconds integer)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_now timestamptz := timezone('utc', now());
  v_cutoff timestamptz;
  v_oldest timestamptz;
  v_count integer;
begin
  if p_max_requests <= 0 then
    return query select true, 0;
    return;
  end if;

  v_cutoff := v_now - make_interval(secs => p_window_seconds);

  perform pg_advisory_xact_lock(
    hashtext('plan_generation_rate_limits'),
    hashtext(p_athlete_id::text)
  );

  delete from public.plan_generation_rate_limits
  where athlete_id = p_athlete_id
    and created_at <= v_cutoff;

  select count(*), min(created_at)
  into v_count, v_oldest
  from public.plan_generation_rate_limits
  where athlete_id = p_athlete_id;

  if v_count >= p_max_requests then
    return query
    select
      false,
      greatest(1, ceil(extract(epoch from ((v_oldest + make_interval(secs => p_window_seconds)) - v_now)))::integer);
    return;
  end if;

  insert into public.plan_generation_rate_limits (athlete_id, created_at)
  values (p_athlete_id, v_now);

  return query select true, 0;
end;
$$;

revoke all on function public.check_plan_generation_short_window_limit(uuid, integer, double precision) from public;
revoke all on function public.check_plan_generation_short_window_limit(uuid, integer, double precision) from anon;
revoke all on function public.check_plan_generation_short_window_limit(uuid, integer, double precision) from authenticated;
grant execute on function public.check_plan_generation_short_window_limit(uuid, integer, double precision) to service_role;

drop function if exists public.create_generation_job_with_daily_limit(uuid, text, text, jsonb, integer, timestamptz, text[], uuid, uuid);

create or replace function public.create_generation_job_with_daily_limit(
  p_athlete_id uuid,
  p_client_request_id text,
  p_source text,
  p_request_payload jsonb,
  p_daily_limit integer,
  p_day_start timestamptz,
  p_counted_sources text[],
  p_plan_id uuid default null,
  p_intake_id uuid default null,
  p_payload_hash text default null
)
returns table (job jsonb, limit_exceeded boolean)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_source text := coalesce(nullif(trim(p_source), ''), 'self_serve');
  v_count integer;
  v_existing public.generation_jobs%rowtype;
  v_active public.generation_jobs%rowtype;
  v_inserted public.generation_jobs%rowtype;
begin
  perform pg_advisory_xact_lock(
    hashtext('generation_jobs_daily_cap'),
    hashtext(p_athlete_id::text)
  );

  select *
  into v_existing
  from public.generation_jobs
  where athlete_id = p_athlete_id
    and client_request_id = p_client_request_id
  order by created_at desc
  limit 1;

  if found then
    return query select to_jsonb(v_existing), false;
    return;
  end if;

  if p_daily_limit > 0 then
    select count(*)
    into v_count
    from public.generation_jobs
    where athlete_id = p_athlete_id
      and created_at >= p_day_start
      and (
        coalesce(array_length(p_counted_sources, 1), 0) = 0
        or source = any(p_counted_sources)
      );

    if v_count >= p_daily_limit then
      return query select null::jsonb, true;
      return;
    end if;
  end if;

  select *
  into v_active
  from public.generation_jobs
  where athlete_id = p_athlete_id
    and status in ('queued', 'running')
  order by created_at desc
  limit 1;

  if found then
    raise exception 'generation_job_in_flight';
  end if;

  insert into public.generation_jobs (
    athlete_id,
    client_request_id,
    source,
    request_payload,
    payload_hash,
    status,
    attempt_count,
    heartbeat_at,
    started_at,
    completed_at,
    error,
    intake_id,
    stage1_result,
    final_result,
    plan_id
  )
  values (
    p_athlete_id,
    p_client_request_id,
    v_source,
    coalesce(p_request_payload, '{}'::jsonb),
    nullif(trim(p_payload_hash), ''),
    'queued',
    0,
    null,
    null,
    null,
    null,
    p_intake_id,
    null,
    null,
    p_plan_id
  )
  returning * into v_inserted;

  return query select to_jsonb(v_inserted), false;
end;
$$;

revoke all on function public.create_generation_job_with_daily_limit(uuid, text, text, jsonb, integer, timestamptz, text[], uuid, uuid, text) from public;
revoke all on function public.create_generation_job_with_daily_limit(uuid, text, text, jsonb, integer, timestamptz, text[], uuid, uuid, text) from anon;
revoke all on function public.create_generation_job_with_daily_limit(uuid, text, text, jsonb, integer, timestamptz, text[], uuid, uuid, text) from authenticated;
grant execute on function public.create_generation_job_with_daily_limit(uuid, text, text, jsonb, integer, timestamptz, text[], uuid, uuid, text) to service_role;

-- Atomic claim: flip an eligible job to running, bump attempt_count, and
-- record worker ownership in one guarded update. Concurrent claimers
-- serialize on the row lock; the loser re-evaluates the status/attempt guards
-- against the winner's row, matches nothing, and receives null (a lost claim
-- race is normal, not an error). Eligibility policy (startup-staleness,
-- retry caps) stays in the application; this function only guarantees the
-- transition itself is atomic and owned.
create or replace function public.claim_generation_job(
  p_job_id uuid,
  p_worker_id text,
  p_expected_status text,
  p_expected_attempt_count integer,
  p_progress_milestones jsonb default null,
  p_claimed_at timestamptz default now()
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_job public.generation_jobs%rowtype;
  v_expected_status text := coalesce(nullif(lower(btrim(p_expected_status)), ''), 'queued');
  v_worker_id text := nullif(btrim(p_worker_id), '');
  v_claimed_at timestamptz := coalesce(p_claimed_at, now());
begin
  if v_worker_id is null then
    raise exception 'missing_generation_job_worker_id:%', p_job_id
      using errcode = 'P0001';
  end if;

  if v_expected_status not in ('queued', 'running') then
    return null;
  end if;

  update public.generation_jobs
  set
    status = 'running',
    attempt_count = coalesce(attempt_count, 0) + 1,
    claimed_by = v_worker_id,
    claimed_at = v_claimed_at,
    heartbeat_at = v_claimed_at,
    started_at = case
      when v_expected_status = 'queued' then v_claimed_at
      else coalesce(started_at, v_claimed_at)
    end,
    error = null,
    completed_at = null,
    failed_at = null,
    progress_milestones = coalesce(p_progress_milestones, '[]'::jsonb),
    updated_at = now()
  where id = p_job_id
    and coalesce(nullif(lower(btrim(status)), ''), 'queued') = v_expected_status
    and coalesce(attempt_count, 0) = p_expected_attempt_count
  returning * into v_job;

  if not found then
    return null;
  end if;

  return to_jsonb(v_job);
end;
$$;

-- The terminal RPCs gained a worker-ownership argument; drop the pre-ownership
-- signatures so re-running this schema never leaves stale overloads behind.
drop function if exists public.complete_generation_job(uuid, text, integer, text, jsonb, uuid, text, timestamptz, timestamptz);
drop function if exists public.fail_generation_job(uuid, text, integer, text, jsonb, uuid, jsonb, timestamptz, timestamptz);

create or replace function public.complete_generation_job(
  p_job_id uuid,
  p_expected_status text,
  p_expected_attempt_count integer,
  p_final_status text,
  p_final_result jsonb default null,
  p_plan_id uuid default null,
  p_error text default null,
  p_completed_at timestamptz default now(),
  p_heartbeat_at timestamptz default null,
  p_expected_worker_id text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_job public.generation_jobs%rowtype;
  v_expected_status text := coalesce(nullif(lower(btrim(p_expected_status)), ''), 'running');
  v_final_status text := lower(btrim(p_final_status));
  v_completed_at timestamptz := coalesce(p_completed_at, now());
  v_heartbeat_at timestamptz := coalesce(p_heartbeat_at, v_completed_at);
begin
  if v_final_status not in ('completed', 'review_required') then
    raise exception 'invalid_terminal_status:%', p_final_status
      using errcode = 'P0001';
  end if;

  select *
  into v_job
  from public.generation_jobs
  where id = p_job_id
  for update;

  if not found then
    raise exception 'generation_job_missing:%', p_job_id
      using errcode = 'P0002';
  end if;

  if coalesce(v_job.status, '') <> v_expected_status then
    raise exception 'wrong_generation_job_status:% expected %, got %',
      p_job_id, v_expected_status, coalesce(v_job.status, '<null>')
      using errcode = 'P0001';
  end if;

  if coalesce(v_job.attempt_count, 0) <> p_expected_attempt_count then
    raise exception 'stale_generation_job_attempt:% expected %, got %',
      p_job_id, p_expected_attempt_count, coalesce(v_job.attempt_count, 0)
      using errcode = 'P0001';
  end if;

  -- Ownership guard: a caller that claims to be a specific worker may only
  -- finish a job that worker still owns. claimed_by stays null only for rows
  -- claimed before the worker-ownership migration, so the null case is let
  -- through and the status/attempt guards above carry the protection there.
  if p_expected_worker_id is not null
    and v_job.claimed_by is not null
    and v_job.claimed_by <> p_expected_worker_id then
    raise exception 'stale_generation_job_worker:% expected %, got %',
      p_job_id, p_expected_worker_id, v_job.claimed_by
      using errcode = 'P0001';
  end if;

  update public.generation_jobs
  set
    status = v_final_status,
    final_result = coalesce(p_final_result, final_result),
    plan_id = coalesce(p_plan_id, plan_id),
    error = p_error,
    completed_at = v_completed_at,
    failed_at = null,
    heartbeat_at = v_heartbeat_at,
    updated_at = now()
  where id = p_job_id
  returning * into v_job;

  return to_jsonb(v_job);
end;
$$;

create or replace function public.fail_generation_job(
  p_job_id uuid,
  p_expected_status text,
  p_expected_attempt_count integer,
  p_error text,
  p_final_result jsonb default null,
  p_plan_id uuid default null,
  p_progress_milestones jsonb default null,
  p_failed_at timestamptz default now(),
  p_heartbeat_at timestamptz default null,
  p_expected_worker_id text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_job public.generation_jobs%rowtype;
  v_expected_status text := coalesce(nullif(lower(btrim(p_expected_status)), ''), 'running');
  v_failed_at timestamptz := coalesce(p_failed_at, now());
  v_heartbeat_at timestamptz := coalesce(p_heartbeat_at, v_failed_at);
begin
  select *
  into v_job
  from public.generation_jobs
  where id = p_job_id
  for update;

  if not found then
    raise exception 'generation_job_missing:%', p_job_id
      using errcode = 'P0002';
  end if;

  if coalesce(v_job.status, '') <> v_expected_status then
    raise exception 'wrong_generation_job_status:% expected %, got %',
      p_job_id, v_expected_status, coalesce(v_job.status, '<null>')
      using errcode = 'P0001';
  end if;

  if coalesce(v_job.attempt_count, 0) <> p_expected_attempt_count then
    raise exception 'stale_generation_job_attempt:% expected %, got %',
      p_job_id, p_expected_attempt_count, coalesce(v_job.attempt_count, 0)
      using errcode = 'P0001';
  end if;

  -- Ownership guard: see complete_generation_job for the null-claimed_by
  -- rationale.
  if p_expected_worker_id is not null
    and v_job.claimed_by is not null
    and v_job.claimed_by <> p_expected_worker_id then
    raise exception 'stale_generation_job_worker:% expected %, got %',
      p_job_id, p_expected_worker_id, v_job.claimed_by
      using errcode = 'P0001';
  end if;

  update public.generation_jobs
  set
    status = 'failed',
    error = coalesce(nullif(p_error, ''), 'Generation job failed.'),
    final_result = coalesce(p_final_result, final_result),
    plan_id = coalesce(p_plan_id, plan_id),
    progress_milestones = coalesce(p_progress_milestones, progress_milestones),
    completed_at = v_failed_at,
    failed_at = v_failed_at,
    heartbeat_at = v_heartbeat_at,
    updated_at = now()
  where id = p_job_id
  returning * into v_job;

  return to_jsonb(v_job);
end;
$$;

revoke all on function public.claim_generation_job(uuid, text, text, integer, jsonb, timestamptz) from public;
revoke all on function public.claim_generation_job(uuid, text, text, integer, jsonb, timestamptz) from anon;
revoke all on function public.claim_generation_job(uuid, text, text, integer, jsonb, timestamptz) from authenticated;
grant execute on function public.claim_generation_job(uuid, text, text, integer, jsonb, timestamptz) to service_role;

revoke all on function public.complete_generation_job(uuid, text, integer, text, jsonb, uuid, text, timestamptz, timestamptz, text) from public;
revoke all on function public.complete_generation_job(uuid, text, integer, text, jsonb, uuid, text, timestamptz, timestamptz, text) from anon;
revoke all on function public.complete_generation_job(uuid, text, integer, text, jsonb, uuid, text, timestamptz, timestamptz, text) from authenticated;
grant execute on function public.complete_generation_job(uuid, text, integer, text, jsonb, uuid, text, timestamptz, timestamptz, text) to service_role;

revoke all on function public.fail_generation_job(uuid, text, integer, text, jsonb, uuid, jsonb, timestamptz, timestamptz, text) from public;
revoke all on function public.fail_generation_job(uuid, text, integer, text, jsonb, uuid, jsonb, timestamptz, timestamptz, text) from anon;
revoke all on function public.fail_generation_job(uuid, text, integer, text, jsonb, uuid, jsonb, timestamptz, timestamptz, text) from authenticated;
grant execute on function public.fail_generation_job(uuid, text, integer, text, jsonb, uuid, jsonb, timestamptz, timestamptz, text) to service_role;

-- Deploy-gate introspection helper. Returns ONLY catalog metadata (object
-- names + per-table RLS flags) for the public schema as a single jsonb object.
-- It never reads application/user row data. Consumed by
-- tools/check_supabase_runtime_schema.py to verify the live database matches the
-- schema the backend depends on before deploy. Restricted to service_role.
create or replace function public.runtime_schema_introspection()
returns jsonb
language sql
security definer
stable
set search_path = public
as $$
  select jsonb_build_object(
    'tables', (
      select coalesce(jsonb_agg(table_name order by table_name), '[]'::jsonb)
      from information_schema.tables
      where table_schema = 'public'
        and table_type = 'BASE TABLE'
    ),
    'columns', (
      select coalesce(jsonb_object_agg(table_name, cols), '{}'::jsonb)
      from (
        select table_name, jsonb_agg(column_name order by column_name) as cols
        from information_schema.columns
        where table_schema = 'public'
        group by table_name
      ) grouped
    ),
    'functions', (
      select coalesce(jsonb_agg(distinct p.proname order by p.proname), '[]'::jsonb)
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
      where n.nspname = 'public'
    ),
    'private_functions', (
      select coalesce(jsonb_agg(distinct p.proname order by p.proname), '[]'::jsonb)
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
      where n.nspname = 'private'
    ),
    'indexes', (
      select coalesce(jsonb_agg(indexname order by indexname), '[]'::jsonb)
      from pg_indexes
      where schemaname = 'public'
    ),
    'constraints', (
      select coalesce(jsonb_agg(c.conname order by c.conname), '[]'::jsonb)
      from pg_constraint c
      join pg_namespace n on n.oid = c.connamespace
      where n.nspname = 'public'
    ),
    'rls', (
      select coalesce(jsonb_object_agg(c.relname, c.relrowsecurity), '{}'::jsonb)
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
      where n.nspname = 'public'
        and c.relkind = 'r'
    )
  );
$$;

revoke all on function public.runtime_schema_introspection() from public;
revoke all on function public.runtime_schema_introspection() from anon;
revoke all on function public.runtime_schema_introspection() from authenticated;
grant execute on function public.runtime_schema_introspection() to service_role;

drop trigger if exists generation_jobs_set_updated_at on public.generation_jobs;
create trigger generation_jobs_set_updated_at
before update on public.generation_jobs
for each row
execute function public.set_updated_at();

drop trigger if exists athlete_intakes_set_updated_at on public.athlete_intakes;
create trigger athlete_intakes_set_updated_at
before update on public.athlete_intakes
for each row
execute function public.set_updated_at();

drop view if exists public.admin_athlete_rollups;
create or replace view public.admin_athlete_rollups
with (security_invoker = true) as
select
  p.id,
  p.email,
  p.username,
  p.role,
  p.full_name,
  p.technical_style,
  p.tactical_style,
  p.stance,
  p.professional_status,
  p.record_summary,
  p.athlete_timezone,
  p.athlete_locale,
  p.appearance_mode,
  p.onboarding_draft,
  p.nutrition_profile,
  p.created_at,
  p.updated_at,
  count(pl.id)::int as plan_count,
  max(pl.created_at) as latest_plan_created_at,
  p.access_status,
  -- Earliest plan the athlete could train from; see
  -- 20261012140100_admin_time_to_first_usable_plan.sql.
  min(pl.created_at) filter (
    where pl.status in ('ready', 'publishable_with_flags')
  ) as first_usable_plan_at
from public.profiles p
left join public.plans pl on pl.athlete_id = p.id
group by
  p.id,
  p.email,
  p.username,
  p.role,
  p.full_name,
  p.technical_style,
  p.tactical_style,
  p.stance,
  p.professional_status,
  p.record_summary,
  p.athlete_timezone,
  p.athlete_locale,
  p.appearance_mode,
  p.onboarding_draft,
  p.nutrition_profile,
  p.created_at,
  p.updated_at,
  p.access_status;

-- The rollup view aggregates every athlete's profile. It is read only by the
-- service-role backend (api/store.py). security_invoker keeps it bound to the
-- caller's RLS context, and the explicit grants ensure browser-facing roles
-- cannot read it directly even if default privileges grant SELECT on new views.
revoke all on public.admin_athlete_rollups from anon;
revoke all on public.admin_athlete_rollups from authenticated;
grant select on public.admin_athlete_rollups to service_role;

alter table public.profiles enable row level security;
alter table public.athlete_intakes enable row level security;
alter table public.plans enable row level security;
alter table public.generation_jobs enable row level security;
alter table public.plan_generation_rate_limits enable row level security;

-- Own-rows-only for browser/anon clients. Cross-athlete admin reads go through
-- the FastAPI service-role endpoints (which enforce the env allowlist), never
-- through client RLS — so public.is_admin() no longer appears here.
drop policy if exists "profiles_self_or_admin_select" on public.profiles;
drop policy if exists "profiles_self_select" on public.profiles;
create policy "profiles_self_select" on public.profiles
for select using (auth.uid() = id);

drop policy if exists "profiles_self_update" on public.profiles;
create policy "profiles_self_update" on public.profiles
for update using (auth.uid() = id)
with check (auth.uid() = id);

drop policy if exists "intakes_self_or_admin_select" on public.athlete_intakes;
drop policy if exists "intakes_self_select" on public.athlete_intakes;
create policy "intakes_self_select" on public.athlete_intakes
for select using (athlete_id = auth.uid());

-- athlete_intakes: authenticated browser clients may read their own records,
-- but writes must go through FastAPI/service-role business logic.
drop policy if exists "intakes_self_or_admin_insert" on public.athlete_intakes;
drop policy if exists "intakes_self_or_admin_update" on public.athlete_intakes;
drop policy if exists "intakes_self_or_admin_delete" on public.athlete_intakes;
drop policy if exists "athlete_intakes_self_or_admin_insert" on public.athlete_intakes;
drop policy if exists "athlete_intakes_self_or_admin_update" on public.athlete_intakes;
drop policy if exists "athlete_intakes_self_or_admin_delete" on public.athlete_intakes;

drop policy if exists "plans_self_or_admin_select" on public.plans;
drop policy if exists "plans_self_select" on public.plans;
create policy "plans_self_select" on public.plans
for select using (athlete_id = auth.uid());

-- plans: authenticated browser clients may read their own plans,
-- but writes must go through FastAPI/service-role business logic.
drop policy if exists "plans_self_or_admin_insert" on public.plans;
drop policy if exists "plans_self_or_admin_update" on public.plans;
drop policy if exists "plans_self_or_admin_delete" on public.plans;

drop policy if exists "generation_jobs_self_or_admin_select" on public.generation_jobs;
drop policy if exists "generation_jobs_self_select" on public.generation_jobs;
create policy "generation_jobs_self_select" on public.generation_jobs
for select using (athlete_id = auth.uid());

-- generation_jobs: authenticated browser clients may read their own jobs,
-- but writes must go through FastAPI/service-role business logic.
drop policy if exists "generation_jobs_self_or_admin_insert" on public.generation_jobs;
drop policy if exists "generation_jobs_self_or_admin_update" on public.generation_jobs;
drop policy if exists "generation_jobs_self_or_admin_delete" on public.generation_jobs;

-- ---------------------------------------------------------------------------
-- Admin role change audit trail
-- (see migration 20260606010000_add_admin_role_audit.sql for rationale)
-- ---------------------------------------------------------------------------
create table if not exists public.admin_role_audit (
  id uuid primary key default gen_random_uuid(),
  target_athlete_id uuid references public.profiles(id) on delete set null,
  target_email text not null,
  previous_role public.app_role,
  new_role public.app_role not null,
  action text not null check (action in ('promote', 'revoke')),
  actor text not null,
  reason text,
  created_at timestamptz not null default now()
);

create index if not exists admin_role_audit_target_email_idx
  on public.admin_role_audit (target_email);
create index if not exists admin_role_audit_created_at_idx
  on public.admin_role_audit (created_at desc);

alter table public.admin_role_audit enable row level security;

-- Admin-only table: no browser access at all. The backend reads it via
-- service_role (which bypasses RLS), so a permanently-false policy plus the
-- revoked authenticated grant keeps every browser client out — including a
-- stale DB-role admin — while the service-role admin endpoints keep working.
drop policy if exists "admin_role_audit_admin_select" on public.admin_role_audit;
drop policy if exists "admin_role_audit_no_client_select" on public.admin_role_audit;
create policy "admin_role_audit_no_client_select" on public.admin_role_audit
for select using (false);

revoke all on public.admin_role_audit from anon;
revoke all on public.admin_role_audit from authenticated;
grant all on public.admin_role_audit to service_role;

-- Atomic admin role change + audit (see migration
-- 20260610150000_atomic_admin_role_change_audit.sql for rationale): the role
-- update and its audit row commit in one transaction, so a role change can
-- never land without its audit record. Policy checks (last-admin lockout,
-- no-op short-circuit) stay in api/store.py::set_profile_role.
create or replace function public.set_profile_role_with_audit(
  p_athlete_id uuid,
  p_new_role text,
  p_actor text,
  p_expected_previous_role text default null,
  p_reason text default null,
  p_target_email text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_profile public.profiles%rowtype;
  v_new_role text := lower(btrim(p_new_role));
  v_actor text := nullif(btrim(p_actor), '');
  v_previous_role text;
  v_target_email text;
  v_action text;
begin
  if v_new_role is null or v_new_role not in ('admin', 'athlete') then
    raise exception 'unsupported_profile_role:%', p_new_role
      using errcode = 'P0001';
  end if;

  if v_actor is null then
    raise exception 'missing_role_change_actor:%', p_athlete_id
      using errcode = 'P0001';
  end if;

  select *
  into v_profile
  from public.profiles
  where id = p_athlete_id
  for update;

  if not found then
    raise exception 'profile_missing:%', p_athlete_id
      using errcode = 'P0002';
  end if;

  v_previous_role := coalesce(v_profile.role::text, 'athlete');

  -- CAS guard: the caller decided on the change after reading this role; a
  -- concurrent change means their decision (and audit previous_role) would be
  -- stale, so refuse instead of recording a misleading trail.
  if p_expected_previous_role is not null
    and v_previous_role <> lower(btrim(p_expected_previous_role)) then
    raise exception 'stale_profile_role:% expected %, got %',
      p_athlete_id, p_expected_previous_role, v_previous_role
      using errcode = 'P0001';
  end if;

  if v_previous_role = v_new_role then
    return jsonb_build_object(
      'athlete_id', v_profile.id,
      'email', lower(btrim(coalesce(v_profile.email, p_target_email, ''))),
      'previous_role', v_previous_role,
      'new_role', v_new_role,
      'changed', false
    );
  end if;

  v_target_email := coalesce(
    nullif(lower(btrim(v_profile.email)), ''),
    nullif(lower(btrim(p_target_email)), '')
  );
  if v_target_email is null then
    raise exception 'missing_role_change_target_email:%', p_athlete_id
      using errcode = 'P0001';
  end if;

  v_action := case when v_new_role = 'admin' then 'promote' else 'revoke' end;

  update public.profiles
  set role = v_new_role::public.app_role
  where id = p_athlete_id;

  -- Same transaction as the role update: if this insert fails, the role
  -- change rolls back with it.
  insert into public.admin_role_audit (
    target_athlete_id,
    target_email,
    previous_role,
    new_role,
    action,
    actor,
    reason
  )
  values (
    p_athlete_id,
    v_target_email,
    v_previous_role::public.app_role,
    v_new_role::public.app_role,
    v_action,
    v_actor,
    p_reason
  );

  return jsonb_build_object(
    'athlete_id', v_profile.id,
    'email', v_target_email,
    'previous_role', v_previous_role,
    'new_role', v_new_role,
    'action', v_action,
    'changed', true
  );
end;
$$;

revoke all on function public.set_profile_role_with_audit(uuid, text, text, text, text, text) from public;
revoke all on function public.set_profile_role_with_audit(uuid, text, text, text, text, text) from anon;
revoke all on function public.set_profile_role_with_audit(uuid, text, text, text, text, text) from authenticated;
grant execute on function public.set_profile_role_with_audit(uuid, text, text, text, text, text) to service_role;

-- ---------------------------------------------------------------------------
-- Live athlete daily tracking (see
-- supabase/migrations/20260611120000_add_live_athlete_daily_tracking.sql).
-- daily_checkins, session_logs, injury_flags, adaptation_notes record the
-- athlete's day-to-day state; admin_reviews is the ops attention queue.
-- Adaptation decisions are append-only history — plans are never silently
-- rewritten.
-- ---------------------------------------------------------------------------

create table if not exists public.daily_checkins (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  checkin_date date not null,
  readiness integer not null check (readiness between 1 and 5),
  fatigue integer not null check (fatigue between 1 and 5),
  soreness integer not null check (soreness between 1 and 5),
  sleep_quality integer not null check (sleep_quality between 1 and 5),
  sleep_hours numeric(4,1) check (sleep_hours is null or (sleep_hours >= 0 and sleep_hours <= 24)),
  injury_note text not null default '',
  notes text not null default '',
  readiness_state text not null default 'ready'
    check (readiness_state in ('ready', 'caution', 'high_fatigue', 'injury_flag')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint daily_checkins_athlete_date_key unique (athlete_id, checkin_date)
);

create index if not exists daily_checkins_athlete_date_idx
  on public.daily_checkins (athlete_id, checkin_date desc);

drop trigger if exists set_daily_checkins_updated_at on public.daily_checkins;
create trigger set_daily_checkins_updated_at
  before update on public.daily_checkins
  for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- session_logs: what the athlete actually did. plan_id is nullable so logs
-- survive plan deletion and ad-hoc sessions can be logged without a plan.
-- ---------------------------------------------------------------------------
create table if not exists public.session_logs (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  plan_id uuid references public.plans(id) on delete set null,
  session_date date not null,
  session_type text not null default 'training',
  completed boolean not null default true,
  rpe integer check (rpe is null or rpe between 1 and 10),
  duration_minutes integer check (duration_minutes is null or (duration_minutes > 0 and duration_minutes <= 600)),
  notes text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists session_logs_athlete_date_idx
  on public.session_logs (athlete_id, session_date desc);

drop trigger if exists set_session_logs_updated_at on public.session_logs;
create trigger set_session_logs_updated_at
  before update on public.session_logs
  for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- injury_flags: open flags drive the 'injury_flag' readiness state and the
-- admin review queue. Flags are resolved (status transition), never deleted,
-- so the injury history stays auditable.
-- ---------------------------------------------------------------------------

-- infection_signs is modelled as list[str] everywhere above the database, so the
-- column constraint checks element type as well as shape. A CHECK cannot contain
-- a subquery, hence this immutable helper.
create or replace function public.injury_flags_infection_signs_valid(signs jsonb)
returns boolean
language sql
immutable
parallel safe
as $$
  select case
    when signs is null then true
    when jsonb_typeof(signs) <> 'array' then false
    when jsonb_array_length(signs) > 8 then false
    else not exists (
      select 1
      from jsonb_array_elements(signs) as element
      where jsonb_typeof(element) <> 'string'
    )
  end;
$$;

create table if not exists public.injury_flags (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  plan_id uuid references public.plans(id) on delete set null,
  source text not null default 'checkin'
    check (source in ('checkin', 'session_log', 'manual', 'admin', 'intake')),
  body_area text not null default '',
  description text not null,
  severity text not null default 'moderate'
    check (severity in ('mild', 'moderate', 'severe')),
  -- Who owns the current severity. The surface follow-up derives a severity
  -- FLOOR from the structured wound answers, so a raise has to be marked as the
  -- system's — otherwise a later clean recheck reads it as the athlete's own
  -- choice and can never lower it. ``manual_severity`` holds the athlete's value
  -- underneath an active floor so releasing the floor restores it. NULL source
  -- reads as 'manual': a legacy severity is never auto-lowered.
  severity_source text
    check (severity_source is null or severity_source in ('manual', 'surface_system')),
  manual_severity text
    check (manual_severity is null or manual_severity in ('mild', 'moderate', 'severe')),
  constraint injury_flags_manual_severity_pairing_check check (
    (severity_source = 'surface_system' and manual_severity is not null)
    or (severity_source is distinct from 'surface_system' and manual_severity is null)
  ),
  status text not null default 'open'
    check (status in ('open', 'monitoring', 'resolved')),
  latest_reported_status text not null default 'ongoing'
    check (latest_reported_status in ('ongoing', 'improving', 'worse', 'resolved')),
  -- Structured surface (skin) safety answers, captured by the Today injury
  -- check-in's conditional follow-up when a skin injury is marked worse. All
  -- optional: a missing answer reads as "unknown", never as "clear".
  skin_integrity text
    check (skin_integrity is null or skin_integrity in ('intact', 'open', 'unknown')),
  bleeding_status text
    check (bleeding_status is null or bleeding_status in ('none', 'controlled', 'uncontrolled')),
  drainage text
    check (drainage is null or drainage in ('none', 'present', 'unknown')),
  infection_signs jsonb not null default '[]'::jsonb
    check (public.injury_flags_infection_signs_valid(infection_signs)),
  coverable text
    check (coverable is null or coverable in ('yes', 'no', 'unknown')),
  friction_or_contact_problem text
    check (
      friction_or_contact_problem is null
      or friction_or_contact_problem in ('yes', 'no', 'unknown')
    ),
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists injury_flags_athlete_status_idx
  on public.injury_flags (athlete_id, status);

drop trigger if exists set_injury_flags_updated_at on public.injury_flags;
create trigger set_injury_flags_updated_at
  before update on public.injury_flags
  for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- adaptation_notes: append-only record of every rule decision the system made
-- from logged data (reduce intensity, add recovery, flag for review, ...).
-- This is the "never silently change a plan" guarantee: any adjustment shown
-- to the athlete has a row here explaining which rule fired and why.
-- ---------------------------------------------------------------------------
create table if not exists public.adaptation_notes (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  plan_id uuid references public.plans(id) on delete set null,
  checkin_id uuid references public.daily_checkins(id) on delete set null,
  session_log_id uuid references public.session_logs(id) on delete set null,
  rule_code text not null,
  decision text not null
    check (decision in ('keep_plan', 'reduce_intensity', 'swap_session', 'add_recovery', 'flag_admin_review')),
  summary text not null,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists adaptation_notes_athlete_created_idx
  on public.adaptation_notes (athlete_id, created_at desc);

-- ---------------------------------------------------------------------------
-- admin_reviews: ops queue of athletes who need human attention (injury
-- reports, sustained high fatigue, repeated misses). Rows are resolved, not
-- deleted. resolved_by stores the admin's email for accountability.
-- ---------------------------------------------------------------------------
create table if not exists public.admin_reviews (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  adaptation_note_id uuid references public.adaptation_notes(id) on delete set null,
  injury_flag_id uuid references public.injury_flags(id) on delete set null,
  reason text not null,
  status text not null default 'pending'
    check (status in ('pending', 'acknowledged', 'resolved')),
  resolution_notes text not null default '',
  resolved_by text not null default '',
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists admin_reviews_status_created_idx
  on public.admin_reviews (status, created_at desc);
create index if not exists admin_reviews_athlete_idx
  on public.admin_reviews (athlete_id);

drop trigger if exists set_admin_reviews_updated_at on public.admin_reviews;
create trigger set_admin_reviews_updated_at
  before update on public.admin_reviews
  for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- RLS: athletes read their own rows, admins read all; writes are service-role
-- only (no insert/update/delete policies), matching plans/athlete_intakes.
-- admin_reviews is admin-read-only, matching admin_role_audit.
-- ---------------------------------------------------------------------------
alter table public.daily_checkins enable row level security;
alter table public.session_logs enable row level security;
alter table public.injury_flags enable row level security;
alter table public.adaptation_notes enable row level security;
alter table public.admin_reviews enable row level security;

-- Own-rows-only for browser/anon clients; cross-athlete admin access is
-- service-role only (see is_admin() note above).
drop policy if exists "daily_checkins_owner_select" on public.daily_checkins;
create policy "daily_checkins_owner_select" on public.daily_checkins
for select using (athlete_id = auth.uid());

drop policy if exists "session_logs_owner_select" on public.session_logs;
create policy "session_logs_owner_select" on public.session_logs
for select using (athlete_id = auth.uid());

drop policy if exists "injury_flags_owner_select" on public.injury_flags;
create policy "injury_flags_owner_select" on public.injury_flags
for select using (athlete_id = auth.uid());

drop policy if exists "adaptation_notes_owner_select" on public.adaptation_notes;
create policy "adaptation_notes_owner_select" on public.adaptation_notes
for select using (athlete_id = auth.uid());

-- Admin-only table: no browser access at all (service_role bypasses RLS).
drop policy if exists "admin_reviews_admin_select" on public.admin_reviews;
drop policy if exists "admin_reviews_no_client_select" on public.admin_reviews;
create policy "admin_reviews_no_client_select" on public.admin_reviews
for select using (false);

revoke all on public.daily_checkins from anon;
revoke all on public.session_logs from anon;
revoke all on public.injury_flags from anon;
revoke all on public.adaptation_notes from anon;
revoke all on public.admin_reviews from anon;

revoke insert, update, delete on public.daily_checkins from authenticated;
revoke insert, update, delete on public.session_logs from authenticated;
revoke insert, update, delete on public.injury_flags from authenticated;
revoke insert, update, delete on public.adaptation_notes from authenticated;
revoke all on public.admin_reviews from authenticated;

grant select on public.daily_checkins to authenticated;
grant select on public.session_logs to authenticated;
grant select on public.injury_flags to authenticated;
grant select on public.adaptation_notes to authenticated;

grant all on public.daily_checkins to service_role;
grant all on public.session_logs to service_role;
grant all on public.injury_flags to service_role;
grant select, insert on public.adaptation_notes to service_role;
grant all on public.admin_reviews to service_role;

-- ---------------------------------------------------------------------------
-- Block 4 Today/Overview persistence: athlete-local Today check-ins (with the
-- server-evaluated recommendation co-located) and per-session completion
-- records. Keyed by athlete-local training day (04:00 rollover). See
-- supabase/migrations/20260618120000_add_today_checkins_and_completions.sql.
-- ---------------------------------------------------------------------------
create table if not exists public.today_checkins (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  plan_id uuid not null references public.plans(id) on delete cascade,
  training_day date not null,
  athlete_timezone text not null default '',
  sleep text not null check (sleep in ('poor', 'okay', 'good')),
  body text not null check (body in ('flat', 'normal', 'sharp')),
  pain text not null check (pain in ('none', 'manageable', 'high')),
  phase text not null check (phase in ('GPP', 'SPP', 'TAPER', 'REINTEGRATION')),
  active_injury text not null default 'none'
    check (active_injury in ('none', 'stable', 'worse')),
  previous_session text not null default 'none'
    check (previous_session in ('none', 'normal', 'very_hard')),
  sharp_pain boolean not null default false,
  instability boolean not null default false,
  swelling boolean not null default false,
  neurological_symptoms boolean not null default false,
  illness_symptoms boolean not null default false,
  cannot_warm_into_movement boolean not null default false,
  worse_next_day_pain boolean not null default false,
  recommendation_state text not null
    check (recommendation_state in ('train_as_planned', 'modify', 'pull_back')),
  recommendation_reason text not null default '',
  recommendation_triggers jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint today_checkins_athlete_plan_day_key unique (athlete_id, plan_id, training_day)
);

create index if not exists today_checkins_athlete_day_idx
  on public.today_checkins (athlete_id, training_day desc);

drop trigger if exists set_today_checkins_updated_at on public.today_checkins;
create trigger set_today_checkins_updated_at
  before update on public.today_checkins
  for each row execute function public.set_updated_at();

create table if not exists public.session_completions (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  plan_id uuid not null references public.plans(id) on delete cascade,
  session_id text not null,
  training_day date not null,
  status text not null default 'not_started'
    check (status in ('not_started', 'started', 'done', 'modified', 'skipped')),
  session_rpe integer check (session_rpe is null or session_rpe between 1 and 10),
  pain_after integer check (pain_after is null or pain_after between 0 and 10),
  modification_reason text not null default '',
  notes text not null default '',
  started_at timestamptz,
  completed_at timestamptz,
  rehab_response_contexts jsonb
    check (
      rehab_response_contexts is null
      or jsonb_typeof(rehab_response_contexts) = 'array'
    ),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint session_completions_athlete_session_day_key unique (athlete_id, session_id, training_day)
);

comment on column public.session_completions.rehab_response_contexts is
  'Server-owned immutable rehab response identities. Pending state is derived from canonical rehab_exposures, never a client-controlled answered flag.';

create index if not exists session_completions_athlete_day_idx
  on public.session_completions (athlete_id, training_day desc);

drop trigger if exists set_session_completions_updated_at on public.session_completions;
create trigger set_session_completions_updated_at
  before update on public.session_completions
  for each row execute function public.set_updated_at();

alter table public.today_checkins enable row level security;
alter table public.session_completions enable row level security;

-- Own-rows-only for browser/anon clients; cross-athlete admin access is
-- service-role only (see is_admin() note above).
drop policy if exists "today_checkins_owner_select" on public.today_checkins;
create policy "today_checkins_owner_select" on public.today_checkins
for select using (athlete_id = auth.uid());

drop policy if exists "session_completions_owner_select" on public.session_completions;
create policy "session_completions_owner_select" on public.session_completions
for select using (athlete_id = auth.uid());

revoke all on public.today_checkins from anon;
revoke all on public.session_completions from anon;

revoke insert, update, delete on public.today_checkins from authenticated;
revoke insert, update, delete on public.session_completions from authenticated;

grant select on public.today_checkins to authenticated;
grant select on public.session_completions to authenticated;

grant all on public.today_checkins to service_role;
grant all on public.session_completions to service_role;


-- Web push subscriptions for athlete notifications. Kept in sync with
-- supabase/migrations/20260721120000_add_push_subscriptions.sql.

create table if not exists public.push_subscriptions (
  id uuid primary key default gen_random_uuid(),
  profile_id uuid not null references public.profiles(id) on delete cascade,
  endpoint text not null,
  p256dh text not null,
  auth text not null,
  timezone text not null default '',
  morning_last_sent_day date,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint push_subscriptions_endpoint_key unique (endpoint)
);

create index if not exists push_subscriptions_profile_idx
  on public.push_subscriptions (profile_id);

drop trigger if exists set_push_subscriptions_updated_at on public.push_subscriptions;
create trigger set_push_subscriptions_updated_at
  before update on public.push_subscriptions
  for each row execute function public.set_updated_at();

alter table public.push_subscriptions enable row level security;

drop policy if exists "push_subscriptions_owner_select" on public.push_subscriptions;
create policy "push_subscriptions_owner_select" on public.push_subscriptions
for select using (profile_id = auth.uid());

revoke all on public.push_subscriptions from anon;
revoke insert, update, delete on public.push_subscriptions from authenticated;
grant select on public.push_subscriptions to authenticated;
grant all on public.push_subscriptions to service_role;

+-- Secure beta feedback. All application access is server-side through the
-- service role; there are intentionally no browser-facing RLS policies.

create table if not exists public.beta_feedback (
  id uuid primary key default gen_random_uuid(),
  submitted_by_profile_id uuid not null references public.profiles(id) on delete cascade,
  context_key text not null check (char_length(context_key) between 1 and 180),
  surface text not null
    constraint beta_feedback_surface_check
      check (surface in ('plan', 'daily_recommendation', 'session', 'global')),
  category text not null
    constraint beta_feedback_category_check
      check (category in (
        'plan_usefulness',
        'recommendation_fit',
        'recommendation_safety',
        'session_review',
        'bug_report',
        'feature_request',
        'safety_issue',
        'general_feedback'
      )),
  response text check (response is null or response in ('yes', 'no', 'unsafe')),
  reason text,
  comment text not null default '' check (char_length(comment) <= 500),
  -- Session surface only: the quick structured answers collected after a
  -- completed session (difficulty / instructions / plan accuracy).
  structured_response jsonb not null default '{}'::jsonb,
  contact_allowed boolean not null default false,
  priority text not null default 'normal',
  plan_id uuid references public.plans(id) on delete set null,
  today_checkin_id uuid references public.today_checkins(id) on delete set null,
  -- Session surface only: the reviewed plan session (session_completions.session_id).
  session_id text,
  camp_phase text,
  readiness_snapshot jsonb not null default '{}'::jsonb,
  injury_snapshot jsonb not null default '{}'::jsonb,
  app_version text not null,
  technical_context jsonb not null default '{}'::jsonb,
  screenshot_path text,
  screenshot_mime text check (
    screenshot_mime is null or screenshot_mime in ('image/png', 'image/jpeg', 'image/webp')
  ),
  screenshot_size_bytes integer check (
    screenshot_size_bytes is null or screenshot_size_bytes between 1 and 5242880
  ),
  screenshot_width integer check (
    screenshot_width is null or screenshot_width between 1 and 4096
  ),
  screenshot_height integer check (
    screenshot_height is null or screenshot_height between 1 and 4096
  ),
  screenshot_expires_at timestamptz,
  screenshot_deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint beta_feedback_submitter_context_key unique (submitted_by_profile_id, context_key),
  constraint beta_feedback_surface_category_check check (
    (surface = 'plan' and category = 'plan_usefulness')
    or (surface = 'daily_recommendation' and category in ('recommendation_fit', 'recommendation_safety'))
    or (surface = 'session' and category = 'session_review')
    or (surface = 'global' and category in ('bug_report', 'feature_request', 'safety_issue', 'general_feedback'))
  ),
  constraint beta_feedback_response_shape_check check (
    (surface in ('global', 'session') and response is null)
    or (surface = 'plan' and response in ('yes', 'no'))
    or (surface = 'daily_recommendation' and response in ('yes', 'no', 'unsafe'))
  ),
  constraint beta_feedback_reason_check check (
    (surface in ('global', 'session') and reason is null)
    or (response in ('yes', 'unsafe') and reason is null)
    or (surface = 'plan' and response = 'no' and (
      reason is null or reason in (
        'too_hard', 'too_easy', 'schedule_mismatch', 'injury_restrictions_wrong',
        'exercises_unsuitable', 'instructions_unclear', 'other'
      )
    ))
    or (surface = 'daily_recommendation' and response = 'no' and (
      reason is null or reason in (
        'too_demanding', 'too_cautious', 'pain_or_injury_ignored',
        'training_mismatch', 'repetitive', 'unclear'
      )
    ))
  ),
  constraint beta_feedback_priority_check check (
    (priority = 'safety' and category in ('recommendation_safety', 'safety_issue'))
    or (priority = 'normal' and category not in ('recommendation_safety', 'safety_issue'))
  ),
  -- Only the session surface stores structured answers, and every answer it
  -- does store has to be one of the offered choices. Keys may be absent: each
  -- question is optional as long as the submission carries something (enforced
  -- by the API).
  constraint beta_feedback_structured_response_check check (
    case
      when surface = 'session' then
        jsonb_typeof(structured_response) = 'object'
        and coalesce(structured_response ->> 'difficulty', 'appropriate')
          in ('too_easy', 'appropriate', 'too_hard')
        and coalesce(structured_response ->> 'instructions', 'clear')
          in ('clear', 'unclear')
        and coalesce(structured_response ->> 'plan_accuracy', 'felt_right')
          in ('felt_right', 'something_wrong')
      else structured_response = '{}'::jsonb
    end
  ),
  -- The 120-character ceiling keeps the derived context key inside its own
  -- 180-character check: "session:{plan_id}:{session_id}:{training_day}" is 56
  -- characters of frame around a UUID plan id and an ISO date.
  constraint beta_feedback_session_id_check check (
    (surface = 'session' and session_id is not null and char_length(session_id) between 1 and 120)
    or (surface <> 'session' and session_id is null)
  ),
  constraint beta_feedback_screenshot_shape_check check (
    (screenshot_path is null and screenshot_mime is null and screenshot_size_bytes is null
      and screenshot_width is null and screenshot_height is null and screenshot_expires_at is null)
    or (screenshot_path is not null and screenshot_mime is not null and screenshot_size_bytes is not null
      and screenshot_width is not null and screenshot_height is not null
      and screenshot_expires_at is not null and screenshot_deleted_at is null)
  )
);

create index if not exists beta_feedback_priority_created_idx
  on public.beta_feedback (priority desc, created_at desc);

create index if not exists beta_feedback_submitter_created_idx
  on public.beta_feedback (submitted_by_profile_id, created_at desc);

create index if not exists beta_feedback_screenshot_expiry_idx
  on public.beta_feedback (screenshot_expires_at)
  where screenshot_path is not null and screenshot_deleted_at is null;

drop trigger if exists set_beta_feedback_updated_at on public.beta_feedback;
create trigger set_beta_feedback_updated_at
  before update on public.beta_feedback
  for each row execute function public.set_updated_at();

create table if not exists public.beta_feedback_rate_limits (
  id bigint generated by default as identity primary key,
  submitted_by_profile_id uuid not null references public.profiles(id) on delete cascade,
  scope text not null check (scope in ('global_report', 'screenshot')),
  created_at timestamptz not null default now()
);

create index if not exists beta_feedback_rate_limits_claim_idx
  on public.beta_feedback_rate_limits (submitted_by_profile_id, scope, created_at desc);

create or replace function public.claim_beta_feedback_rate_limit(
  p_submitted_by_profile_id uuid,
  p_report_limit integer,
  p_screenshot_limit integer,
  p_window_seconds integer,
  p_has_screenshot boolean
)
returns table (allowed boolean, blocked_scope text, retry_after_seconds integer)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_now timestamptz := clock_timestamp();
  v_cutoff timestamptz;
  v_count integer;
  v_oldest timestamptz;
begin
  if p_report_limit < 0 or p_screenshot_limit < 0 or p_window_seconds <= 0 then
    raise exception 'invalid feedback rate-limit configuration';
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_submitted_by_profile_id::text, 0));
  v_cutoff := v_now - make_interval(secs => p_window_seconds);

  delete from public.beta_feedback_rate_limits
  where submitted_by_profile_id = p_submitted_by_profile_id
    and created_at < v_cutoff;

  if p_report_limit > 0 then
    select count(*), min(created_at)
      into v_count, v_oldest
    from public.beta_feedback_rate_limits
    where submitted_by_profile_id = p_submitted_by_profile_id
      and scope = 'global_report'
      and created_at >= v_cutoff;

    if v_count >= p_report_limit then
      return query select false, 'global_report'::text,
        greatest(1, ceil(extract(epoch from (v_oldest + make_interval(secs => p_window_seconds) - v_now)))::integer);
      return;
    end if;
  end if;

  if p_has_screenshot and p_screenshot_limit > 0 then
    select count(*), min(created_at)
      into v_count, v_oldest
    from public.beta_feedback_rate_limits
    where submitted_by_profile_id = p_submitted_by_profile_id
      and scope = 'screenshot'
      and created_at >= v_cutoff;

    if v_count >= p_screenshot_limit then
      return query select false, 'screenshot'::text,
        greatest(1, ceil(extract(epoch from (v_oldest + make_interval(secs => p_window_seconds) - v_now)))::integer);
      return;
    end if;
  end if;

  if p_report_limit > 0 then
    insert into public.beta_feedback_rate_limits (submitted_by_profile_id, scope, created_at)
    values (p_submitted_by_profile_id, 'global_report', v_now);
  end if;
  if p_has_screenshot and p_screenshot_limit > 0 then
    insert into public.beta_feedback_rate_limits (submitted_by_profile_id, scope, created_at)
    values (p_submitted_by_profile_id, 'screenshot', v_now);
  end if;

  return query select true, null::text, 0;
end;
$$;

create or replace function public.guard_profile_feedback_screenshots()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if exists (
    select 1 from public.beta_feedback
    where submitted_by_profile_id = old.id and screenshot_path is not null
  ) then
    raise exception 'feedback_screenshots_must_be_purged:%', old.id;
  end if;
  return old;
end;
$$;

drop trigger if exists guard_profile_feedback_screenshots_before_delete on public.profiles;
create trigger guard_profile_feedback_screenshots_before_delete
  before delete on public.profiles
  for each row execute function public.guard_profile_feedback_screenshots();

alter table public.beta_feedback enable row level security;
alter table public.beta_feedback_rate_limits enable row level security;

revoke all on public.beta_feedback from anon, authenticated;
revoke all on public.beta_feedback_rate_limits from anon, authenticated;
revoke all on function public.claim_beta_feedback_rate_limit(uuid, integer, integer, integer, boolean) from public, anon, authenticated;
revoke all on function public.guard_profile_feedback_screenshots() from public, anon, authenticated;

grant all on public.beta_feedback to service_role;
grant all on public.beta_feedback_rate_limits to service_role;
grant usage, select on sequence public.beta_feedback_rate_limits_id_seq to service_role;
grant execute on function public.claim_beta_feedback_rate_limit(uuid, integer, integer, integer, boolean) to service_role;

-- ---------------------------------------------------------------------------
-- Durable account-scoped XP ledger
-- ---------------------------------------------------------------------------

create table if not exists public.xp_accounts (
  athlete_id uuid primary key references public.profiles(id) on delete cascade,
  total_xp bigint not null default 0 check (total_xp >= 0),
  last_daily_login_date date,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.xp_awards (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  action text not null check (action in (
    'daily_login',
    'training_logged',
    'planned_session_completed',
    'recommended_fighter_content_watched',
    'full_training_week_completed'
  )),
  amount integer not null check (
    (action = 'daily_login' and amount = 10)
    or (action = 'training_logged' and amount = 25)
    or (action = 'planned_session_completed' and amount = 50)
    or (action = 'recommended_fighter_content_watched' and amount = 10)
    or (action = 'full_training_week_completed' and amount = 100)
  ),
  idempotency_key text not null check (
    char_length(btrim(idempotency_key)) between 1 and 200
  ),
  calendar_date date,
  awarded_at timestamptz not null default now(),
  constraint xp_awards_daily_calendar_required check (
    (action = 'daily_login' and calendar_date is not null)
    or (action <> 'daily_login' and calendar_date is null)
  ),
  constraint xp_awards_athlete_idempotency_key
    unique (athlete_id, idempotency_key)
);

create unique index if not exists xp_awards_one_daily_login_per_calendar_date
  on public.xp_awards (athlete_id, calendar_date)
  where action = 'daily_login';

create index if not exists xp_awards_athlete_recent_idx
  on public.xp_awards (athlete_id, awarded_at desc, id desc);

alter table public.xp_accounts enable row level security;
alter table public.xp_accounts force row level security;
alter table public.xp_awards enable row level security;
alter table public.xp_awards force row level security;

drop policy if exists "xp_accounts_owner_select" on public.xp_accounts;
create policy "xp_accounts_owner_select" on public.xp_accounts
for select to authenticated
using ((select auth.uid()) = athlete_id);

drop policy if exists "xp_awards_owner_select" on public.xp_awards;
create policy "xp_awards_owner_select" on public.xp_awards
for select to authenticated
using ((select auth.uid()) = athlete_id);

revoke all on table public.xp_accounts from public, anon, authenticated;
revoke all on table public.xp_awards from public, anon, authenticated;
grant select on table public.xp_accounts to authenticated;
grant select on table public.xp_awards to authenticated;
grant select, insert, update, delete on table public.xp_accounts to service_role;
grant select, insert, update, delete on table public.xp_awards to service_role;

create or replace function public.award_athlete_xp(
  p_athlete_id uuid,
  p_action text,
  p_idempotency_key text,
  p_calendar_date date default null
)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_action text := btrim(coalesce(p_action, ''));
  v_key text := btrim(coalesce(p_idempotency_key, ''));
  v_amount integer;
  v_previous_total bigint;
  v_total bigint;
  v_last_daily_login_date date;
  v_award public.xp_awards%rowtype;
  v_awarded boolean := false;
  v_recent_awards jsonb;
begin
  if coalesce(auth.role(), '') <> 'service_role' then
    raise exception 'award_athlete_xp is restricted to the backend service role'
      using errcode = '42501';
  end if;

  if p_athlete_id is null or not exists (
    select 1 from public.profiles where id = p_athlete_id
  ) then
    raise exception 'xp athlete profile not found' using errcode = '23503';
  end if;

  if char_length(v_key) not between 1 and 200 then
    raise exception 'invalid XP idempotency key' using errcode = '22023';
  end if;

  v_amount := case v_action
    when 'daily_login' then 10
    when 'training_logged' then 25
    when 'planned_session_completed' then 50
    when 'recommended_fighter_content_watched' then 10
    when 'full_training_week_completed' then 100
    else null
  end;

  if v_amount is null then
    raise exception 'unknown XP action' using errcode = '22023';
  end if;

  if v_action = 'daily_login' and p_calendar_date is null then
    raise exception 'daily login XP requires a calendar date' using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text, 0));

  insert into public.xp_accounts (athlete_id)
  values (p_athlete_id)
  on conflict (athlete_id) do nothing;

  select total_xp, last_daily_login_date
    into v_previous_total, v_last_daily_login_date
  from public.xp_accounts
  where athlete_id = p_athlete_id
  for update;

  if v_action <> 'daily_login'
    or v_last_daily_login_date is null
    or p_calendar_date > v_last_daily_login_date then
    insert into public.xp_awards (
      athlete_id,
      action,
      amount,
      idempotency_key,
      calendar_date
    )
    values (
      p_athlete_id,
      v_action,
      v_amount,
      v_key,
      case when v_action = 'daily_login' then p_calendar_date else null end
    )
    on conflict do nothing
    returning * into v_award;

    v_awarded := v_award.id is not null;
  end if;

  if v_awarded then
    update public.xp_accounts
    set
      total_xp = total_xp + v_amount,
      last_daily_login_date = case
        when v_action = 'daily_login' then p_calendar_date
        else last_daily_login_date
      end,
      updated_at = clock_timestamp()
    where athlete_id = p_athlete_id
    returning total_xp, last_daily_login_date
      into v_total, v_last_daily_login_date;
  else
    v_total := v_previous_total;
  end if;

  select coalesce(
    jsonb_agg(recent.item order by recent.awarded_at desc, recent.id desc),
    '[]'::jsonb
  )
  into v_recent_awards
  from (
    select
      award.id,
      award.awarded_at,
      jsonb_strip_nulls(jsonb_build_object(
        'id', award.id,
        'action', award.action,
        'amount', award.amount,
        'awarded_at', award.awarded_at,
        'calendar_date', award.calendar_date
      )) as item
    from public.xp_awards as award
    where award.athlete_id = p_athlete_id
    order by award.awarded_at desc, award.id desc
    limit 20
  ) as recent;

  return jsonb_build_object(
    'state', jsonb_build_object(
      'total_xp', v_total,
      'last_daily_login_date', v_last_daily_login_date,
      'recent_awards', v_recent_awards
    ),
    'previous_total_xp', v_previous_total,
    'awarded', v_awarded,
    'award', case
      when v_awarded then jsonb_strip_nulls(jsonb_build_object(
        'id', v_award.id,
        'action', v_award.action,
        'amount', v_award.amount,
        'awarded_at', v_award.awarded_at,
        'calendar_date', v_award.calendar_date
      ))
      else 'null'::jsonb
    end
  );
end;
$$;

revoke all on function public.award_athlete_xp(uuid, text, text, date)
  from public, anon, authenticated;
grant execute on function public.award_athlete_xp(uuid, text, text, date)
  to service_role;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'feedback-screenshots',
  'feedback-screenshots',
  false,
  5242880,
  array['image/png', 'image/jpeg', 'image/webp']
)
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;


-- Injury / rehab policy persistence (2026-09-30).
-- Injury-specific rehab observations. These rows do not assert tolerance or
-- stage eligibility; interpretation belongs to later condition-specific logic.
alter table public.injury_flags
  add column if not exists episode_id uuid not null default gen_random_uuid(),
  add column if not exists body_region text,
  add column if not exists side text not null default 'unknown'
    check (side in ('left', 'right', 'bilateral', 'unknown'));

create unique index if not exists injury_flags_id_athlete_episode_idx
  on public.injury_flags (id, athlete_id, episode_id);

create or replace function public.rotate_injury_evidence_episode()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  -- A resolved flag explicitly returning to an active lifecycle is the only
  -- reset boundary owned here. Worsening alone does not imply a clinical reset.
  if old.status = 'resolved' and new.status in ('open', 'monitoring') then
    new.episode_id := gen_random_uuid();
  end if;
  return new;
end;
$$;

drop trigger if exists rotate_injury_evidence_episode on public.injury_flags;
create trigger rotate_injury_evidence_episode
  before update of status on public.injury_flags
  for each row execute function public.rotate_injury_evidence_episode();

create table if not exists public.rehab_exposures (
  id uuid primary key,
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  injury_id uuid not null,
  injury_episode_id uuid not null,
  drill_id text not null check (drill_id ~ '^[a-z0-9]+(_[a-z0-9]+)*$'),
  body_region text not null,
  side text not null check (side in ('left', 'right', 'bilateral', 'unknown')),
  demand jsonb not null check (jsonb_typeof(demand) = 'object'),
  prescribed_dose jsonb check (prescribed_dose is null or jsonb_typeof(prescribed_dose) = 'object'),
  completed_dose jsonb not null check (jsonb_typeof(completed_dose) = 'object'),
  response jsonb not null default '{}'::jsonb check (jsonb_typeof(response) = 'object'),
  event_json jsonb not null check (jsonb_typeof(event_json) = 'object'),
  evidence_source text not null check (evidence_source in (
    'athlete_logged_rehab', 'clinician_logged_rehab', 'coach_logged_rehab'
  )),
  occurred_at timestamptz not null,
  recorded_at timestamptz not null,
  created_at timestamptz not null default now(),
  foreign key (injury_id, athlete_id, injury_episode_id)
    references public.injury_flags (id, athlete_id, episode_id) on delete cascade
);

create index if not exists rehab_exposures_injury_episode_occurred_idx
  on public.rehab_exposures (injury_id, injury_episode_id, occurred_at desc);

alter table public.rehab_exposures enable row level security;

drop policy if exists "rehab_exposures_owner_select" on public.rehab_exposures;
create policy "rehab_exposures_owner_select" on public.rehab_exposures
  for select using (athlete_id = auth.uid());
-- Evidence is append-only. Authenticated clients may read their rows but have
-- no direct mutation grant. The backend service calls the validated RPC below.
revoke all on table public.rehab_exposures from anon, authenticated, service_role;
grant select on table public.rehab_exposures to authenticated;



-- Session completion proves that a rehab exposure occurred, but it does not
-- quantify every drill. Admit an explicit unquantified completion state at the
-- database boundary while retaining every identity, demand and response check
-- from the prior RPC revision.



-- One injury-level athlete answer may describe several drill exposures. Keep a
-- queryable shared identity so downstream evidence readers do not count copied
-- response fields as independently assessed responses.
--
-- Older rows cannot be reconstructed into true historical groups because the
-- previous event did not store plan/session response provenance. Giving each
-- one its own group is the conservative backfill: it never falsely groups two
-- independently unknown legacy observations.
update public.rehab_exposures
set event_json = jsonb_set(
  event_json,
  '{response_group_id}',
  to_jsonb(id::text),
  true
)
where nullif(event_json->>'response_group_id', '') is null;

alter table public.rehab_exposures
  add column if not exists response_group_id uuid
    generated always as ((event_json->>'response_group_id')::uuid) stored;

alter table public.rehab_exposures
  alter column response_group_id set not null;

create index if not exists rehab_exposures_response_group_idx
  on public.rehab_exposures (athlete_id, response_group_id);

comment on column public.rehab_exposures.response_group_id is
  'Server-owned deterministic identity for one injury-level athlete response shared by its drill exposures.';


-- Server-owned prescriptions and append-only observations. Ownership mirrors
-- injury_flags / rehab_exposures; an athlete report is not verified clearance.
grant select on public.rehab_exposures to service_role;
alter table public.session_completions
  add column if not exists prescription_snapshot jsonb
    check (prescription_snapshot is null or jsonb_typeof(prescription_snapshot) = 'object'),
  add column if not exists rehab_performance text
    check (rehab_performance in ('done_as_shown', 'changed', 'stopped', 'skipped'));

create or replace function public.lock_injury_prescription_context()
returns trigger language plpgsql set search_path = public, pg_temp as $$
begin
  if tg_op = 'DELETE' then
    perform pg_advisory_xact_lock(hashtextextended('injury:' || old.athlete_id::text, 0));
    return old;
  end if;
  perform pg_advisory_xact_lock(hashtextextended('injury:' || new.athlete_id::text, 0));
  return new;
end;
$$;
drop trigger if exists lock_injury_prescription_context on public.injury_flags;
create trigger lock_injury_prescription_context before insert or update or delete
  on public.injury_flags for each row execute function public.lock_injury_prescription_context();
drop trigger if exists lock_injury_prescription_context on public.today_checkins;
create trigger lock_injury_prescription_context before insert or update or delete
  on public.today_checkins for each row execute function public.lock_injury_prescription_context();

create or replace function public.preserve_started_prescription()
returns trigger language plpgsql set search_path = public, pg_temp as $$
declare
  v_block jsonb;
  v_limit int;
  v_used int;
begin
  if new.prescription_snapshot is not null or (tg_op = 'UPDATE' and old.prescription_snapshot is not null) then
    perform pg_advisory_xact_lock(hashtextextended('injury:' || new.athlete_id::text, 0));
  end if;
  -- INSERT ... ON CONFLICT runs the insert trigger before the update trigger.
  -- The existing occurrence is validated by the UPDATE branch with its real id.
  if tg_op = 'INSERT' and exists (select 1 from public.session_completions s
     where s.athlete_id = new.athlete_id and s.session_id = new.session_id and s.training_day = new.training_day) then
    return new;
  end if;
  if tg_op = 'UPDATE' and old.prescription_snapshot is not null then
    if old.status in ('done','modified') and new.status not in ('done','modified')
       and exists (select 1 from jsonb_array_elements(old.prescription_snapshot->'session'->'blocks') b where b ? 'policy_id') then
      raise exception 'rehab_exposure_cannot_be_reset' using errcode = '23514';
    end if;
    if new.prescription_snapshot is not null and new.prescription_snapshot <> old.prescription_snapshot then
      raise exception 'prescription_revision_conflict' using errcode = '23514';
    end if;
    new.prescription_snapshot := old.prescription_snapshot;
  end if;
  if new.prescription_snapshot is not null then
    if new.prescription_snapshot->>'plan_id' is distinct from new.plan_id::text
       or new.prescription_snapshot->>'training_day' is distinct from new.training_day::text
       or new.prescription_snapshot->'session'->>'session_id' is distinct from new.session_id
       or coalesce(new.prescription_snapshot->>'revision', '') !~ '^[a-f0-9]{64}$' then
      raise exception 'invalid prescription occurrence' using errcode = '23514';
    end if;
    if tg_op = 'INSERT' or old.prescription_snapshot is null then
      perform pg_advisory_xact_lock(hashtextextended('injury:' || new.athlete_id::text, 0));
      if new.prescription_snapshot->'evidence_context'->>'exposure_id' is distinct from
         (select id::text from public.rehab_exposures where athlete_id = new.athlete_id order by created_at desc, id desc limit 1)
         or new.prescription_snapshot->'evidence_context'->>'event_id' is distinct from
         (select id::text from public.injury_episode_events where athlete_id = new.athlete_id
           and event_type in ('injury_checkin','delayed_rehab_response','clinician_clearance_report') order by created_at desc, id desc limit 1) then
        raise exception 'prescription_revision_conflict' using errcode = '23514';
      end if;
      if jsonb_typeof(new.prescription_snapshot->'readiness_context') is distinct from 'object'
         or not exists (select 1 from public.today_checkins c
           where c.athlete_id = new.athlete_id and c.plan_id = new.plan_id
             and c.training_day = new.training_day
             and c.id = (new.prescription_snapshot->'readiness_context'->>'id')::uuid
             and c.updated_at is not distinct from
               (new.prescription_snapshot->'readiness_context'->>'updated_at')::timestamptz) then
        raise exception 'prescription_revision_conflict' using errcode = '23514';
      end if;
      if jsonb_typeof(new.prescription_snapshot->'injury_context') is distinct from 'array'
         or (select count(*) from public.injury_flags where athlete_id = new.athlete_id and status in ('open','monitoring'))
            <> jsonb_array_length(new.prescription_snapshot->'injury_context')
         or exists (select 1 from jsonb_array_elements(new.prescription_snapshot->'injury_context') c
             where not exists (select 1 from public.injury_flags i
               where i.athlete_id = new.athlete_id and i.id = (c->>'id')::uuid
                 and i.episode_id = (c->>'episode_id')::uuid and i.status in ('open','monitoring')
                 and i.updated_at is not distinct from (c->>'updated_at')::timestamptz)) then
        raise exception 'prescription_revision_conflict' using errcode = '23514';
      end if;
    end if;
    if new.status in ('started','done','modified') then
      v_limit := coalesce((new.prescription_snapshot->>'allocation_limit')::int, 2);
      if v_limit not between 1 and 2 then raise exception 'invalid rehab allocation' using errcode = '23514'; end if;
      -- Server-owned bundle identity groups drills without grouping legacy blocks.
      select count(distinct (s.id, coalesce(nullif(b->>'rehab_allocation_id', ''),
                                           'block:' || ordinal::text)))
        into v_used from public.session_completions s,
        lateral jsonb_array_elements(s.prescription_snapshot->'session'->'blocks')
          with ordinality as items(b, ordinal)
        where s.athlete_id = new.athlete_id and s.training_day = new.training_day
          and s.status in ('started','done','modified') and s.id <> new.id and b->>'block_type' = 'rehab';
      if exists (select 1 from jsonb_array_elements(new.prescription_snapshot->'session'->'blocks') b where b->>'block_type' = 'rehab')
         and v_used + (select count(distinct coalesce(nullif(b->>'rehab_allocation_id', ''),
                                                     'block:' || ordinal::text))
                       from jsonb_array_elements(new.prescription_snapshot->'session'->'blocks')
                         with ordinality as items(b, ordinal)
                       where b->>'block_type' = 'rehab') > v_limit then
        raise exception 'rehab_daily_allocation_conflict' using errcode = '23514';
      end if;
      for v_block in select b from jsonb_array_elements(new.prescription_snapshot->'session'->'blocks') b where b ? 'policy_id' loop
        if exists (select 1 from public.session_completions s,
             lateral jsonb_array_elements(s.prescription_snapshot->'session'->'blocks') b
             where s.athlete_id = new.athlete_id and s.id <> new.id and s.status in ('started','done','modified')
               and b->>'injury_id' = v_block->>'injury_id' and b->>'injury_episode_id' = v_block->>'injury_episode_id'
               and (s.training_day = new.training_day or
                 (s.status in ('done','modified') and b->>'rehab_drill_id' = v_block->>'rehab_drill_id'
                   and s.training_day <= new.training_day
                   and s.training_day + greatest(coalesce((b->>'minimum_gap_days')::int,1), (v_block->>'minimum_gap_days')::int) > new.training_day))) then
          raise exception 'rehab_daily_allocation_conflict' using errcode = '23514';
        end if;
      end loop;
    end if;
  end if;
  return new;
end;
$$;
drop trigger if exists preserve_started_prescription on public.session_completions;
create trigger preserve_started_prescription before insert or update
  on public.session_completions for each row execute function public.preserve_started_prescription();

create unique index if not exists injury_flags_owner_identity_idx on public.injury_flags (id, athlete_id);
create table if not exists public.injury_episode_events (
  id uuid primary key,
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  injury_id uuid not null,
  injury_episode_id uuid not null,
  event_type text not null check (event_type in ('clinician_clearance_report', 'delayed_rehab_response', 'injury_checkin')),
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  created_at timestamptz not null default now(),
  foreign key (injury_id, athlete_id) references public.injury_flags (id, athlete_id) on delete cascade
);
create index if not exists injury_episode_events_history_idx on public.injury_episode_events (athlete_id, injury_id, injury_episode_id, created_at desc);
create unique index if not exists injury_episode_events_delayed_once_idx on public.injury_episode_events
  (athlete_id, (payload->>'exposure_id')) where event_type = 'delayed_rehab_response';
alter table public.injury_episode_events enable row level security;
drop policy if exists injury_episode_events_owner_select on public.injury_episode_events;
create policy injury_episode_events_owner_select on public.injury_episode_events
  for select to authenticated using ((select auth.uid()) = athlete_id);
revoke all on public.injury_episode_events from anon, authenticated, service_role;
grant select on public.injury_episode_events to authenticated, service_role;

create or replace function public.capture_injury_episode_change()
returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
begin
  if coalesce(tg_argv[0], '') <> 'explicit_report' and tg_op = 'UPDATE'
     and new.status is not distinct from old.status
     and new.latest_reported_status is not distinct from old.latest_reported_status
     and new.severity is not distinct from old.severity and new.episode_id = old.episode_id then
    return new;
  end if;
  insert into public.injury_episode_events (id, athlete_id, injury_id, injury_episode_id, event_type, payload)
    -- Both row triggers describe one statement, so merge their report marker
    -- before commit rather than recording the same check-in twice.
    values (md5('injury-checkin:' || txid_current()::text || ':' || statement_timestamp()::text
                || ':' || new.id::text || ':' || new.episode_id::text)::uuid,
      new.athlete_id, new.id, new.episode_id, 'injury_checkin',
      jsonb_build_object('status', new.status, 'latest_reported_status', new.latest_reported_status,
                         'severity', new.severity, 'source', new.source,
                         'explicit_report', coalesce(tg_argv[0], '') = 'explicit_report'))
    on conflict (id) do update set payload = excluded.payload || jsonb_build_object(
      'explicit_report', coalesce((injury_episode_events.payload->>'explicit_report')::boolean, false)
                         or (excluded.payload->>'explicit_report')::boolean);
  return new;
end;
$$;
drop trigger if exists capture_injury_episode_change on public.injury_flags;
revoke execute on function public.capture_injury_episode_change()
  from public, anon, authenticated;
create trigger capture_injury_episode_change after insert or update on public.injury_flags
  for each row execute function public.capture_injury_episode_change();
-- UPDATE OF fires for an explicit report even when its status is unchanged.
-- Other edits retain audit history without refreshing recovery evidence.
drop trigger if exists capture_injury_episode_report on public.injury_flags;
create trigger capture_injury_episode_report after update of latest_reported_status on public.injury_flags
  for each row execute function public.capture_injury_episode_change('explicit_report');

create or replace function public.record_injury_episode_event(p_athlete_id uuid, p_event jsonb)
returns public.injury_episode_events language plpgsql security definer
set search_path = public, pg_temp as $$
declare
  v_injury public.injury_flags%rowtype;
  v_result public.injury_episode_events%rowtype;
  v_type text := p_event->>'event_type';
  v_payload jsonb := p_event->'payload';
begin
  -- Always take the athlete lock before any injury row lock.
  perform pg_advisory_xact_lock(hashtextextended('injury:' || p_athlete_id::text, 0));
  select * into v_injury from public.injury_flags
    where id = (p_event->>'injury_id')::uuid and athlete_id = p_athlete_id for share;
  if not found then raise exception 'injury not found' using errcode = '23503'; end if;
  if v_type <> 'delayed_rehab_response' and v_injury.episode_id <> (p_event->>'injury_episode_id')::uuid then
    raise exception 'injury_episode_changed' using errcode = '23514';
  end if;
  if v_type = 'clinician_clearance_report' then
    if v_payload->>'source' is distinct from 'athlete_reported'
       or v_payload->>'externally_verified' is distinct from 'false'
       or jsonb_typeof(v_payload->'scopes') is distinct from 'array'
       or jsonb_array_length(v_payload->'scopes') = 0
       or exists (select 1 from jsonb_array_elements_text(v_payload->'scopes') s
                  where s not in ('rehab','training','contact')) then
      raise exception 'invalid clearance report' using errcode = '22023';
    end if;
  elsif v_type = 'delayed_rehab_response' then
    if coalesce(v_payload->>'response','') not in ('better','same','worse','not_sure')
       or not exists (select 1 from public.rehab_exposures e
            where e.id = (v_payload->>'exposure_id')::uuid and e.athlete_id = p_athlete_id
              and e.injury_id = v_injury.id and e.injury_episode_id = (p_event->>'injury_episode_id')::uuid) then
      raise exception 'invalid delayed response' using errcode = '23514';
    end if;
  else raise exception 'unknown episode event' using errcode = '22023';
  end if;
  insert into public.injury_episode_events (id, athlete_id, injury_id, injury_episode_id, event_type, payload)
    values ((p_event->>'id')::uuid, p_athlete_id, v_injury.id, (p_event->>'injury_episode_id')::uuid, v_type, v_payload)
    on conflict (id) do nothing returning * into v_result;
  if not found then
    select * into v_result from public.injury_episode_events where id = (p_event->>'id')::uuid and athlete_id = p_athlete_id;
    if not found or v_result.payload <> v_payload or v_result.event_type <> v_type
       or v_result.injury_id <> v_injury.id or v_result.injury_episode_id <> (p_event->>'injury_episode_id')::uuid then
      raise exception 'episode_event_conflict' using errcode = '23514';
    end if;
  end if;
  return v_result;
end;
$$;
revoke all on function public.record_injury_episode_event(uuid, jsonb) from public, anon, authenticated;
grant execute on function public.record_injury_episode_event(uuid, jsonb) to service_role;

-- Unanswered rows remain discoverable regardless of how many newer sessions
-- exist. Bounded batches drain oldest first, never dropping durable prompts.
create or replace function public.pending_delayed_rehab(p_athlete_id uuid, p_training_day date)
returns setof public.rehab_exposures language sql security definer
set search_path = public, pg_temp as $$
  select e.* from public.rehab_exposures e
    join public.injury_flags i on i.id = e.injury_id and i.athlete_id = e.athlete_id
    where e.athlete_id = p_athlete_id
      and e.occurred_at::date between p_training_day - 2 and p_training_day
      and coalesce(e.response->>'next_day_response','not_yet_known') = 'not_yet_known'
      and not exists (select 1 from public.injury_episode_events o
         where o.athlete_id = e.athlete_id and o.event_type = 'delayed_rehab_response' and o.payload->>'exposure_id' = e.id::text)
    order by e.occurred_at, e.id limit 100;
$$;
revoke all on function public.pending_delayed_rehab(uuid, date) from public, anon, authenticated;
grant execute on function public.pending_delayed_rehab(uuid, date) to service_role;

-- Historical evidence must survive a resolved flag starting a new episode.
-- The RPC validates the current episode while holding the injury row; the FK
-- enforces ownership without binding old observations to the latest episode.
do $$
 declare v_constraint record;
 begin
   for v_constraint in select c.conname from pg_constraint c
     where c.conrelid = 'public.rehab_exposures'::regclass and c.contype = 'f'
       and c.confrelid = 'public.injury_flags'::regclass
       and exists (select 1 from pg_attribute a where a.attrelid = c.conrelid
                    and a.attnum = any(c.conkey) and a.attname = 'injury_episode_id')
   loop execute format('alter table public.rehab_exposures drop constraint %I', v_constraint.conname); end loop;
   if not exists (select 1 from pg_constraint where conrelid = 'public.rehab_exposures'::regclass
                   and conname = 'rehab_exposures_injury_owner_fkey') then
     alter table public.rehab_exposures add constraint rehab_exposures_injury_owner_fkey
       foreign key (injury_id, athlete_id) references public.injury_flags (id, athlete_id) on delete cascade;
   end if;
 end;
$$;

-- Session completion proves that a rehab exposure occurred, but it does not
-- quantify every drill. Admit an explicit unquantified completion state at the
-- database boundary while retaining every identity, demand and response check
-- from the prior RPC revision.
create or replace function public.record_rehab_exposure(p_athlete_id uuid, p_event jsonb)
returns public.rehab_exposures
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_injury public.injury_flags%rowtype;
  v_existing public.rehab_exposures%rowtype;
  v_result public.rehab_exposures%rowtype;
  v_demand jsonb := p_event->'demand';
  v_completed jsonb := p_event->'dose_completed';
  v_response jsonb := coalesce(p_event->'response', '{}'::jsonb);
  v_side text := p_event->>'side';
  v_injury_side text;
  v_region text := p_event->>'body_region';
  v_completion_state text := v_completed->>'completion_state';
  v_key text;
  v_pain jsonb;
  v_region_guidance boolean;
begin
  if p_event is null or jsonb_typeof(p_event) <> 'object'
     or jsonb_typeof(v_demand) <> 'object'
     or jsonb_typeof(v_completed) <> 'object'
     or jsonb_typeof(v_response) <> 'object' then
    raise exception 'invalid rehab exposure object' using errcode = '22023';
  end if;

  -- Always take the athlete lock before any injury row lock.
  perform pg_advisory_xact_lock(hashtextextended('injury:' || p_athlete_id::text, 0));
  select * into v_injury from public.injury_flags
   where id = (p_event->>'injury_id')::uuid and athlete_id = p_athlete_id for share;
  if not found then raise exception 'injury not found' using errcode = '23503'; end if;
  -- Unknown side is retained only for baseline region-wide guidance accepted
  -- in an exact frozen episode. Unattributed legacy exercise stays ineligible.
  v_injury_side := coalesce(nullif(lower(btrim(v_injury.side)), ''), 'unknown');
  v_region_guidance := v_injury_side = 'unknown' and v_side = 'unknown' and exists (
    select 1 from public.session_completions s,
      lateral jsonb_array_elements(s.prescription_snapshot->'session'->'blocks') b
      where s.athlete_id = p_athlete_id and s.status in ('done','modified')
        and s.prescription_snapshot->>'revision' = p_event#>>'{provenance,prescription_revision}'
        and b->>'injury_id' = p_event->>'injury_id' and b->>'injury_episode_id' = p_event->>'injury_episode_id'
        and b->>'rehab_drill_id' = p_event->>'drill_id'
        and b->>'block_type' = 'rehab'
        and nullif(btrim(b->>'policy_id'), '') is not null
        and b->>'policy_id' = p_event#>>'{provenance,policy_id}'
        and b->'drill_snapshot'->>'rehab_stage' = p_event#>>'{provenance,rehab_stage}'
        and b->'drill_snapshot'->>'rehab_stage' in ('calm','restore')
        and b->'drill_snapshot'->>'laterality_applicability' = 'not_applicable');
  if coalesce(v_side, '') not in ('left','right','bilateral','unknown')
     or v_injury.episode_id <> (p_event->>'injury_episode_id')::uuid
     or v_injury.body_region is null or v_injury.body_region <> v_region
     or ((v_injury_side = 'unknown' or v_side = 'unknown') and not v_region_guidance)
     or not (v_injury_side = v_side or v_injury_side = 'bilateral' or v_side = 'bilateral') then
    raise exception 'exposure does not match injury episode, region and side' using errcode = '23514';
  end if;

  -- Unknown demand remains a valid observation but is never capacity evidence;
  -- RehabExposureEvent.has_unknown_demand keeps it non-qualifying downstream.
  if coalesce(v_demand->>'load','') not in ('unknown','minimal','low','moderate','high')
     or coalesce(v_demand->>'impact','') not in ('unknown','none','low','moderate','high')
     or coalesce(v_demand->>'velocity','') not in ('unknown','low','moderate','high')
     or jsonb_typeof(v_demand->'target_regions') <> 'array'
     or not (v_demand->'target_regions' ? v_region) then
    raise exception 'invalid exposure demand' using errcode = '23514';
  end if;

  if v_completed = '{}'::jsonb or not exists (
    select 1 from jsonb_each(v_completed) item where jsonb_typeof(item.value) <> 'null'
  ) then
    raise exception 'completed dose is empty' using errcode = '23514';
  end if;
  for v_key in select jsonb_object_keys(v_completed) loop
    if v_key not in ('sets','reps','duration_seconds','external_load_kg','distance_metres','hold_seconds','completed_fraction','stopped_early','completion_state') then
      raise exception 'invalid completed dose field' using errcode = '23514';
    end if;
  end loop;
  foreach v_key in array array['sets','reps','duration_seconds','external_load_kg','distance_metres','hold_seconds','completed_fraction'] loop
    if v_completed ? v_key and jsonb_typeof(v_completed->v_key) <> 'null'
       and (jsonb_typeof(v_completed->v_key) <> 'number' or (v_completed->>v_key)::numeric < 0) then
      raise exception 'invalid completed dose value' using errcode = '23514';
    end if;
  end loop;
  if v_completed ? 'completed_fraction' and jsonb_typeof(v_completed->'completed_fraction') <> 'null'
     and (v_completed->>'completed_fraction')::numeric > 1 then
    raise exception 'invalid completed fraction' using errcode = '23514';
  end if;
  if v_completed ? 'stopped_early' and jsonb_typeof(v_completed->'stopped_early') not in ('boolean','null') then
    raise exception 'invalid stopped_early' using errcode = '23514';
  end if;
  if v_completed ? 'completion_state'
     and coalesce(v_completion_state, '') not in ('performed_amount_unknown','partial_amount_unknown','quantified') then
    raise exception 'invalid completion state' using errcode = '23514';
  end if;
  if v_completion_state in ('performed_amount_unknown','partial_amount_unknown') and exists (
    select 1
    from jsonb_each(v_completed) item
    where item.key in ('sets','reps','duration_seconds','external_load_kg','distance_metres','hold_seconds','completed_fraction')
      and jsonb_typeof(item.value) <> 'null'
  ) then
    raise exception 'unquantified completion state contains a measured amount' using errcode = '23514';
  end if;
  if v_completion_state = 'quantified' and not exists (
    select 1
    from jsonb_each(v_completed) item
    where item.key in ('sets','reps','duration_seconds','external_load_kg','distance_metres','hold_seconds','completed_fraction')
      and jsonb_typeof(item.value) = 'number'
  ) then
    raise exception 'quantified completion state lacks a measured amount' using errcode = '23514';
  end if;

  foreach v_key in array array['pain_during','pain_immediate_after'] loop
    v_pain := v_response->v_key;
    if v_pain is not null and jsonb_typeof(v_pain) <> 'null'
       and not (jsonb_typeof(v_pain) = 'string' and v_pain #>> '{}' = 'not_sure')
       and not (jsonb_typeof(v_pain) = 'number' and (v_pain #>> '{}')::numeric between 0 and 10) then
      raise exception 'invalid injury-specific pain response' using errcode = '23514';
    end if;
  end loop;
  if coalesce(v_response->>'next_day_response','not_yet_known') not in ('better','same','worse','not_yet_known','not_sure') then
    raise exception 'invalid next-day response' using errcode = '23514';
  end if;
  if coalesce(v_response->>'during_response','not_reported') not in ('better','same','worse','not_sure','not_reported') then
    raise exception 'invalid during-work response' using errcode = '23514';
  end if;
  foreach v_key in array array['stopped_due_to_symptoms','worsening_reported'] loop
    if v_response ? v_key and jsonb_typeof(v_response->v_key) not in ('boolean','null') then
      raise exception 'invalid response flag' using errcode = '23514';
    end if;
  end loop;

  select * into v_existing from public.rehab_exposures where id = (p_event->>'exposure_id')::uuid;
  if found then
    if v_existing.athlete_id <> p_athlete_id or v_existing.event_json <> p_event then
      raise exception 'exposure id already used with different evidence' using errcode = '23505';
    end if;
    return v_existing;
  end if;

  insert into public.rehab_exposures (
    id, athlete_id, injury_id, injury_episode_id, drill_id, body_region, side,
    demand, prescribed_dose, completed_dose, response, event_json,
    evidence_source, occurred_at, recorded_at
  ) values (
    (p_event->>'exposure_id')::uuid, p_athlete_id, (p_event->>'injury_id')::uuid,
    (p_event->>'injury_episode_id')::uuid, p_event->>'drill_id', v_region, v_side,
    v_demand, nullif(p_event->'prescribed_dose', 'null'::jsonb), v_completed, v_response, p_event,
    p_event#>>'{provenance,source}', (p_event->>'occurred_at')::timestamptz,
    (p_event#>>'{provenance,recorded_at}')::timestamptz
  ) returning * into v_result;
  return v_result;
end;
$$;

revoke all on function public.record_rehab_exposure(uuid, jsonb) from public, anon, authenticated;
grant execute on function public.record_rehab_exposure(uuid, jsonb) to service_role;


-- Completed manual work only. No plan FK: regeneration/deletion cannot erase it.
create table if not exists public.external_training_sessions (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  client_request_id uuid not null,
  request_fingerprint text not null check (request_fingerprint ~ '^[a-f0-9]{64}$'),
  training_day date not null,
  activity text not null check (activity in ('sparring', 'boxing', 'roadwork', 'strength', 'conditioning', 'other')),
  duration_minutes integer not null check (duration_minutes between 1 and 600),
  effort text not null check (effort in ('easy', 'moderate', 'hard')),
  takeaway text not null default '' constraint external_training_sessions_takeaway_length check (char_length(takeaway) <= 280),
  sport text,
  session_type text,
  constraint external_training_sessions_discipline check (
      (sport is null and session_type is null)
      or ((coalesce(sport, ''), coalesce(session_type, ''), activity) in (
        ('boxing', '', 'boxing'), ('boxing', 'pads', 'boxing'), ('boxing', 'bag', 'boxing'), ('boxing', 'shadowboxing', 'boxing'), ('boxing', 'technical', 'boxing'), ('boxing', 'sparring', 'sparring'),
        ('muay_thai', '', 'other'), ('muay_thai', 'pads', 'other'), ('muay_thai', 'bag', 'other'), ('muay_thai', 'clinch', 'other'), ('muay_thai', 'technical', 'other'), ('muay_thai', 'sparring', 'sparring'),
        ('kickboxing', '', 'other'), ('kickboxing', 'pads', 'other'), ('kickboxing', 'bag', 'other'), ('kickboxing', 'technical', 'other'), ('kickboxing', 'sparring', 'sparring'),
        ('mma', '', 'other'), ('mma', 'striking', 'other'), ('mma', 'grappling', 'other'), ('mma', 'wrestling', 'other'), ('mma', 'integrated', 'other'), ('mma', 'sparring', 'sparring'),
        ('bjj', '', 'other'), ('bjj', 'drilling', 'other'), ('bjj', 'positional', 'other'), ('bjj', 'rolling', 'other'),
        ('wrestling', '', 'other'), ('wrestling', 'technical', 'other'), ('wrestling', 'live', 'other')))),
  -- Optional run detail; see migrations/20261102010200_add_external_training_run_distance.sql.
  distance_value numeric,
  distance_unit text,
  duration_seconds integer,
  constraint external_training_sessions_run_distance check (
    (distance_value is null and distance_unit is null)
    or (activity = 'roadwork' and distance_value > 0 and (
      (distance_unit = 'km' and distance_value <= 200)
      or (distance_unit = 'miles' and distance_value <= 125)))),
  constraint external_training_sessions_duration_seconds check (
    duration_seconds is null
    or (duration_seconds between 1 and 36000 and duration_minutes = ceil(duration_seconds / 60.0))),
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint external_training_sessions_request_key unique (athlete_id, client_request_id)
);
create index if not exists external_training_sessions_athlete_day_idx
  on public.external_training_sessions (athlete_id, training_day desc, created_at desc)
  where deleted_at is null;
drop trigger if exists set_external_training_sessions_updated_at on public.external_training_sessions;
create trigger set_external_training_sessions_updated_at
  before update on public.external_training_sessions
  for each row execute function public.set_updated_at();
alter table public.external_training_sessions enable row level security;
revoke all on public.external_training_sessions from anon, authenticated;
grant select on public.external_training_sessions to authenticated;
grant all on public.external_training_sessions to service_role;
drop policy if exists external_training_sessions_select_own on public.external_training_sessions;
create policy external_training_sessions_select_own on public.external_training_sessions
  for select to authenticated using (athlete_id = (select auth.uid()) and deleted_at is null);

-- Optional exercise results for manual sessions; never workload. See
-- migrations/20261019170000_add_manual_exercise_entries.sql.
-- The composite key keeps every link inside the athlete's own records.
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'external_training_sessions_id_athlete_key'
      and conrelid = 'public.external_training_sessions'::regclass) then
    alter table public.external_training_sessions
      add constraint external_training_sessions_id_athlete_key unique (id, athlete_id);
  end if;
end $$;

create table if not exists public.personal_exercises (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 100 and name = btrim(name)),
  -- Case and whitespace only: "back  SQUAT" and "Back squat" are one name;
  -- "Pause back squat" is a different movement.
  name_key text not null check (char_length(name_key) between 1 and 100),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint personal_exercises_name_key unique (athlete_id, name_key),
  constraint personal_exercises_id_athlete_key unique (id, athlete_id)
);

create table if not exists public.manual_exercise_entries (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  external_session_id uuid not null,
  client_entry_id uuid not null,
  training_day date not null,
  exercise_key text check (
    exercise_key is null
    or (char_length(exercise_key) between 1 and 120 and exercise_key ~ '^[a-z0-9]+(-[a-z0-9]+)*$')),
  personal_exercise_id uuid,
  display_name text not null check (char_length(display_name) between 1 and 120),
  measurement_format text not null
    check (measurement_format in ('strength', 'bodyweight', 'isometric', 'intervals', 'distance')),
  -- Only the values the athlete entered, in exercise_logs.actual's shape.
  actual jsonb not null default '{}'::jsonb
    check (jsonb_typeof(actual) = 'object' and octet_length(actual::text) <= 8192),
  notes text not null default '' check (char_length(notes) <= 500),
  revision integer not null default 1 check (revision >= 1),
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint manual_exercise_entries_request_key unique (athlete_id, client_entry_id),
  constraint manual_exercise_entries_one_identity check ((exercise_key is null) <> (personal_exercise_id is null)),
  constraint manual_exercise_entries_session_fkey foreign key (external_session_id, athlete_id)
    references public.external_training_sessions(id, athlete_id) on delete cascade,
  constraint manual_exercise_entries_personal_fkey foreign key (personal_exercise_id, athlete_id)
    references public.personal_exercises(id, athlete_id) on delete cascade
);

create index if not exists manual_exercise_entries_session_idx
  on public.manual_exercise_entries (athlete_id, external_session_id, created_at)
  where deleted_at is null;
create index if not exists manual_exercise_entries_key_idx
  on public.manual_exercise_entries (athlete_id, exercise_key, training_day desc, id desc)
  where deleted_at is null and exercise_key is not null;
create index if not exists manual_exercise_entries_personal_idx
  on public.manual_exercise_entries (athlete_id, personal_exercise_id, training_day desc, id desc)
  where deleted_at is null and personal_exercise_id is not null;

-- An entry belongs to one live parent session for life: its day comes from the
-- parent, it cannot move between sessions or owners, and it is never undeleted.
-- New entries need a live parent and fewer than 30 live siblings; a retry of an
-- existing entry passes through to ON CONFLICT DO NOTHING untouched.
create or replace function public.guard_manual_exercise_entry()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  parent record;
  live_count integer;
begin
  if tg_op = 'INSERT' then
    if exists (select 1 from public.manual_exercise_entries
               where athlete_id = new.athlete_id and client_entry_id = new.client_entry_id) then
      return new;
    end if;
    select training_day, deleted_at into parent from public.external_training_sessions
      where id = new.external_session_id and athlete_id = new.athlete_id for update;
    if not found or parent.deleted_at is not null then
      raise exception 'manual training session not found' using errcode = 'P0002';
    end if;
    select count(*) into live_count from public.manual_exercise_entries
      where athlete_id = new.athlete_id and external_session_id = new.external_session_id and deleted_at is null;
    if live_count >= 30 then
      raise exception 'manual exercise limit reached' using errcode = 'P0001';
    end if;
    new.training_day := parent.training_day;
    new.deleted_at := null;
    new.revision := 1;
    return new;
  end if;
  new.athlete_id := old.athlete_id;
  new.external_session_id := old.external_session_id;
  new.client_entry_id := old.client_entry_id;
  new.created_at := old.created_at;
  new.deleted_at := coalesce(old.deleted_at, new.deleted_at);
  -- Revisions only move forward, one edit at a time.
  if new.revision is distinct from old.revision and new.revision <> old.revision + 1 then
    new.revision := old.revision;
  end if;
  select training_day into new.training_day from public.external_training_sessions
    where id = old.external_session_id and athlete_id = old.athlete_id;
  return new;
end
$$;

drop trigger if exists guard_manual_exercise_entry on public.manual_exercise_entries;
create trigger guard_manual_exercise_entry
  before insert or update on public.manual_exercise_entries
  for each row execute function public.guard_manual_exercise_entry();

drop trigger if exists set_manual_exercise_entries_updated_at on public.manual_exercise_entries;
create trigger set_manual_exercise_entries_updated_at
  before update on public.manual_exercise_entries
  for each row execute function public.set_updated_at();

drop trigger if exists set_personal_exercises_updated_at on public.personal_exercises;
create trigger set_personal_exercises_updated_at
  before update on public.personal_exercises
  for each row execute function public.set_updated_at();

-- A parent's date change or soft delete reaches its entries atomically, so no
-- visible performance outlives its session or sits on a stale date.
create or replace function public.sync_manual_exercise_entries()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.training_day is distinct from old.training_day or new.deleted_at is distinct from old.deleted_at then
    update public.manual_exercise_entries
      set training_day = new.training_day,
          deleted_at = case when new.deleted_at is not null then coalesce(deleted_at, new.deleted_at) else deleted_at end
      where external_session_id = new.id and athlete_id = new.athlete_id;
  end if;
  return new;
end
$$;

drop trigger if exists sync_manual_exercise_entries on public.external_training_sessions;
create trigger sync_manual_exercise_entries
  after update of training_day, deleted_at on public.external_training_sessions
  for each row execute function public.sync_manual_exercise_entries();

revoke all on function public.guard_manual_exercise_entry() from public, anon, authenticated;
revoke all on function public.sync_manual_exercise_entries() from public, anon, authenticated;

alter table public.personal_exercises enable row level security;
revoke all on public.personal_exercises from anon, authenticated;
grant select on public.personal_exercises to authenticated;
grant all on public.personal_exercises to service_role;
drop policy if exists personal_exercises_select_own on public.personal_exercises;
create policy personal_exercises_select_own on public.personal_exercises
  for select to authenticated using (athlete_id = (select auth.uid()));

alter table public.manual_exercise_entries enable row level security;
revoke all on public.manual_exercise_entries from anon, authenticated;
grant select on public.manual_exercise_entries to authenticated;
grant all on public.manual_exercise_entries to service_role;
drop policy if exists manual_exercise_entries_select_own on public.manual_exercise_entries;
create policy manual_exercise_entries_select_own on public.manual_exercise_entries
  for select to authenticated using (athlete_id = (select auth.uid()) and deleted_at is null);

-- Saved self-directed drafts only. No generated-plan, Today or completion FK.
create table if not exists public.custom_training_plans (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  name text not null check (length(trim(name)) between 1 and 100),
  kind text not null default 'self_directed' check (kind = 'self_directed'),
  sessions jsonb not null default '[]'::jsonb check (jsonb_typeof(sessions) = 'array' and jsonb_array_length(sessions) <= 28),
  incomplete boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists custom_training_plans_athlete_updated_idx
  on public.custom_training_plans (athlete_id, updated_at desc);
drop trigger if exists set_custom_training_plans_updated_at on public.custom_training_plans;
create trigger set_custom_training_plans_updated_at
  before update on public.custom_training_plans
  for each row execute function public.set_updated_at();
alter table public.custom_training_plans enable row level security;
revoke all on public.custom_training_plans from anon, authenticated;
grant select on public.custom_training_plans to authenticated;
grant select, insert, update, delete on public.custom_training_plans to service_role;
drop policy if exists custom_training_plans_select_own on public.custom_training_plans;
create policy custom_training_plans_select_own on public.custom_training_plans
  for select to authenticated using (athlete_id = (select auth.uid()));

-- PR A of #2833: private immutable demos; no publication or review privileges.
create table public.private_video_assets (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  status text not null default 'reserved' check (status in ('reserved','queued','processing','verified','rejected','deleted')),
  size_bytes integer not null check (size_bytes between 1 and 20971520),
  content_type text not null check (content_type in ('video/mp4','video/quicktime','video/webm')),
  intake_path text not null unique,
  rendition_path text unique,
  duration_seconds double precision check (duration_seconds > 0 and duration_seconds <= 30),
  width integer check (width between 16 and 1920),
  height integer check (height between 16 and 1920),
  sha256 text,
  processor_version text,
  upload_expires_at timestamptz not null default now() + interval '5 minutes',
  processed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (status <> 'verified' or (rendition_path is not null and duration_seconds is not null
    and width is not null and height is not null and sha256 is not null and processor_version = 'isolated-v1')),
  unique (id, athlete_id)
);
create index private_video_assets_owner_created_idx on public.private_video_assets(athlete_id, created_at);
create trigger private_video_assets_updated before update on public.private_video_assets
  for each row execute function public.set_updated_at();

create table public.private_exercise_media (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  provider text not null check (provider in ('youtube','uploaded')),
  video_id text,
  asset_id uuid unique,
  start_s integer not null default 0 check (start_s >= 0),
  end_s integer not null check (end_s > start_s and end_s - start_s <= 30),
  channel_title text,
  orientation text check (orientation in ('portrait','landscape')),
  made_for_kids boolean,
  created_at timestamptz not null default now(),
  foreign key (asset_id, athlete_id) references public.private_video_assets(id, athlete_id) on delete cascade,
  check ((provider = 'youtube' and video_id ~ '^[A-Za-z0-9_-]{11}$' and asset_id is null and made_for_kids is not null)
    or (provider = 'uploaded' and video_id is null and asset_id is not null and start_s = 0))
);
create index private_exercise_media_owner_idx on public.private_exercise_media(athlete_id);
create table public.private_media_budget (
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  upload boolean not null,
  created_at timestamptz not null default now()
);
create index private_media_budget_owner_created_idx on public.private_media_budget(athlete_id, created_at);
create table public.private_media_worker_health (
  id text primary key check (id = 'isolated-v1'),
  healthy_at timestamptz not null
);
alter table public.private_video_assets enable row level security;
alter table public.private_exercise_media enable row level security;
alter table public.private_media_budget enable row level security;
alter table public.private_media_worker_health enable row level security;
revoke all on public.private_video_assets, public.private_exercise_media, public.private_media_budget,
  public.private_media_worker_health from anon, authenticated;
grant select on public.private_video_assets, public.private_exercise_media to authenticated;
grant all on public.private_video_assets, public.private_exercise_media, public.private_media_budget,
  public.private_media_worker_health to service_role;
create policy private_video_assets_owner on public.private_video_assets for select to authenticated
  using (athlete_id = (select auth.uid()));
create policy private_exercise_media_owner on public.private_exercise_media for select to authenticated
  using (athlete_id = (select auth.uid()));

-- All quota reservations serialize for an owner, including concurrent workers/API instances.
create function public.claim_private_media_budget(p_athlete_id uuid) returns boolean
language plpgsql security invoker set search_path = '' as $$
begin
  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text, 2833));
  delete from public.private_media_budget where athlete_id = p_athlete_id and created_at < now() - interval '1 day';
  if (select count(*) from public.private_media_budget where athlete_id = p_athlete_id and created_at > now() - interval '1 hour') >= 30
    or (select count(*) from public.private_exercise_media where athlete_id = p_athlete_id) >= 200 then
    return false;
  end if;
  insert into public.private_media_budget(athlete_id, upload) values (p_athlete_id, false);
  return true;
end $$;

create function public.reserve_private_video(p_athlete_id uuid, p_size_bytes integer, p_content_type text)
returns setof public.private_video_assets language plpgsql security invoker set search_path = '' as $$
declare asset_uuid uuid := gen_random_uuid();
begin
  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text, 2833));
  if not public.claim_private_media_budget(p_athlete_id) then return; end if;
  if (select count(*) from public.private_media_budget where athlete_id = p_athlete_id and upload and created_at > now() - interval '1 hour') >= 6
    or (select count(*) from public.private_media_budget where athlete_id = p_athlete_id and upload) >= 20
    or (select count(*) from public.private_video_assets where athlete_id = p_athlete_id and status not in ('deleted','rejected')) >= 50
    or (select coalesce(sum(size_bytes),0) from public.private_video_assets where athlete_id = p_athlete_id and status not in ('deleted','rejected')) + p_size_bytes > 1048576000 then
    return;
  end if;
  update public.private_media_budget set upload = true
    where ctid = (select ctid from public.private_media_budget where athlete_id = p_athlete_id order by created_at desc limit 1);
  return query insert into public.private_video_assets(id,athlete_id,size_bytes,content_type,intake_path)
    values (asset_uuid,p_athlete_id,p_size_bytes,p_content_type,p_athlete_id::text || '/' || asset_uuid::text || '/source') returning *;
end $$;
revoke all on function public.claim_private_media_budget(uuid), public.reserve_private_video(uuid,integer,text) from public,anon,authenticated;
grant execute on function public.claim_private_media_budget(uuid), public.reserve_private_video(uuid,integer,text) to service_role;

insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types) values
  ('private-demo-intake','private-demo-intake',false,20971520,array['video/mp4','video/quicktime','video/webm']),
  ('private-demo-ready','private-demo-ready',false,20971520,array['video/mp4'])
on conflict(id) do update set public=false,file_size_limit=excluded.file_size_limit,allowed_mime_types=excluded.allowed_mime_types;

-- Restrictive guards protect these buckets even if another feature has broad permissive policies.
create policy private_demo_no_client_read on storage.objects as restrictive for select to anon,authenticated
  using (bucket_id not in ('private-demo-intake','private-demo-ready'));
create policy private_demo_no_client_update on storage.objects as restrictive for update to anon,authenticated
  using (bucket_id not in ('private-demo-intake','private-demo-ready'))
  with check (bucket_id not in ('private-demo-intake','private-demo-ready'));
create policy private_demo_no_client_delete on storage.objects as restrictive for delete to anon,authenticated
  using (bucket_id not in ('private-demo-intake','private-demo-ready'));
create policy private_demo_intake_guard on storage.objects as restrictive for insert to anon,authenticated
  with check (bucket_id not in ('private-demo-intake','private-demo-ready') or
    (bucket_id = 'private-demo-intake' and exists (
      select 1 from public.private_video_assets a where a.intake_path = name
        and a.athlete_id = (select auth.uid()) and a.status = 'reserved' and a.upload_expires_at > now())));
create policy private_demo_intake_insert on storage.objects for insert to authenticated
  with check (bucket_id = 'private-demo-intake' and exists (
    select 1 from public.private_video_assets a where a.intake_path = name
      and a.athlete_id = (select auth.uid()) and a.status = 'reserved' and a.upload_expires_at > now()));

-- Worker claims only complete objects, once. No client can manipulate processing state.
create function public.claim_private_video() returns setof public.private_video_assets
language plpgsql security invoker set search_path = '' as $$
declare claimed uuid;
begin
  select a.id into claimed from public.private_video_assets a
    where a.status = 'reserved' and exists (select 1 from storage.objects o where o.bucket_id = 'private-demo-intake' and o.name = a.intake_path)
    order by a.created_at for update of a skip locked limit 1;
  return query update public.private_video_assets set status='processing' where id=claimed returning *;
end $$;
create function public.finish_private_video(p_asset_id uuid, p_duration double precision, p_width integer, p_height integer, p_sha256 text)
returns void language plpgsql security invoker set search_path = '' as $$
declare asset public.private_video_assets;
begin
  update public.private_video_assets set status='verified',duration_seconds=p_duration,width=p_width,height=p_height,
    rendition_path=athlete_id::text || '/' || id::text || '/demo.mp4',sha256=p_sha256,processor_version='isolated-v1',processed_at=now()
    where id=p_asset_id and status='processing' returning * into asset;
  if asset.id is null then raise exception 'asset not processing'; end if;
  insert into public.private_exercise_media(athlete_id,provider,asset_id,start_s,end_s,orientation)
    values(asset.athlete_id,'uploaded',asset.id,0,ceil(p_duration)::integer,
      case when p_height > p_width then 'portrait' else 'landscape' end);
end $$;
revoke all on function public.claim_private_video(), public.finish_private_video(uuid,double precision,integer,integer,text) from public,anon,authenticated;
grant execute on function public.claim_private_video(), public.finish_private_video(uuid,double precision,integer,integer,text) to service_role;

-- Unused reservations/rejected/stalled jobs: remove within 24h. Orphan renditions: 7 days.
-- Saved JSON references are checked here, never trusted from cleanup request parameters.
create function public.private_video_cleanup_candidates() returns setof public.private_video_assets
language plpgsql security invoker set search_path = '' as $$
declare candidate public.private_video_assets;
begin
  for candidate in select a.* from public.private_video_assets a where
      (a.status in ('reserved','queued','processing','rejected','deleted') and a.created_at < now() - interval '24 hours')
      or (a.status='verified' and a.created_at < now() - interval '7 days')
      order by a.created_at for update skip locked limit 100
  loop
    -- Recheck references after acquiring the row lock shared with the plan trigger.
    if not exists (
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

-- Account deletion cascades database rows; the worker also removes dangling storage objects.
create function public.private_demo_orphan_objects() returns table(bucket_id text, name text)
language sql security invoker set search_path = '' as $$
  select o.bucket_id,o.name from storage.objects o
    where o.bucket_id in ('private-demo-intake','private-demo-ready') and o.created_at < now() - interval '1 hour'
      and not exists (select 1 from public.private_video_assets a
        where o.name=a.intake_path or o.name=a.athlete_id::text || '/' || a.id::text || '/demo.mp4')
    limit 100;
$$;
revoke all on function public.private_demo_orphan_objects() from public,anon,authenticated;
grant execute on function public.private_demo_orphan_objects() to service_role;

-- Defense in depth and cleanup/save serialization. A direct privileged JSON write
-- cannot attach another athlete's demo or resurrect an asset already being removed.
create function public.validate_custom_plan_private_media() returns trigger
language plpgsql security invoker set search_path = '' as $$
declare identity text; media public.private_exercise_media; asset public.private_video_assets;
begin
  for identity in select e->>'media_id' from jsonb_array_elements(new.sessions) s
    cross join lateral jsonb_array_elements(s->'exercises') e where e->>'media_id' is not null
  loop
    select * into media from public.private_exercise_media where id=identity::uuid and athlete_id=new.athlete_id;
    if media.id is null then raise exception 'private media not owned'; end if;
    if media.provider='uploaded' then
      select * into asset from public.private_video_assets where id=media.asset_id and athlete_id=new.athlete_id for share;
      if asset.id is null or asset.status <> 'verified' then raise exception 'private media not verified'; end if;
    end if;
  end loop;
  return new;
end $$;
revoke all on function public.validate_custom_plan_private_media() from public,anon,authenticated;
create trigger validate_custom_plan_private_media before insert or update of sessions on public.custom_training_plans
  for each row execute function public.validate_custom_plan_private_media();

-- Idempotent finalisation and bounded retention for abandoned private previews.
create or replace function public.claim_private_video() returns setof public.private_video_assets
language plpgsql security invoker set search_path = '' as $$
declare claimed uuid;
begin
  select a.id into claimed from public.private_video_assets a
    where (a.status = 'reserved' or (a.status = 'processing' and a.updated_at < now() - interval '10 minutes'))
      and exists (select 1 from storage.objects o where o.bucket_id='private-demo-intake' and o.name=a.intake_path)
    order by a.created_at for update of a skip locked limit 1;
  return query update public.private_video_assets set status='processing' where id=claimed returning *;
end $$;

create or replace function public.finish_private_video(p_asset_id uuid, p_duration double precision, p_width integer, p_height integer, p_sha256 text)
returns void language plpgsql security invoker set search_path = '' as $$
declare asset public.private_video_assets;
begin
  select * into asset from public.private_video_assets where id=p_asset_id for update;
  if asset.status='verified' then
    if asset.duration_seconds is distinct from p_duration or asset.width is distinct from p_width
      or asset.height is distinct from p_height or asset.sha256 is distinct from p_sha256 then
      raise exception 'finalisation revision mismatch';
    end if;
    return;
  end if;
  if asset.id is null or asset.status <> 'processing' then raise exception 'asset not processing'; end if;
  update public.private_video_assets set status='verified',duration_seconds=p_duration,width=p_width,height=p_height,
    rendition_path=athlete_id::text || '/' || id::text || '/demo.mp4',sha256=p_sha256,processor_version='isolated-v1',processed_at=now()
    where id=p_asset_id;
  insert into public.private_exercise_media(athlete_id,provider,asset_id,start_s,end_s,orientation)
    values(asset.athlete_id,'uploaded',asset.id,0,ceil(p_duration)::integer,
      case when p_height > p_width then 'portrait' else 'landscape' end);
end $$;

create function public.cleanup_private_youtube_previews(p_athlete_id uuid default null) returns integer
language plpgsql security invoker set search_path = '' as $$
declare candidate public.private_exercise_media; removed integer := 0;
begin
  for candidate in select m.* from public.private_exercise_media m
    where m.provider='youtube' and m.created_at < now() - interval '24 hours'
      and (p_athlete_id is null or m.athlete_id=p_athlete_id)
      and not exists (select 1 from public.custom_training_plans p
        cross join lateral jsonb_array_elements(p.sessions) s
        cross join lateral jsonb_array_elements(s->'exercises') e
        where p.athlete_id=m.athlete_id and e->>'media_id'=m.id::text)
    order by m.created_at for update skip locked limit 200
  loop
    -- Lock matches the plan-save trigger. Recheck references after the lock so
    -- a concurrent save either retains the clip or fails without a dangling ID.
    if not exists (select 1 from public.custom_training_plans p
      cross join lateral jsonb_array_elements(p.sessions) s
      cross join lateral jsonb_array_elements(s->'exercises') e
      where p.athlete_id=candidate.athlete_id and e->>'media_id'=candidate.id::text) then
      delete from public.private_exercise_media where id=candidate.id;
      removed := removed + 1;
    end if;
  end loop;
  return removed;
end $$;
revoke all on function public.cleanup_private_youtube_previews(uuid) from public,anon,authenticated;
grant execute on function public.cleanup_private_youtube_previews(uuid) to service_role;

create or replace function public.claim_private_media_budget(p_athlete_id uuid) returns boolean
language plpgsql security invoker set search_path = '' as $$
begin
  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text, 2833));
  perform public.cleanup_private_youtube_previews(p_athlete_id);
  delete from public.private_media_budget where athlete_id=p_athlete_id and created_at < now()-interval '1 day';
  if (select count(*) from public.private_media_budget where athlete_id=p_athlete_id and created_at > now()-interval '1 hour') >= 30
    or (select count(*) from public.private_exercise_media where athlete_id=p_athlete_id) >= 200 then
    return false;
  end if;
  insert into public.private_media_budget(athlete_id,upload) values(p_athlete_id,false);
  return true;
end $$;

create or replace function public.validate_custom_plan_private_media() returns trigger
language plpgsql security invoker set search_path = '' as $$
declare identity text; media public.private_exercise_media; asset public.private_video_assets;
begin
  for identity in select e->>'media_id' from jsonb_array_elements(new.sessions) s
    cross join lateral jsonb_array_elements(s->'exercises') e where e->>'media_id' is not null
  loop
    select * into media from public.private_exercise_media where id=identity::uuid and athlete_id=new.athlete_id for share;
    if media.id is null then raise exception 'private media not owned'; end if;
    if media.provider='uploaded' then
      select * into asset from public.private_video_assets where id=media.asset_id and athlete_id=new.athlete_id for share;
      if asset.id is null or asset.status <> 'verified' then raise exception 'private media not verified'; end if;
    end if;
  end loop;
  return new;
end $$;

-- Raw deletion can lose its response too. Verified raw objects are safe to
-- remove later; the ready object is retained for every saved attachment.
create or replace function public.private_demo_orphan_objects() returns table(bucket_id text, name text)
language sql security invoker set search_path = '' as $$
  select o.bucket_id,o.name from storage.objects o
    where o.bucket_id in ('private-demo-intake','private-demo-ready') and o.created_at < now()-interval '1 hour'
      and (not exists (select 1 from public.private_video_assets a
        where o.name=a.intake_path or o.name=a.athlete_id::text || '/' || a.id::text || '/demo.mp4')
      or (o.bucket_id='private-demo-intake' and exists (select 1 from public.private_video_assets a
        where a.intake_path=o.name and a.status='verified')))
    limit 100;
$$;

-- Active retry leases must expire before stalled-job retention.
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
    if not exists (
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

-- Stage B: immutable community revisions, service-only transitions and audited publication.
create table public.community_exercises (
  id uuid primary key default gen_random_uuid(),
  creator_id uuid references public.profiles(id) on delete set null,
  current_revision_id uuid,
  published_revision_id uuid,
  attribution_consent boolean not null default false,
  consent_at timestamptz,
  withdrawn_at timestamptz,
  created_at timestamptz not null default now()
);
create index community_exercises_creator_idx on public.community_exercises(creator_id);
create table public.community_exercise_revisions (
  id uuid primary key default gen_random_uuid(),
  exercise_id uuid not null references public.community_exercises(id) on delete cascade,
  name text not null check(char_length(btrim(name)) between 1 and 100),
  equipment text check(char_length(equipment)<=120),
  notes text check(char_length(notes)<=2000),
  media_id uuid references public.private_exercise_media(id) on delete set null,
  status text not null default 'private' check(status in ('private','in_review','changes_requested','rejected','published','superseded')),
  submitted_at timestamptz,
  verified_at timestamptz,
  verified_by uuid references public.profiles(id) on delete set null,
  verified_media_id uuid,
  verified_proof jsonb,
  confirmed_at timestamptz,
  confirmed_by uuid references public.profiles(id) on delete set null,
  decision_reason text check(char_length(decision_reason)<=1000),
  created_at timestamptz not null default now(),
  unique(id,exercise_id)
);
alter table public.community_exercises add foreign key(current_revision_id,id) references public.community_exercise_revisions(id,exercise_id) deferrable initially deferred;
alter table public.community_exercises add foreign key(published_revision_id,id) references public.community_exercise_revisions(id,exercise_id) deferrable initially deferred;
create index community_revision_queue_idx on public.community_exercise_revisions(status,submitted_at);
create index community_revision_media_idx on public.community_exercise_revisions(media_id);
create table public.community_exercise_audit (
  id uuid primary key default gen_random_uuid(),
  exercise_id uuid not null references public.community_exercises(id) on delete cascade,
  revision_id uuid references public.community_exercise_revisions(id) on delete set null,
  actor_id uuid references public.profiles(id) on delete set null,
  action text not null,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index community_audit_exercise_idx on public.community_exercise_audit(exercise_id,created_at);
create table public.community_exercise_reports (
  id uuid primary key default gen_random_uuid(),
  revision_id uuid not null references public.community_exercise_revisions(id) on delete cascade,
  reporter_id uuid references public.profiles(id) on delete set null,
  reason text not null check(char_length(btrim(reason)) between 1 and 1000),
  status text not null default 'open' check(status in ('open','resolved')),
  created_at timestamptz not null default now(),
  unique(revision_id,reporter_id)
);
create index community_reports_revision_idx on public.community_exercise_reports(revision_id,status);
-- Index references used during account/media deletion and immutable-history retention.
create index community_revision_exercise_idx on public.community_exercise_revisions(exercise_id);
create index community_revision_verifier_idx on public.community_exercise_revisions(verified_by);
create index community_revision_confirmer_idx on public.community_exercise_revisions(confirmed_by);
create index community_exercises_current_idx on public.community_exercises(current_revision_id,id);
create index community_exercises_published_idx on public.community_exercises(published_revision_id,id);
create index community_audit_revision_idx on public.community_exercise_audit(revision_id);
create index community_audit_actor_idx on public.community_exercise_audit(actor_id,created_at);
create index community_reports_reporter_idx on public.community_exercise_reports(reporter_id,created_at);
alter table public.community_exercises enable row level security;
alter table public.community_exercise_revisions enable row level security;
alter table public.community_exercise_audit enable row level security;
alter table public.community_exercise_reports enable row level security;
revoke all on public.community_exercises,public.community_exercise_revisions,public.community_exercise_audit,public.community_exercise_reports from anon,authenticated;
grant all on public.community_exercises,public.community_exercise_revisions,public.community_exercise_audit,public.community_exercise_reports to service_role;

create function public.community_revision_available(p_revision_id uuid) returns boolean
language sql security invoker set search_path='' as $$
  select exists(select 1 from public.community_exercise_revisions r
    left join public.private_exercise_media m on m.id=r.media_id left join public.private_video_assets a on a.id=m.asset_id
    where r.id=p_revision_id and r.status='published' and r.verified_at is not null
      and r.verified_media_id is not distinct from r.media_id
      and (r.media_id is null or m.provider='youtube' or (m.provider='uploaded' and a.status='verified' and a.sha256=r.verified_proof->>'sha256')));
$$;
revoke all on function public.community_revision_available(uuid) from public,anon,authenticated;
grant execute on function public.community_revision_available(uuid) to service_role;

-- Always resolve attribution from the live consenting profile; never request data.
create function public.community_revision_payload(p_revision_id uuid) returns jsonb
language sql security invoker set search_path='' as $$
  select jsonb_build_object('id',e.id,'revision_id',r.id,'name',r.name,'equipment',r.equipment,'notes',r.notes,
    'media_id',r.media_id,'status',case when r.status='published' and e.published_revision_id is distinct from r.id then 'private' else r.status end,'submitted_at',r.submitted_at,'created_at',r.created_at,
    'reason',r.decision_reason,'verified',r.verified_at is not null,'confirmed',r.confirmed_at is not null,
    'has_publication',e.published_revision_id is not null,
    'published',coalesce(e.published_revision_id=r.id and e.attribution_consent and p.username is not null and public.community_revision_available(r.id),false),
    'contributor',case when e.attribution_consent and p.username is not null then
      jsonb_build_object('username',p.username,'avatar_url',p.avatar_url) else null end,
    'raw_media',case when m.id is null then null else to_jsonb(m)||jsonb_build_object('asset',to_jsonb(a)) end)
  from public.community_exercise_revisions r join public.community_exercises e on e.id=r.exercise_id
  left join public.profiles p on p.id=e.creator_id
  left join public.private_exercise_media m on m.id=r.media_id
  left join public.private_video_assets a on a.id=m.asset_id
  where r.id=p_revision_id;
$$;

create function public.community_detail(p_actor_id uuid,p_revision_id uuid,p_admin boolean default false) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare e public.community_exercises; r public.community_exercise_revisions;
begin
  if not exists(select 1 from public.profiles where id=p_actor_id and access_status='approved') then raise exception 'community forbidden' using errcode='42501'; end if;
  select * into r from public.community_exercise_revisions where id=p_revision_id;
  select * into e from public.community_exercises where id=r.exercise_id;
  if r.id is null then raise exception 'community not found' using errcode='P0002'; end if;
  if p_admin then
    if not exists(select 1 from public.profiles where id=p_actor_id and role='admin') then raise exception 'community forbidden' using errcode='42501'; end if;
  elsif e.creator_id is distinct from p_actor_id and not (
    (e.published_revision_id=r.id and public.community_revision_available(r.id) and e.attribution_consent and exists(select 1 from public.profiles where id=e.creator_id and username is not null))
    or (r.status='published' and exists(select 1 from public.custom_training_plans p
      cross join lateral jsonb_array_elements(p.sessions) s cross join lateral jsonb_array_elements(s->'exercises') x
      where p.athlete_id=p_actor_id and x->>'community_revision_id'=r.id::text))) then
    raise exception 'community not found' using errcode='P0002';
  end if;
  return public.community_revision_payload(r.id) || case when p_admin then jsonb_build_object(
    'audit',(select coalesce(jsonb_agg(to_jsonb(x) order by x.created_at),'[]') from public.community_exercise_audit x where x.exercise_id=e.id),
    'reports',(select coalesce(jsonb_agg(jsonb_build_object('id',x.id,'reason',x.reason,'status',x.status,'created_at',x.created_at) order by x.created_at),'[]') from public.community_exercise_reports x join public.community_exercise_revisions z on z.id=x.revision_id where z.exercise_id=e.id)) else '{}'::jsonb end;
end $$;

create function public.community_list(p_actor_id uuid,p_scope text,p_query text default '',p_offset integer default 0,p_limit integer default 30) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare ids uuid[]; total integer;
begin
  if not exists(select 1 from public.profiles where id=p_actor_id and access_status='approved') then raise exception 'community forbidden' using errcode='42501'; end if;
  if p_scope not in ('public','mine','review','history') then raise exception 'invalid community scope'; end if;
  if p_scope in ('review','history') and not exists(select 1 from public.profiles where id=p_actor_id and role='admin') then raise exception 'community forbidden' using errcode='42501'; end if;
  with candidates as (
    select r.id,r.created_at from public.community_exercises e
    join public.community_exercise_revisions r on r.id=case when p_scope='public' then e.published_revision_id else e.current_revision_id end
    left join public.profiles p on p.id=e.creator_id
    where (p_scope='mine' and e.creator_id=p_actor_id
      or p_scope='review' and (r.status='in_review' or exists(select 1 from public.community_exercise_reports x join public.community_exercise_revisions z on z.id=x.revision_id where z.exercise_id=e.id and x.status='open'))
      or p_scope='history' and (r.status in ('changes_requested','rejected','published') or (r.status='private' and r.submitted_at is not null))
        and not exists(select 1 from public.community_exercise_reports x join public.community_exercise_revisions z on z.id=x.revision_id where z.exercise_id=e.id and x.status='open')
      or p_scope='public' and e.attribution_consent and p.username is not null and public.community_revision_available(r.id))
      and (p_query='' or strpos(lower(r.name||' '||coalesce(r.equipment,'')),lower(left(p_query,100)))>0)
  ) select count(*)::integer, (select array_agg(id order by created_at desc,id) from (select * from candidates order by created_at desc,id offset greatest(p_offset,0) limit least(greatest(p_limit,1),50)) page)
    into total,ids from candidates;
  return jsonb_build_object('entries',(select coalesce(jsonb_agg(public.community_revision_payload(id) order by ord),'[]') from unnest(ids) with ordinality t(id,ord)),
    'total',total,'next_offset',case when greatest(p_offset,0)+least(greatest(p_limit,1),50)<total then greatest(p_offset,0)+least(greatest(p_limit,1),50) else null end);
end $$;

create function public.community_command(p_actor_id uuid,p_action text,p_payload jsonb) returns uuid
language plpgsql security invoker set search_path='' as $$
declare e public.community_exercises; r public.community_exercise_revisions; next_id uuid; actor_admin boolean; m public.private_exercise_media; a public.private_video_assets; proof jsonb;
begin
  if not exists(select 1 from public.profiles where id=p_actor_id and access_status='approved') then raise exception 'community forbidden' using errcode='42501'; end if;
  select role='admin' into actor_admin from public.profiles where id=p_actor_id;
  perform pg_advisory_xact_lock(hashtextextended(p_actor_id::text,2834));
  if not actor_admin and p_action<>'withdraw' and (select count(*) from public.community_exercise_audit where actor_id=p_actor_id and created_at>now()-interval '1 hour')>=30 then raise exception 'community quota'; end if;
  if p_action='draft' then
    if p_payload->>'exercise_id' is null then
      if (select count(*) from public.community_exercises where creator_id=p_actor_id)>=100 then raise exception 'community quota' using errcode='P0001'; end if;
      insert into public.community_exercises(creator_id) values(p_actor_id) returning * into e;
    else
      select * into e from public.community_exercises where id=(p_payload->>'exercise_id')::uuid for update;
      if e.id is null or e.creator_id is distinct from p_actor_id then raise exception 'community not found' using errcode='P0002'; end if;
      if (p_payload->>'expected_revision_id')::uuid is distinct from e.current_revision_id then raise exception 'community revision changed' using errcode='40001'; end if;
    end if;
    if p_payload->>'media_id' is not null then
      select * into m from public.private_exercise_media where id=(p_payload->>'media_id')::uuid and athlete_id=p_actor_id for share;
      if m.id is null then raise exception 'community media not owned' using errcode='42501'; end if;
      if m.provider='uploaded' then
        select * into a from public.private_video_assets where id=m.asset_id for share;
        if a.status<>'verified' then raise exception 'community media not verified'; end if;
      end if;
    end if;
    -- Identical retries do not manufacture revisions or reset decisions.
    select * into r from public.community_exercise_revisions where id=e.current_revision_id;
    if r.id is not null and r.name=btrim(p_payload->>'name') and r.equipment is not distinct from p_payload->>'equipment'
      and r.notes is not distinct from p_payload->>'notes' and r.media_id is not distinct from (p_payload->>'media_id')::uuid then return r.id; end if;
    if (select count(*) from public.community_exercise_audit where actor_id=p_actor_id and action='draft' and created_at>now()-interval '1 hour')>=30 then raise exception 'community quota'; end if;
    insert into public.community_exercise_revisions(exercise_id,name,equipment,notes,media_id)
      values(e.id,btrim(p_payload->>'name'),p_payload->>'equipment',p_payload->>'notes',(p_payload->>'media_id')::uuid) returning id into next_id;
    if r.status='in_review' then update public.community_exercise_revisions set status='superseded' where id=r.id; end if;
    update public.community_exercises set current_revision_id=next_id where id=e.id;
  else
    select * into r from public.community_exercise_revisions where id=(p_payload->>'revision_id')::uuid;
    select * into e from public.community_exercises where id=r.exercise_id for update;
    -- Re-read after the exercise lock; concurrent admin decisions cannot race.
    select * into r from public.community_exercise_revisions where id=(p_payload->>'revision_id')::uuid for update;
    if e.id is null then raise exception 'community not found' using errcode='P0002'; end if;
    next_id:=r.id;
    if p_action in ('edit','verify','confirm','approve','request_changes','reject','remove') then
      if not actor_admin then raise exception 'community forbidden' using errcode='42501'; end if;
      if p_action<>'remove' and e.current_revision_id<>r.id then raise exception 'community revision changed' using errcode='40001'; end if;
    elsif p_action in ('submit','withdraw') then
      if e.creator_id is distinct from p_actor_id then raise exception 'community forbidden' using errcode='42501'; end if;
      if p_action='submit' and e.current_revision_id<>r.id then raise exception 'community revision changed' using errcode='40001'; end if;
    elsif p_action<>'report' then raise exception 'invalid community action'; end if;
    if p_action='approve' and r.status='published' and e.published_revision_id=r.id and e.attribution_consent
      and coalesce((p_payload->>'final_confirmation')::boolean,false) then return r.id; end if;
    if p_action='withdraw' and not e.attribution_consent and e.published_revision_id is null then return e.current_revision_id; end if;
    if p_action='submit' then
      if not coalesce((p_payload->>'share_rights')::boolean,false) or not coalesce((p_payload->>'attribution_consent')::boolean,false)
        or not exists(select 1 from public.profiles where id=p_actor_id and username is not null) then raise exception 'community consent required'; end if;
      if r.status='in_review' then return r.id; end if;
      if r.media_id is not null then
        select * into m from public.private_exercise_media where id=r.media_id for share;
        if m.id is null then raise exception 'community media missing'; end if;
        if m.provider='uploaded' then select * into a from public.private_video_assets where id=m.asset_id for share; if a.status<>'verified' then raise exception 'community media not verified'; end if; end if;
      end if;
      if r.status not in ('private','changes_requested','rejected') then raise exception 'invalid review transition'; end if;
      if (select count(*) from public.community_exercise_audit where actor_id=p_actor_id and action='submit' and created_at>now()-interval '1 day')>=10 then raise exception 'community quota'; end if;
      update public.community_exercises set attribution_consent=true,consent_at=now(),withdrawn_at=null where id=e.id;
      update public.community_exercise_revisions set status='in_review',submitted_at=now(),verified_at=null,verified_by=null,verified_media_id=null,verified_proof=null,confirmed_at=null,confirmed_by=null,decision_reason=null where id=r.id;
    elsif p_action='withdraw' then
      update public.community_exercises set attribution_consent=false,published_revision_id=null,withdrawn_at=now() where id=e.id;
      update public.community_exercise_revisions set status='private',verified_at=null,confirmed_at=null where id=e.current_revision_id and status<>'published';
    elsif p_action='report' then
      if e.published_revision_id is distinct from r.id or not e.attribution_consent or e.creator_id is null then raise exception 'community not found' using errcode='P0002'; end if;
      if (select count(*) from public.community_exercise_reports where reporter_id=p_actor_id and created_at>now()-interval '1 day')>=5 then raise exception 'community quota'; end if;
      insert into public.community_exercise_reports(revision_id,reporter_id,reason) values(r.id,p_actor_id,btrim(p_payload->>'reason')) on conflict(revision_id,reporter_id) do nothing;
    elsif p_action='remove' then
      if nullif(btrim(p_payload->>'reason'),'') is null then raise exception 'decision reason required'; end if;
      update public.community_exercises set published_revision_id=null where id=e.id;
      update public.community_exercise_reports set status='resolved' where revision_id in(select id from public.community_exercise_revisions where exercise_id=e.id);
    else
      if r.status<>'in_review' and not(p_action='edit' and r.status='published') then raise exception 'invalid review transition'; end if;
      if p_action='edit' then
        if p_payload->>'media_id' is not null then
          select * into m from public.private_exercise_media where id=(p_payload->>'media_id')::uuid and athlete_id in(e.creator_id,p_actor_id) for share;
          if m.id is null then raise exception 'community media not owned' using errcode='42501'; end if;
          if m.provider='uploaded' then select * into a from public.private_video_assets where id=m.asset_id for share; if a.status<>'verified' then raise exception 'community media not verified'; end if; end if;
        end if;
        insert into public.community_exercise_revisions(exercise_id,name,equipment,notes,media_id,status,submitted_at)
          values(e.id,btrim(p_payload->>'name'),p_payload->>'equipment',p_payload->>'notes',(p_payload->>'media_id')::uuid,'in_review',r.submitted_at) returning id into next_id;
        update public.community_exercise_revisions set status='superseded' where id=r.id and status='in_review';
        update public.community_exercises set current_revision_id=next_id where id=e.id;
      elsif p_action='verify' then
        if (p_payload->>'verified_media_id')::uuid is distinct from r.media_id then raise exception 'community revision changed' using errcode='40001'; end if;
        if r.media_id is not null then
          select * into m from public.private_exercise_media where id=r.media_id for share;
          if m.id is null then raise exception 'community media missing'; end if;
          if m.provider='uploaded' then
            select * into a from public.private_video_assets where id=m.asset_id for share;
            if a.status<>'verified' or a.sha256 is distinct from p_payload->>'sha256' then raise exception 'community media not verified'; end if;
          else
            if p_payload->>'youtube_id' is distinct from m.video_id or not coalesce((p_payload->>'youtube_verified')::boolean,false) then raise exception 'community media not verified'; end if;
          end if;
        end if;
        proof:=jsonb_build_object('media_id',r.media_id,'sha256',a.sha256,'youtube_id',m.video_id);
        update public.community_exercise_revisions set verified_at=now(),verified_by=p_actor_id,verified_media_id=r.media_id,verified_proof=proof,confirmed_at=null,confirmed_by=null where id=r.id;
      elsif p_action='confirm' then
        if r.verified_at is null or r.verified_media_id is distinct from r.media_id or not coalesce((p_payload->>'final_confirmation')::boolean,false) then raise exception 'final revision must be verified and confirmed'; end if;
        update public.community_exercise_revisions set confirmed_at=now(),confirmed_by=p_actor_id where id=r.id;
      elsif p_action='approve' then
        if r.verified_at is null or r.verified_at<now()-interval '24 hours' or r.confirmed_at is null or r.verified_media_id is distinct from r.media_id
          or not coalesce((p_payload->>'final_confirmation')::boolean,false) then raise exception 'final revision must be verified and confirmed'; end if;
        if not e.attribution_consent or not exists(select 1 from public.profiles where id=e.creator_id and username is not null) then raise exception 'community consent required'; end if;
        if r.media_id is not null then
          select * into m from public.private_exercise_media where id=r.media_id for share;
          if m.id is null then raise exception 'community media missing'; end if;
          if m.provider='uploaded' then
            select * into a from public.private_video_assets where id=m.asset_id for share;
            if a.status<>'verified' or a.sha256 is distinct from r.verified_proof->>'sha256' then raise exception 'community media not verified'; end if;
          end if;
        end if;
        update public.community_exercise_revisions set status='published' where id=r.id;
        update public.community_exercises set published_revision_id=r.id where id=e.id;
      elsif p_action in ('reject','request_changes') then
        if nullif(btrim(p_payload->>'reason'),'') is null then raise exception 'decision reason required'; end if;
        update public.community_exercise_revisions set status=case when p_action='reject' then 'rejected' else 'changes_requested' end,
          decision_reason=btrim(p_payload->>'reason'),confirmed_at=null where id=r.id;
      end if;
    end if;
  end if;
  insert into public.community_exercise_audit(exercise_id,revision_id,actor_id,action,details)
    values(e.id,next_id,p_actor_id,p_action,jsonb_build_object('previous_revision_id',r.id,'reason',p_payload->>'reason','share_rights',p_payload->'share_rights',
      'attribution_consent',p_payload->'attribution_consent','before',case when p_action='edit' then to_jsonb(r) else null end,
      'after',case when p_action in('draft','edit') then public.community_revision_payload(next_id)-'raw_media'-'contributor' else null end));
  return next_id;
end $$;

revoke all on function public.community_revision_payload(uuid),public.community_detail(uuid,uuid,boolean),public.community_list(uuid,text,text,integer,integer),public.community_command(uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.community_revision_payload(uuid),public.community_detail(uuid,uuid,boolean),public.community_list(uuid,text,text,integer,integer),public.community_command(uuid,text,jsonb) to service_role;

-- Retention must treat an active review/publication and saved community snapshot
-- as a durable reference. Stage A still owns all byte processing and deletion.
create function public.community_media_retained(p_media_id uuid) returns boolean
language sql security invoker set search_path='' as $$
  select exists(select 1 from public.community_exercise_revisions r join public.community_exercises e on e.id=r.exercise_id
    where r.media_id=p_media_id and ((e.current_revision_id=r.id and r.status='in_review')
      or (e.published_revision_id=r.id and e.attribution_consent and e.creator_id is not null)
      or exists(select 1 from public.custom_training_plans p cross join lateral jsonb_array_elements(p.sessions) s
        cross join lateral jsonb_array_elements(s->'exercises') x where x->>'community_revision_id'=r.id::text)));
$$;
revoke all on function public.community_media_retained(uuid) from public,anon,authenticated;
grant execute on function public.community_media_retained(uuid) to service_role;

create or replace function public.cleanup_private_youtube_previews(p_athlete_id uuid default null) returns integer
language plpgsql security invoker set search_path = '' as $$
declare candidate public.private_exercise_media; removed integer := 0;
begin
  for candidate in select m.* from public.private_exercise_media m
    where m.provider='youtube' and m.created_at < now() - interval '24 hours'
      and (p_athlete_id is null or m.athlete_id=p_athlete_id) and not public.community_media_retained(m.id)
      and not exists (select 1 from public.custom_training_plans p
        cross join lateral jsonb_array_elements(p.sessions) s
        cross join lateral jsonb_array_elements(s->'exercises') e
        where p.athlete_id=m.athlete_id and e->>'media_id'=m.id::text)
    order by m.created_at for update skip locked limit 200
  loop
    -- Lock matches the plan-save trigger. Recheck references after the lock so
    -- a concurrent save either retains the clip or fails without a dangling ID.
    if not public.community_media_retained(candidate.id) and not exists (select 1 from public.custom_training_plans p
      cross join lateral jsonb_array_elements(p.sessions) s
      cross join lateral jsonb_array_elements(s->'exercises') e
      where p.athlete_id=candidate.athlete_id and e->>'media_id'=candidate.id::text) then
      delete from public.private_exercise_media where id=candidate.id;
      removed := removed + 1;
    end if;
  end loop;
  return removed;
end $$;

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
    if not exists (select 1 from public.private_exercise_media m where m.asset_id=candidate.id and public.community_media_retained(m.id)) and not exists (
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

create or replace function public.validate_custom_plan_private_media() returns trigger
language plpgsql security invoker set search_path = '' as $$
declare identity text; owned_id uuid; reference uuid; contribution public.community_exercises; revision public.community_exercise_revisions; media public.private_exercise_media; asset public.private_video_assets;
begin
  for identity in select e->>'media_id' from jsonb_array_elements(new.sessions) s
    cross join lateral jsonb_array_elements(s->'exercises') e where e->>'media_id' is not null
  loop
    select * into media from public.private_exercise_media where id=identity::uuid and athlete_id=new.athlete_id for share;
    if media.id is null then raise exception 'private media not owned'; end if;
    if media.provider='uploaded' then
      select * into asset from public.private_video_assets where id=media.asset_id and athlete_id=new.athlete_id for share;
      if asset.id is null or asset.status <> 'verified' then raise exception 'private media not verified'; end if;
    end if;
  end loop;
  for owned_id in select (x->>'community_id')::uuid from jsonb_array_elements(new.sessions) s
    cross join lateral jsonb_array_elements(s->'exercises') x where x->>'community_id' is not null
  loop
    if not exists(select 1 from public.community_exercises where id=owned_id and creator_id=new.athlete_id) then raise exception 'community exercise not owned'; end if;
  end loop;
  for reference in select (x->>'community_revision_id')::uuid from jsonb_array_elements(new.sessions) s
    cross join lateral jsonb_array_elements(s->'exercises') x where x->>'community_revision_id' is not null
  loop
    select * into revision from public.community_exercise_revisions where id=reference;
    select * into contribution from public.community_exercises where id=revision.exercise_id for share;
    if revision.id is null then raise exception 'community revision not found'; end if;
    if not coalesce((contribution.published_revision_id=reference and contribution.attribution_consent and contribution.creator_id is not null and public.community_revision_available(reference) and exists(select 1 from public.profiles where id=contribution.creator_id and username is not null)),false)
      and not (tg_op='UPDATE' and exists(select 1 from jsonb_array_elements(old.sessions) s
        cross join lateral jsonb_array_elements(s->'exercises') x where x->>'community_revision_id'=reference::text)) then
      raise exception 'community revision not published';
    end if;
  end loop;
  return new;
end $$;


create function public.community_owned_revision(p_actor_id uuid,p_exercise_id uuid) returns uuid
language plpgsql security invoker set search_path='' as $$
declare identity uuid;
begin
  select current_revision_id into identity from public.community_exercises where id=p_exercise_id and creator_id=p_actor_id;
  if identity is null then raise exception 'community not found' using errcode='P0002'; end if;
  return identity;
end $$;
revoke all on function public.community_owned_revision(uuid,uuid) from public,anon,authenticated;
grant execute on function public.community_owned_revision(uuid,uuid) to service_role;

create function public.community_creator_deleted() returns trigger
language plpgsql security invoker set search_path='' as $$
begin
  insert into public.community_exercise_audit(exercise_id,revision_id,action)
    select id,current_revision_id,'creator_deleted' from public.community_exercises where creator_id=old.id;
  update public.community_exercises set attribution_consent=false,published_revision_id=null,withdrawn_at=now() where creator_id=old.id;
  return old;
end $$;
revoke all on function public.community_creator_deleted() from public,anon,authenticated;
create trigger community_creator_deleted before delete on public.profiles for each row execute function public.community_creator_deleted();

-- Corrective migration; the applied Stage B migration remains immutable.
alter table public.community_exercise_revisions add column changes_requested_at timestamptz;
-- Existing correction requests receive the full grace period from this rollout.
update public.community_exercise_revisions set changes_requested_at=now() where status='changes_requested';
create function public.community_correction_window() returns trigger
language plpgsql security invoker set search_path='' as $$
begin
  if TG_OP='UPDATE' then
    if new.status='changes_requested' and old.status is distinct from new.status then new.changes_requested_at:=now();
    elsif new.status='private' and not exists(select 1 from public.community_exercises where id=new.exercise_id and attribution_consent) then new.changes_requested_at:=null; end if;
  elsif new.status='private' then
    select r.changes_requested_at into new.changes_requested_at from public.community_exercises e
      join public.community_exercise_revisions r on r.id=e.current_revision_id
      where e.id=new.exercise_id and r.status in('changes_requested','private');
  end if;
  return new;
end $$;
revoke all on function public.community_correction_window() from public,anon,authenticated;
grant execute on function public.community_correction_window() to service_role;
create trigger community_correction_window before insert or update on public.community_exercise_revisions
for each row execute function public.community_correction_window();

create or replace function public.community_media_retained(p_media_id uuid) returns boolean
language sql security invoker set search_path='' as $$
  select exists(select 1 from public.community_exercise_revisions r join public.community_exercises e on e.id=r.exercise_id
    where r.media_id=p_media_id and ((e.current_revision_id=r.id and (r.status='in_review' or (r.status in('changes_requested','private') and r.changes_requested_at>now()-interval '30 days')))
      or (e.published_revision_id=r.id and e.attribution_consent and e.creator_id is not null)
      or exists(select 1 from public.custom_training_plans p cross join lateral jsonb_array_elements(p.sessions) s
        cross join lateral jsonb_array_elements(s->'exercises') x where x->>'community_revision_id'=r.id::text)));
$$;
revoke all on function public.community_media_retained(uuid) from public,anon,authenticated;
grant execute on function public.community_media_retained(uuid) to service_role;


-- Resolve one report without changing publication, consent, content or verification.
create function public.community_dismiss_report(p_actor_id uuid,p_revision_id uuid,p_report_id uuid,p_reason text) returns uuid
language plpgsql security invoker set search_path='' as $$
declare e public.community_exercises; report public.community_exercise_reports;
begin
  if not exists(select 1 from public.profiles where id=p_actor_id and role='admin' and access_status='approved') then raise exception 'community forbidden' using errcode='42501'; end if;
  if nullif(btrim(p_reason),'') is null or char_length(p_reason)>1000 then raise exception 'decision reason required'; end if;
  select x.* into e from public.community_exercises x join public.community_exercise_revisions r on r.exercise_id=x.id where r.id=p_revision_id for update of x;
  select x.* into report from public.community_exercise_reports x join public.community_exercise_revisions r on r.id=x.revision_id where x.id=p_report_id and r.exercise_id=e.id for update of x;
  if e.id is null or report.id is null then raise exception 'community not found' using errcode='P0002'; end if;
  if report.status='resolved' then return e.current_revision_id; end if;
  update public.community_exercise_reports set status='resolved' where id=report.id;
  insert into public.community_exercise_audit(exercise_id,revision_id,actor_id,action,details)
    values(e.id,report.revision_id,p_actor_id,'dismiss_report',jsonb_build_object('report_id',report.id,'reason',btrim(p_reason)));
  return e.current_revision_id;
end $$;
revoke all on function public.community_dismiss_report(uuid,uuid,uuid,text) from public,anon,authenticated;
grant execute on function public.community_dismiss_report(uuid,uuid,uuid,text) to service_role;

-- Tracking-first XP and approved YouTube contributions. Backend-only earning.
-- Apply after the manual exercise migration and before the new backend.
-- Existing awards, active-plan guards, RLS and historical totals are retained.

alter table public.xp_awards
  drop constraint if exists xp_awards_action_check;

alter table public.xp_awards
  add constraint xp_awards_action_check check (action in (
    'daily_login',
    'training_logged',
    'planned_session_completed',
    'recommended_fighter_content_watched',
    'full_training_week_completed',
    'profile_completed',
    'first_intake_completed',
    'first_plan_ready',
    'first_checkin_completed',
    'readiness_checkin_completed',
    'injury_update_completed',
    'stop_decision_followed',
    'feedback_submitted',
    'feedback_with_comment',
    'first_plan_completed',
    'phase_completed',
    'camp_completed',
    'manual_training_logged',
    'first_session_logged',
    'first_exercise_logged',
    'training_days_10',
    'training_days_25',
    'training_days_50',
    'training_days_100',
    'exercise_contribution_approved'
  ));

alter table public.xp_awards
  drop constraint if exists xp_awards_amount_check;

alter table public.xp_awards
  add constraint xp_awards_amount_check check (
    (action = 'daily_login' and amount = 10)
    or (action = 'training_logged' and amount = 25)
    or (action = 'planned_session_completed' and amount = 50)
    or (action = 'recommended_fighter_content_watched' and amount = 10)
    or (action = 'full_training_week_completed' and amount = 100)
    or (action = 'profile_completed' and amount = 25)
    or (action = 'first_intake_completed' and amount = 50)
    or (action = 'first_plan_ready' and amount = 100)
    or (action = 'first_checkin_completed' and amount = 25)
    or (action = 'readiness_checkin_completed' and amount = 10)
    or (action = 'injury_update_completed' and amount = 10)
    or (action = 'stop_decision_followed' and amount = 15)
    or (action = 'feedback_submitted' and amount = 1)
    or (action = 'feedback_with_comment' and amount = 3)
    or (action = 'first_plan_completed' and amount = 250)
    or (action = 'phase_completed' and amount = 200)
    or (action = 'camp_completed' and amount = 500)
    or (action = 'manual_training_logged' and amount = 25)
    or (action = 'first_session_logged' and amount = 50)
    or (action = 'first_exercise_logged' and amount = 25)
    or (action = 'training_days_10' and amount = 100)
    or (action = 'training_days_25' and amount = 150)
    or (action = 'training_days_50' and amount = 250)
    or (action = 'training_days_100' and amount = 500)
    or (action = 'exercise_contribution_approved' and amount = 100)
  );



-- Derive all evidence from owned, directly reported occurrences; never day credit.
create or replace function public.xp_recorded_training_days(p_athlete_id uuid)
returns table(training_day date) language sql stable security invoker
set search_path = pg_catalog, public as $$
  with today as (
    select ((now() at time zone coalesce(nullif(athlete_timezone,''),'UTC'))
      - interval '3 hours')::date as day from public.profiles where id=p_athlete_id
  )
  select s.training_day from public.external_training_sessions s, today
  where s.athlete_id=p_athlete_id and s.deleted_at is null and s.training_day<=today.day
  union
  select s.training_day from public.session_completions s, today
  where s.athlete_id=p_athlete_id and s.completion_origin='reported'
    and s.status in ('done','modified') and s.training_day<=today.day;
$$;

create or replace function public.xp_has_measurements(p_values jsonb)
returns boolean language sql immutable security invoker
set search_path = pg_catalog, public as $$
  select jsonb_typeof(p_values)='object' and not exists (
    select 1 from jsonb_each(p_values) item
    where (item.key in ('sets','reps','rounds') and item.value='0'::jsonb)
      or (item.key in ('duration','work','distance') and item.value->'value'='0'::jsonb)
  ) and exists (
    select 1 from jsonb_each(p_values) item
    where item.key in ('sets','reps','rounds','load','duration','work','distance')
      and case when jsonb_typeof(item.value)='number' then (item.value::text)::numeric>0
        when jsonb_typeof(item.value)='object' and jsonb_typeof(item.value->'value')='number'
          then (item.value->>'value')::numeric>0 else false end
  );
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
      and s.training_day<=today.day and public.xp_has_measurements(e.actual)
  );
$$;

-- Source validation also protects direct service-role inserts. Account locks
-- serialize the shared daily credit across manual and prescribed save races.
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
      where s.athlete_id=new.athlete_id and s.deleted_at is null
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

drop trigger if exists xp_awards_00_tracking_source on public.xp_awards;
create trigger xp_awards_00_tracking_source before insert on public.xp_awards
for each row execute function public.enforce_tracking_xp_source();

create unique index if not exists xp_awards_tracking_one_time
on public.xp_awards(athlete_id,action) where action in (
  'first_session_logged','first_exercise_logged','training_days_10','training_days_25','training_days_50','training_days_100');
create unique index if not exists xp_awards_manual_daily
on public.xp_awards(athlete_id,calendar_date) where action='manual_training_logged';
-- Revision changes/reapproval cannot earn twice for the same exercise.
create unique index if not exists xp_awards_contribution_exercise
on public.xp_awards(athlete_id,split_part(idempotency_key,':',2)) where action='exercise_contribution_approved';
-- A duplicate YouTube video, including another clip or account, is not another contribution.
create unique index if not exists xp_awards_contribution_video
on public.xp_awards(split_part(idempotency_key,':',3)) where action='exercise_contribution_approved';

alter table public.xp_awards drop constraint if exists xp_awards_calendar_scope_check;
alter table public.xp_awards add constraint xp_awards_calendar_scope_check check (
  (action in ('daily_login','training_logged','manual_training_logged','planned_session_completed',
    'full_training_week_completed','readiness_checkin_completed','injury_update_completed',
    'stop_decision_followed','feedback_submitted','feedback_with_comment')) = (calendar_date is not null));

create or replace function public.reconcile_training_engagement_xp(
  p_athlete_id uuid, p_manual_session_id uuid default null
) returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare v_days integer; v_item record; v_day date; v_results jsonb:='[]'::jsonb; v_result jsonb;
begin
  if coalesce(auth.role(),'')<>'service_role' then raise exception 'backend only' using errcode='42501'; end if;
  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text,0));
  if p_manual_session_id is not null then
    select training_day into v_day from public.external_training_sessions
    where id=p_manual_session_id and athlete_id=p_athlete_id and deleted_at is null;
    if v_day is not null then
      v_result:=public.award_athlete_xp(p_athlete_id,'manual_training_logged','manual-training:'||p_manual_session_id::text,v_day);
      v_results:=v_results||jsonb_build_array(v_result);
    end if;
  end if;
  select count(*) into v_days from public.xp_recorded_training_days(p_athlete_id);
  for v_item in select * from (values ('first_session_logged',1),('training_days_10',10),
    ('training_days_25',25),('training_days_50',50),('training_days_100',100)) rewards(action,threshold)
  loop
    if v_days>=v_item.threshold and not exists(select 1 from public.xp_awards where athlete_id=p_athlete_id and action=v_item.action) then
      v_results:=v_results||jsonb_build_array(public.award_athlete_xp(p_athlete_id,v_item.action,v_item.action||':'||p_athlete_id::text));
    end if;
  end loop;
  if not exists(select 1 from public.xp_awards where athlete_id=p_athlete_id and action='first_exercise_logged')
    and public.xp_has_exercise_result(p_athlete_id) then
    v_results:=v_results||jsonb_build_array(public.award_athlete_xp(p_athlete_id,'first_exercise_logged','first_exercise_logged:'||p_athlete_id::text));
  end if;
  -- The existing athlete-wide award covers either route to a usable plan.
  if not exists(select 1 from public.xp_awards where athlete_id=p_athlete_id and action='first_plan_ready')
    and exists(select 1 from public.plans where athlete_id=p_athlete_id and status in ('ready','publishable_with_flags')) then
    v_results:=v_results||jsonb_build_array(public.award_athlete_xp(p_athlete_id,'first_plan_ready','first-plan-ready:'||p_athlete_id::text));
  end if;
  return v_results;
end; $$;

create or replace function public.reconcile_approved_contribution_xp(p_revision_id uuid)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare v_creator uuid; v_exercise uuid; v_video text; v_zone text; v_result jsonb;
begin
  if coalesce(auth.role(),'')<>'service_role' then raise exception 'backend only' using errcode='42501'; end if;
  select e.creator_id,e.id,m.video_id,p.athlete_timezone into v_creator,v_exercise,v_video,v_zone
  from public.community_exercises e
  join public.community_exercise_revisions r on r.id=e.published_revision_id and r.exercise_id=e.id
  join public.private_exercise_media m on m.id=r.media_id
  join public.profiles p on p.id=e.creator_id and p.role::text='athlete'
  where r.id=p_revision_id and r.status='published' and m.provider='youtube';
  if v_creator is null then return null; end if;
  v_result:=public.award_athlete_xp(v_creator,'exercise_contribution_approved',
    'exercise-contribution:'||v_exercise::text||':'||v_video);
  return jsonb_build_object('athlete_id',v_creator,'timezone',coalesce(v_zone,'UTC'),'results',jsonb_build_array(v_result));
end; $$;

create or replace function public.award_athlete_xp(
  p_athlete_id uuid,
  p_action text,
  p_idempotency_key text,
  p_calendar_date date default null
)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_action text := btrim(coalesce(p_action, ''));
  v_key text := btrim(coalesce(p_idempotency_key, ''));
  v_amount integer;
  v_previous bigint;
  v_total bigint;
  v_last_login date;
  v_award public.xp_awards%rowtype;
  v_awarded boolean := false;
  v_recent jsonb;
  v_calendar_scoped boolean;
  v_calendar_date date := p_calendar_date;
begin
  if coalesce(auth.role(), '') <> 'service_role' then
    raise exception 'award_athlete_xp is restricted to the backend service role'
      using errcode = '42501';
  end if;
  if p_athlete_id is null or not exists (
    select 1
    from public.profiles
    where id = p_athlete_id
      and role::text = 'athlete'
  ) then
    raise exception 'xp athlete profile not found' using errcode = '23503';
  end if;
  if char_length(v_key) not between 1 and 200 then
    raise exception 'invalid XP idempotency key' using errcode = '22023';
  end if;

  v_amount := case v_action
    when 'daily_login' then 0
    when 'training_logged' then 25
    when 'planned_session_completed' then 50
    when 'recommended_fighter_content_watched' then 10
    when 'full_training_week_completed' then 100
    when 'profile_completed' then 25
    when 'first_intake_completed' then 50
    when 'first_plan_ready' then 100
    when 'first_checkin_completed' then 25
    when 'readiness_checkin_completed' then 10
    when 'injury_update_completed' then 10
    when 'stop_decision_followed' then 15
    when 'feedback_submitted' then 1
    when 'feedback_with_comment' then 3
    when 'first_plan_completed' then 250
    when 'phase_completed' then 200
    when 'camp_completed' then 500
    when 'manual_training_logged' then 25
    when 'first_session_logged' then 50
    when 'first_exercise_logged' then 25
    when 'training_days_10' then 100
    when 'training_days_25' then 150
    when 'training_days_50' then 250
    when 'training_days_100' then 500
    when 'exercise_contribution_approved' then 100
  end;
  if v_amount is null then
    raise exception 'unknown XP action' using errcode = '22023';
  end if;
  if v_action in ('feedback_submitted', 'feedback_with_comment') then
    raise exception 'feedback XP must use reconcile_feedback_xp'
      using errcode = '22023';
  end if;

  v_calendar_scoped := v_action in (
    'daily_login',
    'training_logged',
    'manual_training_logged',
    'planned_session_completed',
    'full_training_week_completed',
    'readiness_checkin_completed',
    'injury_update_completed',
    'stop_decision_followed'
  );

  if v_calendar_scoped and v_calendar_date is null then
    v_calendar_date := public.xp_legacy_calendar_date(
      p_athlete_id,
      v_action,
      v_key
    );
  end if;

  -- Daily login is retired and writes no row, so an old caller with no date may
  -- still receive the zero-XP compatibility response.
  if v_action <> 'daily_login'
    and v_calendar_scoped
    and v_calendar_date is null then
    raise exception 'invalid calendar scope for XP action' using errcode = '22023';
  end if;
  if not v_calendar_scoped and v_calendar_date is not null then
    raise exception 'invalid calendar scope for XP action' using errcode = '22023';
  end if;

  if v_action = 'profile_completed' and not exists (
    select 1
    from public.profiles p
    where p.id = p_athlete_id
      and char_length(btrim(coalesce(p.full_name, ''))) > 0
      and exists (
        select 1
        from unnest(coalesce(p.technical_style, array[]::text[])) sport(value)
        where lower(btrim(sport.value)) in ('boxing', 'kickboxing', 'mma')
      )
  ) then
    raise exception 'profile activation milestone is not complete'
      using errcode = '23514';
  end if;

  if v_action = 'first_intake_completed' and not exists (
    select 1
    from public.athlete_intakes
    where athlete_id = p_athlete_id
  ) then
    raise exception 'intake activation milestone is not complete'
      using errcode = '23514';
  end if;

  if v_action = 'first_plan_ready' and not exists (
    select 1
    from public.plans
    where athlete_id = p_athlete_id
      and status in ('ready', 'publishable_with_flags')
  ) then
    raise exception 'plan activation milestone is not complete'
      using errcode = '23514';
  end if;

  if v_action = 'first_checkin_completed' and not exists (
    select 1
    from public.today_checkins
    where athlete_id = p_athlete_id
  ) then
    raise exception 'first check-in milestone is not complete'
      using errcode = '23514';
  end if;

  if v_action = 'readiness_checkin_completed' and not exists (
    select 1
    from public.today_checkins
    where athlete_id = p_athlete_id
      and training_day = v_calendar_date
  ) then
    raise exception 'daily check-in milestone is not complete'
      using errcode = '23514';
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_athlete_id::text, 0));

  insert into public.xp_accounts (athlete_id)
  values (p_athlete_id)
  on conflict (athlete_id) do nothing;

  select total_xp, last_daily_login_date
  into v_previous, v_last_login
  from public.xp_accounts
  where athlete_id = p_athlete_id
  for update;

  if v_action <> 'daily_login' then
    insert into public.xp_awards (
      athlete_id,
      action,
      amount,
      idempotency_key,
      calendar_date
    ) values (
      p_athlete_id,
      v_action,
      v_amount,
      v_key,
      v_calendar_date
    )
    on conflict do nothing
    returning * into v_award;
    v_awarded := v_award.id is not null;
  end if;

  if v_awarded then
    update public.xp_accounts
    set
      total_xp = total_xp + v_amount,
      updated_at = clock_timestamp()
    where athlete_id = p_athlete_id
    returning total_xp, last_daily_login_date
    into v_total, v_last_login;
  else
    v_total := v_previous;
  end if;

  select coalesce(
    jsonb_agg(item order by awarded_at desc, id desc),
    '[]'::jsonb
  )
  into v_recent
  from (
    select
      award.id,
      award.awarded_at,
      jsonb_strip_nulls(jsonb_build_object(
        'id', award.id,
        'action', award.action,
        'amount', award.amount,
        'awarded_at', award.awarded_at,
        'calendar_date', award.calendar_date
      )) item
    from public.xp_awards award
    where award.athlete_id = p_athlete_id
    order by award.awarded_at desc, award.id desc
    limit 20
  ) recent;

  return jsonb_build_object(
    'state', jsonb_build_object(
      'total_xp', v_total,
      'last_daily_login_date', v_last_login,
      'recent_awards', v_recent
    ),
    'previous_total_xp', v_previous,
    'awarded', v_awarded,
    'award', case
      when v_awarded then jsonb_strip_nulls(jsonb_build_object(
        'id', v_award.id,
        'action', v_award.action,
        'amount', v_award.amount,
        'awarded_at', v_award.awarded_at,
        'calendar_date', v_award.calendar_date
      ))
      else 'null'::jsonb
    end
  );
end;
$$;

revoke all on function public.award_athlete_xp(uuid, text, text, date)
  from public, anon, authenticated;
grant execute on function public.award_athlete_xp(uuid, text, text, date)
  to service_role;

revoke all on function public.xp_recorded_training_days(uuid) from public,anon,authenticated;
grant execute on function public.xp_recorded_training_days(uuid) to service_role;
revoke all on function public.xp_has_measurements(jsonb) from public,anon,authenticated;
grant execute on function public.xp_has_measurements(jsonb) to service_role;
revoke all on function public.xp_has_exercise_result(uuid) from public,anon,authenticated;
grant execute on function public.xp_has_exercise_result(uuid) to service_role;
revoke all on function public.enforce_tracking_xp_source() from public,anon,authenticated;
grant execute on function public.enforce_tracking_xp_source() to service_role;
revoke all on function public.reconcile_training_engagement_xp(uuid,uuid) from public,anon,authenticated;
grant execute on function public.reconcile_training_engagement_xp(uuid,uuid) to service_role;
revoke all on function public.reconcile_approved_contribution_xp(uuid) from public,anon,authenticated;
grant execute on function public.reconcile_approved_contribution_xp(uuid) to service_role;
