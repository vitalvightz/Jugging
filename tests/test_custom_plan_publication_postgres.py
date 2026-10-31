"""Real PostgreSQL plans schema + real PostgREST client serialization.

The HTTP transport translates the client's wire payload into SQL, allowing the
production store/exception path to run against a disposable localhost database.
Auth, draft and unrelated Today fixtures remain in memory; plan inserts, reads
and the active-plan pointer use PostgreSQL. No production credentials are used.
"""
import json
import os
from pathlib import Path
import re
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from supabase import ClientOptions, create_client

from api.app import create_app
from api.auth import AuthenticatedUser
from api.models import PlanRequest
from api.store import SupabaseAppStore
from tests.support import FakeAuthService, FakeStore
from tests.test_custom_open_plans import AUTH, WEEKDAYS, body
from api.services.today_service import resolve_training_day
from datetime import date


ROOT = Path(__file__).resolve().parents[1]
OWNER = "00000000-0000-4000-8000-000000000001"
INTAKE = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(scope="module")
def database_dsn():
    dsn = os.environ.get("CUSTOM_PLAN_TEST_DATABASE_URL") or os.environ.get("REHAB_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set CUSTOM_PLAN_TEST_DATABASE_URL to a disposable localhost PostgreSQL cluster")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    assert conninfo_to_dict(dsn).get("host") in {"localhost", "127.0.0.1", "::1"}, "test cluster must be local"
    name = "unlxck_custom_plan_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    test_dsn = make_conninfo(dsn, dbname=name)
    try:
        schema = (ROOT / "supabase/schema.sql").read_text(encoding="utf-8")
        plans = re.search(r"create table if not exists public\.plans \([\s\S]*?\n\);", schema)[0]
        metadata = re.search(r"alter table public\.plans add column if not exists parsing_metadata[^;]+;", schema)[0]
        status = re.search(r"add constraint plans_status_check\s+check \([\s\S]*?\)\s+not valid", schema)[0]
        with psycopg.connect(test_dsn, autocommit=True) as setup:
            setup.execute("create table profiles(id uuid primary key); create table athlete_intakes(id uuid primary key)")
            setup.execute(plans + metadata + "alter table public.plans " + status + ";")
            setup.execute("alter table profiles add column active_plan_id uuid references plans(id)")
            setup.execute("insert into profiles(id) values (%s);", (OWNER,))
            setup.execute("insert into athlete_intakes(id) values (%s);", (INTAKE,))
        yield test_dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))


@pytest.fixture
def persistence(database_dsn):
    import psycopg
    from psycopg import sql

    with psycopg.connect(database_dsn, autocommit=True) as db:
        db.execute("update profiles set active_plan_id=null; delete from plans")
        payloads = []

        def transport(request):
            table = request.url.path.rsplit("/", 1)[-1]
            assert table in {"plans", "profiles"}
            assert request.headers.get("Content-Profile", request.headers.get("Accept-Profile")) == "public"
            filters, params = [], []
            for key, value in request.url.params.multi_items():
                if key in {"id", "athlete_id"}:
                    assert value.startswith("eq.")
                    filters.append(sql.SQL("{}=%s").format(sql.Identifier(key)))
                    params.append(value[3:])
            where = sql.SQL(" where ") + sql.SQL(" and ").join(filters) if filters else sql.SQL("")
            try:
                if request.method == "POST":
                    payload = json.loads(request.content)
                    payloads.append(payload)
                    columns = sql.SQL(",").join(map(sql.Identifier, payload))
                    # This preserves explicit JSON null and SQL text/jsonb types
                    # in exactly the way PostgREST's insert record expansion does.
                    query = sql.SQL("insert into {t} ({c}) select {c} from json_populate_record(null::{t}, %s::json) returning to_jsonb({t}.*)").format(
                        t=sql.Identifier(table), c=columns,
                    )
                    rows = db.execute(query, (json.dumps(payload),)).fetchall()
                elif request.method == "GET":
                    query = sql.SQL("select to_jsonb({t}.*) from {t}").format(t=sql.Identifier(table)) + where
                    rows = db.execute(query, params).fetchall()
                elif request.method == "PATCH":
                    payload = json.loads(request.content)
                    assignments = sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(key)) for key in payload)
                    query = sql.SQL("update {t} set {a}").format(t=sql.Identifier(table), a=assignments) + where + sql.SQL(" returning to_jsonb({}.*)").format(sql.Identifier(table))
                    rows = db.execute(query, [*payload.values(), *params]).fetchall()
                else:
                    raise AssertionError(request.method)
                return httpx.Response(201 if request.method == "POST" else 200, json=[row[0] for row in rows])
            except psycopg.Error as exc:
                return httpx.Response(400, json=dict(code=exc.sqlstate, message=exc.diag.message_primary,
                                                    details=exc.diag.message_detail, hint=exc.diag.message_hint))

        with httpx.Client(transport=httpx.MockTransport(transport)) as http:
            client = create_client("https://local-test.supabase.co", "test-key",
                                   options=ClientOptions(httpx_client=http))
            yield SupabaseAppStore(client=client, admin_emails=set()), db, payloads


