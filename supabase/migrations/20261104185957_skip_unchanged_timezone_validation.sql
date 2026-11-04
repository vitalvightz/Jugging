-- validate_profile_athlete_timezone looked the value up in pg_timezone_names
-- (a scan of the system zone list, ~0.5 s on this instance) before checking
-- whether the timezone changed at all. Every profile/onboarding save sends
-- athlete_timezone, so every save paid for it. An unchanged value on UPDATE was
-- validated when it was written; it now returns before the lookup. Inserts and
-- real changes are validated exactly as before.

create or replace function public.validate_profile_athlete_timezone()
returns trigger
language plpgsql
set search_path to 'pg_catalog', 'public'
as $function$
declare
  v_new_timezone text := btrim(coalesce(new.athlete_timezone, ''));
  v_old_timezone text := case
    when tg_op = 'UPDATE' then btrim(coalesce(old.athlete_timezone, ''))
    else ''
  end;
  v_recent_activity_cutoff timestamptz := clock_timestamp() - interval '12 hours';
begin
  new.athlete_timezone := v_new_timezone;

  -- Unchanged on update: already validated when it was set.
  if tg_op = 'UPDATE' and v_new_timezone = v_old_timezone then
    return new;
  end if;

  if v_new_timezone <> '' and not exists (
    select 1
    from pg_timezone_names
    where name = v_new_timezone
  ) then
    raise exception 'athlete_timezone must be a valid IANA timezone'
      using errcode = '22023';
  end if;

  if tg_op = 'INSERT' then
    if v_new_timezone <> '' then
      new.athlete_timezone_updated_at := clock_timestamp();
    end if;
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
$function$;
