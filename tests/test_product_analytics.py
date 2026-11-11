from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from api.services.product_analytics import summarize
from api.routes.product_analytics import build_product_analytics_router
from api.dependencies import get_store, require_admin, require_profile

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


def athlete(id="a", signup="2026-10-01T12:00:00+00:00", **kw):
    return {
        **dict(
            id=id,
            created_at=signup,
            professional_status="",
            technical_style=[],
            starting_path="tracking_first",
        ),
        **kw,
    }


def fact(
    day,
    aid="a",
    action="session_completed",
    meaningful=True,
    count=1,
    feature="strength",
):
    return dict(
        athlete_id=aid,
        day=day,
        action=action,
        meaningful=meaningful,
        count=count,
        feature=feature,
        first_at=f"{day}T12:00:00+00:00",
        last_at=f"{day}T12:00:00+00:00",
    )


def report(athletes=None, facts=None, **kw):
    return summarize(
        dict(athletes=athletes or [], facts=facts or [], **kw), now=NOW, days=30
    )


def test_empty_denominators():
    r = report()
    assert r["registered"] == 0
    assert r["stickiness"] is None
    assert r["retention"]["7"] == dict(eligible=0, returned=0, percent=None)


def test_unique_users_days_and_discovery_excluded():
    r = report(
        [athlete()],
        [
            fact("2026-10-25", count=4),
            fact("2026-10-25", action="plan_opened", meaningful=False),
            fact("2026-10-24"),
        ],
    )
    assert (r["dau"], r["wau"], r["mau"]) == (1, 1, 1)
    assert r["average_active_days"] == 2
    assert r["athletes"][0]["first_activity"].startswith("2026-10-24")
    assert r["athletes"][0]["last_activity"].startswith("2026-10-25")
    assert next(f for f in r["features"] if f["feature"] == "strength")["uses"] == 5


def test_exact_return_days_and_incomplete_cohorts():
    ps = [
        athlete("a"),
        athlete("b", "2026-10-24T00:00:00+00:00"),
        athlete("c", "2026-09-01T00:00:00+00:00"),
    ]
    r = summarize(
        dict(
            athletes=ps,
            facts=[fact("2026-10-02"), fact("2026-10-08"), fact("2026-10-01", "c")],
        ),
        now=NOW,
        days=0,
    )
    assert r["retention"]["1"] == dict(eligible=2, returned=1, percent=50.0)
    assert r["retention"]["7"] == dict(eligible=2, returned=1, percent=50.0)
    assert r["retention"]["30"] == dict(eligible=1, returned=1, percent=100.0)
    # Yesterday's signup has not finished its D1 return window today.
    assert r["retention"]["1"]["eligible"] == 2


def test_plan_free_onboarding_and_observed_only_dropoffs():
    r = report(
        [athlete()],
        [
            fact("2026-10-01", action="onboarding_started", meaningful=False),
            fact("2026-10-02", action="onboarding_completed", meaningful=False),
            fact("2026-10-03", feature="manual"),
        ],
    )
    c = next(
        c
        for c in r["conversions"]
        if c["label"] == "Onboarded plan-first → usable plan"
    )
    assert c["eligible"] == 0 and c["percent"] is None
    assert r["dropoffs"]["observed_onboarding_not_completed_24h"] == 0
    assert report([athlete()])["dropoffs"]["observed_onboarding_not_completed_24h"] == 0


def test_session_denominator_matched_and_mature_abandonment():
    sessions = [
        dict(
            athlete_id="a",
            started_at="2026-10-01T12:00:00+00:00",
            completed_at=None,
            status="started",
        ),
        dict(
            athlete_id="a",
            started_at="2026-10-25T10:00:00+00:00",
            completed_at=None,
            status="started",
        ),
        dict(
            athlete_id="a",
            started_at=None,
            completed_at="2026-10-01T14:00:00+00:00",
            status="done",
        ),
    ]
    r = report([athlete()], sessions=sessions)
    assert r["sessions_started"] == 2 and r["observed_session_abandonments"] == 1
    c = next(
        c
        for c in r["conversions"]
        if c["label"] == "Observed session starts → completed"
    )
    assert c["eligible"] == 2 and c["converted"] == 0