def test_original_null_insert_fails_with_23502_and_safe_response(persistence, caplog):
    store, _, payloads = persistence
    # Force precisely the old wire payload through the production insert path.
    insert = store.client.table

    def old_table(name):
        query = insert(name)
        original = query.insert

        def null_insert(payload):
            return original({**payload, "parsing_metadata": None})

        query.insert = null_insert
        return query

    store.client.table = old_table
    with pytest.raises(HTTPException) as rejected:
        store.create_plan(athlete_id=OWNER, intake_id=INTAKE, request=PlanRequest(athlete={"full_name": "Test athlete"}), result={})
    assert payloads[0]["parsing_metadata"] is None
    assert rejected.value.__cause__.code == "23502"
    assert 'null value in column "parsing_metadata"' in rejected.value.__cause__.message
    assert rejected.value.detail == "plan persistence failed"
    assert "Failing row contains" not in caplog.text
    assert "Test athlete" not in caplog.text


def test_saved_custom_plan_publish_view_activate_today_and_retries(persistence):
    persistent, db, payloads = persistence

    # Only unrelated fixtures use FakeStore. Exercise production methods for
    # all plan persistence and active-pointer operations, with real SQL.
    store = FakeStore()
    for method in (
        "create_plan", "get_plan", "get_plan_for_athlete", "get_active_plan_id",
        "set_active_plan_id", "list_user_plans", "get_plan_identity_for_athlete",
        "get_plan_schedule_for_athlete", "get_plan_execution_for_athlete",
    ):
        if hasattr(persistent, method):
            setattr(store, method, getattr(persistent, method))
    user = AuthenticatedUser(user_id=OWNER, email="test@example.com", full_name="Test athlete", metadata={})
    store.ensure_profile(user)
    store.intakes[OWNER] = [{"id": INTAKE, "athlete_id": OWNER}]
    client = TestClient(create_app(store=store, auth_service=FakeAuthService({"athlete-token": user}), enable_in_process_generation=False))
    weekday = WEEKDAYS[date.fromisoformat(resolve_training_day(None)).weekday()]
    saved = client.post("/api/custom-plans", headers=AUTH, json=body(weekday))
    assert saved.status_code == 201, saved.text
    draft = saved.json()
    endpoint = f"/api/custom-plans/{draft['id']}/open-plan"
    published = client.post(endpoint, headers=AUTH)
    assert published.status_code == 200, published.text
    plan_id = published.json()["plan_id"]
    row = store.get_plan(plan_id)
    assert isinstance(row["planning_brief"], str)
    assert json.loads(row["planning_brief"])["custom_plan"]["id"] == draft["id"]
    assert isinstance(row["structured_plan"], dict) and row["parsing_metadata"] == {}
    assert row["structured_plan"] == payloads[0]["structured_plan"]
    detail = client.get(f"/api/plans/{plan_id}", headers=AUTH)
    assert detail.status_code == 200, detail.text
    assert detail.json()["structured_card_state"]["state"] == "live"
    assert detail.json()["custom_plan_id"] == draft["id"]
    activated = client.post(f"/api/plans/{plan_id}/set-active", headers=AUTH)
    assert activated.status_code == 200, activated.text
    assert store.get_active_plan_id(OWNER) == plan_id
    today = client.get("/api/today", headers=AUTH)
    assert today.status_code == 200, today.text
    assert today.json()["today"]["next_session"]["title"] == "Strength / Rounds"
    assert client.post(endpoint, headers=AUTH).json()["plan_id"] == plan_id
    assert db.execute("select count(*) from plans").fetchone()[0] == 1
    assert client.get(f"/api/custom-plans/{draft['id']}", headers=AUTH).json()["sessions"] == draft["sessions"]
    # A racing retry exercises the existing primary-key conflict recovery too.
    lookup = store.get_plan_for_athlete
    store.get_plan_for_athlete = lambda *args: None
    # Restore visibility only after the duplicate SQL insert fails.
    create = store.create_plan

    def racing_insert(**kw):
        try:
            return create(**kw)
        finally:
            store.get_plan_for_athlete = lookup

    store.create_plan = racing_insert
    assert client.post(endpoint, headers=AUTH).json()["plan_id"] == plan_id
    assert db.execute("select count(*) from plans").fetchone()[0] == 1


