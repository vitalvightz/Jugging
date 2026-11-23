revoke all on function public.guard_manual_exercise_entry() from public, anon, authenticated;
revoke all on function public.sync_manual_exercise_entries() from public, anon, authenticated;

alter table public.personal_exercises enable row level security;
revoke all on public.personal_exercises from anon, authenticated;
grant select on public.personal_exercises to authenticated;
grant all on public.personal_exercises to service_role;
drop policy if exists personal_exercises_select_own on public.personal_exercises;
create policy personal_exercises_select_own on public.personal_exercises
  for select to authenticated using (athlete_id = (select auth.uid()));

alter table public.manual_exercise_entries enable row level security;
revoke all on public.manual_exercise_entries from anon, authenticated;
grant select on public.manual_exercise_entries to authenticated;
grant all on public.manual_exercise_entries to service_role;
drop policy if exists manual_exercise_entries_select_own on public.manual_exercise_entries;
create policy manual_exercise_entries_select_own on public.manual_exercise_entries
  for select to authenticated using (athlete_id = (select auth.uid()) and deleted_at is null);