def test_date_boundaries_and_segments():
    data = dict(
        athletes=[athlete(), athlete("b", professional_status="professional")],
        facts=[fact("2026-10-19"), fact("2026-10-18"), fact("2026-10-26")],
    )
    r = summarize(data, now=NOW, days=7)
    assert (
        r["active"] == 1
        and r["previous_active"] == 1
        and r["average_active_days"] == 0.5
    )
    assert (
        summarize(data, now=NOW, days=7, professional_status="professional")["active"]
        == 0
    )


def test_errors_never_become_activity():
    r = report(
        [athlete()],
        [fact("2026-10-24", action="operation_failed", meaningful=False, count=3)],
    )
    assert r["ever_active"] == 0 and r["errors"][0]["errors_per_athlete"] == 3


class Client:
    def __init__(self):
        self.calls = []

    def rpc(self, name, args):
        self.calls.append((name, args))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=True))


def api_client():
    app = FastAPI()
    app.include_router(build_product_analytics_router())
    store = SimpleNamespace(client=Client(), admin_emails=set())
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[require_profile] = lambda: SimpleNamespace(
        athlete_id="owner"
    )

    def deny():
        raise HTTPException(403, "admin access required")

    app.dependency_overrides[require_admin] = deny
    return TestClient(app), store


def test_ingestion_actor_timestamp_and_controlled_events():
    c, s = api_client()
    body = dict(event_name="timer_started", feature="timer", request_id=str(uuid4()))
    assert c.post("/api/analytics/events", json=body).status_code == 202
    assert s.client.calls[0][1]["p_athlete_id"] == "owner"
    for extra in (
        {"athlete_id": "other"},
        {"created_at": "2000-01-01"},
        {"payload": {"notes": "private"}},
    ):
        assert (
            c.post("/api/analytics/events", json={**body, **extra}).status_code == 422
        )
    assert (
        c.post("/api/analytics/events", json={**body, "feature": "plans"}).status_code
        == 422
    )
    assert (
        c.post(
            "/api/analytics/events", json={**body, "event_name": "operation_failed"}
        ).status_code
        == 422
    )


def test_all_reports_admin_only():
    c, _ = api_client()
    assert c.get("/api/admin/product-analytics").status_code == 403
    assert c.get(f"/api/admin/athletes/{uuid4()}/activity").status_code == 403


def test_timer_completions_match_observed_flow_starts():
    r = report(
        [athlete()],
        timer_flows=[
            dict(
                athlete_id="a",
                started_at="2026-10-10T12:00:00+00:00",
                completed_at="2026-10-10T12:05:00+00:00",
                exited_at=None,
            ),
            dict(
                athlete_id="a",
                started_at="2026-10-11T12:00:00+00:00",
                completed_at=None,
                exited_at="2026-10-11T12:01:00+00:00",
            ),
        ],
    )
    f = next(f for f in r["features"] if f["feature"] == "timer")
    assert f["completion"] == dict(started=2, completed=1, percent=50.0)
    assert f["observed_early_exits"] == 1


def test_failure_followup_requires_full_window_and_does_not_claim_causality():
    r = report(
        [athlete()],
        [
            fact("2026-10-01", action="operation_failed", meaningful=False),
            fact("2026-10-20"),
        ],
        error_returns=[
            dict(
                athlete_id="a",
                feature="strength",
                action="operation_failed",
                error_at="2026-10-01T12:00:00+00:00",
                returned=False,
            )
        ],
    )
    assert r["friction_followup"][0]["eligible_athletes"] == 1
    assert r["friction_followup"][0]["no_recorded_return_7d"] == 1
    r = report(
        [athlete()], [fact("2026-10-24", action="operation_failed", meaningful=False)]
    )
    assert r["friction_followup"][0]["eligible_athletes"] == 0


