"""Manual exercise entry lifecycle, limits and RLS on a real PostgreSQL server.

The triggers are the last line of defence: an entry always carries its parent's
day, never outlives a deleted parent, never moves between sessions or owners,
and a retry never adds a second occurrence. No production credentials are used.
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ("20261010184612_add_external_training_sessions.sql", "20261019103258_add_external_training_takeaway.sql",
              "20261019121808_add_external_training_discipline.sql", "20261019180404_add_manual_exercise_entries.sql")


@pytest.fixture(scope="module")
def dsn():
    base = os.environ.get("REHAB_TEST_DATABASE_URL")
    if not base:
        pytest.skip("set REHAB_TEST_DATABASE_URL to a disposable localhost PostgreSQL cluster")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    assert conninfo_to_dict(base).get("host") in {"localhost", "127.0.0.1", "::1"}, "test cluster must be local"
    name = "unlxck_manual_exercise_test_" + uuid4().hex
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
              grant usage on schema auth to authenticated;
              create function auth.uid() returns uuid language sql
                as $f$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $f$;
              create table public.profiles (id uuid primary key);
              create function public.set_updated_at() returns trigger language plpgsql
                as $f$ begin new.updated_at = now(); return new; end $f$;
            """)
            for migration in MIGRATIONS:
                setup.execute((ROOT / "supabase/migrations" / migration).read_text(encoding="utf-8"))
            # Repeatable: applying the new migration twice is harmless.
            setup.execute((ROOT / "supabase/migrations" / MIGRATIONS[-1]).read_text(encoding="utf-8"))
        yield test_dsn
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))


