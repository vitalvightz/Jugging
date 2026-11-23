-- Private beta reminders are eligible without Settings opt-in.
-- A disabled preference row is an explicit email unsubscribe and remains excluded.
create or replace function public.reactivation_email_candidates()
returns table(profile_id uuid, email text, first_name text)
language sql stable security invoker set search_path = public, pg_temp as $$
  select p.id, p.email, split_part(btrim(p.full_name), ' ', 1)
  from public.profiles p
  left join public.reactivation_email_preferences pref on pref.profile_id = p.id
  join lateral (
    select greatest(
      (select max(a.occurred_at) from public.product_activity a where a.athlete_id = p.id),
      -- Daily app-open tracking is independent of analytics and AI plans. Allow
      -- an extra two days for day-only timestamps and all athlete time zones.
      (select (max(d.activity_date)::timestamp at time zone 'UTC') + interval '2 days'
       from public.athlete_daily_activity d where d.athlete_id = p.id)
    ) last_at
  ) activity on activity.last_at <= now() - interval '7 days'
  where (pref.profile_id is null or pref.enabled)
    and p.role = 'athlete'
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
