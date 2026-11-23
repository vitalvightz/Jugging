create or replace function public.guard_manual_exercise_entry()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  parent record;
  live_count integer;
begin
  if tg_op = 'INSERT' then
    if exists (select 1 from public.manual_exercise_entries
               where athlete_id = new.athlete_id and client_entry_id = new.client_entry_id) then
      return new;
    end if;
    select training_day, deleted_at into parent from public.external_training_sessions
      where id = new.external_session_id and athlete_id = new.athlete_id for update;
    if not found or parent.deleted_at is not null then
      raise exception 'manual training session not found' using errcode = 'P0002';
    end if;
    select count(*) into live_count from public.manual_exercise_entries
      where athlete_id = new.athlete_id and external_session_id = new.external_session_id and deleted_at is null;
    if live_count >= 30 then
      raise exception 'manual exercise limit reached' using errcode = 'P0001';
    end if;
    new.training_day := parent.training_day;
    new.deleted_at := null;
    new.revision := 1;
    return new;
  end if;
  new.athlete_id := old.athlete_id;
  new.external_session_id := old.external_session_id;
  new.client_entry_id := old.client_entry_id;
  new.created_at := old.created_at;
  new.deleted_at := coalesce(old.deleted_at, new.deleted_at);
  -- Revisions only move forward, one edit at a time.
  if new.revision is distinct from old.revision and new.revision <> old.revision + 1 then
    new.revision := old.revision;
  end if;
  select training_day into new.training_day from public.external_training_sessions
    where id = old.external_session_id and athlete_id = old.athlete_id;
  return new;
end
$$;

drop trigger if exists guard_manual_exercise_entry on public.manual_exercise_entries;
create trigger guard_manual_exercise_entry
  before insert or update on public.manual_exercise_entries
  for each row execute function public.guard_manual_exercise_entry();

drop trigger if exists set_manual_exercise_entries_updated_at on public.manual_exercise_entries;
create trigger set_manual_exercise_entries_updated_at
  before update on public.manual_exercise_entries
  for each row execute function public.set_updated_at();

drop trigger if exists set_personal_exercises_updated_at on public.personal_exercises;
create trigger set_personal_exercises_updated_at
  before update on public.personal_exercises
  for each row execute function public.set_updated_at();

