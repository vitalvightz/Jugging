-- A separate, explicit email choice; push and analytics consent are not email consent.
create table public.reactivation_email_preferences (
  profile_id uuid primary key references public.profiles(id) on delete cascade,
  enabled boolean not null default false,
  opted_in_at timestamptz,
  updated_at timestamptz not null default now()
);
alter table public.reactivation_email_preferences enable row level security;
revoke all on public.reactivation_email_preferences from anon, authenticated;
grant select, insert, update on public.reactivation_email_preferences to service_role;

-- One lifetime reminder. Retries stop before Resend’s 24-hour idempotency expiry.
create table public.reactivation_email_deliveries (
  profile_id uuid primary key references public.profiles(id) on delete cascade,
  status text not null default 'claimed' check (status in ('claimed','sent','failed','skipped')),
  claimed_at timestamptz not null default now(),
  attempts integer not null default 0,
  last_attempt_at timestamptz,
  next_attempt_at timestamptz not null default now(),
  payload jsonb,
  provider_id text unique,
  delivered_at timestamptz,
  clicked_at timestamptz,
  reactivated_at timestamptz,
  sent_at timestamptz
);
alter table public.reactivation_email_deliveries enable row level security;
revoke all on public.reactivation_email_deliveries from anon, authenticated;
grant select, insert, update on public.reactivation_email_deliveries to service_role;

-- Previously active adults, explicit opt-in, and at least seven days away.
-- No training notes, health information or AI payloads are read or sent.
create function public.reactivation_email_candidates()
returns table(profile_id uuid, email text, first_name text)
language sql stable security invoker set search_path = public, pg_temp as $$
  select p.id, p.email, split_part(btrim(p.full_name), ' ', 1)
  from public.profiles p
  join public.reactivation_email_preferences pref
    on pref.profile_id = p.id and pref.enabled and pref.opted_in_at is not null
  join lateral (
    select greatest(
      (select max(a.occurred_at) from public.product_activity a where a.athlete_id = p.id),
      -- Daily app-open tracking is independent of analytics and AI plans. Allow
      -- an extra two days for day-only timestamps and all athlete time zones.
      (select (max(d.activity_date)::timestamp at time zone 'UTC') + interval '2 days'
       from public.athlete_daily_activity d where d.athlete_id = p.id)
    ) last_at
  ) activity on activity.last_at <= now() - interval '7 days'
  where p.role = 'athlete'
    and p.access_status = 'approved'
    and p.date_of_birth is not null
    and (date_part('year', age(current_date, p.date_of_birth))) >= 18
    and not p.analytics_excluded
    and not exists (
      select 1 from public.product_analytics_excluded_emails x
      where x.email = lower(btrim(p.email))
    )
    and not exists (
      select 1 from public.reactivation_email_deliveries d where d.profile_id = p.id
      and (d.status in ('sent','skipped') or d.attempts >= 10
        or d.claimed_at <= now() - interval '23 hours' or d.next_attempt_at > now())
    )
  order by activity.last_at
  limit 10;
$$;
revoke all on function public.reactivation_email_candidates() from public, anon, authenticated;
grant execute on function public.reactivation_email_candidates() to service_role;

-- Serialize claims across workers; one request/second and twenty new reminders/day.
create function public.claim_reactivation_email(p_profile_id uuid)
returns setof public.reactivation_email_deliveries
language plpgsql security invoker set search_path = public, pg_temp as $$
begin
  perform pg_advisory_xact_lock(hashtextextended('reactivation-email', 0));
  if exists (select 1 from public.reactivation_email_deliveries
             where last_attempt_at > now() - interval '1 second'
                or (status = 'failed' and next_attempt_at > now())) then return; end if;
  if not exists (select 1 from public.reactivation_email_candidates() where profile_id = p_profile_id) then return; end if;
  if not exists (select 1 from public.reactivation_email_deliveries where profile_id = p_profile_id)
     and (select count(*) from public.reactivation_email_deliveries
          where claimed_at > now() - interval '24 hours') >= 20 then return; end if;
  insert into public.reactivation_email_deliveries(profile_id) values(p_profile_id)
    on conflict do nothing;
  return query update public.reactivation_email_deliveries set
    status = 'claimed', attempts = attempts + 1, last_attempt_at = now(),
    next_attempt_at = now() + interval '60 seconds'
    where profile_id = p_profile_id returning *;
end;
$$;
revoke all on function public.claim_reactivation_email(uuid) from public, anon, authenticated;
grant execute on function public.claim_reactivation_email(uuid) to service_role;

-- Lifecycle events use existing privacy-filtered analytics ingestion.
alter table public.product_events drop constraint product_events_event_name_check;
alter table public.product_events add constraint product_events_event_name_check check (event_name in (
 'plan_opened','plan_editor_opened','plan_edited','plan_replaced',
 'timer_started','timer_round_completed','timer_completed','timer_exited',
 'history_opened','nutrition_opened','nutrition_saved','feedback_prompt_shown',
 'checkin_started','sparring_log_started',
 'app_opened','page_viewed','operation_failed','frontend_error','reactivation_email_delivered','reactivation_email_clicked','reactivation_email_returned'));
create function public.reactivation_email_analytics() returns trigger
language plpgsql security invoker set search_path = public, pg_temp as $$
begin
  if old.delivered_at is null and new.delivered_at is not null then
    perform public.ingest_product_event(new.profile_id, 'reactivation_email_delivered', 'onboarding', new.profile_id);
  end if;
  if old.clicked_at is null and new.clicked_at is not null then
    perform public.ingest_product_event(new.profile_id, 'reactivation_email_clicked', 'onboarding', new.profile_id);
  end if;
  if old.reactivated_at is null and new.reactivated_at is not null then
    perform public.ingest_product_event(new.profile_id, 'reactivation_email_returned', 'onboarding', new.profile_id);
  end if;
  return new;
end;
$$;
revoke all on function public.reactivation_email_analytics() from public, anon, authenticated;
create trigger reactivation_email_analytics after update on public.reactivation_email_deliveries
for each row execute function public.reactivation_email_analytics();

-- A real authenticated app open, not an email scanner, establishes reactivation.
create function public.reactivation_email_return() returns trigger
language plpgsql security invoker set search_path = public, pg_temp as $$
begin
  update public.reactivation_email_deliveries set reactivated_at = now()
    where profile_id = new.athlete_id and sent_at is not null
      and sent_at < now() and reactivated_at is null;
  return new;
end;
$$;
revoke all on function public.reactivation_email_return() from public, anon, authenticated;
create trigger reactivation_email_return after insert on public.athlete_daily_activity
for each row execute function public.reactivation_email_return();
