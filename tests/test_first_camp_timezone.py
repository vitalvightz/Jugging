"""Regression: a new athlete who earns XP can still build a first camp.

Quick Build saved the browser timezone on every Generate. The database guard
rejects timezone changes within 12 hours of training or XP activity, so the
draft save failed with "Failed to update profile." and generation never ran.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.auth import AuthenticatedUser
from api.generation_runtime import run_generation_job
from api.models import ProfileUpdateRequest
from support import FakeStage2Automator, FakeStore, _build_client, _build_request, _planner, finalized_result

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase/migrations"
GUARD_MIGRATION = MIGRATIONS / "20260803182000_guard_timezone_day_rollover.sql"
FIRST_SETUP_MIGRATION = MIGRATIONS / "20261019160000_allow_first_athlete_timezone_setup.sql"
SKIP_UNCHANGED_MIGRATION = MIGRATIONS / "20261104185957_skip_unchanged_timezone_validation.sql"
AUTH = {"Authorization": "Bearer athlete-token"}


def _guard_timezone_like_the_database(store, *, recent_activity: bool) -> None:
    """Mirror the profile timezone trigger on the fake store's update path."""

    original = store.update_profile

    def guarded(athlete_id: str, update: ProfileUpdateRequest) -> dict:
        old = (store.profiles[athlete_id].get("athlete_timezone") or "").strip()
        new = (update.athlete_timezone or "").strip()
        if update.athlete_timezone is not None and new != old and old and recent_activity:
            raise HTTPException(500, "failed to update profile")
        return original(athlete_id, update)

    store.update_profile = guarded


def _quick_build_generate(client, *, browser_timezone: str, send_timezone: bool = True):
    request = _build_request()
    request.athlete.athlete_timezone = browser_timezone
    draft = client.patch(
        "/api/onboarding/draft",
        headers=AUTH,
        json={
            "full_name": request.athlete.full_name,
            "technical_style": request.athlete.technical_style,
            **({"athlete_timezone": browser_timezone} if send_timezone else {}),
            "onboarding_draft": {"current_step": 0, "plan_source": "quick_build"},
        },
    )
    generate = client.post("/api/plans/generate", headers=AUTH, json=request.model_dump(mode="json"))
    return draft, generate


def _latest_job_timezone(store) -> str:
    job = list(store.generation_jobs.values())[-1]
    return job["request_payload"]["athlete"]["athlete_timezone"]


def test_new_athlete_with_xp_sets_first_timezone_and_generates_first_camp():
    client, store, _ = _build_client()
    assert store.profiles["athlete-1"]["athlete_timezone"] == ""
    _guard_timezone_like_the_database(store, recent_activity=True)

    draft, generate = _quick_build_generate(client, browser_timezone="America/New_York")

    assert draft.status_code == 200
    assert store.profiles["athlete-1"]["athlete_timezone"] == "America/New_York"
    assert generate.status_code == 202
    assert _latest_job_timezone(store) == "America/New_York"


def test_saved_timezone_is_not_overwritten_by_browser_zone_and_drives_generation():
    client, store, _ = _build_client()
    store.profiles["athlete-1"]["athlete_timezone"] = "Europe/London"
    _guard_timezone_like_the_database(store, recent_activity=True)

    draft, generate = _quick_build_generate(client, browser_timezone="America/New_York")

    assert draft.status_code == 200
    assert store.profiles["athlete-1"]["athlete_timezone"] == "Europe/London"
    assert generate.status_code == 202
    assert _latest_job_timezone(store) == "Europe/London"


def test_settings_timezone_change_still_hits_the_guard():
    client, store, _ = _build_client()
    store.profiles["athlete-1"]["athlete_timezone"] = "Europe/London"
    _guard_timezone_like_the_database(store, recent_activity=True)

    with pytest.raises(HTTPException):
        store.update_profile("athlete-1", ProfileUpdateRequest(athlete_timezone="Asia/Tokyo"))
    assert store.profiles["athlete-1"]["athlete_timezone"] == "Europe/London"


def test_queued_job_with_stale_timezone_keeps_saved_zone_and_refreshes_profile():
    import asyncio

    store = FakeStore()
    store.ensure_profile(AuthenticatedUser(user_id="athlete-1", email="ari@example.com", full_name="Ari Mensah", metadata={}))
    store.profiles["athlete-1"]["athlete_timezone"] = "Europe/London"
    _guard_timezone_like_the_database(store, recent_activity=True)
    request = _build_request({"athlete": {**_build_request().athlete.model_dump(), "athlete_timezone": "America/New_York"}})
    refreshes: list[ProfileUpdateRequest] = []
    guarded = store.update_profile

    def recording(athlete_id: str, update: ProfileUpdateRequest) -> dict:
        refreshes.append(update)
        return guarded(athlete_id, update)

    store.update_profile = recording
    job = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="queued-before-fix",
        source="quick_build",
        request_payload=request.model_dump(mode="json"),
    )

    asyncio.run(run_generation_job(job_id=job["id"], store=store, planner_fn=_planner,
                                   stage2=FakeStage2Automator(result=finalized_result()), active_tasks=set()))

    assert store.get_generation_job(job["id"])["status"] == "completed"
    assert store.profiles["athlete-1"]["athlete_timezone"] == "Europe/London"
    assert refreshes and all(update.athlete_timezone == "Europe/London" for update in refreshes)