def test_admin_pagination_and_filters_are_validated():
    c, store = api_client()
    c.app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
        athlete_id="admin"
    )
    store.client.rpc = lambda name, args: SimpleNamespace(
        execute=lambda: SimpleNamespace(
            data=dict(
                athletes=[athlete("a"), athlete("b")],
                facts=[],
                optional_coverage=[],
                tracking_health={"healthy": True},
                tracking_since="2026-10-30T00:00:00Z",
                training_records=[],
                error_returns=[],
            )
        )
    )
    response = c.get("/api/admin/product-analytics?days=7&offset=1&limit=1")
    assert response.status_code == 200 and response.json()["athlete_total"] == 2
    assert len(response.json()["athletes"]) == 1
    assert c.get("/api/admin/product-analytics?days=8").status_code == 422
    assert c.get("/api/admin/product-analytics?offset=-1").status_code == 422
    assert c.get("/api/admin/product-analytics?limit=101").status_code == 422
    assert (
        c.get("/api/admin/product-analytics?professional_status=professional").json()[
            "registered"
        ]
        == 0
    )


def test_beta_analytics_has_no_optional_switch():
    c, store = api_client()
    for enabled in (True, False):
        assert (
            c.put("/api/analytics/preference", json=dict(enabled=enabled)).status_code
            == 409
        )
    assert not store.client.calls


def test_generated_plan_and_profile_timestamps_never_replace_meaningful_activity():
    r = report(
        [athlete()],
        [
            fact("2026-10-01"),
            fact(
                "2026-10-25", action="plan_generated", meaningful=False, feature="plans"
            ),
        ],
    )
    assert r["athletes"][0]["first_activity"].startswith("2026-10-01")
    assert r["athletes"][0]["last_activity"].startswith("2026-10-01")
    assert r["dau"] == 0


def test_observed_signals_identify_athletes_without_guessing_unobserved_intent():
    r = report(
        [athlete("a"), athlete("b")],
        [fact("2026-10-01", action="onboarding_started", meaningful=False)],
    )
    assert "Observed onboarding start unresolved for 24h" in r["athletes"][0]["signals"]
    assert r["athletes"][1]["signals"] == []


@pytest.mark.parametrize(
    "path,feature",
    [("/api/training/sessions", "manual"), ("/api/feedback/session", "feedback")],
)
def test_failed_feature_write_is_observed_without_copying_error_text(path, feature):
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from api.services.product_friction import install_product_friction

    app = FastAPI()
    store = SimpleNamespace(client=Client())
    app.state.store = store

    @app.post(path)
    def fail(request: Request):
        request.state.athlete_id = "verified-owner"
        request.state.request_id = "request-identity"
        return JSONResponse(
            {"detail": "Private operational error text"}, status_code=503
        )

    install_product_friction(app)
    response = TestClient(app).post(path)
    assert response.status_code == 503
    name, args = store.client.calls[0]
    assert name == "ingest_product_event"
    assert args["p_athlete_id"] == "verified-owner"
    assert args["p_event_name"] == "operation_failed" and args["p_feature"] == feature
    assert "Private operational error text" not in str(args)


def test_legacy_choice_cannot_disable_beta_and_actor_spoof_is_rejected():
    c, store = api_client()
    assert (
        c.put("/api/analytics/preference", json={"enabled": False}).status_code == 409
    )
    assert (
        c.put(
            "/api/analytics/preference", json={"enabled": True, "athlete_id": "other"}
        ).status_code
        == 422
    )
    assert not store.client.calls


def test_history_discovery_is_not_training_recorded():
    r = report(
        [athlete()],
        [
            fact("2026-10-20"),
            fact("2026-10-21", feature="manual"),
            fact(
                "2026-10-22",
                action="history_opened",
                feature="history",
                meaningful=False,
            ),
        ],
    )
    history = next(f for f in r["features"] if f["feature"] == "history")
    assert history["athletes"] is None and history["uses"] is None
    assert history["discovered"] == 1
    assert next(f for f in r["features"] if f["feature"] == "strength")["athletes"] == 1


