create or replace function public.sync_manual_exercise_entries()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.training_day is distinct from old.training_day or new.deleted_at is distinct from old.deleted_at then
    update public.manual_exercise_entries
      set training_day = new.training_day,
          deleted_at = case when new.deleted_at is not null then coalesce(deleted_at, new.deleted_at) else deleted_at end
      where external_session_id = new.id and athlete_id = new.athlete_id;
  end if;
  return new;
end
$$;

drop trigger if exists sync_manual_exercise_entries on public.external_training_sessions;
create trigger sync_manual_exercise_entries
  after update of training_day, deleted_at on public.external_training_sessions
  for each row execute function public.sync_manual_exercise_entries();