def test_atomic_only_plan_activation_checks_candidate_and_uniqueness(persistence):
    store, db, _ = persistence
    # Execute the actual migration function in the disposable PostgreSQL DB.
    # The test DB has no Supabase API roles, so omit only its GRANT/REVOKE.
    migration = (ROOT / "supabase/migrations/20261031200000_atomic_single_plan_activation.sql")
    db.execute(migration.read_text(encoding="utf-8").split("revoke all on function")[0])

    request = PlanRequest(athlete={"full_name": "Test athlete"})
    first = store.create_plan(
        athlete_id=OWNER, intake_id=INTAKE, request=request,
        result={"status": "ready", "plan_text": "# First"},
    )
    def activate(candidate, expected=None):
        return db.execute(
            "select public.try_activate_only_unarchived_plan(%s, %s, %s, current_date)",
            (OWNER, candidate, expected),
        ).fetchone()[0]

    assert activate(first["id"]) is True
    assert activate(first["id"]) is False  # Idempotent: don't overwrite the pointer.
    db.execute("update profiles set active_plan_id=null")

    second = store.create_plan(
        athlete_id=OWNER, intake_id=INTAKE, request=request,
        result={"status": "ready", "plan_text": "# Second"},
    )
    assert activate(first["id"]) is False  # Ambiguous with two unarchived plans.
    db.execute("update plans set status='archived' where id=%s", (second["id"],))
    assert activate(first["id"]) is True
    db.execute("update profiles set active_plan_id=null")
    db.execute("update plans set status='archived' where id=%s", (first["id"],))
    assert activate(first["id"]) is False  # Cannot activate an archived candidate.


def test_existing_ai_generated_metadata_and_text_remain_unchanged(persistence):
    store, _, _ = persistence
    metadata = {"source": "stage1", "warnings": ["review"]}
    row = store.create_plan(athlete_id=OWNER, intake_id=INTAKE, request=PlanRequest(athlete={"full_name": "Test athlete"}),
                            result={"status": "ready", "plan_text": "# AI plan", "planning_brief": {"source": "AI"}, "parsing_metadata": metadata})
    assert row["plan_text"] == "# AI plan" and row["parsing_metadata"] == metadata
    assert json.loads(row["planning_brief"]) == {"source": "AI"}
    assert row["structured_plan"] is None
