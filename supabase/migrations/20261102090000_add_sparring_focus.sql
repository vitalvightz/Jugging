-- One optional thing to work on next time, recorded after sparring and shown
-- before the next relevant session ("Last training focus").
--
-- Written by the API with a plain owner-scoped update after
-- public.record_sparring_log, so the safety RPC (head-contact protection and
-- rocked reviews) is unchanged. Existing rows read as no focus.
--
-- Repeatable: every step is guarded.

alter table public.sparring_logs
  add column if not exists focus text not null default '';

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'sparring_logs_focus_length'
      and conrelid = 'public.sparring_logs'::regclass) then
    alter table public.sparring_logs
      add constraint sparring_logs_focus_length check (char_length(focus) <= 140);
  end if;
end $$;
