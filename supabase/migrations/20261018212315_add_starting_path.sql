-- Existing accounts keep their landing. New profiles start without a choice.
-- The temporary default fills existing rows atomically; dropping it makes new
-- rows NULL. Re-running this migration never overwrites an athlete's choice.
begin;
alter table public.profiles
    add column if not exists starting_path text default 'plan_first'
    check (starting_path in ('tracking_first', 'plan_first'));
alter table public.profiles alter column starting_path drop default;
comment on column public.profiles.starting_path is
    'Optional starting experience, never a feature entitlement or active-plan selector.';
commit;
