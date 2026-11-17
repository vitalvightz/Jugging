"""Real PostgreSQL connections: RPCs wait before taking an injury row lock.

REHAB_TEST_DATABASE_URL must point at a disposable localhost PostgreSQL cluster
with CREATE DATABASE/ROLE privileges. Each run creates and drops its own database.
CI supplies this through its PostgreSQL service; ordinary unit runs skip it.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260930173118_injury_episode_prescription_history.sql"
ATHLETE = "00000000-0000-4000-8000-000000000001"
INJURY = "00000000-0000-4000-8000-000000000002"
EPISODE = "00000000-0000-4000-8000-000000000003"


@pytest.fixture(scope="module")
def postgres_database():
    dsn = os.environ.get("REHAB_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set REHAB_TEST_DATABASE_URL to run the real PostgreSQL lock tests")
    # Missing drivers must fail in CI when a database was explicitly configured.
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    config = conninfo_to_dict(dsn)
    assert config.get("host") in {"localhost", "127.0.0.1", "::1"}, "test cluster must be local"
    name = "unlxck_rehab_lock_test_" + uuid4().hex
    created_roles = []
    with psycopg.connect(dsn, autocommit=True) as admin:
        for role in ("anon", "authenticated", "service_role"):
            if not admin.execute("select 1 from pg_roles where rolname=%s", (role,)).fetchone():
                admin.execute(sql.SQL("create role {} {}").format(
                    sql.Identifier(role), sql.SQL("bypassrls" if role == "service_role" else "")))
                created_roles.append(role)
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    test_dsn = make_conninfo(dsn, dbname=name)
    try:
        schema = (ROOT / "supabase/schema.sql").read_text(encoding="utf-8")

        def table(table_name):
            return re.search(rf"create table if not exists public\.{table_name} \([\s\S]*?\n\);", schema)[0]

        validator = re.search(
            r"create or replace function public\.injury_flags_infection_signs_valid[\s\S]*?\$\$;", schema)[0]
        with psycopg.connect(test_dsn, autocommit=True) as setup:
            setup.execute(f"""
                create schema auth;
                grant usage on schema auth, public to authenticated, service_role;
                create function auth.uid() returns uuid language sql stable as $$
                  select nullif(current_setting('request.jwt.claim.sub', true),'')::uuid $$;
                create table public.profiles(id uuid primary key);
                create table public.plans(id uuid primary key);
                {validator} {table('injury_flags')}
                alter table injury_flags add column episode_id uuid not null default gen_random_uuid(),
                  add column body_region text, add column side text not null default 'unknown';
                create unique index injury_flags_episode_owner_idx on injury_flags(id,athlete_id,episode_id);
                {table('today_checkins')} {table('session_completions')} {table('rehab_exposures')}
            """)
            # Execute the actual migration, including its injury-update trigger.
            setup.execute((ROOT / "supabase/migrations/20260820170000_add_rehab_response_group_identity.sql").read_text(encoding="utf-8"))
            setup.execute(MIGRATION.read_text(encoding="utf-8"))
            setup.execute((ROOT / "supabase/migrations/20261001122553_restrict_injury_episode_trigger_execution.sql").read_text(encoding="utf-8"))
            setup.execute((ROOT / "supabase/migrations/20261002132000_count_rehab_bundle_allocations.sql").read_text(encoding="utf-8"))
            setup.execute((ROOT / "supabase/migrations/20261002134527_clinician_clearance_prescription_freshness.sql").read_text(encoding="utf-8"))
            assessment_migration = (ROOT / "supabase/migrations/20261005215205_add_achilles_progression_observations.sql").read_text(encoding="utf-8")
            setup.execute(assessment_migration)
            setup.execute(assessment_migration)  # Repeatable unmerged migration.
            # Exercise the upgrade from the deployed constraint, not only fresh schema.
            setup.execute("alter table session_completions drop constraint session_completions_rehab_performance_check")
            setup.execute("alter table session_completions add constraint session_completions_rehab_performance_check check (rehab_performance in ('done_as_shown', 'changed', 'stopped'))")
            completion_migration = (ROOT / "supabase/migrations/20261009151500_allow_skipped_rehab_performance.sql").read_text(encoding="utf-8")
            setup.execute(completion_migration)
            setup.execute(completion_migration)
            setup.execute("insert into profiles values (%s)", (ATHLETE,))
            setup.execute("""insert into injury_flags(id,athlete_id,description,body_region,side,episode_id)
                values(%s,%s,'ankle sprain','ankle','left',%s)""", (INJURY, ATHLETE, EPISODE))
        yield test_dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))
            for role in reversed(created_roles):
                admin.execute(sql.SQL("drop role {}").format(sql.Identifier(role)))


def test_skipped_rehab_constraint_upgrade(postgres_database):
    import psycopg
    with psycopg.connect(postgres_database) as connection:
        plan = str(uuid4())
        connection.execute("insert into plans values (%s)", (plan,))
        values = (ATHLETE, plan, "skipped-rehab-migration")
        connection.execute("""insert into session_completions
            (athlete_id,plan_id,session_id,training_day,status,rehab_performance)
            values(%s,%s,%s,current_date,'done','skipped')""", values)
        assert connection.execute("select rehab_performance from session_completions where session_id=%s", (values[2],)).fetchone()[0] == "skipped"
        with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
            connection.execute("update session_completions set rehab_performance='unrecognised' where session_id=%s", (values[2],))


def test_assessment_rpc_generic_envelope_retry_concern_and_service_boundary(postgres_database):
    import psycopg
    from psycopg.types.json import Jsonb
    from copy import deepcopy

    with psycopg.connect(postgres_database) as connection:
        injury, episode, identity = str(uuid4()), str(uuid4()), str(uuid4())
        connection.execute("""insert into injury_flags(id,athlete_id,description,body_area,body_region,side,episode_id,created_at)
            values(%s,%s,'Achilles tendonitis','Left Achilles','achilles','left',%s,now()-interval '30 days')""", (injury, ATHLETE, episode))
        stamp = connection.execute("select (now()-interval '1 hour')::text").fetchone()[0]
        # The SQL boundary is protocol-neutral; registered service code owns
        # clinical validation. This second protocol exists only in this fixture.
        event = dict(id=identity, injury_id=injury, injury_episode_id=episode, event_type="rehab_progression_assessment",
            payload=dict(region="achilles", injury_type="tendonitis", profile_id="achilles_tendonitis",
                source="athlete_reported", externally_verified=False, medical_concern=True,
                observation_times=[stamp], injury_context=dict(body_area="Left Achilles", description="Achilles tendonitis"),
                assessment=dict(schema_version=1, assessment_kind="synthetic_transport_fixture_v1", protocol_version=1,
                    side="left", assessed_at=stamp, assessor="coach_observed", payload=dict(observed=True))))
        # Normalize PostgreSQL's +00 shorthand to the transport's aware ISO format.
        from datetime import datetime
        stamp = datetime.fromisoformat(stamp).isoformat()
        event['payload']['observation_times'] = [stamp]
        event['payload']['assessment']['assessed_at'] = stamp
        before = connection.execute("select updated_at from injury_flags where id=%s", (injury,)).fetchone()[0]
        for role in ('anon', 'authenticated'):
            assert not connection.execute("select has_function_privilege(%s,'public.record_injury_episode_event(uuid,jsonb)','EXECUTE')", (role,)).fetchone()[0]
        connection.execute("grant select on injury_flags to service_role")
        connection.execute("set local role service_role")
        saved = connection.execute("select (record_injury_episode_event(%s,%s)).id", (ATHLETE, Jsonb(event))).fetchone()[0]
        revision = connection.execute("select updated_at from injury_flags where id=%s", (injury,)).fetchone()[0]
        assert revision > before
        assert connection.execute("select (record_injury_episode_event(%s,%s)).id", (ATHLETE, Jsonb(event))).fetchone()[0] == saved
        assert connection.execute("select updated_at from injury_flags where id=%s", (injury,)).fetchone()[0] == revision
        for mutate in (lambda e: e.update(injury_episode_id=ATHLETE),
            lambda e: e['payload']['assessment'].update(side='bilateral'),
            lambda e: e['payload'].update(source='clinician_verified'),
            lambda e: e['payload'].update(externally_verified=True),
            lambda e: e['payload']['injury_context'].update(description='Elbow tendonitis'),
            lambda e: e['payload']['assessment'].update(assessed_at='2099-01-01T00:00:00Z'),
            lambda e: e['payload']['assessment'].update(assessed_at='2026-10-05T12:00:00'),
            lambda e: e['payload']['assessment']['payload'].update(observed=False)):
            bad = deepcopy(event)
            mutate(bad)
            with pytest.raises(psycopg.Error), connection.transaction():
                connection.execute("select record_injury_episode_event(%s,%s)", (ATHLETE, Jsonb(bad)))


def test_internal_episode_trigger_still_records_backend_updates(postgres_database):
    import psycopg

    identity = str(uuid4())
    with psycopg.connect(postgres_database) as connection:
        connection.execute("""insert into injury_flags(id,athlete_id,description,body_region,side)
            values(%s,%s,'ankle sprain','ankle','left')""", (identity, ATHLETE))
        connection.execute("grant select,update on injury_flags to service_role")
        connection.execute("set local role service_role")
        for role in ("anon", "authenticated"):
            assert connection.execute("select has_function_privilege(%s,'public.capture_injury_episode_change()','EXECUTE')", (role,)).fetchone()[0] is False
        connection.execute("update injury_flags set latest_reported_status='improving' where id=%s", (identity,))
        rows = connection.execute("select payload from injury_episode_events where injury_id=%s", (identity,)).fetchall()
        assert len(rows) == 2
        assert sum(r[0]["explicit_report"] for r in rows) == 1


def event_for(rpc, episode):
    if rpc == "record_injury_episode_event":
        return {"id": str(uuid4()), "injury_id": INJURY, "injury_episode_id": episode,
                "event_type": "clinician_clearance_report",
                "payload": {"source": "athlete_reported", "externally_verified": False, "scopes": ["rehab"]}}
    return {"exposure_id": str(uuid4()), "response_group_id": str(uuid4()), "injury_id": INJURY, "injury_episode_id": episode,
            "drill_id": "ankle_sprain_heel_lowering", "body_region": "ankle", "side": "left",
            "demand": {"target_regions": ["ankle"], "load": "low", "impact": "none", "velocity": "low"},
            "dose_completed": {"completion_state": "performed_amount_unknown"},
            "response": {"during_response": "same", "next_day_response": "not_yet_known"},
            "occurred_at": "2026-09-30T12:00:00Z",
            "provenance": {"source": "athlete_logged_rehab", "recorded_at": "2026-09-30T12:30:00Z"}}


@pytest.mark.parametrize("rpc", ["record_injury_episode_event", "record_rehab_exposure"])
@pytest.mark.parametrize("reopen", [False, True], ids=["same-episode", "reopened-episode"])
def test_rpc_and_injury_update_serialize_without_deadlock(postgres_database, rpc, reopen):
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb

    # Only these two backend connections participate in the race. The writer
    # observes pg_locks itself; no observer connection or timing guess is needed.
    with psycopg.connect(postgres_database) as writer, psycopg.connect(postgres_database) as recorder:
        for connection in (writer, recorder):
            connection.execute("set statement_timeout = '8s'")
            connection.execute("set deadlock_timeout = '200ms'")
            connection.commit()
        episode = str(writer.execute("select episode_id from injury_flags where id=%s", (INJURY,)).fetchone()[0])
        writer.execute("select pg_advisory_xact_lock(hashtextextended('injury:' || %s, 0))", (ATHLETE,))
        event = event_for(rpc, episode)

        def record():
            try:
                recorder.execute(sql.SQL("select public.{}(%s,%s)").format(sql.Identifier(rpc)),
                                 (ATHLETE, Jsonb(event)))
                recorder.commit()
                return None
            except psycopg.Error as error:
                recorder.rollback()
                return error.sqlstate or "client_error"

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(record)
            try:
                deadline = time.monotonic() + 5
                while not writer.execute("""select 1 from pg_locks
                        where pid=%s and locktype='advisory' and not granted""",
                        (recorder.info.backend_pid,)).fetchone():
                    assert not pending.done(), "RPC finished before waiting for the athlete lock"
                    assert time.monotonic() < deadline, "RPC did not reach its advisory lock"
                    time.sleep(0.01)
                # Under the old order the RPC already owns FOR SHARE, so this
                # actual UPDATE and the waiting RPC form a deadlock cycle.
                if reopen:
                    writer.execute("update injury_flags set episode_id=%s, updated_at=now() where id=%s",
                                   (str(uuid4()), INJURY))
                else:
                    writer.execute("update injury_flags set description='updated ankle sprain', updated_at=now() where id=%s",
                                   (INJURY,))
                writer.commit()
                result = pending.result(timeout=10)
                # Reading after the advisory wait must see a reopened episode
                # and reject the stale report instead of accepting old evidence.
                assert result == ("23514" if reopen else None)
            finally:
                writer.rollback()  # release the blocker even if an assertion fails


def test_only_explicit_reports_refresh_recovery_evidence(postgres_database):
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    from api.contracts.injury_policy import resolve_injury_policy
    from api.contracts.rehab_schedule import schedule_rehab
    from api.services.injury_episode_service import apply_episode_observations
    from fightcamp.rehab_clinical import load_clinical_policies
    from fightcamp.rehab_protocols import get_rehab_bank

    identity, episode = str(uuid4()), str(uuid4())
    with psycopg.connect(postgres_database, autocommit=True, row_factory=dict_row) as connection:
        connection.execute("""insert into injury_flags(id,athlete_id,description,body_region,side,episode_id,
            status,severity,latest_reported_status) values(%s,%s,'ankle sprain','ankle','left',%s,
            'monitoring','mild','improving')""", (identity, ATHLETE, episode))
        event = event_for("record_rehab_exposure", episode)
        event["injury_id"] = identity
        event["response"]["next_day_response"] = "worse"
        connection.execute("select record_rehab_exposure(%s,%s)", (ATHLETE, Jsonb(event)))

        def decision():
            row = connection.execute("select * from injury_flags where id=%s", (identity,)).fetchone()
            row.update(id=str(row["id"]), athlete_id=str(row["athlete_id"]), episode_id=str(row["episode_id"]),
                       canonical_location="ankle", injury_type="sprain", rehab_stage="restore")
            observations = connection.execute("select * from injury_episode_events where injury_id=%s", (identity,)).fetchall()
            for observation in observations:
                for key in ("athlete_id", "injury_id", "injury_episode_id"):
                    observation[key] = str(observation[key])
            exposures = connection.execute("select * from rehab_exposures where injury_id=%s", (identity,)).fetchall()
            for exposure in exposures:
                exposure["athlete_id"] = str(exposure["athlete_id"])
            row = apply_episode_observations(row, observations)
            result = resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(), exposures=exposures)
            return row, result, exposures

        original_report = decision()[0]["latest_reported_at"]
        assert original_report is None  # insert is baseline history, not a report
        initial = connection.execute("select payload from injury_episode_events where injury_id=%s", (identity,)).fetchall()
        assert len(initial) == 1 and initial[0]["payload"]["explicit_report"] is False
        assert decision()[1]["stage"] == "calm"
        # A severity change emits audit history with the inherited improving
        # status; a description change also updates the generic row timestamp.
        connection.execute("update injury_flags set severity='moderate', updated_at=now() where id=%s", (identity,))
        connection.execute("update injury_flags set description='edited ankle sprain', updated_at=now() where id=%s", (identity,))
        row, result, _ = decision()
        assert row["latest_reported_at"] == original_report
        assert result["stage"] == "calm" and not result["prescription"]["is_loading"]
        # An explicit repeat must be recorded even though the value is unchanged.
        connection.execute("update injury_flags set latest_reported_status='improving', updated_at=now() where id=%s", (identity,))
        row, result, exposures = decision()
        assert row["latest_reported_at"] is not None
        assert result["stage"] == "restore" and result["prescription"]["is_loading"]
        assert schedule_rehab(row, result, training_day="2026-10-08", exposures=exposures)["state"] == "due"


def test_status_reports_record_one_event_per_statement(postgres_database):
    import psycopg
    from psycopg.rows import dict_row

    identity = str(uuid4())
    with psycopg.connect(postgres_database, autocommit=True, row_factory=dict_row) as connection:
        connection.execute("""insert into injury_flags(id,athlete_id,description,body_region,side,
            latest_reported_status) values(%s,%s,'ankle sprain','ankle','left','improving')""", (identity, ATHLETE))

        def events():
            return connection.execute("select payload from injury_episode_events where injury_id=%s", (identity,)).fetchall()

        assert len(events()) == 1 and not events()[0]["payload"]["explicit_report"]
        for expected_count, statement in enumerate([
            "update injury_flags set latest_reported_status='worse' where id=%s",
            "update injury_flags set latest_reported_status='improving', severity='moderate' where id=%s",
            "update injury_flags set latest_reported_status='improving' where id=%s",
            "update injury_flags set latest_reported_status='improving', severity='mild' where id=%s",
        ], start=2):
            connection.execute(statement, (identity,))
            rows = events()
            assert len(rows) == expected_count
            assert sum(r["payload"]["explicit_report"] for r in rows) == expected_count - 1
        connection.execute("update injury_flags set severity='moderate' where id=%s", (identity,))
        assert len(events()) == 6
        assert sum(r["payload"]["explicit_report"] for r in events()) == 4


def test_historical_delayed_feedback_remains_owned_and_episode_scoped(postgres_database):
    import psycopg
    from psycopg.types.json import Jsonb

    identity, old_episode, new_episode = str(uuid4()), str(uuid4()), str(uuid4())
    with psycopg.connect(postgres_database, autocommit=True) as connection:
        connection.execute("""insert into injury_flags(id,athlete_id,description,body_region,side,episode_id)
            values(%s,%s,'ankle sprain','ankle','left',%s)""", (identity, ATHLETE, old_episode))
        exposure = event_for("record_rehab_exposure", old_episode)
        exposure["injury_id"] = identity
        connection.execute("select record_rehab_exposure(%s,%s)", (ATHLETE, Jsonb(exposure)))
        connection.execute("update injury_flags set episode_id=%s where id=%s", (new_episode, identity))
        pending = connection.execute("select id from pending_delayed_rehab(%s,'2026-10-08') where injury_id=%s", (ATHLETE, identity)).fetchall()
        assert [str(r[0]) for r in pending] == [exposure["exposure_id"]]
        report = dict(id=str(uuid4()), injury_id=identity, injury_episode_id=old_episode,
                      event_type="delayed_rehab_response", payload=dict(exposure_id=exposure["exposure_id"], response="worse"))
        connection.execute("select record_injury_episode_event(%s,%s)", (ATHLETE, Jsonb(report)))
        saved = connection.execute("select injury_episode_id from injury_episode_events where id=%s", (report["id"],)).fetchone()
        assert str(saved[0]) == old_episode
        assert not connection.execute("select id from pending_delayed_rehab(%s,'2026-10-08') where injury_id=%s", (ATHLETE, identity)).fetchall()
        with pytest.raises(psycopg.Error) as failure:
            connection.execute("select record_injury_episode_event(%s,%s)", (ATHLETE, Jsonb({**report, "id": str(uuid4()), "injury_episode_id": new_episode})))
        assert failure.value.sqlstate == "23514"


@pytest.mark.parametrize("limit", [1, 2])
def test_database_counts_bundle_once_and_enforces_daily_ceiling(postgres_database, limit):
    import psycopg
    from psycopg.types.json import Jsonb

    # Roll back fixture rows to isolate this allocation check from lock tests.
    with psycopg.connect(postgres_database) as connection:
        plan = str(uuid4())
        connection.execute("insert into plans values (%s)", (plan,))
        checkin = connection.execute("""insert into today_checkins(athlete_id,plan_id,training_day,sleep,body,pain,phase,recommendation_state)
            values(%s,%s,'2026-10-02','good','normal','none','GPP','train_as_planned') returning id,updated_at""", (ATHLETE, plan)).fetchone()
        injuries = connection.execute("""select id,episode_id,updated_at from injury_flags
            where athlete_id=%s and status in ('open','monitoring')""", (ATHLETE,)).fetchall()
        evidence = {
            "exposure_id": connection.execute("select id::text from rehab_exposures where athlete_id=%s order by created_at desc,id desc limit 1", (ATHLETE,)).fetchone(),
            "event_id": connection.execute("""select id::text from injury_episode_events where athlete_id=%s
                and event_type in ('injury_checkin','delayed_rehab_response','clinician_clearance_report') order by created_at desc,id desc limit 1""", (ATHLETE,)).fetchone(),
        }
        evidence = {key: value[0] if value else None for key, value in evidence.items()}

        def start(session_id, blocks):
            accepted = dict(plan_id=plan, training_day="2026-10-02", revision="a" * 64,
                session={"session_id": session_id, "blocks": blocks}, allocation_limit=limit,
                readiness_context={"id": str(checkin[0]), "updated_at": checkin[1].isoformat()},
                injury_context=[{"id": str(row[0]), "episode_id": str(row[1]),
                                 "updated_at": row[2].isoformat()} for row in injuries],
                evidence_context=evidence)
            connection.execute("""insert into session_completions(athlete_id,plan_id,session_id,training_day,status,prescription_snapshot)
                values(%s,%s,%s,'2026-10-02','started',%s)""", (ATHLETE, plan, session_id, Jsonb(accepted)))

        # Three individually identifiable drills form one server-owned allocation.
        start("bundle", [dict(block_type="rehab", block_id=str(index),
                             rehab_drill_id=f"test_drill_{index}", rehab_allocation_id="rehab:episode:bundle")
                         for index in range(3)])
        if limit == 2:
            start("legacy", [dict(block_type="rehab", block_id="legacy")])
        with pytest.raises(psycopg.Error) as failure:
            start("over-budget", [dict(block_type="rehab", block_id="extra")])
        assert failure.value.sqlstate == "23514"
        assert "rehab_daily_allocation_conflict" in str(failure.value)


def test_clinician_clearance_report_invalidates_snapshot_after_bundle_migration(postgres_database):
    import psycopg
    from psycopg.types.json import Jsonb

    # The fixture applies the bundle-allocation migration last. All context
    # stays unchanged except for a newly recorded clinician-clearance report.
    with psycopg.connect(postgres_database) as connection:
        plan = str(uuid4())
        session_id = "clearance-freshness"
        connection.execute("insert into plans values (%s)", (plan,))
        checkin = connection.execute("""insert into today_checkins(athlete_id,plan_id,training_day,sleep,body,pain,phase,recommendation_state)
            values(%s,%s,'2026-10-02','good','normal','none','GPP','train_as_planned') returning id,updated_at""", (ATHLETE, plan)).fetchone()
        injuries = connection.execute("""select id,episode_id,updated_at from injury_flags
            where athlete_id=%s and status in ('open','monitoring')""", (ATHLETE,)).fetchall()
        exposure = connection.execute("""select id::text from rehab_exposures where athlete_id=%s
            order by created_at desc,id desc limit 1""", (ATHLETE,)).fetchone()
        event = connection.execute("""select id::text from injury_episode_events where athlete_id=%s
            and event_type in ('injury_checkin','delayed_rehab_response','clinician_clearance_report')
            order by created_at desc,id desc limit 1""", (ATHLETE,)).fetchone()
        accepted = dict(plan_id=plan, training_day="2026-10-02", revision="a" * 64,
            session={"session_id": session_id, "blocks": []}, allocation_limit=1,
            readiness_context={"id": str(checkin[0]), "updated_at": checkin[1].isoformat()},
            injury_context=[{"id": str(row[0]), "episode_id": str(row[1]),
                             "updated_at": row[2].isoformat()} for row in injuries],
            evidence_context={"exposure_id": exposure[0] if exposure else None,
                              "event_id": event[0] if event else None})

        def start():
            connection.execute("""insert into session_completions(athlete_id,plan_id,session_id,training_day,status,prescription_snapshot)
                values(%s,%s,%s,'2026-10-02','started',%s)""", (ATHLETE, plan, session_id, Jsonb(accepted)))

        # Prove the snapshot is valid before introducing the new evidence.
        connection.execute("savepoint before_report")
        start()
        connection.execute("rollback to savepoint before_report")
        episode = str(connection.execute("select episode_id from injury_flags where id=%s", (INJURY,)).fetchone()[0])
        report = event_for("record_injury_episode_event", episode)
        connection.execute("select record_injury_episode_event(%s,%s)", (ATHLETE, Jsonb(report)))
        with pytest.raises(psycopg.Error) as failure, connection.transaction():
            start()
        assert failure.value.sqlstate == "23514"
        assert "prescription_revision_conflict" in str(failure.value)

        # Refreshing only the evidence identity restores validity.
        accepted["evidence_context"]["event_id"] = report["id"]
        start()
        connection.rollback()


def test_clearance_report_invalidates_a_snapshot_waiting_to_be_accepted(postgres_database):
    import psycopg
    from psycopg.types.json import Jsonb

    plan, occurrence = str(uuid4()), str(uuid4())
    with psycopg.connect(postgres_database) as recorder, psycopg.connect(postgres_database) as accepter:
        recorder.execute("insert into plans values (%s)", (plan,))
        readiness = recorder.execute("""insert into today_checkins
            (athlete_id,plan_id,training_day,sleep,body,pain,phase,recommendation_state)
            values(%s,%s,'2026-09-30','good','normal','none','GPP','train_as_planned')
            returning id,updated_at""", (ATHLETE, plan)).fetchone()
        injuries = recorder.execute("select id,episode_id,updated_at from injury_flags where athlete_id=%s and status in ('open','monitoring')", (ATHLETE,)).fetchall()
        latest_event = recorder.execute("""select id from injury_episode_events where athlete_id=%s
            and event_type in ('injury_checkin','delayed_rehab_response','clinician_clearance_report')
            order by created_at desc,id desc limit 1""", (ATHLETE,)).fetchone()
        latest_exposure = recorder.execute("select id from rehab_exposures where athlete_id=%s order by created_at desc,id desc limit 1", (ATHLETE,)).fetchone()
        snapshot = dict(plan_id=plan, training_day="2026-09-30", revision="a" * 64, allocation_limit=2,
            session=dict(session_id=occurrence, session_type="sparring", title="Hard sparring", blocks=[]),
            evidence_context=dict(event_id=str(latest_event[0]) if latest_event else None,
                                  exposure_id=str(latest_exposure[0]) if latest_exposure else None),
            readiness_context=dict(id=str(readiness[0]), updated_at=readiness[1].isoformat()),
            injury_context=[dict(id=str(i[0]), episode_id=str(i[1]), updated_at=i[2].isoformat()) for i in injuries])
        recorder.commit()
        accepter.execute("set statement_timeout = '8s'")
        accepter.commit()
        # The report owns the existing athlete lock until commit. The accepted
        # occurrence must wait, then reject the now-stale evidence revision.
        episode = recorder.execute("select episode_id from injury_flags where id=%s", (INJURY,)).fetchone()[0]
        event = event_for("record_injury_episode_event", str(episode))
        recorder.execute("select record_injury_episode_event(%s,%s)", (ATHLETE, Jsonb(event)))

        def accept():
            try:
                accepter.execute("""insert into session_completions
                    (athlete_id,plan_id,session_id,training_day,status,prescription_snapshot)
                    values(%s,%s,%s,'2026-09-30','started',%s)""", (ATHLETE, plan, occurrence, Jsonb(snapshot)))
                accepter.commit()
                return None
            except psycopg.Error as error:
                accepter.rollback()
                return str(error)

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(accept)
            deadline = time.monotonic() + 5
            while not recorder.execute("select 1 from pg_locks where pid=%s and locktype='advisory' and not granted", (accepter.info.backend_pid,)).fetchone():
                assert not pending.done(), "acceptance did not wait for the report"
                assert time.monotonic() < deadline
                time.sleep(0.01)
            recorder.commit()
            assert "prescription_revision_conflict" in pending.result(timeout=8)
        assert accepter.execute("select count(*) from session_completions where session_id=%s", (occurrence,)).fetchone()[0] == 0
        snapshot["evidence_context"]["event_id"] = event["id"]
        assert accept() is None
        assert accepter.execute("select prescription_snapshot from session_completions where session_id=%s", (occurrence,)).fetchone()[0] == snapshot


@pytest.mark.parametrize("production_order", [False, True], ids=["fresh-replay", "production-backfill"])
def test_prescription_function_composes_with_recorded_clearance_history(postgres_database, production_order):
    import psycopg

    pattern = r"create or replace function public\.preserve_started_prescription\(\)[\s\S]*?\$\$;"
    schema_definition = re.search(pattern, (ROOT / "supabase/schema.sql").read_text())[0]
    bundle_migration = (ROOT / "supabase/migrations/20261002132000_count_rehab_bundle_allocations.sql").read_text()
    history_marker = (ROOT / "supabase/migrations/20261002134527_clinician_clearance_prescription_freshness.sql").read_text()
    # The already-applied production version was the pre-bundle function with
    # exactly the clinician report added to its atomic freshness event filter.
    applied_clearance = re.search(pattern, MIGRATION.read_text())[0].replace(
        "'injury_checkin','delayed_rehab_response'",
        "'injury_checkin','delayed_rehab_response','clinician_clearance_report'")
    with psycopg.connect(postgres_database) as connection:
        def definition():
            return connection.execute("select pg_get_functiondef('public.preserve_started_prescription()'::regprocedure)").fetchone()[0]

        connection.execute(schema_definition)
        expected = definition()
        assert "rehab_allocation_id" in expected and "clinician_clearance_report" in expected
        connection.execute(applied_clearance)
        if production_order:
            # Production skips its already-recorded 134527 and applies missing 132000.
            connection.execute(bundle_migration)
            assert definition() == expected
        else:
            connection.execute(bundle_migration)
            assert definition() == expected
            connection.execute(history_marker)
            assert definition() == expected  # No later migration undoes bundle counting.
        connection.rollback()


def test_plan_free_rehab_uses_the_same_postgres_episode_and_allocation_guards(postgres_database):
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(postgres_database) as connection:
        connection.execute((ROOT / "supabase/migrations/20261019061051_allow_plan_free_today_checkins.sql").read_text(encoding="utf-8"))
        connection.execute((ROOT / "supabase/migrations/20261022184651_standalone_rehab_completions.sql").read_text(encoding="utf-8"))
        athlete = str(uuid4())
        connection.execute("insert into profiles values (%s)", (athlete,))
        for _ in range(3):
            connection.execute("""insert into injury_flags(athlete_id,body_area,description,body_region,side)
                values(%s,'Chest','Chest strain','chest','unknown')""", (athlete,))
        injuries = connection.execute("select id,episode_id,updated_at from injury_flags where athlete_id=%s order by id", (athlete,)).fetchall()
        checkin = connection.execute("""insert into today_checkins(athlete_id,training_day,sleep,body,pain,phase,recommendation_state)
            values(%s,'2026-10-22','good','normal','none',null,'train_as_planned') returning id,updated_at""", (athlete,)).fetchone()
        event = connection.execute("select id from injury_episode_events where athlete_id=%s order by created_at desc,id desc limit 1", (athlete,)).fetchone()

        def accept(index, owner):
            identity = "rehab-2026-10-22-" + str(index) * 24
            snapshot = dict(plan_id=None, training_day="2026-10-22", revision="a" * 64, rehab_only=True, allocation_limit=2,
                session=dict(session_id=identity, session_type="rehab", blocks=[dict(block_type="rehab", policy_id="test",
                    injury_id=str(injuries[owner][0]), injury_episode_id=str(injuries[owner][1]), rehab_drill_id="test", minimum_gap_days=1)]),
                readiness_context=dict(id=str(checkin[0]), updated_at=checkin[1].isoformat()),
                injury_context=[dict(id=str(i[0]), episode_id=str(i[1]), updated_at=i[2].isoformat()) for i in injuries],
                evidence_context=dict(exposure_id=None, event_id=str(event[0]) if event else None))
            connection.execute("""insert into session_completions(athlete_id,session_id,training_day,status,prescription_snapshot)
                values(%s,%s,'2026-10-22','done',%s)""", (athlete, identity, Jsonb(snapshot)))

        accept(0, 0)
        with pytest.raises(psycopg.errors.CheckViolation, match="rehab_daily_allocation_conflict"), connection.transaction():
            accept(1, 0)
        accept(1, 1)
        with pytest.raises(psycopg.errors.CheckViolation, match="rehab_daily_allocation_conflict"), connection.transaction():
            accept(2, 2)
        with pytest.raises(psycopg.errors.CheckViolation, match="standalone_rehab_prescription_required"), connection.transaction():
            connection.execute("""insert into session_completions(athlete_id,session_id,training_day,status)
                values(%s,'fake','2026-10-22','done')""", (athlete,))
        assert connection.execute("select count(*) from session_completions where athlete_id=%s", (athlete,)).fetchone()[0] == 2
        connection.rollback()


def test_independent_standalone_owners_use_existing_postgres_allocation_guard(postgres_database):
    import psycopg
    from psycopg.types.json import Jsonb
    from api.contracts.injury_policy import reconcile_session_prescription, resolve_injury_policy, rehab_allocation_count
    from fightcamp.rehab_clinical import load_clinical_policies, content_hash
    from fightcamp.rehab_protocols import get_rehab_bank

    athlete, plan = str(uuid4()), str(uuid4())
    with psycopg.connect(postgres_database) as connection:
        connection.execute("insert into profiles values (%s)", (athlete,))
        connection.execute("insert into plans values (%s)", (plan,))
        for region in ("ankle", "chest", "chest"):
            # The reviewed ankle RESTORE drills are side-specific. Unknown
            # laterality correctly makes the atomic bundle ineligible.
            connection.execute("""insert into injury_flags(athlete_id,body_area,description,body_region,side,
                severity,status,latest_reported_status,created_at)
                values(%s,%s,%s,%s,%s,'mild','monitoring','improving','2026-09-29T00:00:00Z')""",
                (athlete, "Left ankle" if region == "ankle" else "Chest",
                 region + (" sprain" if region == "ankle" else " strain"), region, "left" if region == "ankle" else "unknown"))
        injuries = connection.execute("""select id,episode_id,updated_at,body_region,description,side,body_area,
            severity,status,latest_reported_status,created_at from injury_flags where athlete_id=%s order by body_region,id""", (athlete,)).fetchall()
        readiness = connection.execute("""insert into today_checkins(athlete_id,plan_id,training_day,sleep,body,pain,phase,recommendation_state)
            values(%s,%s,'2026-10-02','good','normal','none','GPP','train_as_planned') returning id,updated_at""", (athlete, plan)).fetchone()
        # Injury inserts record real audit events. Bind the accepted snapshot
        # to the same current evidence revision checked by the production guard.
        evidence = {
            "exposure_id": connection.execute("select id::text from rehab_exposures where athlete_id=%s order by created_at desc,id desc limit 1", (athlete,)).fetchone(),
            "event_id": connection.execute("""select id::text from injury_episode_events where athlete_id=%s
                and event_type in ('injury_checkin','delayed_rehab_response','clinician_clearance_report') order by created_at desc,id desc limit 1""", (athlete,)).fetchone(),
        }
        evidence = {key: value[0] if value else None for key, value in evidence.items()}

        def prescription(row):
            decision = resolve_injury_policy(dict(id=str(row[0]), episode_id=str(row[1]), status=row[8],
                body_region=row[3], body_area=row[6], description=row[4], side=row[5], severity=row[7],
                latest_reported_status=row[9], created_at=row[10].isoformat(), rehab_stage="restore"),
                policies=load_clinical_policies(), bank=get_rehab_bank())
            assert decision["activation"] == "live" and decision["outcome"] == "prescribed_rehab", decision["reason_codes"]
            saved = reconcile_session_prescription(None, decisions=[decision], plan_id=plan, training_day="2026-10-02")
            assert saved and not saved["safety_hold"]
            saved.update(readiness_context=dict(id=str(readiness[0]), updated_at=readiness[1].isoformat()),
                injury_context=[dict(id=str(i[0]), episode_id=str(i[1]), updated_at=i[2].isoformat()) for i in injuries],
                evidence_context=evidence)
            return saved

        def accept(saved):
            saved["revision"] = content_hash({key: value for key, value in saved.items() if key != "revision"})
            connection.execute("""insert into session_completions(athlete_id,plan_id,session_id,training_day,status,prescription_snapshot)
                values(%s,%s,%s,'2026-10-02','done',%s)""", (athlete, plan, saved["session"]["session_id"], Jsonb(saved)))

        ankle, chest, remaining = [prescription(row) for row in injuries]
        expected = next(p for p in load_clinical_policies() if p.policy_id == "ankle_sprain").stage_bundles["restore"]
        assert {b["rehab_drill_id"] for b in ankle["session"]["blocks"]} == set(expected)
        assert len(ankle["session"]["blocks"]) == 2 and rehab_allocation_count(ankle["session"]["blocks"]) == 1
        # A legacy terminal ankle bundle owns just one allocation; the chest
        # occurrence has independent ownership using the current identity model.
        ankle["session"]["session_id"] = "rehab-2026-10-02"
        accept(ankle)
        accept(chest)
        connection.execute("""update session_completions set status='done'
            where athlete_id=%s and session_id=%s""", (athlete, chest["session"]["session_id"]))
        assert connection.execute("select prescription_snapshot from session_completions where athlete_id=%s and session_id=%s",
            (athlete, ankle["session"]["session_id"])).fetchone()[0] == ankle
        with pytest.raises(psycopg.Error) as failure, connection.transaction():
            accept(remaining)
        assert failure.value.sqlstate == "23514" and "rehab_daily_allocation_conflict" in str(failure.value)
        assert connection.execute("select count(*) from session_completions where athlete_id=%s", (athlete,)).fetchone()[0] == 2
        # Free a slot: a different session id still cannot reuse ankle ownership.
        connection.execute("delete from session_completions where athlete_id=%s and session_id=%s", (athlete, chest["session"]["session_id"]))
        assert connection.execute("select count(*) from session_completions where athlete_id=%s", (athlete,)).fetchone()[0] == 1
        duplicate = prescription(injuries[0])
        with pytest.raises(psycopg.Error) as failure, connection.transaction():
            accept(duplicate)
        assert failure.value.sqlstate == "23514" and "rehab_daily_allocation_conflict" in str(failure.value)


@pytest.mark.parametrize('checkin_plan', ['previous-plan', 'plan-free', 'same-plan'])
def test_rehab_start_reuses_daily_readiness_across_plan_switch(postgres_database, checkin_plan):
    """Real trigger accepts shared readiness but still rejects wrong/stale identity."""
    import psycopg
    from psycopg.types.json import Jsonb
    from copy import deepcopy

    with psycopg.connect(postgres_database) as connection:
        for migration in ('20261019061051_allow_plan_free_today_checkins.sql',
                          '20261022184651_standalone_rehab_completions.sql',
                          '20261117032109_share_daily_readiness_for_rehab.sql'):
            connection.execute((ROOT / 'supabase/migrations' / migration).read_text())
        athlete, other_athlete, current_plan, previous_plan = (str(uuid4()) for _ in range(4))
        connection.execute('insert into profiles values (%s), (%s)', (athlete, other_athlete))
        connection.execute('insert into plans values (%s), (%s)', (current_plan, previous_plan))
        bound_plan = {'previous-plan': previous_plan, 'plan-free': None, 'same-plan': current_plan}[checkin_plan]
        checkin = connection.execute('''insert into today_checkins
            (athlete_id,plan_id,training_day,sleep,body,pain,phase,recommendation_state)
            values (%s,%s,'2026-11-17','good','normal','none',%s,'train_as_planned')
            returning id, updated_at''', (athlete, bound_plan, 'GPP' if bound_plan else None)).fetchone()
        identity = 'rehab-switch'
        snapshot = dict(plan_id=current_plan, training_day='2026-11-17', revision='a' * 64,
            session=dict(session_id=identity, session_type='rehab', blocks=[dict(block_type='rehab', block_id='rehab')]),
            allocation_limit=1, injury_context=[], evidence_context=dict(exposure_id=None, event_id=None),
            readiness_context=dict(id=str(checkin[0]), updated_at=checkin[1].isoformat()))

        def start(body=snapshot, owner=athlete):
            connection.execute('''insert into session_completions
                (athlete_id,plan_id,session_id,training_day,status,prescription_snapshot)
                values (%s,%s,%s,'2026-11-17','started',%s)''', (owner, current_plan, identity, Jsonb(body)))

        # Before the fix both cross-plan cases fail, even with valid current answers.
        if checkin_plan != 'same-plan':
            with connection.transaction():
                old_sql = (ROOT / 'supabase/migrations/20261022184651_standalone_rehab_completions.sql').read_text()
                connection.execute(old_sql[old_sql.index('create or replace function public.preserve_started_prescription()'):])
                with pytest.raises(psycopg.errors.CheckViolation, match='prescription_revision_conflict'), connection.transaction():
                    start()
                connection.execute((ROOT / 'supabase/migrations/20261117032109_share_daily_readiness_for_rehab.sql').read_text())

        for field, value in [('id', str(uuid4())), ('updated_at', '2000-01-01T00:00:00Z')]:
            bad = deepcopy(snapshot)
            bad['readiness_context'][field] = value
            with pytest.raises(psycopg.errors.CheckViolation, match='prescription_revision_conflict'), connection.transaction():
                start(bad)
        with pytest.raises(psycopg.errors.CheckViolation, match='prescription_revision_conflict'), connection.transaction():
            start(owner=other_athlete)
        with connection.transaction():
            connection.execute("update today_checkins set training_day='2026-11-16' where id=%s", (checkin[0],))
            with pytest.raises(psycopg.errors.CheckViolation, match='prescription_revision_conflict'), connection.transaction():
                start()
            connection.execute("update today_checkins set training_day='2026-11-17' where id=%s", (checkin[0],))
        start()
        saved = connection.execute('select prescription_snapshot from session_completions where athlete_id=%s', (athlete,)).fetchone()[0]
        assert saved == snapshot
        with pytest.raises(psycopg.errors.CheckViolation, match='prescription_revision_conflict'), connection.transaction():
            connection.execute("update session_completions set prescription_snapshot=jsonb_set(prescription_snapshot,'{revision}',%s) where athlete_id=%s",
                               (Jsonb('b' * 64), athlete))
        connection.rollback()