@pytest.fixture
def db(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        yield connection


def athlete(db):
    athlete_id = uuid4()
    db.execute("insert into public.profiles (id) values (%s)", (athlete_id,))
    return athlete_id


def parent(db, athlete_id, day="2026-10-19"):
    return db.execute("""insert into public.external_training_sessions
        (athlete_id, client_request_id, request_fingerprint, training_day, activity, duration_minutes, effort)
        values (%s, %s, %s, %s, 'strength', 60, 'hard') returning id""",
        (athlete_id, uuid4(), "a" * 64, day)).fetchone()[0]


def insert(db, athlete_id, session_id, entry_id=None, *, day="2001-01-01", key="back-squat", personal=None, load=80):
    return db.execute("""insert into public.manual_exercise_entries
        (athlete_id, external_session_id, client_entry_id, training_day, exercise_key, personal_exercise_id,
         display_name, measurement_format, actual)
        values (%s, %s, %s, %s, %s, %s, 'Back Squat', 'strength', %s)
        on conflict (athlete_id, client_entry_id) do nothing returning id""",
        (athlete_id, session_id, entry_id or uuid4(), day, None if personal else key, personal,
         json.dumps({"load": {"value": load, "unit": "kg"}}))).fetchone()


def entries(db, session_id):
    return db.execute("""select training_day::text, deleted_at is not null from public.manual_exercise_entries
        where external_session_id = %s order by created_at""", (session_id,)).fetchall()


def test_day_comes_from_the_parent_and_retries_never_duplicate(db):
    owner = athlete(db)
    session_id = parent(db, owner)
    entry_id = uuid4()
    assert insert(db, owner, session_id, entry_id) is not None
    assert insert(db, owner, session_id, entry_id) is None
    assert entries(db, session_id) == [("2026-10-19", False)]


def test_concurrent_retries_of_one_entry_store_one_occurrence(dsn, db):
    import psycopg

    owner = athlete(db)
    session_id = parent(db, owner)
    entry_id = uuid4()

    def attempt(_):
        with psycopg.connect(dsn, autocommit=True) as connection:
            return insert(connection, owner, session_id, entry_id)

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(result is not None for result in results) == 1
    assert len(entries(db, session_id)) == 1


def test_no_entry_under_a_deleted_or_another_athletes_session(db):
    import psycopg

    owner, other = athlete(db), athlete(db)
    session_id = parent(db, owner)
    with pytest.raises(psycopg.errors.Error):
        insert(db, other, session_id)
    db.execute("update public.external_training_sessions set deleted_at = now() where id = %s", (session_id,))
    with pytest.raises(psycopg.errors.Error):
        insert(db, owner, session_id)


def test_parent_delete_and_date_change_reach_entries_and_nothing_undeletes(db):
    owner = athlete(db)
    session_id = parent(db, owner)
    entry_id = uuid4()
    insert(db, owner, session_id, entry_id)
    db.execute("update public.external_training_sessions set training_day = '2026-10-18' where id = %s", (session_id,))
    assert entries(db, session_id) == [("2026-10-18", False)]
    # An entry update cannot move its day, session, owner or retry key.
    other_session = parent(db, owner)
    db.execute("""update public.manual_exercise_entries set training_day = '2020-01-01', external_session_id = %s,
        client_entry_id = %s where client_entry_id = %s""", (other_session, uuid4(), entry_id))
    assert entries(db, session_id) == [("2026-10-18", False)]
    db.execute("update public.external_training_sessions set deleted_at = now() where id = %s", (session_id,))
    assert entries(db, session_id) == [("2026-10-18", True)]
    db.execute("update public.manual_exercise_entries set deleted_at = null where external_session_id = %s", (session_id,))
    assert entries(db, session_id) == [("2026-10-18", True)]
    # A retry of the deleted entry is a no-op, never a resurrection.
    assert insert(db, owner, session_id, entry_id) is None


def test_revisions_start_at_one_and_only_step_forward(db):
    owner = athlete(db)
    session_id = parent(db, owner)
    entry_id = uuid4()
    insert(db, owner, session_id, entry_id)

    def revision():
        return db.execute("select revision from public.manual_exercise_entries where client_entry_id = %s", (entry_id,)).fetchone()[0]

    assert revision() == 1
    # Compare-and-set as the store does it: a stale base revision changes nothing.
    update = """update public.manual_exercise_entries set actual = %s, revision = %s
                where client_entry_id = %s and revision = %s returning id"""
    assert db.execute(update, (json.dumps({"load": {"value": 90, "unit": "kg"}}), 2, entry_id, 1)).fetchone()
    assert db.execute(update, (json.dumps({"load": {"value": 80, "unit": "kg"}}), 2, entry_id, 1)).fetchone() is None
    assert revision() == 2
    # Skips and rewinds are ignored; parent sync never bumps it.
    db.execute("update public.manual_exercise_entries set revision = 9 where client_entry_id = %s", (entry_id,))
    db.execute("update public.manual_exercise_entries set revision = 1 where client_entry_id = %s", (entry_id,))
    db.execute("update public.external_training_sessions set training_day = '2026-10-18' where id = %s", (session_id,))
    assert revision() == 2
    assert db.execute("select actual->'load'->>'value' from public.manual_exercise_entries where client_entry_id = %s",
                      (entry_id,)).fetchone()[0] == "90"


def test_thirty_live_entries_per_session_but_an_existing_retry_still_passes(db):
    import psycopg

    owner = athlete(db)
    session_id = parent(db, owner)
    first = uuid4()
    insert(db, owner, session_id, first)
    for _ in range(29):
        insert(db, owner, session_id)
    assert insert(db, owner, session_id, first) is None
    with pytest.raises(psycopg.errors.Error):
        insert(db, owner, session_id)


def test_exactly_one_identity_and_private_names_stay_with_their_owner(db):
    import psycopg

    owner, other = athlete(db), athlete(db)
    session_id = parent(db, owner)
    mine = db.execute("insert into public.personal_exercises (athlete_id, name, name_key) values (%s, 'Drill', 'drill') returning id",
                      (owner,)).fetchone()[0]
    theirs = db.execute("insert into public.personal_exercises (athlete_id, name, name_key) values (%s, 'Drill', 'drill') returning id",
                        (other,)).fetchone()[0]
    assert insert(db, owner, session_id, personal=mine) is not None
    with pytest.raises(psycopg.errors.Error):
        insert(db, owner, session_id, personal=theirs)
    with pytest.raises(psycopg.errors.Error):
        db.execute("insert into public.personal_exercises (athlete_id, name, name_key) values (%s, 'drill', 'drill')", (owner,))
    with pytest.raises(psycopg.errors.Error):
        db.execute("""insert into public.manual_exercise_entries (athlete_id, external_session_id, client_entry_id,
            training_day, exercise_key, personal_exercise_id, display_name, measurement_format)
            values (%s, %s, %s, '2026-10-19', 'back-squat', %s, 'x', 'strength')""", (owner, session_id, uuid4(), mine))


def test_athletes_read_only_their_own_live_rows_and_cannot_write(dsn, db):
    import psycopg

    owner, other = athlete(db), athlete(db)
    live, gone = parent(db, owner), parent(db, owner)
    insert(db, owner, live)
    insert(db, owner, gone)
    db.execute("update public.external_training_sessions set deleted_at = now() where id = %s", (gone,))
    insert(db, other, parent(db, other))
    with psycopg.connect(dsn) as connection:
        connection.execute("set local role authenticated")
        connection.execute("select set_config('request.jwt.claim.sub', %s, true)", (str(owner),))
        rows = connection.execute("select athlete_id, external_session_id from public.manual_exercise_entries").fetchall()
        assert rows == [(owner, live)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("""insert into public.manual_exercise_entries (athlete_id, external_session_id, client_entry_id,
                training_day, exercise_key, display_name, measurement_format) values (%s, %s, %s, '2026-10-19', 'x', 'x', 'strength')""",
                (owner, live, uuid4()))
