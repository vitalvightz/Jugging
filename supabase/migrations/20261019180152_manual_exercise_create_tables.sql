create table if not exists public.personal_exercises (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 100 and name = btrim(name)),
  -- Case and whitespace only: "back  SQUAT" and "Back squat" are one name;
  -- "Pause back squat" is a different movement.
  name_key text not null check (char_length(name_key) between 1 and 100),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint personal_exercises_name_key unique (athlete_id, name_key),
  constraint personal_exercises_id_athlete_key unique (id, athlete_id)
);

create table if not exists public.manual_exercise_entries (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  external_session_id uuid not null,
  client_entry_id uuid not null,
  training_day date not null,
  exercise_key text check (
    exercise_key is null
    or (char_length(exercise_key) between 1 and 120 and exercise_key ~ '^[a-z0-9]+(-[a-z0-9]+)*$')),
  personal_exercise_id uuid,
  display_name text not null check (char_length(display_name) between 1 and 120),
  measurement_format text not null
    check (measurement_format in ('strength', 'bodyweight', 'isometric', 'intervals', 'distance')),
  -- Only the values the athlete entered, in exercise_logs.actual's shape.
  actual jsonb not null default '{}'::jsonb
    check (jsonb_typeof(actual) = 'object' and octet_length(actual::text) <= 4096),
  notes text not null default '' check (char_length(notes) <= 500),
  revision integer not null default 1 check (revision >= 1),
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint manual_exercise_entries_request_key unique (athlete_id, client_entry_id),
  constraint manual_exercise_entries_one_identity check ((exercise_key is null) <> (personal_exercise_id is null)),
  constraint manual_exercise_entries_session_fkey foreign key (external_session_id, athlete_id)
    references public.external_training_sessions(id, athlete_id) on delete cascade,
  constraint manual_exercise_entries_personal_fkey foreign key (personal_exercise_id, athlete_id)
    references public.personal_exercises(id, athlete_id) on delete cascade
);

