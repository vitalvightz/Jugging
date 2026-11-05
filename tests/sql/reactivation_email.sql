-- Disposable database fixture only. Run before the reactivation migration.
create role anon;
create role authenticated;
create role service_role bypassrls;
create table public.profiles (
 id uuid primary key, email text, full_name text, role text default 'athlete',
 access_status text default 'approved', date_of_birth date default '1990-01-01',
 analytics_excluded boolean default false
);
create table public.product_activity (id text, athlete_id uuid, occurred_at timestamptz);
create table public.athlete_daily_activity (athlete_id uuid, activity_date date);
create table public.product_analytics_excluded_emails(email text);
create table public.product_events(event_name text constraint product_events_event_name_check check(event_name in ('app_opened')));
create table public.test_events(athlete_id uuid, event_name text);
create function public.ingest_product_event(uuid,text,text,uuid) returns boolean language plpgsql as $$
begin insert into public.test_events values($1,$2); return true; end $$;
grant select on all tables in schema public to service_role;
