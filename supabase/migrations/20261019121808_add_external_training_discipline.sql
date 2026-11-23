-- Optional sport and session type for manual training (shared/training-disciplines.json).
--
-- Both are nullable and have no default: existing rows keep null, so nothing
-- already logged is reclassified. ``activity`` is unchanged and still drives the
-- sparring follow-up link (activity = 'sparring'), so a sport/session type is
-- only stored with the activity that pair maps to. Neither
-- column is read by workload, readiness, contact or clearance logic.
--
-- Repeatable: every step is guarded.

alter table public.external_training_sessions
  add column if not exists sport text,
  add column if not exists session_type text;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'external_training_sessions_discipline'
      and conrelid = 'public.external_training_sessions'::regclass) then
    alter table public.external_training_sessions
      add constraint external_training_sessions_discipline check (
      (sport is null and session_type is null)
      or ((coalesce(sport, ''), coalesce(session_type, ''), activity) in (
        ('boxing', '', 'boxing'), ('boxing', 'pads', 'boxing'), ('boxing', 'bag', 'boxing'), ('boxing', 'shadowboxing', 'boxing'), ('boxing', 'technical', 'boxing'), ('boxing', 'sparring', 'sparring'),
        ('muay_thai', '', 'other'), ('muay_thai', 'pads', 'other'), ('muay_thai', 'bag', 'other'), ('muay_thai', 'clinch', 'other'), ('muay_thai', 'technical', 'other'), ('muay_thai', 'sparring', 'sparring'),
        ('kickboxing', '', 'other'), ('kickboxing', 'pads', 'other'), ('kickboxing', 'bag', 'other'), ('kickboxing', 'technical', 'other'), ('kickboxing', 'sparring', 'sparring'),
        ('mma', '', 'other'), ('mma', 'striking', 'other'), ('mma', 'grappling', 'other'), ('mma', 'wrestling', 'other'), ('mma', 'integrated', 'other'), ('mma', 'sparring', 'sparring'),
        ('bjj', '', 'other'), ('bjj', 'drilling', 'other'), ('bjj', 'positional', 'other'), ('bjj', 'rolling', 'other'),
        ('wrestling', '', 'other'), ('wrestling', 'technical', 'other'), ('wrestling', 'live', 'other'))));
  end if;
end $$;