def test_absent_partial_and_measurable_zero_coverage():
    empty = report([athlete()])
    assert empty["coverage"]["status"] == "not_measured"
    assert next(f for f in empty["features"] if f["feature"] == "timer")["uses"] is None
    assert empty["plan_funnel"]["observed_opened"] is None
    partial = report(
        [athlete(), athlete("b")],
        optional_coverage=[dict(athlete_id="a", since="2026-10-20T00:00:00Z")],
    )
    assert partial["coverage"]["status"] == "partial"
    assert partial["coverage"]["measurable_athletes"] == 1
    assert "optional_coverage" not in partial
    measured = report(
        [athlete()],
        optional_coverage=[dict(athlete_id="a", since="2026-10-01T12:00:00Z")],
    )
    assert measured["coverage"]["status"] == "measured"
    timer = next(f for f in measured["features"] if f["feature"] == "timer")
    assert timer["uses"] == 0 and timer["percent"] == 0
    # An event remains positive evidence even after withdrawal; no past interval is invented.
    withdrawn = report(
        [athlete()], [fact("2026-10-20", feature="nutrition", action="nutrition_saved")]
    )
    assert withdrawn["coverage"]["status"] == "partial"


def test_plan_funnel_only_mature_usable_and_unique_generators():
    plans = [
        dict(
            id="mature",
            athlete_id="a",
            status="ready",
            created_at="2026-10-18T12:00:00Z",
            completed_within_7d=True,
        ),
        dict(
            id="edge",
            athlete_id="a",
            status="publishable_with_flags",
            created_at="2026-10-18T12:00:00.000001Z",
            completed_within_7d=True,
        ),
        dict(
            id="late",
            athlete_id="a",
            status="ready",
            created_at="2026-10-17T12:00:00Z",
            completed_within_7d=False,
        ),
        dict(
            id="review",
            athlete_id="a",
            status="review_required",
            created_at="2026-10-10T12:00:00Z",
            completed_within_7d=True,
        ),
        dict(
            id="pending",
            athlete_id="a",
            status="generated",
            created_at="2026-10-10T12:00:00Z",
        ),
        dict(
            id="failed",
            athlete_id="a",
            status="failed",
            created_at="2026-10-10T12:00:00Z",
        ),
    ]
    f = report([athlete()], plans_used=plans)["plan_funnel"]
    assert (f["generated"], f["generating_athletes"], f["usable"]) == (6, 1, 3)
    assert (
        f["pending"] + f["unusable"],
        f["review_required"],
        f["immature_usable"],
    ) == (
        3,
        1,
        1,
    )
    assert (f["trained_within_7d"], f["mature_usable"], f["percent"]) == (1, 2, 50)
    assert report([athlete()], plans_used=plans[1:2])["plan_funnel"]["percent"] is None


def test_planned_and_manual_session_populations_are_separate():
    sessions = [
        dict(
            id="same",
            athlete_id="a",
            started_at="2026-10-20T12:00:00Z",
            completed_at="2026-10-20T13:00:00Z",
            status="done",
        )
    ]
    sessions += sessions.copy()  # retry cannot duplicate a persisted identity
    sessions += [
        dict(
            id="modified",
            athlete_id="a",
            started_at=None,
            completed_at="2026-10-20T13:00:00Z",
            status="modified",
        )
    ]
    r = report(
        [athlete()],
        [fact("2026-10-20", feature="manual", count=3)],
        sessions=sessions,
        training_records=[
            dict(id="manual:1", athlete_id="a", occurred_at="2026-10-20T12:00:00Z")
        ]
        * 2,
    )
    assert r["session_metrics"] == dict(
        planned_started=1,
        planned_completed=1,
        planned_modified=1,
        manual_logged=3,
        distinct_recorded=1,
        matched_completed=1,
    )
    assert (
        next(
            c
            for c in r["conversions"]
            if c["label"] == "Observed session starts → completed"
        )["percent"]
        == 100
    )


def test_utc_cohort_maturity_on_midnight_and_selected_signup_period():
    data = dict(
        athletes=[
            athlete("a", "2026-10-17T23:30:00-01:00"),
            athlete("b", "2026-09-01T00:00:00Z"),
        ],
        facts=[fact("2026-10-25"), fact("2026-10-24", "b")],
    )
    r = summarize(data, days=30, now=NOW)
    assert r["retention"]["7"]["eligible"] == 0  # signup UTC Oct18 -> D7 today
    r = summarize(data, days=0, now=datetime(2026, 10, 26, tzinfo=timezone.utc))
    assert r["retention"]["7"] == dict(eligible=2, returned=1, percent=50)


