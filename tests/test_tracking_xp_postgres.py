"""Real XP ledger: ownership, retries, source-neutral milestones and approval."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
from uuid import uuid4

import pytest

from test_open_plan_xp_postgres import save_plan, sql_sessions, weeks_for, xp_database as _xp_database

xp_database = _xp_database

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20261019195932_add_tracking_and_contribution_xp.sql"
OWNER = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(scope="module")
def tracking_database(xp_database):
    import psycopg

    with psycopg.connect(xp_database, autocommit=True) as db:
        db.execute("""create or replace function public.set_updated_at() returns trigger language plpgsql
          as $$ begin new.updated_at=now(); return new; end $$;""")
        for filename in ("20261010184612_add_external_training_sessions.sql", "20261007141918_add_exercise_logs.sql",
                         "20261019180404_add_manual_exercise_entries.sql"):
            db.execute((ROOT / "supabase/migrations" / filename).read_text())
        # Publication storage used by the real source validator. The availability
        # function below is copied verbatim from its real migration, not mocked.
        db.execute("""
          create table private_video_assets(id uuid primary key,status text,sha256 text);
          create table private_exercise_media(id uuid primary key,athlete_id uuid,provider text,video_id text,asset_id uuid);
          create table community_exercises(id uuid primary key,creator_id uuid,published_revision_id uuid,
            attribution_consent boolean default true,withdrawn_at timestamptz);
          create table community_exercise_revisions(id uuid primary key,exercise_id uuid,media_id uuid,status text,
            confirmed_at timestamptz,verified_at timestamptz,verified_media_id uuid,verified_proof jsonb);
        """)
        community = (ROOT / "supabase/migrations/20261011212949_community_exercise_review.sql").read_text()
        db.execute(re.search(r"create function public.community_revision_available[\s\S]*?\$\$;", community)[0])
        db.execute(MIGRATION.read_text())
        db.execute(MIGRATION.read_text())  # migration replay must preserve awards/schema
        db.execute("grant select,insert,update,delete on all tables in schema public to service_role")
    return xp_database


@pytest.fixture
def db(tracking_database):
    import psycopg

    with psycopg.connect(tracking_database, autocommit=True) as conn:
        conn.execute("truncate profiles cascade")
        conn.execute("truncate community_exercises,community_exercise_revisions,private_exercise_media cascade")
        conn.execute("insert into profiles(id) values (%s),(%s)", (OWNER, OTHER))
        yield conn


def manual(db, owner=OWNER, day=None):
    identifier = str(uuid4())
    db.execute("""insert into external_training_sessions(id,athlete_id,client_request_id,request_fingerprint,
      training_day,activity,duration_minutes,effort) values(%s,%s,%s,%s,%s,'strength',30,'moderate')""",
      (identifier, owner, str(uuid4()), "a" * 64, day or date.today()-timedelta(days=1)))
    return identifier


def reconcile(db, session=None, owner=OWNER):
    db.execute("set role service_role")
    try:
        return db.execute("select reconcile_training_engagement_xp(%s,%s)", (owner, session)).fetchone()[0]
    finally:
        db.execute("reset role")


def actions(results):
    return {item["award"]["action"]: item["award"]["amount"] for item in results if item.get("awarded")}


def test_first_manual_save_retry_and_same_day_sessions(db):
    entry = manual(db)
    assert actions(reconcile(db, entry)) == {"manual_training_logged": 25, "first_session_logged": 50}
    assert not actions(reconcile(db, entry))
    assert not actions(reconcile(db, manual(db)))
    assert db.execute("select total_xp from xp_accounts where athlete_id=%s", (OWNER,)).fetchone()[0] == 75
    db.execute("update external_training_sessions set deleted_at=now() where id=%s", (entry,))
    assert not actions(reconcile(db, entry))


def test_manual_source_cannot_cross_owners_or_earn_from_future(db):
    entry = manual(db, OTHER)
    assert not actions(reconcile(db, entry))
    future = manual(db, day=date.today()+timedelta(days=2))
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        reconcile(db, future)


def test_training_day_rolls_at_0300_in_athlete_timezone(db):
    now = datetime.now(timezone.utc)
    before = next(offset for offset in range(-12, 10) if (now.hour+offset) % 24 < 3)
    def zone(offset):
        return "UTC" if offset == 0 else f"Etc/GMT{-offset:+d}"
    # Set the initial timezone at account creation; changing it after insert
    # correctly hits the existing 24-hour anti-farming guard.
    db.execute("delete from profiles where id in (%s,%s)", (OWNER, OTHER))
    for owner, offset in [(OWNER, before), (OTHER, before+4)]:
        db.execute("insert into profiles(id,athlete_timezone) values(%s,%s)", (owner, zone(offset)))
    local_date = (now+timedelta(hours=before)).date()
    manual(db, OWNER, local_date)
    manual(db, OTHER, local_date)
    assert db.execute("select count(*) from xp_recorded_training_days(%s)", (OWNER,)).fetchone()[0] == 0
    assert db.execute("select count(*) from xp_recorded_training_days(%s)", (OTHER,)).fetchone()[0] == 1


def test_distinct_training_days_not_sessions_and_no_day_credit(db):
    for offset in range(1, 11):
        manual(db, day=date.today()-timedelta(days=offset))
    for _ in range(3):
        manual(db)
    assert actions(reconcile(db)) == {"first_session_logged": 50, "training_days_10": 100}
    assert not actions(reconcile(db))
    assert db.execute("select count(*) from xp_recorded_training_days(%s)", (OWNER,)).fetchone()[0] == 10


def test_reported_prescribed_days_share_milestones_but_day_credit_does_not(db):
    plan = save_plan(db)
    for offset, origin, status in [(1, "reported", "done"), (2, "reported", "modified"), (3, "day_credit", "done"), (4, None, "done"), (5, "reported", "skipped")]:
        db.execute("""insert into session_completions(athlete_id,plan_id,session_id,training_day,status,completion_origin)
          values(%s,%s,%s,%s,%s,%s)""", (OWNER, plan["id"], str(uuid4()), date.today()-timedelta(days=offset), status, origin))
    assert db.execute("select count(*) from xp_recorded_training_days(%s)", (OWNER,)).fetchone()[0] == 2
    assert actions(reconcile(db)) == {"first_session_logged": 50, "first_plan_ready": 100}
    # Publishing a second plan does not create another first-plan reward.
    save_plan(db)
    assert not actions(reconcile(db))


@pytest.mark.parametrize("manual_first", [True, False])
def test_daily_recording_credit_is_shared_with_prescribed_training(db, manual_first):
    plan = save_plan(db)
    session, day = sorted(sql_sessions(db, plan, weeks_for(plan)[0]))[0]
    completion = str(uuid4())
    db.execute("""insert into session_completions(id,athlete_id,plan_id,session_id,training_day,status,completion_origin)
      values(%s,%s,%s,%s,%s,'done','reported')""", (completion, OWNER, plan["id"], session, day))
    entry = manual(db, day=day)
    if manual_first:
        assert "manual_training_logged" in actions(reconcile(db, entry))
    db.execute("set role service_role")
    try:
        result = db.execute("select award_athlete_xp(%s,'training_logged',%s,%s)",
                            (OWNER, f"training_logged:{completion}", day)).fetchone()[0]
    finally:
        db.execute("reset role")
    assert result["awarded"] is (not manual_first)
    if not manual_first:
        assert "manual_training_logged" not in actions(reconcile(db, entry))
    assert db.execute("select count(*) from xp_awards where action in ('manual_training_logged','training_logged')").fetchone()[0] == 1


def test_all_day_milestones_once_and_deletion_never_removes_banked_xp(db):
    for offset in range(1, 101):
        manual(db, day=date.today()-timedelta(days=offset))
    earned = actions(reconcile(db))
    assert earned == {"first_session_logged": 50, "training_days_10": 100, "training_days_25": 150,
                     "training_days_50": 250, "training_days_100": 500}
    db.execute("update external_training_sessions set deleted_at=now()")
    assert not actions(reconcile(db))
    assert db.execute("select total_xp from xp_accounts where athlete_id=%s", (OWNER,)).fetchone()[0] == sum(earned.values())


def test_first_exercise_requires_measurements_and_live_parent(db):
    parent = manual(db)
    personal = str(uuid4())
    db.execute("insert into personal_exercises(id,athlete_id,name,name_key) values(%s,%s,'Squat','squat')", (personal, OWNER))
    entry = str(uuid4())
    db.execute("""insert into manual_exercise_entries(athlete_id,external_session_id,client_entry_id,
      personal_exercise_id,display_name,measurement_format,actual) values(%s,%s,%s,%s,'Squat','strength','{}')""",
      (OWNER, parent, entry, personal))
    assert "first_exercise_logged" not in actions(reconcile(db))
    db.execute("update manual_exercise_entries set actual=%s where client_entry_id=%s",
               (json.dumps({"sets": 3, "reps": 5, "load": {"value": 80, "unit": "kg"}}), entry))
    assert actions(reconcile(db)) == {"first_exercise_logged": 25}
    assert not actions(reconcile(db))


def contribution(db, owner=OWNER, video="abcDEF12345", status="published"):
    exercise, revision, media = [str(uuid4()) for _ in range(3)]
    db.execute("insert into private_exercise_media values(%s,%s,'youtube',%s,null)", (media, owner, video))
    db.execute("insert into community_exercises(id,creator_id,published_revision_id) values(%s,%s,%s)", (exercise, owner, revision))
    db.execute("""insert into community_exercise_revisions values(%s,%s,%s,%s,now(),now(),%s,%s)""",
      (revision, exercise, media, status, media, json.dumps({"youtube_id": video, "youtube_verified": True})))
    return exercise, revision


def approve(db, revision):
    db.execute("set role service_role")
    try:
        return db.execute("select reconcile_approved_contribution_xp(%s)", (revision,)).fetchone()[0]
    finally:
        db.execute("reset role")


def test_approval_rewards_creator_not_admin_and_deduplicates_video(db):
    _, revision = contribution(db)
    result = approve(db, revision)
    assert result["athlete_id"] == OWNER
    assert actions(result["results"]) == {"exercise_contribution_approved": 100}
    assert not actions(approve(db, revision)["results"])
    _, duplicate = contribution(db, OTHER)
    assert not actions(approve(db, duplicate)["results"])
    _, pending = contribution(db, video="newDEF12345", status="in_review")
    assert approve(db, pending) is None


def test_changed_video_on_same_contribution_cannot_earn_again(db):
    exercise, revision = contribution(db)
    assert actions(approve(db, revision)["results"])
    db.execute("update private_exercise_media set video_id='newABC12345'")
    db.execute("update community_exercise_revisions set verified_proof=%s where id=%s",
               (json.dumps({"youtube_id": "newABC12345", "youtube_verified": True}), revision))
    assert not actions(approve(db, revision)["results"])
    assert db.execute("select count(*) from xp_awards where action='exercise_contribution_approved'").fetchone()[0] == 1


def test_private_withdrawn_and_unverified_contributions_do_not_earn(db):
    import psycopg
    for field, replacement in [("attribution_consent", "false"), ("withdrawn_at", "now()")]:
        exercise, revision = contribution(db, video=str(uuid4())[:11])
        db.execute(f"update community_exercises set {field}={replacement} where id=%s", (exercise,))
        with pytest.raises(psycopg.errors.CheckViolation):
            approve(db, revision)
    _, revision = contribution(db, video="otherAB1234")
    db.execute("update community_exercise_revisions set verified_at=null where id=%s", (revision,))
    with pytest.raises(psycopg.errors.CheckViolation):
        approve(db, revision)


def test_concurrent_manual_retries_bank_one_reward(tracking_database, db):
    import psycopg
    entry = manual(db)
    def save(_):
        with psycopg.connect(tracking_database, autocommit=True) as conn:
            return actions(reconcile(conn, entry))
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(save, range(8)))
    assert sum("manual_training_logged" in outcome for outcome in outcomes) == 1
    assert sum("first_session_logged" in outcome for outcome in outcomes) == 1


def test_browser_roles_cannot_call_reward_rpcs(db):
    import psycopg
    for role in ("anon", "authenticated"):
        db.execute(f"set role {role}")
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute("select reconcile_training_engagement_xp(%s)", (OWNER,))
        finally:
            db.execute("reset role")


class ContributionReplayStore:
    def __init__(self, db, *, fail_approval=False):
        self.db = db
        self.fail_approval = fail_approval

    def validate_xp_abuse_hardening(self):
        self.db.execute("set role service_role")
        try:
            return self.db.execute("select validate_xp_abuse_hardening()").fetchone()[0]
        finally:
            self.db.execute("reset role")

    def list_unrewarded_contribution_revisions(self, *, after_id=None, limit=50):
        self.db.execute("set role service_role")
        try:
            return [{"exercise_id": str(row[0]), "revision_id": str(row[1])} for row in self.db.execute(
                "select * from list_unrewarded_contribution_revisions(%s,%s)", (after_id, limit)).fetchall()]
        finally:
            self.db.execute("reset role")

    def reconcile_approved_contribution_xp(self, revision_id):
        if self.fail_approval:
            raise RuntimeError("XP RPC temporarily unavailable after publication")
        return approve(self.db, revision_id)


def test_replay_recovers_persisted_approval_after_xp_failure(db, monkeypatch):
    from api.services import tracking_xp
    captured = []
    monkeypatch.setattr(tracking_xp, "dispatch_push_candidate", lambda store, candidate, **kw: captured.append(candidate))
    exercise, revision = contribution(db)
    store = ContributionReplayStore(db, fail_approval=True)
    tracking_xp.reconcile_contribution_xp(store, revision)
    assert db.execute("select published_revision_id from community_exercises where id=%s", (exercise,)).fetchone()[0]
    assert db.execute("select count(*) from xp_awards").fetchone()[0] == 0
    assert store.list_unrewarded_contribution_revisions() == [{"exercise_id": exercise, "revision_id": revision}]
    store.fail_approval = False
    assert tracking_xp.replay_contribution_xp(store) == exercise
    assert db.execute("select total_xp from xp_accounts where athlete_id=%s", (OWNER,)).fetchone()[0] == 100
    assert captured[0].profile_id == OWNER
    assert tracking_xp.replay_contribution_xp(store) is None
    assert len(captured) == 1


def test_replay_discovery_skips_paid_duplicate_and_ineligible_sources(db):
    paid_exercise, paid_revision = contribution(db)
    approve(db, paid_revision)
    contribution(db, owner=OTHER)  # Same canonical video, another creator.
    for field, value in [("attribution_consent", "false"), ("withdrawn_at", "now()")]:
        exercise, _ = contribution(db, video=uuid4().hex[:11])
        db.execute(f"update community_exercises set {field}={value} where id=%s", (exercise,))
    contribution(db, video=uuid4().hex[:11], status="in_review")
    _, unverified = contribution(db, video=uuid4().hex[:11])
    db.execute("update community_exercise_revisions set verified_at=null where id=%s", (unverified,))
    eligible = sorted(contribution(db, video=uuid4().hex[:11]) for _ in range(3))
    store = ContributionReplayStore(db)
    first = store.list_unrewarded_contribution_revisions(limit=2)
    assert [(row["exercise_id"], row["revision_id"]) for row in first] == eligible[:2]
    rest = store.list_unrewarded_contribution_revisions(after_id=first[-1]["exercise_id"], limit=2)
    assert [(row["exercise_id"], row["revision_id"]) for row in rest] == eligible[2:]
    assert paid_exercise not in {row["exercise_id"] for row in first + rest}
    assert db.execute("select count(*) from xp_awards").fetchone()[0] == 1


def test_concurrent_replays_bank_once(tracking_database, db, monkeypatch):
    import psycopg
    from api.services import tracking_xp
    contribution(db)
    monkeypatch.setattr(tracking_xp, "dispatch_push_candidate", lambda *args, **kw: None)
    def replay(_):
        with psycopg.connect(tracking_database, autocommit=True) as conn:
            return tracking_xp.replay_contribution_xp(ContributionReplayStore(conn))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(replay, range(8)))
    assert db.execute("select count(*) from xp_awards where action='exercise_contribution_approved'").fetchone()[0] == 1
    assert db.execute("select total_xp from xp_accounts where athlete_id=%s", (OWNER,)).fetchone()[0] == 100


def test_replay_discovery_is_backend_only(db):
    import psycopg
    for role in ("anon", "authenticated"):
        db.execute(f"set role {role}")
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute("select * from list_unrewarded_contribution_revisions()")
        finally:
            db.execute("reset role")
