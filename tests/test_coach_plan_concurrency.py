"""Real PostgreSQL consent races; only a disposable localhost database is used.

COACH_PLAN_TEST_DATABASE_URL=postgresql://postgres:...@127.0.0.1:55433/postgres
"""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
from threading import Barrier
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'supabase/migrations/20261116145410_coach_plan_assignments.sql'


@pytest.fixture
def db():
    dsn = os.getenv('COACH_PLAN_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set COACH_PLAN_TEST_DATABASE_URL to disposable localhost PostgreSQL')
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    assert conninfo_to_dict(dsn).get('host') in {'localhost', '127.0.0.1', '::1'}
    name = 'coach_plan_test_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        for role in ('anon', 'authenticated', 'service_role'):
            if not admin.execute('select 1 from pg_roles where rolname=%s', (role,)).fetchone():
                admin.execute(sql.SQL('create role {}').format(sql.Identifier(role)))
        admin.execute(sql.SQL('create database {}').format(sql.Identifier(name)))
    test_dsn = make_conninfo(dsn, dbname=name)
    try:
        with psycopg.connect(test_dsn, autocommit=True) as setup:
            # Minimal Auth/Profile platform fixtures, real plans/drafts/connection
            # table DDL and the production migration. Full-schema permission
            # coverage is also in tools/test_coach_workspace.py.
            setup.execute("""create schema auth;
                create function auth.uid() returns uuid language sql as $$ select null::uuid $$;
                create table public.profiles(id uuid primary key, role text not null, full_name text,
                  access_status text default 'approved', active_plan_id uuid);
                create table public.athlete_intakes(id uuid primary key);
                create function public.set_updated_at() returns trigger language plpgsql as $$
                  begin new.updated_at=now(); return new; end $$;
            """)
            schema = (ROOT / 'supabase/schema.sql').read_text()
            setup.execute(re.search(r'create table if not exists public\.plans \([\s\S]*?\n\);', schema)[0])
            setup.execute(re.search(r'create table public\.coach_connections \([\s\S]*?\n\);',
                (ROOT / 'supabase/migrations/20261116001139_coach_workspace.sql').read_text())[0])
            setup.execute((ROOT / 'supabase/migrations/20261011164637_add_custom_training_plans.sql').read_text())
            setup.execute(MIGRATION.read_text())
            setup.execute((ROOT / "supabase/migrations/20261116171039_dated_custom_plans.sql").read_text())
        yield test_dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL('drop database {} with (force)').format(sql.Identifier(name)))


def fixture(dsn, count=2):
    import psycopg
    coach, fighter = str(uuid4()), str(uuid4())
    drafts, plans = [str(uuid4()) for _ in range(count)], [str(uuid4()) for _ in range(count)]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into profiles(id,role,full_name) values(%s,'coach','Coach'),(%s,'athlete','Fighter')", (coach, fighter))
        conn.execute("insert into coach_connections(coach_id,athlete_id,invited_email,status,accepted_at) values(%s,%s,'test@test.invalid','accepted',now())", (coach, fighter))
        for draft, plan in zip(drafts, plans):
            conn.execute("select coach_plan_command(%s,%s,%s,'create','{\"name\":\"Week\",\"sessions\":[],\"incomplete\":false}')", (coach, fighter, draft))
            conn.execute("select coach_plan_command(%s,%s,%s,'assign')", (coach, fighter, draft))
            conn.execute("insert into plans(id,athlete_id,status,planning_brief) values(%s,%s,'review_required',%s)",
                (plan, fighter, '{"coach_assignment_id":"' + draft + '"}'))
    return coach, fighter, drafts, plans


def response(dsn, fighter, draft, action, plan=None, expected=None, replace=False, barrier=None):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("set statement_timeout='8s'")
        if barrier:
            barrier.wait(timeout=5)
        return conn.execute('select respond_coach_plan(%s,%s,%s,%s,%s,%s)',
            (fighter, draft, action, plan, expected, replace)).fetchone()[0]


def test_concurrent_different_activations_have_one_winner(db):
    import psycopg
    _, fighter, drafts, plans = fixture(db)
    barrier = Barrier(2)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda i: response(db, fighter, drafts[i], 'activate', plans[i], barrier=barrier), range(2)))
    assert sorted(results) == [False, True]
    winner = results.index(True)
    with psycopg.connect(db) as conn:
        assert str(conn.execute('select active_plan_id from profiles where id=%s', (fighter,)).fetchone()[0]) == plans[winner]
        assert conn.execute('select count(*) from custom_training_plans where accepted_at is not null').fetchone()[0] == 1
        assert conn.execute("select count(*) from plans where status='review_required'").fetchone()[0] == 1


def test_duplicate_activation_and_decline_races(db):
    _, fighter, drafts, plans = fixture(db)
    barrier = Barrier(2)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: response(db, fighter, drafts[0], 'activate', plans[0], barrier=barrier), range(2)))
    assert results == [True, True]
    barrier = Barrier(2)
    with ThreadPoolExecutor(2) as pool:
        activate = pool.submit(response, db, fighter, drafts[1], 'activate', plans[1], plans[0], False, barrier)
        decline = pool.submit(response, db, fighter, drafts[1], 'decline', None, None, False, barrier)
        assert sorted([activate.result(), decline.result()]) == [False, True]


def test_revocation_wins_against_in_flight_coach_write(db):
    import psycopg
    coach, fighter, drafts, _ = fixture(db)
    with psycopg.connect(db) as revocation, ThreadPoolExecutor(1) as pool:
        revocation.execute("update coach_connections set status='revoked' where coach_id=%s", (coach,))
        def assign():
            with psycopg.connect(db, autocommit=True) as conn:
                conn.execute("set statement_timeout='8s'")
                conn.execute("select coach_plan_command(%s,%s,%s,'assign')", (coach, fighter, drafts[0]))
        pending = pool.submit(assign)
        revocation.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pending.result(timeout=10)


def test_failed_replacement_rolls_back_consent_and_pointer(db):
    import psycopg
    _, fighter, drafts, plans = fixture(db)
    assert response(db, fighter, drafts[0], 'activate', plans[0])
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("""create function reject_archive() returns trigger language plpgsql as $$ begin
          if new.status='archived' then raise exception 'archive unavailable'; end if; return new; end $$;
          create trigger reject_archive before update on plans for each row execute function reject_archive();""")
    with pytest.raises(psycopg.errors.RaiseException):
        response(db, fighter, drafts[1], 'activate', plans[1], plans[0], True)
    with psycopg.connect(db) as conn:
        assert str(conn.execute('select active_plan_id from profiles where id=%s', (fighter,)).fetchone()[0]) == plans[0]
        assert conn.execute('select accepted_at from custom_training_plans where id=%s', (drafts[1],)).fetchone()[0] is None
        assert conn.execute('select status from plans where id=%s', (plans[1],)).fetchone()[0] == 'review_required'


def test_revocation_blocks_pending_acceptance_without_releasing_the_plan(db):
    import psycopg
    coach, fighter, drafts, plans = fixture(db)
    with psycopg.connect(db) as revocation, ThreadPoolExecutor(1) as pool:
        revocation.execute("update coach_connections set status='revoked' where coach_id=%s", (coach,))
        pending = pool.submit(response, db, fighter, drafts[0], 'activate', plans[0])
        revocation.commit()
        assert pending.result(timeout=10) is False
    with psycopg.connect(db) as conn:
        assert conn.execute('select active_plan_id from profiles where id=%s', (fighter,)).fetchone()[0] is None
        assert conn.execute('select status from plans where id=%s', (plans[0],)).fetchone()[0] == 'review_required'
