-- Saved self-directed drafts only. No generated-plan, Today or completion FK.
create table if not exists public.custom_training_plans (
  id uuid primary key default gen_random_uuid(),
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  name text not null check (length(trim(name)) between 1 and 100),
  kind text not null default 'self_directed' check (kind = 'self_directed'),
  sessions jsonb not null default '[]'::jsonb check (jsonb_typeof(sessions) = 'array' and jsonb_array_length(sessions) <= 28),
  incomplete boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists custom_training_plans_athlete_updated_idx
  on public.custom_training_plans (athlete_id, updated_at desc);
drop trigger if exists set_custom_training_plans_updated_at on public.custom_training_plans;
create trigger set_custom_training_plans_updated_at
  before update on public.custom_training_plans
  for each row execute function public.set_updated_at();
alter table public.custom_training_plans enable row level security;
revoke all on public.custom_training_plans from anon, authenticated;
grant select on public.custom_training_plans to authenticated;
grant select, insert, update, delete on public.custom_training_plans to service_role;
drop policy if exists custom_training_plans_select_own on public.custom_training_plans;
create policy custom_training_plans_select_own on public.custom_training_plans
  for select to authenticated using (athlete_id = (select auth.uid()));
