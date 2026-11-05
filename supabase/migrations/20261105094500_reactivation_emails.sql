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

-- A claim is permanent even after an uncertain Resend response: no duplicate reminders.
create table public.reactivation_email_deliveries (
  profile_id uuid primary key references public.profiles(id) on delete cascade,
  status text not null default 'claimed' check (status in ('claimed','sent','failed','skipped')),
  claimed_at timestamptz not null default now(),
  sent_at timestamptz
);
alter table public.reactivation_email_deliveries enable row level security;
revoke all on public.reactivation_email_deliveries from anon, authenticated;
grant select, insert, update on public.reactivation_email_deliveries to service_role;

-- Only previously active adults with an available plan, a fresh opt-in, and 7+ days away.
-- No training notes, health information or AI payloads are read or sent.
create function public.reactivation_email_candidates()
returns table(profile_id uuid, email text, first_name text, plan_id uuid)
language sql stable security invoker set search_path = public, pg_temp as $$
  select p.id, p.email, split_part(btrim(p.full_name), ' ', 1), pl.id
  from public.profiles p
  join public.reactivation_email_preferences pref
    on pref.profile_id = p.id and pref.enabled and pref.opted_in_at is not null
  join lateral (
    select max(a.occurred_at) last_at
    from public.product_activity a where a.athlete_id = p.id
  ) activity on activity.last_at between now() - interval '21 days'
                              and now() - interval '7 days'
  join public.plans pl on pl.id = p.active_plan_id
    and pl.athlete_id = p.id and pl.status in ('ready', 'publishable_with_flags')
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
    )
  order by activity.last_at
  limit greatest(0, least(10, 20 - (
    select count(*) from public.reactivation_email_deliveries
    where claimed_at > now() - interval '24 hours'
  )));
$$;
revoke all on function public.reactivation_email_candidates() from public, anon, authenticated;
grant execute on function public.reactivation_email_candidates() to service_role;