# ---------------------------------------------------------------------------
# Real PostgreSQL trigger behaviour (skips without a disposable local cluster).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def timezone_db():
    dsn = os.environ.get("XP_TEST_DATABASE_URL") or os.environ.get("REHAB_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set XP_TEST_DATABASE_URL to a disposable localhost PostgreSQL cluster")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    assert conninfo_to_dict(dsn).get("host") in {"localhost", "127.0.0.1", "::1"}, "test cluster must be local"
    name = "unlxck_tz_" + uuid4().hex
    created_roles = []
    with psycopg.connect(dsn, autocommit=True) as admin:
        for role in ("anon", "authenticated", "service_role"):
            if not admin.execute("select 1 from pg_roles where rolname=%s", (role,)).fetchone():
                admin.execute(sql.SQL("create role {}").format(sql.Identifier(role)))
                created_roles.append(role)
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    local = make_conninfo(dsn, dbname=name)
    try:
        with psycopg.connect(local, autocommit=True) as db:
            db.execute("""
                create table profiles(id uuid primary key, athlete_timezone text not null default '');
                create table xp_awards(athlete_id uuid, calendar_date date, awarded_at timestamptz default now());
                create table session_completions(athlete_id uuid, updated_at timestamptz default now());
                create table today_checkins(athlete_id uuid, updated_at timestamptz default now());
            """)
            db.execute(GUARD_MIGRATION.read_text(encoding="utf-8"))
            db.execute(FIRST_SETUP_MIGRATION.read_text(encoding="utf-8"))
            db.execute(FIRST_SETUP_MIGRATION.read_text(encoding="utf-8"))
            db.execute(SKIP_UNCHANGED_MIGRATION.read_text(encoding="utf-8"))
            db.execute(SKIP_UNCHANGED_MIGRATION.read_text(encoding="utf-8"))
        yield local
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))
            for role in reversed(created_roles):
                admin.execute(sql.SQL("drop role {}").format(sql.Identifier(role)))


def _new_athlete_with_daily_xp(db) -> str:
    athlete_id = str(uuid4())
    db.execute("insert into profiles(id) values (%s)", (athlete_id,))
    db.execute("insert into xp_awards(athlete_id, calendar_date) values (%s, current_date)", (athlete_id,))
    return athlete_id


def test_postgres_first_timezone_setup_is_allowed_after_xp(timezone_db):
    import psycopg

    with psycopg.connect(timezone_db, autocommit=True) as db:
        athlete_id = _new_athlete_with_daily_xp(db)
        db.execute("update profiles set athlete_timezone='Europe/London' where id=%s", (athlete_id,))
        row = db.execute(
            "select athlete_timezone, athlete_timezone_updated_at is not null from profiles where id=%s",
            (athlete_id,),
        ).fetchone()
        assert row == ("Europe/London", True)
        # Re-saving the same zone (what every later draft save does) is a no-op.
        db.execute("update profiles set athlete_timezone='Europe/London' where id=%s", (athlete_id,))


def test_postgres_genuine_timezone_changes_stay_guarded(timezone_db):
    import psycopg

    with psycopg.connect(timezone_db, autocommit=True) as db:
        athlete_id = _new_athlete_with_daily_xp(db)
        db.execute("update profiles set athlete_timezone='Europe/London' where id=%s", (athlete_id,))
        with pytest.raises(psycopg.errors.CheckViolation, match="within 12 hours"):
            db.execute("update profiles set athlete_timezone='Asia/Tokyo' where id=%s", (athlete_id,))

        db.execute("update xp_awards set awarded_at = now() - interval '13 hours' where athlete_id=%s", (athlete_id,))
        with pytest.raises(psycopg.errors.CheckViolation, match="once every 24 hours"):
            db.execute("update profiles set athlete_timezone='Asia/Tokyo' where id=%s", (athlete_id,))

        with pytest.raises(psycopg.errors.CheckViolation, match="cannot be cleared"):
            db.execute("update profiles set athlete_timezone='' where id=%s", (athlete_id,))
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            db.execute("update profiles set athlete_timezone='Mars/Olympus' where id=%s", (athlete_id,))


def test_postgres_unchanged_timezone_saves_skip_validation_and_keep_the_stamp(timezone_db):
    import psycopg

    with psycopg.connect(timezone_db, autocommit=True) as db:
        athlete_id = _new_athlete_with_daily_xp(db)
        db.execute("update profiles set athlete_timezone='Europe/London' where id=%s", (athlete_id,))
        stamp = db.execute("select athlete_timezone_updated_at from profiles where id=%s", (athlete_id,)).fetchone()[0]

        # A profile save resends the same zone (padded here) right after training:
        # not a change, so neither the activity nor the 24-hour guard applies.
        db.execute("update profiles set athlete_timezone=' Europe/London ' where id=%s", (athlete_id,))
        row = db.execute(
            "select athlete_timezone, athlete_timezone_updated_at from profiles where id=%s", (athlete_id,)
        ).fetchone()
        assert row == ("Europe/London", stamp)

        # The zone lookup is skipped only for an unchanged value.
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            db.execute("update profiles set athlete_timezone='Mars/Olympus' where id=%s", (athlete_id,))
