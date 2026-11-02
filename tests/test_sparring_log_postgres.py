"""record_sparring_log against a real PostgreSQL server: retries and races.

PGlite has a single connection, so it cannot race two requests. This runs the
migrations on a disposable localhost cluster and fires the same report, and
competing corrections, from separate connections at the same instant. No
production credentials are used.
"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    "20261010184612_add_external_training_sessions.sql",
    "20260930141404_add_sparring_logs.sql",
    "20261019014437_link_sparring_detail_to_manual_sessions.sql",
    "20261019103324_protect_sparring_head_contact.sql",
    "20261102010203_add_sparring_focus.sql",
)
COPIES = 12


@pytest.fixture(scope="module")
def dsn():
    base = os.environ.get("SPARRING_TEST_DATABASE_URL") or os.environ.get("REHAB_TEST_DATABASE_URL")
    if not base:
        pytest.skip("set REHAB_TEST_DATABASE_URL to a disposable localhost PostgreSQL cluster")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    assert conninfo_to_dict(base).get("host") in {"localhost", "127.0.0.1", "::1"}, "test cluster must be local"
    name = "unlxck_sparring_test_" + uuid4().hex
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
              create table public.admin_reviews (id uuid primary key default gen_random_uuid(), athlete_id uuid not null,
                reason text not null, status text not null default 'pending', created_at timestamptz not null default now());
              create function public.set_updated_at() returns trigger language plpgsql
                as $f$ begin new.updated_at = now(); return new; end $f$;
            """)
            for migration in MIGRATIONS:
                setup.execute((ROOT / "supabase/migrations" / migration).read_text(encoding="utf-8"))
        yield test_dsn
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))


@pytest.fixture
def athlete(dsn):
    import psycopg

    athlete_id = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into public.profiles values (%s)", (athlete_id,))
    return athlete_id


def _session(dsn, athlete_id):
    import psycopg

    session_id = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """insert into public.external_training_sessions
               (id, athlete_id, client_request_id, request_fingerprint, training_day, activity, duration_minutes, effort)
               values (%s, %s, gen_random_uuid(), repeat('a', 64), '2026-10-17', 'sparring', 45, 'hard')""",
            (session_id, athlete_id),
        )
    return session_id


def _record(dsn, athlete_id, *, reason=None, **fields):
    import psycopg

    payload = {"source": "free", "training_day": "2026-10-19", "intensity": "hard", "rounds_completed": 6,
               "head_contact": "light", "rocked": False, "notes": "", **fields}
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute("select public.record_sparring_log(%s, %s, %s)",
                            (athlete_id, json.dumps(payload), reason)).fetchone()[0]


def _at_once(calls):
    """Run every call on its own connection, released together."""
    barrier = threading.Barrier(len(calls))

    def run(call):
        barrier.wait()
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def _row(dsn, query, *params):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(query, params).fetchone()


def _scalar(dsn, query, *params):
    return _row(dsn, query, *params)[0]


@pytest.mark.parametrize("attempt", range(5))
def test_simultaneous_copies_of_one_report_write_once(dsn, athlete, attempt):
    session = _session(dsn, athlete)
    request = str(uuid4())
    results = _at_once([
        lambda: _record(dsn, athlete, reason="rocked", external_session_id=session,
                        client_request_id=request, detail_revision=1, rocked=True)
        for _ in range(COPIES)
    ])

    assert len({result["log"]["id"] for result in results}) == 1
    assert sum(not result["replayed"] for result in results) == 1
    assert len({result["review"]["id"] for result in results}) == 1
    assert _scalar(dsn, "select count(*) from public.sparring_logs where external_session_id = %s", session) == 1
    assert _scalar(dsn, "select count(*) from public.admin_reviews where athlete_id = %s", athlete) == 1
    assert _scalar(dsn, "select count(*) from public.sparring_log_requests where client_request_id = %s", request) == 1


