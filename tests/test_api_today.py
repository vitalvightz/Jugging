"""API tests for the Block 4 Today/Overview endpoints.

Covers the Today check-in submit (+ server recommendation), session-completion
upsert, the normalized command view, and the landing endpoint. Uses the
in-process FakeStore via the shared test client.
"""

from datetime import date, timedelta

from api.routes import today as today_routes
from tests.support import _build_client, withdraw_health_consent

ATHLETE = {"Authorization": "Bearer athlete-token"}
PLAN_ID = "11111111-1111-1111-1111-111111111111"
OTHER_PLAN = "22222222-2222-2222-2222-222222222222"


def _seed_plan(store, plan_id: str = PLAN_ID, athlete_id: str = "athlete-1") -> str:
    store.plans[plan_id] = {
        "id": plan_id,
        "athlete_id": athlete_id,
        "status": "ready",
        "plan_name": "Camp A",
        "created_at": "2026-06-01T00:00:00+00:00",
    }
    # The active-plan pointer is authoritative (see api/services/active_plan.py):
    # seeding plans[...] alone leaves the athlete with no active plan, so the
    # command view degrades to the no-plan shape.
    store.set_active_plan_id(athlete_id, plan_id)
    return plan_id


def _taper_planning_brief() -> dict:
    return {
        "weekly_role_map": {
            "weeks": [
                {
                    "phase": "TAPER",
                    "hard_sparring_plan": [],
                }
            ]
        }
    }


def _checkin_body(**overrides) -> dict:
    base = {"plan_id": PLAN_ID, "sleep": "good", "body": "normal", "pain": "none", "phase": "GPP"}
    return {**base, **overrides}


class TestTodayCheckin:
    def test_checkin_persists_and_returns_recommendation(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        resp = client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body(sleep="poor"))
        assert resp.status_code == 201
        body = resp.json()
        assert body["recommendation_state"] == "modify"
        assert body["recommendation_reason"].splitlines() == [
            "Session reduced.",
            "Poor sleep means your body has less room to recover today.",
            "Cut 1 round and do not add extra conditioning.",
        ]
        assert "poor_sleep" in body["triggers"]
        assert store.today_checkins["athlete-1"], "check-in row must persist"

    def test_same_day_checkin_upserts_single_row(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body())
        client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body(sleep="poor"))
        assert len(store.today_checkins["athlete-1"]) == 1
        assert store.today_checkins["athlete-1"][0]["recommendation_state"] == "modify"

    def test_same_day_other_plan_checkin_reuses_daily_answers(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        _seed_plan(store, plan_id=OTHER_PLAN)

        first = client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body(plan_id=OTHER_PLAN))
        checkin_id = store.today_checkins["athlete-1"][0]["id"]
        second = client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body())

        assert first.status_code == 201
        assert second.status_code == 201
        assert len(store.today_checkins["athlete-1"]) == 1
        assert store.today_checkins["athlete-1"][0]["id"] == checkin_id
        assert second.json()["warnings"] == []

    def test_client_supplied_recommendation_is_ignored(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        resp = client.post(
            "/api/today/checkin",
            headers=ATHLETE,
            json=_checkin_body(pain="high", recommendation_state="train_as_planned"),
        )
        assert resp.json()["recommendation_state"] == "pull_back"


class TestPlanOwnership:
    def _seed_other(self, store):
        # Plan belongs to a different athlete.
        store.plans[OTHER_PLAN] = {
            "id": OTHER_PLAN,
            "athlete_id": "someone-else",
            "status": "ready",
            "plan_name": "Other",
            "created_at": "2026-06-01T00:00:00+00:00",
        }

    def test_checkin_rejected_for_unowned_plan(self):
        client, store, _ = _build_client()
        self._seed_other(store)
        resp = client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body(plan_id=OTHER_PLAN))
        assert resp.status_code == 404
        assert not store.today_checkins.get("athlete-1")

    def test_completion_rejected_for_unowned_plan(self):
        client, store, _ = _build_client()
        self._seed_other(store)
        resp = client.post(
            "/api/today/session-completion",
            headers=ATHLETE,
            json={"plan_id": OTHER_PLAN, "session_id": "s1", "status": "started"},
        )
        assert resp.status_code == 404
        assert not store.session_completions.get("athlete-1")

    def test_checkin_rejects_malformed_plan_id(self):
        client, store, _ = _build_client()
        resp = client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body(plan_id="not-a-uuid"))
        assert resp.status_code == 422
        assert not store.today_checkins.get("athlete-1")


