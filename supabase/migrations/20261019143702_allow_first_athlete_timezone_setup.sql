-- First-time timezone setup must not be blocked by recent activity.
--
-- New accounts start with an empty athlete_timezone (Today falls back to UTC).
-- An athlete who logs training or checks in before building a first camp then
-- hit "athlete_timezone cannot change within 12 hours of training or XP
-- activity" when Quick Build saved their timezone, so profile saves failed and
-- generation never started.
--
-- Setting a timezone from empty is a one-time event: it stamps
-- athlete_timezone_updated_at, so every later change is still subject to both
-- the 12-hour activity guard and the 24-hour change limit. The worst case is a
-- single shift away from the UTC fallback, which cannot be repeated to walk
-- through adjacent training dates.

create or replace function public.validate_profile_athlete_timezone()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_new_timezone text := btrim(coalesce(new.athlete_timezone, ''));
  v_old_timezone text := case
    when tg_op = 'UPDATE' then btrim(coalesce(old.athlete_timezone, ''))
    else ''
  end;
  v_recent_activity_cutoff timestamptz := clock_timestamp() - interval '12 hours';
begin
  if v_new_timezone <> '' and not exists (
    select 1
    from pg_timezone_names
    where name = v_new_timezone
  ) then
    raise exception 'athlete_timezone must be a valid IANA timezone'
      using errcode = '22023';
  end if;

  new.athlete_timezone := v_new_timezone;

  if tg_op = 'INSERT' then
    if v_new_timezone <> '' then
      new.athlete_timezone_updated_at := clock_timestamp();
    end if;
    return new;
  end if;

  if v_new_timezone = v_old_timezone then
    return new;
  end if;

  if v_old_timezone <> '' and v_new_timezone = '' then
    raise exception 'athlete_timezone cannot be cleared after it is set'
      using errcode = '23514';
  end if;

  -- First-time setup from empty is allowed once regardless of activity.
  if v_old_timezone = '' then
    new.athlete_timezone_updated_at := clock_timestamp();
    return new;
  end if;

  if exists (
    select 1
    from public.xp_awards as award
    where award.athlete_id = new.id
      and award.calendar_date is not null
      and award.awarded_at > v_recent_activity_cutoff
  )
  or exists (
    select 1
    from public.session_completions as completion
    where completion.athlete_id = new.id
      and completion.updated_at > v_recent_activity_cutoff
  )
  or exists (
    select 1
    from public.today_checkins as checkin
    where checkin.athlete_id = new.id
      and checkin.updated_at > v_recent_activity_cutoff
  ) then
    raise exception 'athlete_timezone cannot change within 12 hours of training or XP activity'
      using errcode = '23514';
  end if;

  -- Later changes are genuine travel/account events, but cannot be repeated fast
  -- enough to walk through adjacent server-owned training dates.
  if old.athlete_timezone_updated_at is not null
    and old.athlete_timezone_updated_at > clock_timestamp() - interval '24 hours' then
    raise exception 'athlete_timezone can only be changed once every 24 hours'
      using errcode = '23514';
  end if;

  new.athlete_timezone_updated_at := clock_timestamp();
  return new;
end;
$$;

revoke all on function public.validate_profile_athlete_timezone()
  from public, anon, authenticated;
grant execute on function public.validate_profile_athlete_timezone()
  to service_role;
