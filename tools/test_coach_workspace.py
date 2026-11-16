"""Run the coach migration and permission tests in an isolated PostgreSQL container.

Usage: python tools/test_coach_workspace.py --container unlxck-coach-test
Never connects to Supabase or writes production data. Uses real source table DDL.
"""
import argparse
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--container', required=True)
args = parser.parse_args()

def sql(source):
    subprocess.run(['docker', 'exec', '-i', args.container, 'psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-q'], input=source, text=True, check=True)

# Load every public table, trigger, policy and function from the canonical
# application schema. Only Supabase-owned Auth/Storage platform objects are
# bootstrapped locally; no application tables are replaced by test stubs.
bootstrap = """
begin;
do $$ begin
  if not exists(select 1 from pg_roles where rolname='anon') then create role anon; end if;
  if not exists(select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if;
  if not exists(select 1 from pg_roles where rolname='service_role') then create role service_role bypassrls; end if;
end $$;
-- Supabase public-schema default grants; source migrations revoke as needed.
alter default privileges in schema public grant all on tables to anon,authenticated,service_role;
alter default privileges in schema public grant all on sequences to anon,authenticated,service_role;
create schema auth;
create schema storage;
create table auth.users(id uuid primary key,email text,email_confirmed_at timestamptz,
  raw_user_meta_data jsonb,created_at timestamptz not null default now());
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
create function auth.role() returns text language sql stable as $$ select coalesce(nullif(current_setting('request.jwt.claim.role',true),''),current_user::text) $$;
create table storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint,allowed_mime_types text[]);
create table storage.objects(id uuid primary key,bucket_id text,name text,owner uuid,created_at timestamptz default now());
grant usage on schema auth to authenticated,anon,service_role;
-- The canonical snapshot declares some functions before their referenced tables.
set local check_function_bodies=off;
"""
bootstrap += (root / 'supabase/schema.sql').read_text()
bootstrap += "\nset local check_function_bodies=on;\n"
# Source migrations supplying objects omitted by the canonical snapshot, followed
# by the complete coach stack. Use their real DDL, constraints, RLS and triggers.
for filename in (
    '20261007141918_add_exercise_logs.sql',
    '20260930141404_add_sparring_logs.sql',
    '20261019014437_link_sparring_detail_to_manual_sessions.sql',
    '20261018114339_add_completion_origin.sql',
    '20261116001139_coach_workspace.sql',
    '20261116001150_coach_signup_invitations.sql',
    '20261116003447_coach_self_signup.sql',
    '20261116030000_coach_weight_consent.sql',
    '20261116120000_coach_logged_sessions.sql',
    '20261116120000_coach_logged_sessions.sql',  # Safe replay preserves records.
):
    bootstrap += (root / 'supabase/migrations' / filename).read_text() + '\n'
bootstrap += 'grant all on all tables in schema public to service_role; grant all on all sequences in schema public to service_role;\n'
for filename in ('coach_workspace_permissions.sql', 'coach_signup_invitations.sql',
                 'coach_self_signup.sql', 'coach_weight_consent.sql', 'coach_logged_sessions.sql'):
    source = (root / 'tests' / filename).read_text()
    # Match the JWT role to each simulated request; fixture writes use the
    # backend role and must still pass the actual compliance triggers.
    source = source.replace('reset role;', "reset role; select set_config('request.jwt.claim.role','service_role',true);")
    for role in ('authenticated', 'anon', 'service_role'):
        source = source.replace(f'set local role {role};', f"set local role {role}; select set_config('request.jwt.claim.role','{role}',true);")
    bootstrap += "select set_config('request.jwt.claim.role','service_role',true);\n" + source + '\n'
bootstrap += '\nrollback;\n'
sql(bootstrap)
print('Full application schema: coach migrations, replay and permission assertions passed (rolled back).')