@pytest.mark.parametrize("attempt", range(5))
def test_competing_corrections_keep_the_newest_and_one_review(dsn, athlete, attempt):
    session = _session(dsn, athlete)
    _at_once([
        (lambda revision=revision: _record(dsn, athlete, reason="rocked" if revision % 3 == 0 else None,
                                          external_session_id=session, client_request_id=str(uuid4()),
                                          detail_revision=revision, rounds_completed=revision,
                                          rocked=revision % 3 == 0))
        for revision in range(1, COPIES + 1)
    ])

    rounds, revision, rocked, log_id = _row(
        dsn, "select rounds_completed, detail_revision, rocked, id::text "
             "from public.sparring_logs where external_session_id = %s", session)
    assert (rounds, revision, rocked) == (COPIES, COPIES, True)
    assert _scalar(dsn, "select count(*) from public.admin_reviews where athlete_id = %s", athlete) == 1
    assert _scalar(dsn, "select count(*) from public.sparring_log_requests where sparring_log_id = %s", log_id) == COPIES


@pytest.mark.parametrize("attempt", range(5))
def test_delayed_retries_of_a_never_overwrite_correction_b(dsn, athlete, attempt):
    session = _session(dsn, athlete)
    report_a = str(uuid4())
    _record(dsn, athlete, external_session_id=session, client_request_id=report_a,
            detail_revision=1, rounds_completed=3)
    _at_once(
        [lambda: _record(dsn, athlete, external_session_id=session, client_request_id=str(uuid4()),
                         detail_revision=2, rounds_completed=8)]
        + [lambda: _record(dsn, athlete, external_session_id=session, client_request_id=report_a,
                           detail_revision=1, rounds_completed=3) for _ in range(COPIES - 1)]
    )
    # And once more after B has certainly landed.
    stale = _record(dsn, athlete, external_session_id=session, client_request_id=report_a,
                    detail_revision=1, rounds_completed=3)

    assert stale["replayed"] is True
    assert stale["log"]["rounds_completed"] == 8
    assert _scalar(dsn, "select rounds_completed from public.sparring_logs where external_session_id = %s",
                   session) == 8


def test_a_later_correction_cannot_lower_head_contact_or_withdraw_rocked(dsn, athlete):
    session = _session(dsn, athlete)
    _record(dsn, athlete, reason="rocked", external_session_id=session, client_request_id=str(uuid4()),
            detail_revision=1, head_contact="heavy", rocked=True, rounds_completed=6)
    corrected = _record(dsn, athlete, external_session_id=session, client_request_id=str(uuid4()),
                        detail_revision=2, head_contact="none", rocked=False, rounds_completed=4, notes="Was 4")

    assert corrected["log"]["rounds_completed"] == 4
    assert corrected["log"]["notes"] == "Was 4"
    assert corrected["log"]["head_contact"] == "heavy"
    assert corrected["log"]["rocked"] is True
    assert corrected["review"] is not None
    assert _scalar(dsn, "select count(*) from public.admin_reviews where athlete_id = %s", athlete) == 1


def test_a_later_correction_can_raise_head_contact(dsn, athlete):
    session = _session(dsn, athlete)
    _record(dsn, athlete, external_session_id=session, client_request_id=str(uuid4()),
            detail_revision=1, head_contact="none")
    corrected = _record(dsn, athlete, external_session_id=session, client_request_id=str(uuid4()),
                        detail_revision=2, head_contact="heavy")

    assert corrected["log"]["head_contact"] == "heavy"


def test_focus_sits_beside_the_rpc_and_survives_a_correction(dsn, athlete):
    import psycopg

    session = _session(dsn, athlete)
    first = _record(dsn, athlete, external_session_id=session, client_request_id=str(uuid4()), detail_revision=1)
    assert first["log"]["focus"] == ""
    # The API writes the focus with a plain update; the RPC never takes it.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("update public.sparring_logs set focus = %s where id = %s", ("Hands up", first["log"]["id"]))
        try:
            conn.execute("update public.sparring_logs set focus = %s where id = %s", ("x" * 141, first["log"]["id"]))
            raise AssertionError("an over-long focus was accepted")
        except psycopg.errors.CheckViolation:
            pass
    corrected = _record(dsn, athlete, external_session_id=session, client_request_id=str(uuid4()),
                        detail_revision=2, rounds_completed=4)
    assert corrected["log"]["rounds_completed"] == 4
    assert corrected["log"]["focus"] == "Hands up"