def test_repeat_and_segmentation_do_not_count_refreshes_or_other_athletes():
    r = summarize(
        dict(
            athletes=[
                athlete(technical_style=["boxing"]),
                athlete("b", technical_style=["mma"]),
            ],
            facts=[
                fact("2026-10-20", feature="sparring", count=9),
                fact("2026-10-21", "b", feature="sparring"),
            ],
            optional_coverage=[dict(athlete_id="b", since="2026-10-01T00:00:00Z")],
        ),
        now=NOW,
        days=7,
        sport="boxing",
    )
    f = next(f for f in r["features"] if f["feature"] == "sparring")
    assert f["athletes"] == 1 and f["repeat_athletes"] == 0 and f["percent"] == 100
    assert r["coverage"]["measurable_athletes"] == 0


def test_identity_projection_is_admin_only_and_consent_stays_aggregate():
    c, store = api_client()
    payload = dict(
        athletes=[
            athlete(full_name=" Example Athlete ", email=" example@example.test ")
        ],
        facts=[],
        optional_coverage=[],
        tracking_health={"healthy": True},
        tracking_since="2026-10-30T00:00:00Z",
        training_records=[],
        error_returns=[],
    )
    store.client.rpc = lambda *_: SimpleNamespace(
        execute=lambda: SimpleNamespace(data=payload)
    )
    assert c.get("/api/admin/product-analytics").status_code == 403
    c.app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
        athlete_id="admin"
    )
    data = c.get("/api/admin/product-analytics").json()
    assert data["athletes"][0]["full_name"] == "Example Athlete"
    assert data["athletes"][0]["email"] == "example@example.test"
    assert all("since" not in p and "enabled" not in p for p in data["athletes"])
    assert set(
        c.post(
            "/api/analytics/events",
            json=dict(
                event_name="timer_started", feature="timer", request_id=str(uuid4())
            ),
        ).json()
    ) == {"accepted"}


def test_exact_error_return_evidence_is_required():
    facts = [
        fact("2026-10-10", action="operation_failed", meaningful=False),
        fact("2026-10-17"),
    ]
    r = report([athlete()], facts)
    assert r["friction_followup"][0]["eligible_athletes"] == 0
    r = report(
        [athlete()],
        facts,
        error_returns=[
            dict(
                athlete_id="a",
                feature="strength",
                action="operation_failed",
                error_at="2026-10-10T12:00:00Z",
                returned=True,
            )
        ],
    )
    assert r["friction_followup"][0]["eligible_athletes"] == 1
    assert r["friction_followup"][0]["no_recorded_return_7d"] == 0


def test_deleting_evidence_changes_history_without_backfill():
    before = report([athlete()], [fact("2026-10-20")])
    after_plan_delete = report([athlete()])
    after_account_delete = report()
    assert before["ever_active"] == 1
    assert (
        after_plan_delete["ever_active"] == 0
        and after_plan_delete["athletes"][0]["last_activity"] is None
    )
    assert after_account_delete["registered"] == 0


def test_api_refuses_old_projection_instead_of_inventing_zeroes():
    c, store = api_client()
    c.app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
        athlete_id="admin"
    )
    store.client.rpc = lambda *_: SimpleNamespace(
        execute=lambda: SimpleNamespace(data=dict(athletes=[], facts=[]))
    )
    assert c.get("/api/admin/product-analytics").status_code == 503


def test_internal_preference_is_disabled_without_exposing_identity():
    c, store = api_client()
    c.app.dependency_overrides[require_profile] = lambda: SimpleNamespace(
        athlete_id="owner", email=" internal@example.test "
    )

    class Query:
        def __init__(self, table):
            self.table = table

        def select(self, *_):
            return self

        def eq(self, *_):
            return self

        def limit(self, *_):
            return self

        def execute(self):
            return SimpleNamespace(
                data=[{"role": "athlete", "analytics_excluded": False}]
                if self.table == "profiles"
                else [{"email": "internal@example.test"}]
            )

    store.client.table = Query
    response = c.get("/api/analytics/preference")
    assert response.status_code == 200 and response.json() == {"enabled": False}


