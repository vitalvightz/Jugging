create index if not exists manual_exercise_entries_session_idx
  on public.manual_exercise_entries (athlete_id, external_session_id, created_at)
  where deleted_at is null;
create index if not exists manual_exercise_entries_key_idx
  on public.manual_exercise_entries (athlete_id, exercise_key, training_day desc, id desc)
  where deleted_at is null and exercise_key is not null;
create index if not exists manual_exercise_entries_personal_idx
  on public.manual_exercise_entries (athlete_id, personal_exercise_id, training_day desc, id desc)
  where deleted_at is null and personal_exercise_id is not null;

