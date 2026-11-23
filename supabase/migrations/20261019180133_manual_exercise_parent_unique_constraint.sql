do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname='external_training_sessions_id_athlete_key'
      and conrelid='public.external_training_sessions'::regclass
  ) then
    alter table public.external_training_sessions
      add constraint external_training_sessions_id_athlete_key
      unique using index external_training_sessions_id_athlete_key;
  end if;
end $$;