def test_weekly_cohorts_use_monday_and_per_athlete_maturity():
    r = summarize(
        dict(
            athletes=[
                athlete("a", "2026-10-12T12:00:00Z"),
                athlete("b", "2026-10-13T12:00:00Z"),
                athlete("c", "2026-10-19T12:00:00Z"),
            ],
            facts=[fact("2026-10-19"), fact("2026-10-14", "b")],
        ),
        now=NOW,
        days=0,
    )
    cohorts = {c["week"]: c for c in r["cohorts"]}
    assert cohorts["2026-10-12"]["retention"]["7"] == dict(
        eligible=2, returned=1, percent=50
    )
    assert cohorts["2026-10-19"]["retention"]["7"] == dict(
        eligible=0, returned=0, percent=None
    )


def test_three_active_windows_use_unique_athletes_and_distinct_days():
    r = report(
        [athlete(), athlete("b"), athlete("c", "2026-09-01T00:00:00Z")],
        [
            fact("2026-10-25", count=10),
            fact("2026-10-25", feature="manual"),
            fact("2026-10-19", "b"),
            fact("2026-09-28", "c"),
        ],
    )
    assert (r["dau"], r["wau"], r["mau"], r["stickiness"]) == (1, 2, 3, 33.33)
    assert r["average_active_days"] == 1


def test_missing_historical_onboarding_is_not_a_measured_failure():
    r = report([athlete()])
    stage = next(
        c
        for c in r["conversions"]
        if c["label"] == "Signup → onboarding completed (observed)"
    )
    assert stage["converted"] is None and stage["percent"] is None
    assert "not measured" in stage["measurement_note"]
    today = report([athlete()], [fact("2026-10-25")])
    repeat = next(
        c for c in today["funnel"] if c["label"] == "first action → repeat activity"
    )
    assert repeat["eligible"] == 0


def test_completed_training_proves_linked_plan_use_but_not_a_plan_or_history_visit():
    plans = [
        dict(id="p1", athlete_id="a", created_at="2026-10-10T12:00:00Z", status="ready")
    ]
    sessions = [
        dict(
            id="s1",
            athlete_id="a",
            plan_id="p1",
            started_at=None,
            completed_at="2026-10-20T12:00:00Z",
            status="done",
        ),
        dict(
            id="s2",
            athlete_id="a",
            plan_id="foreign",
            started_at=None,
            completed_at="2026-10-21T12:00:00Z",
            status="done",
        ),
    ]
    r = report([athlete()], sessions=sessions, plans_used=plans)
    plan = next(f for f in r["features"] if f["feature"] == "plans")
    assert plan["athletes"] == 1 and plan["uses"] == 1 and plan["repeat_athletes"] == 0
    assert plan["discovered"] is None
    assert next(f for f in r["features"] if f["feature"] == "history")["uses"] is None


@pytest.mark.parametrize(
    "event_name,feature",
    [
        ("checkin_started", "recovery"),
        ("sparring_log_started", "sparring"),
        ("push_prompt_shown", "onboarding"),
        ("push_prompt_accepted", "onboarding"),
        ("push_subscribed", "onboarding"),
    ],
)
def test_workflow_starts_are_controlled_and_never_activity(event_name, feature):
    c, s = api_client()
    body = dict(event_name=event_name, feature=feature, request_id=str(uuid4()))
    assert c.post("/api/analytics/events", json=body).status_code == 202
    assert s.client.calls[0][1]["p_event_name"] == event_name
    assert (
        c.post("/api/analytics/events", json={**body, "feature": "plans"}).status_code
        == 422
    )
    # A start alone is not a meaningful action: the athlete stays never-active.
    r = report(
        [athlete()],
        [fact("2026-10-20", action=event_name, meaningful=False, feature=feature)],
    )
    assert r["ever_active"] == 0 and r["active"] == 0


def test_preference_tolerates_a_profile_without_email():
    c, store = api_client()
    c.app.dependency_overrides[require_profile] = lambda: SimpleNamespace(
        athlete_id="owner", email=None
    )

    class Query:
        def __init__(self, table):
            self.table = table

        def select(self, *_):
            return self

        def eq(self, *_):
            return self

        def limit(self, *_):
            return self

        def execute(self):
            return SimpleNamespace(
                data=[{"role": "athlete", "analytics_excluded": False}]
                if self.table == "profiles"
                else []
            )

    store.client.table = Query
    response = c.get("/api/analytics/preference")
    assert response.status_code == 200 and response.json() == {"enabled": True}


