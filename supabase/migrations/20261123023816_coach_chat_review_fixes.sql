-- Coach chat review fixes.
--
-- 1. A disconnected coach can no longer move read receipts. The fighter keeps
--    managing their retained history.
-- 2. A check-in nudge claim is a 15-minute lease, not a sent mark. The worker
--    confirms the day only after a push actually went out, so a failed
--    delivery is retried inside the same two-hour window instead of being
--    lost for the day. The push ledger's dedupe key still makes a retry the
--    same notification, so a fighter never gets two.

create or replace function coach_private.chat_read(p_chat_id uuid) returns void
language sql security definer set search_path = '' as $$
  update public.coach_chats ch set
    coach_unread = case when ch.coach_id = auth.uid() then 0 else ch.coach_unread end,
    coach_read_at = case when ch.coach_id = auth.uid() then now() else ch.coach_read_at end,
    athlete_unread = case when ch.athlete_id = auth.uid() then 0 else ch.athlete_unread end,
    athlete_read_at = case when ch.athlete_id = auth.uid() then now() else ch.athlete_read_at end
  where ch.id = p_chat_id and ch.last_message_at is not null and (
    (ch.athlete_id = auth.uid()
      and (ch.athlete_unread > 0 or ch.athlete_read_at is null or ch.athlete_read_at < ch.last_message_at))
    or (ch.coach_id = auth.uid()
      and (ch.coach_unread > 0 or ch.coach_read_at is null or ch.coach_read_at < ch.last_message_at)
      and exists (select 1 from public.coach_connections c where c.coach_id = ch.coach_id
        and c.athlete_id = ch.athlete_id and c.status = 'accepted')));
$$;

alter table public.coach_checkin_nudges add column if not exists last_claimed_at timestamptz;

create or replace function public.claim_coach_checkin_nudges(p_limit integer default 200)
returns table (nudge_id uuid, coach_id uuid, athlete_id uuid, coach_name text, timezone text, training_day date)
language sql volatile security definer set search_path = '' as $$
  with zones as materialized (select name from pg_catalog.pg_timezone_names),
  candidates as (
    select n.id, n.coach_id, n.athlete_id, n.days, n.local_time, n.last_sent_day, n.last_claimed_at,
      coach.full_name as coach_name, coalesce(z.name, 'UTC') as zone,
      (now() at time zone coalesce(z.name, 'UTC')) as local_now
    from public.coach_checkin_nudges n
    join public.coach_connections c on c.coach_id = n.coach_id and c.athlete_id = n.athlete_id and c.status = 'accepted'
    join public.profiles a on a.id = n.athlete_id and a.role = 'athlete' and a.access_status = 'approved'
      and a.health_data_consent is true and a.date_of_birth is not null
      and a.date_of_birth <= current_date - interval '18 years'
    join public.profiles coach on coach.id = n.coach_id and coach.role in ('coach', 'admin') and coach.access_status = 'approved'
    left join zones z on z.name = a.athlete_timezone
    where n.enabled
  ),
  due as (
    select cd.id, cd.coach_name, cd.zone, cd.local_now::date as day
    from candidates cd
    where (cd.days & (1 << (extract(isodow from cd.local_now)::integer - 1))) <> 0
      and cd.local_now::time >= cd.local_time
      and cd.local_now::time < cd.local_time + interval '2 hours'
      and cd.last_sent_day is distinct from cd.local_now::date
      and (cd.last_claimed_at is null or cd.last_claimed_at < now() - interval '15 minutes')
      and not exists (select 1 from public.today_checkins t
        where t.athlete_id = cd.athlete_id and t.training_day = cd.local_now::date)
    order by cd.local_time
    limit greatest(1, least(coalesce(p_limit, 200), 500))
  )
  -- Rechecked under the row lock: two workers never hold the same lease.
  update public.coach_checkin_nudges n set last_claimed_at = now()
  from due d
  where n.id = d.id and n.last_sent_day is distinct from d.day
    and (n.last_claimed_at is null or n.last_claimed_at < now() - interval '15 minutes')
  returning n.id, n.coach_id, n.athlete_id, d.coach_name, d.zone, d.day;
$$;
revoke all on function public.claim_coach_checkin_nudges(integer) from public, anon, authenticated;
grant execute on function public.claim_coach_checkin_nudges(integer) to service_role;

-- Called by the worker once the push went out: the day is done.
create or replace function public.confirm_coach_checkin_nudge(p_nudge_id uuid, p_day date) returns void
language sql security definer set search_path = '' as $$
  update public.coach_checkin_nudges set last_sent_day = p_day, last_sent_at = now()
  where id = p_nudge_id and last_sent_day is distinct from p_day;
$$;
revoke all on function public.confirm_coach_checkin_nudge(uuid, date) from public, anon, authenticated;
grant execute on function public.confirm_coach_checkin_nudge(uuid, date) to service_role;
