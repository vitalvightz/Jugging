"""History removal and run-detail checks against real PostgreSQL.

Applies the real migrations on a disposable localhost cluster. The rehab reset
guard is reproduced by a stand-in trigger raising the same error.
"""
import os
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    "20261010184612_add_external_training_sessions.sql",
    "20261019150000_add_external_training_discipline.sql",
    "20261102010200_add_external_training_run_distance.sql",
    "20261102999999_history_removal_and_run_checks.sql",
)


@pytest.fixture(scope="module")
def dsn():
    base = os.environ.get("REHAB_TEST_DATABASE_URL")
    if not base:
        pytest.skip("set REHAB_TEST_DATABASE_URL to a disposable localhost PostgreSQL cluster")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    assert conninfo_to_dict(base).get("host") in {"localhost", "127.0.0.1", "::1"}, "test cluster must be local"
    name = "unlxck_history_test_" + uuid4().hex
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
              create function auth.uid() returns uuid language sql as $f$ select null::uuid $f$;
              create table public.profiles (id uuid primary key);
              create table public.plans (id uuid primary key);
              create function public.set_updated_at() returns trigger language plpgsql
                as $f$ begin new.updated_at = now(); return new; end $f$;
              create table public.session_completions (
                id uuid primary key default gen_random_uuid(), athlete_id uuid not null, plan_id uuid not null,
                session_id text not null, training_day date not null, status text not null default 'not_started',
                session_rpe integer, pain_after integer, modification_reason text not null default '',
                notes text not null default '', started_at timestamptz, completed_at timestamptz,
                prescription_snapshot jsonb);
              create table public.exercise_logs (
                id uuid primary key default gen_random_uuid(), athlete_id uuid not null, plan_id uuid,
                session_id text, block_id text not null, training_day date not null);
              -- Stand-in for public.preserve_started_prescription's rehab guard.
              create function public.stand_in_rehab_guard() returns trigger language plpgsql as $f$
              begin
                if old.status in ('done','modified') and new.status not in ('done','modified')
                   and old.prescription_snapshot ? 'policy' then
                  raise exception 'rehab_exposure_cannot_be_reset' using errcode = '23514';
                end if;
                return new;
              end $f$;
              create trigger stand_in_rehab_guard before update on public.session_completions
                for each row execute function public.stand_in_rehab_guard();
            """)
            for migration in MIGRATIONS:
                setup.execute((ROOT / "supabase/migrations" / migration).read_text(encoding="utf-8"))
        yield test_dsn
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))


def _seed(dsn, *, snapshot=None):
    import json

    import psycopg

    athlete, plan = str(uuid4()), str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        completion = conn.execute(
            "insert into public.session_completions (athlete_id, plan_id, session_id, training_day, status, session_rpe,"
            " completed_at, prescription_snapshot) values (%s, %s, 's1', '2026-11-01', 'done', 7, now(), %s) returning id::text",
            (athlete, plan, json.dumps(snapshot) if snapshot else None)).fetchone()[0]
        for session_id in ("s1", "s2"):
            conn.execute("insert into public.exercise_logs (athlete_id, plan_id, session_id, block_id, training_day)"
                         " values (%s, %s, %s, 'b1', '2026-11-01')", (athlete, plan, session_id))
    return athlete, completion


def _call(dsn, athlete, completion):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute("select public.reset_history_plan_session(%s, %s)", (athlete, completion)).fetchone()[0]


def _logs(dsn, athlete):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        return sorted(row[0] for row in conn.execute(
            "select session_id from public.exercise_logs where athlete_id = %s", (athlete,)).fetchall())


def test_removal_resets_the_completion_and_its_results_together(dsn):
    athlete, completion = _seed(dsn)
    row = _call(dsn, athlete, completion)
    assert (row["status"], row["session_rpe"], row["completed_at"], row["id"]) == ("not_started", None, None, completion)
    assert _logs(dsn, athlete) == ["s2"]
    # Repeating it is a no-op.
    assert _call(dsn, athlete, completion)["status"] == "not_started"
    assert _logs(dsn, athlete) == ["s2"]


def test_a_refused_reset_keeps_the_exercise_results(dsn):
    import psycopg

    athlete, completion = _seed(dsn, snapshot={"policy": "rehab"})
    with pytest.raises(psycopg.errors.CheckViolation, match="rehab_exposure_cannot_be_reset"):
        _call(dsn, athlete, completion)
    assert _logs(dsn, athlete) == ["s1", "s2"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute("select status from public.session_completions where id = %s",
                            (completion,)).fetchone()[0] == "done"


def test_another_athletes_completion_is_untouched(dsn):
    athlete, completion = _seed(dsn)
    assert _call(dsn, str(uuid4()), completion) is None
    assert _logs(dsn, athlete) == ["s1", "s2"]


@pytest.mark.parametrize(("activity", "distance", "unit", "seconds"), [
    ("roadwork", 5, None, None),
    ("roadwork", None, "km", None),
    ("strength", None, None, 1475),
])
def test_run_checks_reject_partial_distance_and_non_roadwork_time(dsn, activity, distance, unit, seconds):
    import psycopg

    athlete = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into public.profiles values (%s)", (athlete,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "insert into public.external_training_sessions (athlete_id, client_request_id, request_fingerprint,"
                " training_day, activity, duration_minutes, distance_value, distance_unit, duration_seconds, effort)"
                " values (%s, gen_random_uuid(), repeat('a', 64), '2026-11-01', %s, 25, %s, %s, %s, 'easy')",
                (athlete, activity, distance, unit, seconds))
