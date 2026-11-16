"""Run the coach migration and permission tests in an isolated PostgreSQL container.

Usage: python tools/test_coach_workspace.py --container unlxck-coach-test
Never connects to Supabase or writes production data. Uses real source table DDL.
"""
import argparse
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--container', required=True)
args = parser.parse_args()

def sql(source):
    subprocess.run(['docker', 'exec', '-i', args.container, 'psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-q'], input=source, text=True, check=True)

schema = (root / 'supabase/schema.sql').read_text()
bootstrap = """
begin;
create role anon;
create role authenticated;
create role service_role bypassrls;
create schema auth;
create schema private;
create function auth.role() returns text language sql stable as $$ select coalesce(nullif(current_setting('request.jwt.claim.role',true),''),current_user::text) $$;
create table auth.users(id uuid primary key, email text, email_confirmed_at timestamptz, created_at timestamptz not null default now());
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
grant usage on schema auth to authenticated,anon;
grant execute on function auth.uid() to authenticated,anon;
create type public.app_role as enum ('athlete','coach','gym_owner','admin');
"""
for table in ('profiles', 'athlete_intakes', 'plans', 'daily_checkins', 'session_logs', 'today_checkins', 'session_completions', 'external_training_sessions'):
    ddl = re.search(r'create table if not exists public\.' + table + r' \([\s\S]*?\n\);', schema)
    assert ddl, table
    bootstrap += ddl[0] + '\n'
bootstrap += """
alter table public.profiles add column active_plan_id uuid references public.plans(id);
alter table public.session_completions add column prescription_snapshot jsonb, add column rehab_performance text;
"""
helper = re.search(r'create or replace function public.injury_flags_infection_signs_valid[\s\S]*?\n\$\$;', schema)
bootstrap += helper[0] + '\n'
bootstrap += re.search(r'create table if not exists public\.injury_flags \([\s\S]*?\n\);', schema)[0] + '\n'
sparring = (root / 'supabase/migrations/20260930141404_add_sparring_logs.sql').read_text()
bootstrap += re.search(r'create table if not exists public\.sparring_logs \([\s\S]*?\n\);', sparring)[0] + '\n'
bootstrap += 'alter table public.sparring_logs add column external_session_id uuid;\n'
bootstrap += (root / 'supabase/migrations/20261018114339_add_completion_origin.sql').read_text()
for table in ('profiles','plans','daily_checkins','session_logs','today_checkins','session_completions','external_training_sessions','injury_flags'):
    owner = 'id' if table == 'profiles' else 'athlete_id'
    bootstrap += f'alter table public.{table} enable row level security; grant select on public.{table} to authenticated; create policy owner_only on public.{table} for select to authenticated using ({owner}=auth.uid());\n'
bootstrap += (root / 'supabase/migrations/20261116001139_coach_workspace.sql').read_text()
bootstrap += (root / 'supabase/migrations/20261116001150_coach_signup_invitations.sql').read_text()
bootstrap += (root / 'supabase/migrations/20261116003447_coach_self_signup.sql').read_text()
bootstrap += (root / 'supabase/migrations/20261116030000_coach_weight_consent.sql').read_text()
bootstrap += (root / 'tests/coach_workspace_permissions.sql').read_text()
bootstrap += (root / 'tests/coach_signup_invitations.sql').read_text()
bootstrap += (root / 'tests/coach_self_signup.sql').read_text()
bootstrap += (root / 'tests/coach_weight_consent.sql').read_text()
bootstrap += '\nrollback;\n'
sql(bootstrap)
print('Coach migration and PostgreSQL permission assertions passed (rolled back).')
