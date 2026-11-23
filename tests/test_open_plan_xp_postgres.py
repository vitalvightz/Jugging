"""Real local PostgreSQL completion + XP projection/trigger integration.

Set XP_TEST_DATABASE_URL (or CI's REHAB_TEST_DATABASE_URL) to a disposable
localhost cluster. Each module run creates/drops its own database. These tests
never connect to Supabase or use production credentials.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.services.effective_structured_plan import resolve_effective_structured_plan
from api.services.open_plan_timeline import open_plan_anchor_date, project_open_structured_plan
from api.services.progress_notifications import award_session_progress
from api.services.today_service import upsert_session_completion
from api.services.week_progress import _planned_session_ids, award_completed_week
from test_open_plan_programming_fidelity import _reference_row

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase/migrations"
MIGRATION = MIGRATIONS / "20261016143502_align_open_plan_xp_calendar.sql"
COMPLETION_ORIGIN_MIGRATION = MIGRATIONS / "20261018155432_add_completion_origin.sql"
OWNER = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(scope="module")
def xp_database():
    dsn = os.environ.get("XP_TEST_DATABASE_URL") or os.environ.get("REHAB_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set XP_TEST_DATABASE_URL to a disposable localhost PostgreSQL cluster")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    assert conninfo_to_dict(dsn).get("host") in {"localhost", "127.0.0.1", "::1"}, "test cluster must be local"
    name = "unlxck_open_xp_" + uuid4().hex
    created_roles = []
    with psycopg.connect(dsn, autocommit=True) as admin:
        for role in ("anon", "authenticated", "service_role"):
            if not admin.execute("select 1 from pg_roles where rolname=%s", (role,)).fetchone():
                admin.execute(sql.SQL("create role {} {}").format(
                    sql.Identifier(role), sql.SQL("bypassrls" if role == "service_role" else "")))
                created_roles.append(role)
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    local = make_conninfo(dsn, dbname=name)
    try:
        schema = (ROOT / "supabase/schema.sql").read_text(encoding="utf-8")

        def table(name):
            return re.search(rf"create table if not exists public\.{name} \([\s\S]*?\n\);", schema)[0]

        with psycopg.connect(local, autocommit=True) as db:
            db.execute("""
                create schema auth;
                create function auth.uid() returns uuid language sql stable as $$
                  select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
                create function auth.role() returns text language sql stable as $$ select current_user::text $$;
                grant usage on schema auth,public to anon,authenticated,service_role;
                create table profiles(id uuid primary key, role text default 'athlete',
                  full_name text default 'Test athlete', technical_style text[] default '{mma}',
                  athlete_timezone text default 'UTC', active_plan_id uuid);
                create table athlete_intakes(id uuid primary key, athlete_id uuid references profiles(id));
            """)
            db.execute(table("plans") + table("session_completions") + table("today_checkins"))
            # Extracting CREATE TABLE omits subsequent clinical-column ALTERs.
            # Keep this disposable database independent of hosted schema state,
            # then exercise the actual provenance migration (including replay).
            db.execute(re.search(
                r"alter table public\.session_completions\s+add column if not exists prescription_snapshot[\s\S]*?;",
                schema,
            )[0])
            for _ in range(2):
                db.execute(COMPLETION_ORIGIN_MIGRATION.read_text(encoding="utf-8"))
            # Unrelated source tables are required by the real ledger RPCs.
            db.execute("create table injury_flags(id uuid primary key, athlete_id uuid, updated_at timestamptz)")
            milestones = (MIGRATIONS / "20260803123000_add_plan_progress_milestones.sql").read_text(encoding="utf-8")
            db.execute(re.search(r"create table if not exists public\.plan_milestones \([\s\S]*?\n\);", milestones)[0])
            for filename in (
                "20260801203204_add_account_xp_ledger.sql",
                "20260803115000_expand_xp_actions.sql",
                "20260803123000_add_plan_progress_milestones.sql",
                "20260803174400_prepare_xp_legacy_compatibility.sql",
                "20260803174500_harden_xp_abuse_boundaries.sql",
                "20260803174600_cap_feedback_xp.sql",
                "20260803175500_enforce_xp_source_integrity.sql",
                "20260803180000_remove_xp_session_sample_cap.sql",
                "20260803181000_lock_xp_to_one_plan_per_day.sql",
                "20260803182000_guard_timezone_day_rollover.sql",
                "20260803183000_add_xp_rollout_projection_helpers.sql",
                "20260803183100_validate_xp_sources_with_projected_weeks.sql",
                "20260803183200_enforce_projected_full_week_completion.sql",
                "20260803183300_keep_legacy_xp_calls_compatible.sql",
                "20260803183400_scope_open_plan_phase_rewards.sql",
                "20260803183500_finalize_xp_rollout_readiness.sql",
                "20260803212533_remove_stale_xp_legacy_constraints.sql",
            ):
                db.execute((MIGRATIONS / filename).read_text(encoding="utf-8"))
            db.execute(MIGRATION.read_text(encoding="utf-8"))
            db.execute(MIGRATION.read_text(encoding="utf-8"))
            db.execute("grant select,insert,update,delete on all tables in schema public to service_role")
        yield local
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))
            for role in reversed(created_roles):
                admin.execute(sql.SQL("drop role {}").format(sql.Identifier(role)))


class PostgresXPStore:
    """Only the persistence methods used by the real completion/XP services."""

    def __init__(self, db):
        self.db = db

    def get_plan_for_athlete(self, plan_id, athlete_id):
        row = self.db.execute("select to_jsonb(p) from plans p where id=%s and athlete_id=%s", (plan_id, athlete_id)).fetchone()
        return row[0] if row else None

    def get_session_completion(self, athlete_id, session_id, training_day):
        row = self.db.execute("select to_jsonb(c) from session_completions c where athlete_id=%s and session_id=%s and training_day=%s",
                              (athlete_id, session_id, training_day)).fetchone()
        return row[0] if row else None

    def get_training_plan_for_athlete(self, plan_id, athlete_id):
        return self.get_plan_for_athlete(plan_id, athlete_id)

    def upsert_session_completion(self, athlete_id, fields):
        from psycopg import sql
        values = {"athlete_id": athlete_id, **fields}
        columns = sql.SQL(',').join(map(sql.Identifier, values))
        update = sql.SQL(',').join(sql.SQL('{c}=excluded.{c}').format(c=sql.Identifier(key)) for key in fields)
        statement = sql.SQL("""insert into session_completions({columns}) values({params})
            on conflict(athlete_id,session_id,training_day) do update set {update}
            returning to_jsonb(session_completions.*)""").format(
                columns=columns, params=sql.SQL(',').join(sql.Placeholder() for _ in values), update=update)
        return self.db.execute(statement, tuple(values.values())).fetchone()[0]

    def validate_xp_abuse_hardening(self):
        return self.db.execute("select validate_xp_abuse_hardening()").fetchone()[0]

    def award_xp(self, athlete_id, *, action, idempotency_key, calendar_date=None):
        return self.db.execute("select award_athlete_xp(%s,%s,%s,%s)",
                               (athlete_id, action, idempotency_key, calendar_date)).fetchone()[0]

    def list_plan_session_completions(self, athlete_id, plan_id, *, limit=500):
        return [row[0] for row in self.db.execute("select to_jsonb(c) from session_completions c where athlete_id=%s and plan_id=%s limit %s",
                                                (athlete_id, plan_id, limit)).fetchall()]


@pytest.fixture
def database(xp_database, monkeypatch):
    import psycopg
    with psycopg.connect(xp_database, autocommit=True) as db:
        db.execute("truncate profiles cascade")
        db.execute("insert into profiles(id) values(%s),(%s)", (OWNER, OTHER))
        db.execute("set role service_role")
        # Notification and lifecycle delivery are separate side effects. All
        # completion rows, source proof, ledger entries and account totals are SQL.
        monkeypatch.setattr("api.services.progress_notifications.dispatch_progress_award_notification", lambda *a, **kw: 0)
        monkeypatch.setattr("api.services.week_progress._reconcile_completed_week_lifecycle", lambda *a, **kw: None)
        yield db, PostgresXPStore(db)


def save_plan(db, *, legacy=True, idless=False, deterministic=True):
    from psycopg.types.json import Jsonb
    row = {**_reference_row(), "id": str(uuid4()), "athlete_id": OWNER, "status": "ready"}
    if not deterministic:
        row["planning_brief"]["open_plan_spec"].pop("weekly_session_plan")
    if idless:
        for day in row["structured_plan"]["weeks"][0]["days"]:
            for session in day["sessions"]:
                session.pop("session_id", None)
    if not legacy:
        row["structured_plan"] = resolve_effective_structured_plan(row)
    db.execute("insert into plans(id,athlete_id,status,created_at,planning_brief,structured_plan) values(%s,%s,'ready',%s,%s,%s)",
               (row["id"], OWNER, row["created_at"], json.dumps(row["planning_brief"]), Jsonb(row["structured_plan"])))
    db.execute("update profiles set active_plan_id=%s where id=%s", (row["id"], OWNER))
    return row


def weeks_for(row, block=0):
    anchor = open_plan_anchor_date(row) + timedelta(days=28 * block)
    result, context = project_open_structured_plan(row, resolve_effective_structured_plan(row), current_training_day=anchor)
    assert context["projection_status"] == "projected"
    return result["weeks"]


def sql_sessions(db, row, week):
    return {(s, d.isoformat()) for s, d in db.execute("select * from xp_full_week_planned_sessions(%s,%s,%s)",
                                                    (row["id"], week["week_id"], week["start_date"])).fetchall()}


@pytest.mark.parametrize("legacy,idless,deterministic", [(True, False, True), (False, False, True), (True, True, True), (True, False, False)])
def test_api_and_postgres_agree_on_exact_sessions_dates_and_repeated_cycles(database, legacy, idless, deterministic):
    db, _ = database
    row = save_plan(db, legacy=legacy, idless=idless, deterministic=deterministic)
    for block in range(2):
        for week in weeks_for(row, block):
            planned = _planned_session_ids(week)
            expected = {(s["session_id"], day["date"]) for day in week["days"] for s in day["sessions"] if s["session_id"] in planned}
            assert sql_sessions(db, row, week) == expected
    assert db.execute("select structured_plan from plans where id=%s", (row["id"],)).fetchone()[0] == row["structured_plan"]


def complete_day(store, row, day):
    primary = next((s for s in day["sessions"] if s["session_type"] in {"strength_power", "conditioning"}), day["sessions"][0])
    now = datetime.combine(date.fromisoformat(day["date"]) + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).replace(hour=12)
    completion = upsert_session_completion(store, athlete_id=OWNER, athlete_timezone="UTC",
        payload={"plan_id": row["id"], "session_id": primary["session_id"], "training_day": day["date"], "status": "done"}, now=now)
    rewards = award_session_progress(store, athlete_id=OWNER, athlete_timezone="UTC", completion=completion, now_utc=now)
    assert len(rewards) == 2
    return completion, rewards


def test_real_completion_session_xp_full_week_bonuses_and_second_block_are_idempotent(database):
    db, store = database
    row = save_plan(db)
    for block in range(2):
        for week in weeks_for(row, block):
            for day in week["days"]:
                if not day["sessions"]:
                    continue
                completion, rewards = complete_day(store, row, day)
                assert all(r["awarded"] for r in rewards)
                again, duplicate = complete_day(store, row, day)
                assert again["id"] == completion["id"]
                assert all(not r["awarded"] for r in duplicate)
            award = award_completed_week(store, athlete_id=OWNER, athlete_timezone="UTC", plan=row, training_day=week["end_date"])
            assert award and award["awarded"]
            repeated = award_completed_week(store, athlete_id=OWNER, athlete_timezone="UTC", plan=row, training_day=week["end_date"])
            assert repeated and not repeated["awarded"]
    assert db.execute("select count(*) from xp_awards where action='full_training_week_completed'").fetchone()[0] == 8
    assert db.execute("select total_xp from xp_accounts where athlete_id=%s", (OWNER,)).fetchone()[0] == 2000
    assert db.execute("select structured_plan from plans where id=%s", (row["id"],)).fetchone()[0] == row["structured_plan"]


def test_database_rejects_forged_cross_week_unavailable_and_incomplete_xp_sources(database):
    import psycopg
    db, store = database
    row = save_plan(db)
    weeks = weeks_for(row)
    wrong_day = weeks[0]["days"][2]["date"]  # Wednesday unavailable.
    wrong_week = weeks[3]["days"][5]["date"]  # Week 4 has walk flush, not Week 1 footwork.
    for training_day, session_id, status, actor, award_day in (
        (wrong_day, "ses-strength", "done", OWNER, wrong_day),
        (wrong_week, "ses-footwork", "done", OWNER, wrong_week),
        (weeks[0]["days"][5]["date"], "ses-strength", "started", OWNER, weeks[0]["days"][5]["date"]),
        (weeks[0]["days"][5]["date"], "ses-strength", "done", OTHER, weeks[0]["days"][5]["date"]),
        (weeks[0]["days"][5]["date"], "ses-strength", "done", OWNER, wrong_day),
    ):
        completion = store.upsert_session_completion(OWNER, {"plan_id": row["id"], "session_id": session_id, "training_day": training_day, "status": status})
        with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
            store.award_xp(actor, action="planned_session_completed", idempotency_key=f"planned_session_completed:{completion['id']}", calendar_date=award_day)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        store.award_xp(OWNER, action="full_training_week_completed", idempotency_key=f"full-week:{row['id']}:{weeks[1]['week_id']}", calendar_date=weeks[1]["start_date"])
    assert db.execute("select count(*) from xp_awards").fetchone()[0] == 0


def test_unavailable_completion_rejected_by_real_service_before_any_sql_or_xp_write(database):
    db, store = database
    row = save_plan(db)
    day = weeks_for(row)[0]["days"][2]["date"]
    with pytest.raises(HTTPException) as failure:
        upsert_session_completion(store, athlete_id=OWNER, athlete_timezone="UTC",
            payload={"plan_id": row["id"], "session_id": "ses-strength", "training_day": day, "status": "done"},
            now=datetime.fromisoformat(day + 'T12:00:00+00:00') + timedelta(days=1))
    assert failure.value.status_code == 422
    assert db.execute("select count(*) from session_completions").fetchone()[0] == 0
    assert db.execute("select count(*) from xp_awards").fetchone()[0] == 0


def test_authenticated_and_anon_cannot_call_projection_or_award_rpcs(database):
    import psycopg
    db, _ = database
    row = save_plan(db)
    db.execute("reset role")
    for role in ("anon", "authenticated"):
        db.execute(f"set role {role}")
        for query, params in (
            ("select xp_open_plan_session_weeks(%s)", (row["id"],)),
            ("select award_athlete_xp(%s,'full_training_week_completed','forged',current_date)", (OWNER,)),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
                db.execute(query, params)
    db.execute("reset role")


def test_switching_active_plans_cannot_earn_another_sessions_xp_on_the_same_day(database):
    import psycopg
    db, store = database
    first = save_plan(db)
    day = weeks_for(first)[0]["days"][4]
    complete_day(store, first, day)
    second = save_plan(db, idless=True)
    replacement = weeks_for(second)[0]["days"][4]["sessions"][-1]
    completion = store.upsert_session_completion(OWNER, {
        "plan_id": second["id"], "session_id": replacement["session_id"], "training_day": day["date"], "status": "done"})
    with pytest.raises(psycopg.errors.CheckViolation, match="locked to another plan"), db.transaction():
        store.award_xp(OWNER, action="planned_session_completed", idempotency_key=f"planned_session_completed:{completion['id']}", calendar_date=day["date"])
    assert db.execute("select total_xp from xp_accounts where athlete_id=%s", (OWNER,)).fetchone()[0] == 75
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"), db.transaction():
        db.execute("update xp_awards set source_plan_id=%s where athlete_id=%s", (second["id"], OWNER))


def test_full_week_requires_all_exact_occurrences_and_allows_modified_but_not_skipped(database):
    import psycopg
    db, store = database
    row = save_plan(db)
    week = weeks_for(row)[1]
    occurrences = sorted(sql_sessions(db, row, week))
    first = None
    for index, (session_id, day) in enumerate(occurrences):
        completion = store.upsert_session_completion(OWNER, {
            "plan_id": row["id"], "session_id": session_id, "training_day": day,
            "status": "skipped" if index == 0 else "modified"})
        if index == 0:
            first = completion
    key = f"full-week:{row['id']}:{week['week_id']}"
    with pytest.raises(psycopg.errors.CheckViolation, match="every planned session"), db.transaction():
        store.award_xp(OWNER, action="full_training_week_completed", idempotency_key=key, calendar_date=week["start_date"])
    # A completion with the correct id on another day does not satisfy the gap.
    store.upsert_session_completion(OWNER, {
        "plan_id": row["id"], "session_id": first["session_id"],
        "training_day": (date.fromisoformat(first["training_day"]) + timedelta(days=1)).isoformat(), "status": "done"})
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        store.award_xp(OWNER, action="full_training_week_completed", idempotency_key=key, calendar_date=week["start_date"])
    db.execute("update session_completions set status='modified' where id=%s", (first["id"],))
    result = store.award_xp(OWNER, action="full_training_week_completed", idempotency_key=key, calendar_date=week["start_date"])
    assert result["awarded"]
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        store.award_xp(OWNER, action="full_training_week_completed", idempotency_key=f"full-week:{row['id']}:{weeks_for(row)[2]['week_id']}", calendar_date=week["start_date"])
    assert db.execute("select count(*) from xp_awards").fetchone()[0] == 1


def test_migration_reapplication_preserves_saved_plan_completions_and_prior_awards(database):
    db, store = database
    row = save_plan(db)
    day = weeks_for(row)[0]["days"][5]
    complete_day(store, row, day)
    snapshot = db.execute("""select (select jsonb_agg(to_jsonb(p)) from plans p),
        (select jsonb_agg(to_jsonb(c)) from session_completions c),
        (select jsonb_agg(to_jsonb(a)) from xp_awards a),
        (select jsonb_agg(to_jsonb(x)) from xp_accounts x)""").fetchone()
    db.execute("reset role")
    db.execute(MIGRATION.read_text(encoding="utf-8"))
    db.execute(COMPLETION_ORIGIN_MIGRATION.read_text(encoding="utf-8"))
    db.execute("set role service_role")
    assert db.execute("""select (select jsonb_agg(to_jsonb(p)) from plans p),
        (select jsonb_agg(to_jsonb(c)) from session_completions c),
        (select jsonb_agg(to_jsonb(a)) from xp_awards a),
        (select jsonb_agg(to_jsonb(x)) from xp_accounts x)""").fetchone() == snapshot


@pytest.mark.parametrize("created,weekday,expected_anchor", [
    ("2026-07-31T00:30:00Z", "Thursday", date(2026, 7, 27)),
    ("2026-07-30T23:30:00Z", "Friday", date(2026, 8, 3)),
])
def test_sql_and_api_share_athlete_local_anchor_and_renewal_dates(database, created, weekday, expected_anchor):
    db, _ = database
    row = save_plan(db)
    row["created_at"] = created
    row["planning_brief"]["athlete_snapshot"] = {
        "plan_creation_weekday": weekday, "plan_creation_weekday_basis": "athlete_local_weekday"}
    db.execute("update plans set created_at=%s,planning_brief=%s where id=%s", (created, json.dumps(row["planning_brief"]), row["id"]))
    assert open_plan_anchor_date(row) == expected_anchor
    assert db.execute("select xp_open_plan_anchor_date(%s)", (row["id"],)).fetchone()[0] == expected_anchor
    for block in range(2):
        for week in weeks_for(row, block):
            expected = {(s["session_id"], d["date"]) for d in week["days"] for s in d["sessions"] if not s.get("optional")}
            assert sql_sessions(db, row, week) == expected


def test_dated_fight_camp_completion_and_full_week_xp_keep_the_existing_path(database):
    from psycopg.types.json import Jsonb
    db, store = database
    row = save_plan(db)
    row["fight_date"] = "2027-01-01"
    row["planning_brief"] = {}
    row["structured_plan"]["plan_metadata"]["plan_type"] = "fight_camp"
    week = row["structured_plan"]["weeks"][0]
    week.update(week_id="dated-week-1", start_date="2026-10-12", end_date="2026-10-18")
    for day in week["days"]:
        offset = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun").index(day["weekday"])
        day["date"] = (date(2026, 10, 12) + timedelta(days=offset)).isoformat()
    db.execute("update plans set fight_date=%s,planning_brief='{}',structured_plan=%s where id=%s",
               (row["fight_date"], Jsonb(row["structured_plan"]), row["id"]))
    expected = {(s["session_id"], d["date"]) for d in week["days"] for s in d["sessions"]}
    assert sql_sessions(db, row, week) == expected
    for day in week["days"]:
        if day["sessions"]:
            complete_day(store, row, day)
    result = award_completed_week(store, athlete_id=OWNER, athlete_timezone="UTC", plan=row, training_day=week["end_date"])
    assert result and result["awarded"]
    assert db.execute("select total_xp from xp_accounts where athlete_id=%s", (OWNER,)).fetchone()[0] == 250
