-- Optional athlete-confirmed logging routines. No planned sessions or completion evidence.
-- Keep existing profile ownership/RLS; writes use the authenticated profile API.
alter table public.profiles
  add column if not exists training_habits jsonb not null default '{"enabled":true,"decisions":[]}'::jsonb,
  add column if not exists training_habits_revision integer not null default 0;
alter table public.profiles drop constraint if exists profiles_training_habits_shape;
alter table public.profiles add constraint profiles_training_habits_shape check (
  jsonb_typeof(training_habits) = 'object'
  and training_habits ?& array['enabled', 'decisions']
  and jsonb_typeof(training_habits->'enabled') = 'boolean'
  and jsonb_typeof(training_habits->'decisions') = 'array'
  and jsonb_array_length(training_habits->'decisions') <= 42
  and training_habits_revision >= 0
);