class TestSessionCompletion:
    def _post(self, client, **overrides):
        base = {"plan_id": PLAN_ID, "session_id": "s1", "status": "started"}
        # Logging today's session needs today's check-in (session_hold).
        client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body())
        return client.post("/api/today/session-completion", headers=ATHLETE, json={**base, **overrides})

    def test_started_completion(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        resp = self._post(client, status="started")
        assert resp.status_code == 201
        body = resp.json()
        assert body["completion_status"] == "started"
        assert body["landing_session_state"] == "resume"
        assert body["completion"]["started_at"]

    def test_completion_without_health_consent_strips_pain(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        withdraw_health_consent(store)

        resp = self._post(client, status="done", session_rpe=7, pain_after=8)

        assert resp.status_code == 201
        assert resp.json()["completion"]["status"] == "done"
        assert resp.json()["completion"]["pain_after"] is None
        assert store.session_completions["athlete-1"][0]["pain_after"] is None

    def test_done_requires_completed_at_is_stamped(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        body = self._post(client, status="done").json()
        assert body["completion_status"] == "done"
        assert body["completion"]["completed_at"]
        assert body["landing_session_state"] == "completed"

    def test_modified_without_reason_is_rejected(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        assert self._post(client, status="modified").status_code == 422
        ok = self._post(client, status="modified", modification_reason="swapped to recovery")
        assert ok.status_code == 201

    def test_skipped_completion(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        body = self._post(client, status="skipped", modification_reason="travel day").json()
        assert body["completion_status"] == "skipped"
        assert body["landing_session_state"] == "completed"

    def test_skipped_without_reason_is_rejected(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        assert self._post(client, status="skipped").status_code == 422

    def test_retro_training_day_is_accepted_and_persisted(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        # Resolve the server's current training day from a normal completion,
        # then back-fill the day before it.
        today = self._post(client, status="started").json()["completion"]["training_day"]
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        resp = self._post(
            client, status="done", training_day=yesterday, session_id="s-past", session_rpe=6
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["completion"]["training_day"] == yesterday
        assert body["completion"]["status"] == "done"

    def test_retro_training_day_rejects_malformed_dates(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        assert self._post(client, status="done", training_day="not-a-date").status_code == 422

    def test_retro_training_day_rejects_past_non_terminal_status(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        today = self._post(client, status="started").json()["completion"]["training_day"]
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        assert self._post(client, status="started", training_day=yesterday).status_code == 422

    def test_duplicate_completion_upserts_single_row(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        self._post(client, status="started")
        self._post(client, status="done")
        assert len(store.session_completions["athlete-1"]) == 1
        assert store.session_completions["athlete-1"][0]["status"] == "done"


class TestTodayState:
    def test_no_active_plan_returns_intake_cta(self):
        client, _store, _ = _build_client()
        body = client.get("/api/today", headers=ATHLETE).json()
        assert body["active_plan"] == {}
        assert [a["id"] for a in body["quick_actions"]] == ["complete_intake"]
        assert body["today"]["recommendation_state"] == "not_checked_in"

    def test_active_plan_without_checkin_is_not_checked_in(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        body = client.get("/api/today", headers=ATHLETE).json()
        assert body["active_plan"]["id"] == PLAN_ID
        assert body["today"]["recommendation_state"] == "not_checked_in"
        assert {a["id"] for a in body["quick_actions"]} == {"open_today", "view_plan"}

    def test_valid_recommendation_is_mirrored(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body(sleep="poor"))
        body = client.get("/api/today", headers=ATHLETE).json()
        assert body["today"]["recommendation_state"] == "modify"
        assert body["today"]["recommendation_reason"]

    def test_active_plan_phase_is_serialized_from_current_week(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        store.plans[PLAN_ID]["planning_brief"] = _taper_planning_brief()
        body = client.get("/api/today", headers=ATHLETE).json()
        assert body["active_plan"]["phase"] == "TAPER"


class TestHistoryEndpoints:
    def _seed_completion(self, store, *, session_id: str, training_day: str, status: str = "done"):
        store.upsert_session_completion(
            "athlete-1",
            {
                "plan_id": PLAN_ID,
                "session_id": session_id,
                "training_day": training_day,
                "status": status,
                "session_rpe": 7,
                "pain_after": 1,
                "modification_reason": "reason" if status in {"modified", "skipped"} else "",
                "notes": "",
                "started_at": f"{training_day}T10:00:00+00:00",
                "completed_at": f"{training_day}T11:00:00+00:00",
            },
        )

    def test_session_completion_history_is_newest_first(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        self._seed_completion(store, session_id="s1", training_day="2026-06-01")
        self._seed_completion(store, session_id="s2", training_day="2026-06-03")
        self._seed_completion(store, session_id="s3", training_day="2026-06-02", status="skipped")
        body = client.get("/api/today/session-completions", headers=ATHLETE).json()
        assert [row["training_day"] for row in body] == ["2026-06-03", "2026-06-02", "2026-06-01"]
        assert body[1]["status"] == "skipped"
        assert body[0]["session_rpe"] == 7

    def test_session_history_names_the_exact_owned_plan_session(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        store.plans[PLAN_ID]["structured_plan"] = {
            "weeks": [{"days": [{
                "date": "2026-06-01",
                "today_card": {"headline": "Training day"},
                "sessions": [
                    {"session_id": "s1", "title": "Strength base"},
                    {"session_id": "s2", "title": "Boxing rounds"},
                ],
            }]}],
        }
        self._seed_completion(store, session_id="s2", training_day="2026-06-01")
        self._seed_completion(store, session_id="missing", training_day="2026-06-02")
        body = client.get("/api/today/session-completions", headers=ATHLETE).json()
        assert body[0]["session_title"] is None
        assert body[1]["session_title"] == "Boxing rounds"

        # A completion pointing at another athlete's plan must not expose its title.
        store.plans[PLAN_ID]["athlete_id"] = "someone-else"
        body = client.get("/api/today/session-completions", headers=ATHLETE).json()
        assert all(row["session_title"] is None for row in body)

    def test_session_completion_history_respects_limit(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        for day in ("2026-06-01", "2026-06-02", "2026-06-03"):
            self._seed_completion(store, session_id=f"s-{day}", training_day=day)
        body = client.get("/api/today/session-completions?limit=2", headers=ATHLETE).json()
        assert len(body) == 2
        assert client.get("/api/today/session-completions?limit=0", headers=ATHLETE).status_code == 422
        assert client.get("/api/today/session-completions?limit=999", headers=ATHLETE).status_code == 422

    def test_session_completion_history_requires_auth(self):
        client, _store, _ = _build_client()
        assert client.get("/api/today/session-completions").status_code in (401, 403)

    def test_session_completion_history_is_scoped_to_the_athlete(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        store.upsert_session_completion(
            "someone-else",
            {
                "plan_id": OTHER_PLAN,
                "session_id": "sx",
                "training_day": "2026-06-01",
                "status": "done",
                "started_at": "2026-06-01T10:00:00+00:00",
                "completed_at": "2026-06-01T11:00:00+00:00",
            },
        )
        assert client.get("/api/today/session-completions", headers=ATHLETE).json() == []

    def test_checkin_history_is_newest_first(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body(sleep="poor"))
        body = client.get("/api/today/checkins", headers=ATHLETE).json()
        assert len(body) == 1
        assert body[0]["sleep"] == "poor"
        assert body[0]["recommendation_state"] == "modify"
        assert isinstance(body[0]["recommendation_triggers"], list)

    def test_checkin_history_requires_auth(self):
        client, _store, _ = _build_client()
        assert client.get("/api/today/checkins").status_code in (401, 403)


class TestInjuryCheckin:
    def test_injury_checkin_opens_flag_and_lists_it_on_command_view(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        resp = client.post(
            "/api/today/injury-checkin",
            headers=ATHLETE,
            json={"injuries": [{"body_area": "left knee", "status": "ongoing"}]},
        )
        assert resp.status_code == 201
        open_injuries = resp.json()["open_injuries"]
        assert len(open_injuries) == 1
        assert open_injuries[0]["status"] == "open"
        assert open_injuries[0]["latest_reported_status"] == "ongoing"

        command = client.get("/api/today", headers=ATHLETE).json()
        assert len(command["open_injuries"]) == 1
        assert command["open_injuries"][0]["latest_reported_status"] == "ongoing"
        categories = [risk["category"] for risk in command["risk_watch"]]
        assert "reminder" in categories

    def test_today_injury_rows_carry_the_loading_group_in_load_region(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        opened = client.post(
            "/api/today/injury-checkin",
            headers=ATHLETE,
            json={"injuries": [{"body_area": "left ankle", "status": "ongoing"}]},
        ).json()
        # A stored flag keeps the specific location rehab exposures match on.
        assert opened["open_injuries"][0]["body_region"] == "ankle"

        [injury] = client.get("/api/today", headers=ATHLETE).json()["open_injuries"]
        assert injury["canonical_location"] == "ankle"
        assert injury["load_region"] == "lower_limb"
        assert injury["body_region"] == "ankle"

    def test_injury_checkin_resolve_clears_open_injuries(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        opened = client.post(
            "/api/today/injury-checkin",
            headers=ATHLETE,
            json={"injuries": [{"body_area": "calf", "status": "ongoing"}]},
        ).json()
        flag_id = opened["open_injuries"][0]["id"]

        resolved = client.post(
            "/api/today/injury-checkin",
            headers=ATHLETE,
            json={"injuries": [{"flag_id": flag_id, "status": "resolved"}]},
        )
        assert resolved.status_code == 201
        assert resolved.json()["open_injuries"] == []

    def test_injury_checkin_status_update_preserves_existing_severity(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        opened = client.post(
            "/api/today/injury-checkin",
            headers=ATHLETE,
            json={"injuries": [{"body_area": "shoulder", "severity": "severe", "status": "worse"}]},
        ).json()
        flag_id = opened["open_injuries"][0]["id"]

        updated = client.post(
            "/api/today/injury-checkin",
            headers=ATHLETE,
            json={"injuries": [{"flag_id": flag_id, "status": "ongoing"}]},
        )

        assert updated.status_code == 201
        assert updated.json()["open_injuries"][0]["severity"] == "severe"

    def test_injury_update_invalidates_only_the_submitted_existing_flag(self, monkeypatch):
        invalidated_actions: list[str] = []
        monkeypatch.setattr(
            today_routes,
            "invalidate_notification_action",
            lambda _store, **kwargs: invalidated_actions.append(kwargs["action_key"]) or 0,
        )
        client, store, _ = _build_client()
        _seed_plan(store)
        opened = client.post(
            "/api/today/injury-checkin",
            headers=ATHLETE,
            json={
                "injuries": [
                    {"body_area": "left shoulder", "status": "ongoing"},
                    {"body_area": "right knee", "status": "ongoing"},
                ]
            },
        ).json()["open_injuries"]
        injury_ids = {injury["body_area"]: injury["id"] for injury in opened}

        invalidated_actions.clear()
        response = client.post(
            "/api/today/injury-checkin",
            headers=ATHLETE,
            json={
                "injuries": [
                    {
                        "flag_id": injury_ids["left shoulder"],
                        "status": "improving",
                    }
                ]
            },
        )

        assert response.status_code == 201
        assert invalidated_actions == [f"update-injury:{injury_ids['left shoulder']}"]
        assert f"update-injury:{injury_ids['right knee']}" not in invalidated_actions

    def test_injury_checkin_refreshes_existing_readiness_recommendation(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        checkin = client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body())
        assert checkin.status_code == 201
        assert checkin.json()["recommendation_state"] == "train_as_planned"

        injury = client.post(
            "/api/today/injury-checkin",
            headers=ATHLETE,
            json={"injuries": [{"body_area": "belly", "severity": "severe", "status": "worse"}]},
        )
        assert injury.status_code == 201

        body = client.get("/api/today", headers=ATHLETE).json()
        assert body["today"]["recommendation_state"] == "pull_back"
        assert "Rehab only today." in body["today"]["recommendation_reason"]

    def test_new_injury_without_identity_is_rejected(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        resp = client.post(
            "/api/today/injury-checkin",
            headers=ATHLETE,
            json={"injuries": [{"status": "ongoing"}]},
        )
        assert resp.status_code == 422

    def test_stale_flag_id_without_identity_is_rejected(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        resp = client.post(
            "/api/today/injury-checkin",
            headers=ATHLETE,
            json={"injuries": [{"flag_id": "ghost", "status": "ongoing"}]},
        )
        assert resp.status_code == 422
        assert "body_area or description" in resp.json()["detail"]
        assert store.injury_flags.get("athlete-1", []) == []


class TestLanding:
    def test_no_plan_routes_to_intake(self):
        client, _store, _ = _build_client()
        body = client.get("/api/today/landing", headers=ATHLETE).json()
        assert body["target"] == "intake"
        assert body["row"] == 1

    def test_plan_without_checkin_routes_to_overview_cta(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        body = client.get("/api/today/landing", headers=ATHLETE).json()
        assert body["target"] == "overview"
        assert body["cta"] == "check_in"
        assert body["row"] == 6

    def test_checked_in_today_routes_to_today(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body())
        body = client.get("/api/today/landing", headers=ATHLETE).json()
        assert body["target"] == "today"
        assert body["row"] == 5


class TestTypedSafetyContract:
    """P3: the response carries backend-owned typed safety fields alongside the
    existing shape, so the frontend never infers safety from prose."""

    def test_normal_readiness_typed_fields(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        body = client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body()).json()
        assert body["decision"] == "train_as_planned"
        assert body["decision_tier"] == "clear"
        assert body["display_state"] == "ready"
        assert body["blocks_training"] is False
        # Copy fields are present and typed separately from the joined prose.
        assert body["title"]
        assert isinstance(body["reason_codes"], list)

    def test_severe_injury_typed_fields_block_training(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        # pain=high is a hard readiness stop (pull_back) with complete context.
        body = client.post(
            "/api/today/checkin", headers=ATHLETE, json=_checkin_body(pain="high")
        ).json()
        assert body["recommendation_state"] == "pull_back"
        assert body["decision"] == "pull_back"
        assert body["decision_tier"] == "stop"
        assert body["display_state"] == "hold"
        assert body["blocks_training"] is True

    def test_typed_contract_is_backward_compatible(self):
        client, store, _ = _build_client()
        _seed_plan(store)
        body = client.post("/api/today/checkin", headers=ATHLETE, json=_checkin_body()).json()
        # Existing fields are untouched.
        assert "recommendation_state" in body
        assert "recommendation_reason" in body
        assert "triggers" in body
        assert "warnings" in body
        # The typed decision agrees with the legacy state field.
        assert body["decision"] == body["recommendation_state"]
