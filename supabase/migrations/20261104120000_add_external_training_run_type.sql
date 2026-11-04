-- Optional run type for manually logged runs (roadwork): easy, tempo,
-- intervals, sprints, long or race.
--
-- Nullable with no default: existing rows keep null and nothing already
-- logged changes. Only stored with activity = 'roadwork', matching the API.
--
-- Repeatable: every step is guarded.

alter table public.external_training_sessions
  add column if not exists run_type text;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'external_training_sessions_run_type'
      and conrelid = 'public.external_training_sessions'::regclass) then
    alter table public.external_training_sessions
      add constraint external_training_sessions_run_type check (
        run_type is null
        or (activity = 'roadwork' and run_type in ('easy', 'tempo', 'intervals', 'sprints', 'long', 'race')));
  end if;
end $$;
