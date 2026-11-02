-- Optional distance and exact finish time for manually logged runs (roadwork).
--
-- All three columns are nullable with no default: existing rows keep null and
-- nothing already logged changes. ``duration_minutes`` stays the workload value;
-- when ``duration_seconds`` is set the API stores minutes as ceil(seconds / 60).
-- Distance is only stored with activity = 'roadwork'.
--
-- Repeatable: every step is guarded.

alter table public.external_training_sessions
  add column if not exists distance_value numeric,
  add column if not exists distance_unit text,
  add column if not exists duration_seconds integer;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'external_training_sessions_run_distance'
      and conrelid = 'public.external_training_sessions'::regclass) then
    alter table public.external_training_sessions
      add constraint external_training_sessions_run_distance check (
        (distance_value is null and distance_unit is null)
        or (activity = 'roadwork' and distance_value > 0 and (
          (distance_unit = 'km' and distance_value <= 200)
          or (distance_unit = 'miles' and distance_value <= 125))));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'external_training_sessions_duration_seconds'
      and conrelid = 'public.external_training_sessions'::regclass) then
    alter table public.external_training_sessions
      add constraint external_training_sessions_duration_seconds check (
        duration_seconds is null
        or (duration_seconds between 1 and 36000 and duration_minutes = ceil(duration_seconds / 60.0)));
  end if;
end $$;
