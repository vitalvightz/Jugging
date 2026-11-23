-- A training session may be completed while its rehab is explicitly skipped.
-- Keep the existing completion record and ownership/RLS unchanged.
alter table public.session_completions
  drop constraint if exists session_completions_rehab_performance_check;
alter table public.session_completions
  add constraint session_completions_rehab_performance_check
  check (rehab_performance in ('done_as_shown', 'changed', 'stopped', 'skipped'));
