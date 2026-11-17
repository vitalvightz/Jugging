"""Plan-free check-in upserts racing on a real PostgreSQL server.

A unique constraint treats NULL plan_ids as distinct, so only the partial key
and the upsert RPC stand between simultaneous submissions and duplicate rows.
No production credentials are used.
"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPIES = 12


@pytest.fixture(scope="module")
def dsn():
    base = os.environ.get("REHAB_TEST_DATABASE_URL")
    if not base:
        pytest.skip("set REHAB_TEST_DATABASE_URL to a disposable localhost PostgreSQL cluster")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    assert conninfo_to_dict(base).get("host") in {"localhost", "127.0.0.1", "::1"}, "test cluster must be local"
    name = "unlxck_checkin_test_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    test_dsn = make_conninfo(base, dbname=name)
    try:
        with psycopg.connect(test_dsn, autocommit=True) as setup:
            setup.execute("""
              do $$ begin
                if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon; end if;
                if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated; end if;
                if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role bypassrls; end if;
              end $$;
              create schema auth;
              create function auth.uid() returns uuid language sql
                as $f$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $f$;
              create table public.profiles (id uuid primary key);
              create table public.plans (id uuid primary key, athlete_id uuid);
              create function public.is_admin() returns boolean language sql as $f$ select false $f$;
              create function public.set_updated_at() returns trigger language plpgsql
                as $f$ begin new.updated_at = now(); return new; end $f$;
            """)
            for migration in ("20260618120000_add_today_checkins_and_completions.sql",
                              "20261019061051_allow_plan_free_today_checkins.sql",
                              "20261117030658_one_daily_checkin_per_athlete.sql"):
                setup.execute((ROOT / "supabase/migrations" / migration).read_text(encoding="utf-8"))
        yield test_dsn
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))


def _upsert(dsn, athlete_id, **fields):
    import psycopg

    payload = {"training_day": "2026-10-19", "sleep": "good", "body": "normal", "pain": "none",
               "recommendation_state": "train_as_planned", **fields}
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute("select (public.upsert_plan_free_today_checkin(%s, %s)).id",
                            (athlete_id, json.dumps(payload))).fetchone()[0]


@pytest.mark.parametrize("attempt", range(5))
def test_simultaneous_plan_free_check_ins_leave_one_row(dsn, attempt):
    import psycopg

    athlete = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into public.profiles values (%s)", (athlete,))
    barrier = threading.Barrier(COPIES)
    pains = ["none", "manageable", "high"]

    def submit(index):
        barrier.wait()
        return _upsert(dsn, athlete, pain=pains[index % 3])

    with ThreadPoolExecutor(max_workers=COPIES) as pool:
        ids = list(pool.map(submit, range(COPIES)))

    assert len(set(ids)) == 1
    with psycopg.connect(dsn, autocommit=True) as conn:
        count = conn.execute("select count(*) from public.today_checkins where athlete_id = %s", (athlete,)).fetchone()[0]
    assert count == 1


def test_plan_switch_and_plan_free_share_one_checkin(dsn):
    import psycopg
    athlete, first, second = (str(uuid4()) for _ in range(3))
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into public.profiles values (%s)", (athlete,))
        for plan in (first, second):
            conn.execute("insert into public.plans values (%s, %s)", (plan, athlete))
            conn.execute("""insert into public.today_checkins
                (athlete_id, plan_id, phase, training_day, sleep, body, pain, recommendation_state)
                values (%s, %s, 'GPP', '2026-10-19', 'good', 'normal', 'none', 'train_as_planned')
                on conflict (athlete_id, training_day) do update set plan_id = excluded.plan_id
                """, (athlete, plan))
        saved = conn.execute("select id from public.today_checkins where athlete_id = %s", (athlete,)).fetchone()[0]
    assert _upsert(dsn, athlete) == saved
    with psycopg.connect(dsn) as conn:
        assert conn.execute("select count(*) from public.today_checkins where athlete_id = %s", (athlete,)).fetchone()[0] == 1