def test_visits_are_unique_and_separate_from_training():
    r = report(
        [athlete(), athlete("b")],
        [
            fact(
                "2026-10-25",
                action="app_opened",
                feature="onboarding",
                meaningful=False,
                count=4,
            ),
            fact(
                "2026-10-24",
                action="app_opened",
                feature="onboarding",
                meaningful=False,
            ),
            fact("2026-10-25", aid="b"),
        ],
    )
    assert (r["visitors_today"], r["visitors_7d"], r["visitors_30d"]) == (2, 2, 2)
    assert r["active"] == 1
    assert r["athletes"][0]["last_seen"] == "2026-10-25T12:00:00+00:00"
    assert r["athletes"][0]["last_activity"] is None


def test_page_events_accept_only_categories_and_authenticated_actor():
    c, store = api_client()
    body = dict(event_name="page_viewed", feature="plans", request_id=str(uuid4()))
    assert c.post("/api/analytics/events", json=body).status_code == 202
    assert store.client.calls[-1][1]["p_athlete_id"] == "owner"
    assert (
        c.post(
            "/api/analytics/events", json={**body, "url": "/private?health=foo"}
        ).status_code
        == 422
    )
    assert (
        c.post(
            "/api/analytics/events", json={**body, "feature": "arbitrary"}
        ).status_code
        == 422
    )


def test_browsing_counts_as_discovery_without_inflating_training():
    r = report(
        [athlete()],
        [fact("2026-10-25", action="page_viewed", feature="timer", meaningful=False)],
    )
    timer = next(f for f in r["features"] if f["feature"] == "timer")
    assert timer["discovered"] == 1
    assert timer["athletes"] == 0
    assert timer["actions"] == {"page_viewed": 1}
    assert r["active"] == 0


def test_page_views_and_saved_actions_prove_visits_without_app_open_event():
    r = report(
        [athlete(), athlete("b"), athlete("c")],
        [
            fact(
                "2026-10-25",
                action="page_viewed",
                feature="history",
                meaningful=False,
                count=5,
            ),
            fact("2026-10-24", action="page_viewed", feature="plans", meaningful=False),
            fact("2026-10-25", aid="b"),
            fact("2026-10-25", aid="c", action="plan_generated", meaningful=False),
            fact("2026-10-25", aid="c", action="operation_failed", meaningful=False),
        ],
    )
    assert r["visitors_today"] == 2
    assert r["no_visit_7d"] == 1
    assert r["seen_recently"] == 2
    assert r["browser_reporting_athletes"] == 1
    assert r["latest_browser_event"] == "2026-10-25T12:00:00+00:00"
    assert r["athletes"][0]["visit_days_7"] == 2
    assert r["athletes"][0]["latest_action"] == {
        "feature": "history",
        "action": "page_viewed",
    }
    assert r["athletes"][2]["last_seen"] is None
    assert r["active"] == 1


def test_directory_search_before_pagination_leaves_headlines_unchanged():
    c, store = api_client()
    c.app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
        athlete_id="admin"
    )
    payload = dict(
        athletes=[
            athlete(str(n), full_name=f"Athlete {n}", email=f"tester{n}@example.test")
            for n in range(40)
        ],
        facts=[],
        optional_coverage=[],
        training_records=[],
        error_returns=[],
        tracking_health={"healthy": True},
        tracking_since="2026-10-01T00:00:00Z",
    )
    store.client.rpc = lambda *_: SimpleNamespace(
        execute=lambda: SimpleNamespace(data=payload)
    )
    result = c.get(
        "/api/admin/product-analytics?athlete_search=TESTER39&limit=1"
    ).json()
    assert result["registered"] == 40
    assert result["athlete_total"] == 1
    assert result["athletes"][0]["id"] == "39"
    assert (
        c.get("/api/admin/product-analytics?visit_filter=never_seen").json()[
            "athlete_total"
        ]
        == 40
    )
    assert (
        c.get("/api/admin/product-analytics?visit_filter=seen_today").json()[
            "athlete_total"
        ]
        == 0
    )
    assert c.get("/api/admin/product-analytics?visit_filter=invalid").status_code == 422
