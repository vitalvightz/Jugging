"""Regression tests for the consolidated Today readiness fail-safe boundary.

The boundary (``api.services.today_readiness_boundary``) delegates all decision
and typed-signal semantics to the single canonical authority
(``api.services.readiness_failsafe``). These tests prove:

* the boundary does not invent its own fail-safe decision mapping;
* check-in still fails closed with a consistent typed signal;
* unavailable injury / schedule context is a conservative ``pull_back``;
* the command view revokes a stale green recommendation;
* current-session execution is blocked (retryable 503) when injury state is
  unknown, and injury reconciliation refuses to run on a failed flag read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from api.services import today_readiness_boundary as boundary
from api.services import today_readiness_boundary_core as boundary_core
from api.services import today_service
from api.services.today_readiness_boundary import (
    build_today_command_view,
    submit_today_checkin,
    submit_today_injury_checkin,
    upsert_session_completion,
)
from api.services.readiness_failsafe import (
    CHECKINS_UNAVAILABLE,
    COMPLETIONS_UNAVAILABLE,
    CONTEXT_UNAVAILABLE,
    INJURY_CONTEXT_UNAVAILABLE,
    INTAKE_UNAVAILABLE,
    SESSION_UNAVAILABLE,
)
from tests.support import FakeStore

ATHLETE = "athlete-1"
PLAN = "11111111-1111-1111-1111-111111111111"
INTAKE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
NOW = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)

_SIGNAL_FIELDS = {
    "decision",
    "decision_tier",
    "display_state",
    "reason_codes",
    "title",
    "detail",
    "action",
    "safety",
    "blocks_training",
}


def _store_with_plan(store_type=FakeStore, *, intake_id: str | None = None) -> FakeStore:
    store = store_type()
    store.plans[PLAN] = {
        "id": PLAN,
        "athlete_id": ATHLETE,
        "status": "ready",
        "plan_name": "Camp A",
        "created_at": "2026-06-01T00:00:00+00:00",
        **({"intake_id": intake_id} if intake_id else {}),
    }
    # The active-plan pointer is authoritative (see api/services/active_plan.py):
    # seeding plans[...] alone leaves the athlete with no active plan, so the
    # command view degrades to the no-plan shape.
    store.set_active_plan_id(ATHLETE, PLAN)
    return store


def _checkin_payload(**overrides):
    payload = {
        "plan_id": PLAN,
        "sleep": "good",
        "body": "normal",
        "pain": "none",
        "phase": "GPP",
        "active_injury": "none",
        "previous_session": "none",
        "sharp_pain": False,
        "instability": False,
        "swelling": False,
        "neurological_symptoms": False,
        "illness_symptoms": False,
        "cannot_warm_into_movement": False,
        "worse_next_day_pain": False,
    }
    return {**payload, **overrides}


def _submit(store) -> dict:
    return submit_today_checkin(
        store, athlete_id=ATHLETE, athlete_timezone="", payload=_checkin_payload(), now=NOW
    )


class FailingRecentCheckinsStore(FakeStore):
    def list_today_checkins(self, athlete_id: str, *, limit: int = 14) -> list[dict]:
        raise RuntimeError("check-in history unavailable")


class FailingCompletionsStore(FakeStore):
    def list_session_completions(self, athlete_id: str, *, limit: int = 30) -> list[dict]:
        raise RuntimeError("completion history unavailable")


class FailingIntakeStore(FakeStore):
    def get_intake(self, intake_id: str):
        raise RuntimeError("intake unavailable")


class FailingInjuryFlagsStore(FakeStore):
    def list_injury_flags(
        self,
        athlete_id: str,
        *,
        statuses: tuple = ("open", "monitoring"),
        limit: int = 20,
    ) -> list[dict]:
        raise RuntimeError("injury flags unavailable")


# ---------------------------------------------------------------------------
# Module consolidation
# ---------------------------------------------------------------------------
def test_check_in_delegates_to_canonical_service(monkeypatch):
    # The boundary adds no second decision layer: it shares the save's reads
    # (like the other Today writes) and returns the canonical service's result.
    from api.services.today_reads import _BuildReadCache
    seen = {}

    def canonical(store, **kwargs):
        seen.update(store=store, **kwargs)
        return {"decided": "canonically"}

    monkeypatch.setattr(today_service, "submit_today_checkin", canonical)
    payload = {"plan_id": None, "sleep": "good", "body": "normal", "pain": "none"}
    result = boundary.submit_today_checkin(FakeStore(), athlete_id="athlete-1", athlete_timezone="UTC", payload=payload)

    assert result == {"decided": "canonically"}
    assert isinstance(seen["store"], _BuildReadCache)
    assert seen["payload"] is payload and seen["athlete_id"] == "athlete-1"
    assert boundary.resolve_today_landing is today_service.resolve_today_landing


def test_boundary_defines_no_competing_failsafe_decision():
    # There is one canonical decision mapping (apply_context_failsafe); the
    # boundary core imports it and does not define its own. The synchronization
    # wrapper holds no decision logic at all.
    assert not hasattr(boundary, "_fail_safe_decision")
    assert not hasattr(boundary_core, "_fail_safe_decision")
    from api.services import readiness_failsafe

    assert boundary_core.apply_context_failsafe is readiness_failsafe.apply_context_failsafe


def test_legacy_failsafe_module_is_gone():
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("api.services.readiness_fail_safe")


# ---------------------------------------------------------------------------
# Today check-in — fails closed, typed signal consistent
# ---------------------------------------------------------------------------
def _assert_signal_consistent(row: dict) -> dict:
    signal = row["readiness_signal"]
    assert set(signal) == _SIGNAL_FIELDS, "typed signal must always carry the full field set"
    assert row["recommendation_state"] == signal["decision"]
    decision = signal["decision"]
    if decision == "pull_back":
        assert signal["decision_tier"] == "stop"
        assert signal["blocks_training"] is True
        assert signal["display_state"] in {"hold", "unavailable"}
    elif decision == "modify":
        assert signal["decision_tier"] == "caution"
        assert signal["blocks_training"] is False
        assert signal["display_state"] == "modify"
    else:
        assert decision == "train_as_planned"
        assert signal["decision_tier"] == "clear"
        assert signal["blocks_training"] is False
        assert signal["display_state"] == "ready"
    return signal


def test_genuine_empty_context_can_still_return_train_as_planned():
    row = _submit(_store_with_plan())
    assert row["recommendation_state"] == "train_as_planned"
    signal = _assert_signal_consistent(row)
    for code in (
        CONTEXT_UNAVAILABLE,
        CHECKINS_UNAVAILABLE,
        COMPLETIONS_UNAVAILABLE,
        INTAKE_UNAVAILABLE,
        INJURY_CONTEXT_UNAVAILABLE,
        SESSION_UNAVAILABLE,
    ):
        assert code not in signal["reason_codes"]


def test_failed_checkin_read_cannot_return_train_as_planned():
    row = _submit(_store_with_plan(FailingRecentCheckinsStore))
    assert row["recommendation_state"] == "modify"
    signal = _assert_signal_consistent(row)
    assert CHECKINS_UNAVAILABLE in signal["reason_codes"]


def test_failed_completion_read_cannot_return_train_as_planned():
    row = _submit(_store_with_plan(FailingCompletionsStore))
    assert row["recommendation_state"] == "modify"
    signal = _assert_signal_consistent(row)
    assert COMPLETIONS_UNAVAILABLE in signal["reason_codes"]


def test_failed_intake_read_cannot_return_train_as_planned():
    store = _store_with_plan(FailingIntakeStore, intake_id=INTAKE)
    row = _submit(store)
    assert row["recommendation_state"] != "train_as_planned"
    signal = _assert_signal_consistent(row)
    assert INTAKE_UNAVAILABLE in signal["reason_codes"]


def test_failed_injury_flag_read_returns_pull_back():
    row = _submit(_store_with_plan(FailingInjuryFlagsStore))
    assert row["recommendation_state"] == "pull_back"
    signal = _assert_signal_consistent(row)
    assert signal["display_state"] == "unavailable"
    assert signal["blocks_training"] is True
    assert INJURY_CONTEXT_UNAVAILABLE in signal["reason_codes"]
    assert CONTEXT_UNAVAILABLE in signal["reason_codes"]


def test_failed_injury_classification_returns_pull_back(monkeypatch):
    store = _store_with_plan()
    store.create_injury_flag(
        ATHLETE,
        {
            "body_area": "Head / Neck",
            "description": "neck symptoms",
            "severity": "moderate",
            "status": "open",
        },
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("injury classifier unavailable")

    monkeypatch.setattr("api.services.today_service.injury_consequence_tier", _boom)

    row = _submit(store)
    assert row["recommendation_state"] == "pull_back"
    signal = _assert_signal_consistent(row)
    assert INJURY_CONTEXT_UNAVAILABLE in signal["reason_codes"]


def test_failed_schedule_resolution_holds_conservative_pull_back(monkeypatch):
    # Deliberate rule: if the current-day session risk cannot be resolved, hold.
    store = _store_with_plan()

    def _boom(*args, **kwargs):
        raise RuntimeError("schedule resolver unavailable")

    monkeypatch.setattr("api.services.today_service._resolve_today_session_entry", _boom)

    row = _submit(store)
    assert row["recommendation_state"] == "pull_back"
    signal = _assert_signal_consistent(row)
    assert SESSION_UNAVAILABLE in signal["reason_codes"]
    assert CONTEXT_UNAVAILABLE in signal["reason_codes"]


def test_context_failure_never_weakens_existing_pull_back():
    store = _store_with_plan(FailingRecentCheckinsStore)
    row = submit_today_checkin(
        store,
        athlete_id=ATHLETE,
        athlete_timezone="",
        payload=_checkin_payload(pain="high"),
        now=NOW,
    )
    assert row["recommendation_state"] == "pull_back"
    assert "pain is high" in row["recommendation_reason"].lower()
    _assert_signal_consistent(row)


# ---------------------------------------------------------------------------
# Command view — stale green revoked
# ---------------------------------------------------------------------------
def _seed_stored_green(store) -> None:
    store.upsert_today_checkin(
        ATHLETE,
        {
            **_checkin_payload(),
            "training_day": NOW.date().isoformat(),
            "athlete_timezone": "",
            "recommendation_state": "train_as_planned",
            "recommendation_reason": "Train as planned.",
            "recommendation_triggers": [],
        },
    )


def test_command_view_holds_stored_green_when_injury_context_unavailable():
    store = _store_with_plan(FailingInjuryFlagsStore)
    _seed_stored_green(store)

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=NOW)

    # Injury flag read failure is UNAVAILABLE -> conservative pull-back hold.
    assert view.today.recommendation_state == "pull_back"
    assert view.today.decision_tier == "pull_back"
    # Structured reason code token present (no prose parsing required).
    assert f"reason_code:{INJURY_CONTEXT_UNAVAILABLE}" in view.today.warnings
    assert any(w.startswith("readiness_context_status=unavailable") for w in view.today.warnings)
    assert any(risk.text == boundary_core._COMMAND_VIEW_REMINDER for risk in view.risk_watch)
    assert next(
        risk for risk in view.risk_watch if risk.text == boundary_core._COMMAND_VIEW_REMINDER
    ).timeframe == "active"


def test_command_view_softens_stored_green_when_context_degraded():
    store = _store_with_plan(FailingRecentCheckinsStore)
    _seed_stored_green(store)

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=NOW)

    # Recent-checkin read failure is DEGRADED -> soften to modify.
    assert view.today.recommendation_state == "modify"
    assert view.today.decision_tier == "modify"
    assert f"reason_code:{CHECKINS_UNAVAILABLE}" in view.today.warnings


def test_command_view_unaffected_when_context_complete():
    store = _store_with_plan()
    _seed_stored_green(store)
    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=NOW)
    assert view.today.recommendation_state == "train_as_planned"
    assert not any(w.startswith("readiness_context_status=") for w in view.today.warnings)


# ---------------------------------------------------------------------------
# Session completion — current-session execution blocked when injury unknown
# ---------------------------------------------------------------------------
def test_current_session_execution_blocked_when_injury_state_unknown():
    store = _store_with_plan(FailingInjuryFlagsStore)
    for exec_status in ("started", "done", "modified"):
        with pytest.raises(HTTPException) as exc:
            upsert_session_completion(
                store,
                athlete_id=ATHLETE,
                athlete_timezone="",
                payload={"plan_id": PLAN, "session_id": "session-1", "status": exec_status},
                now=NOW,
            )
        assert exc.value.status_code == 503
        assert exc.value.headers == {"Retry-After": "30"}
    assert not store.session_completions.get(ATHLETE)


def test_retro_log_does_not_require_current_injury_snapshot():
    store = _store_with_plan(FailingInjuryFlagsStore)
    yesterday = (NOW.date() - timedelta(days=1)).isoformat()
    row = upsert_session_completion(
        store,
        athlete_id=ATHLETE,
        athlete_timezone="",
        payload={
            "plan_id": PLAN,
            "session_id": "session-1",
            "status": "done",
            "training_day": yesterday,
        },
        now=NOW,
    )
    assert row["status"] == "done"
    assert row["training_day"] == yesterday


def test_skip_and_not_started_do_not_require_injury_snapshot():
    store = _store_with_plan(FailingInjuryFlagsStore)
    row = upsert_session_completion(
        store,
        athlete_id=ATHLETE,
        athlete_timezone="",
        payload={
            "plan_id": PLAN,
            "session_id": "session-1",
            "status": "skipped",
            "modification_reason": "rest day",
        },
        now=NOW,
    )
    assert row["status"] == "skipped"


# ---------------------------------------------------------------------------
# Injury reconciliation — failed flag read must not become empty state
# ---------------------------------------------------------------------------
def test_injury_reconciliation_blocked_on_failed_flag_read():
    store = _store_with_plan(FailingInjuryFlagsStore)
    with pytest.raises(HTTPException) as exc:
        submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={"injuries": [{"body_area": "knee", "description": "left knee", "status": "new"}]},
            now=NOW,
        )
    assert exc.value.status_code == 503
    assert exc.value.headers == {"Retry-After": "30"}
    # A failed read must not have created any injury (no duplicate / phantom state).
    assert not store.injury_flags.get(ATHLETE)


# ---------------------------------------------------------------------------
# The explanation is revoked with the decision it explained
#
# The fail-safe can turn a stored "train as planned" into a hold. If only the
# decision moved, the card would pair that hold with the GREEN decision's own
# contributors and confidence — "PULL BACK / Confidence: High / Fight week" —
# which is precisely the contradiction the explanation feature exists to prevent.
# ---------------------------------------------------------------------------
def _seed_stored_green_with_explanation(store) -> None:
    """A green check-in whose triggers produce real contributors and a high band."""
    store.upsert_today_checkin(
        ATHLETE,
        {
            **_checkin_payload(),
            "training_day": NOW.date().isoformat(),
            "athlete_timezone": "",
            "recommendation_state": "train_as_planned",
            "recommendation_reason": "Sharp work only.\nFight week rewards freshness.",
            "recommendation_triggers": ["fight_week", "session_risk_high", "phase_taper"],
        },
    )


def _stored_green_explanation_is_stale(view) -> bool:
    """True when any part of the green decision's explanation survived the hold."""
    return (
        "Fight week" in view.today.recommendation_context_labels
        or "Hard session planned" in view.today.recommendation_context_labels
        or view.today.recommendation_confidence == "high"
    )


def test_unavailable_context_reports_low_confidence_and_names_the_safety_context():
    store = _store_with_plan(FailingInjuryFlagsStore)
    _seed_stored_green_with_explanation(store)

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=NOW)

    assert view.today.recommendation_state == "pull_back"
    assert view.today.recommendation_confidence == "low"
    assert "injury history" in view.today.recommendation_confidence_note
    assert view.today.recommendation_trigger_labels == ["Safety history unavailable"]
    assert not _stored_green_explanation_is_stale(view)


def test_degraded_context_reports_moderate_confidence_and_incomplete_history():
    store = _store_with_plan(FailingRecentCheckinsStore)
    _seed_stored_green_with_explanation(store)

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=NOW)

    assert view.today.recommendation_state == "modify"
    assert view.today.recommendation_confidence == "moderate"
    assert "couldn't load your recent check-ins" in view.today.recommendation_confidence_note
    assert view.today.recommendation_trigger_labels == ["Recent history incomplete"]
    assert not _stored_green_explanation_is_stale(view)


def test_the_hold_never_claims_a_source_it_could_not_read():
    store = _store_with_plan(FailingRecentCheckinsStore)
    _seed_stored_green_with_explanation(store)

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=NOW)

    # The read that failed is exactly the history, so it cannot be named as an
    # input the hold was based on.
    assert "your last few check-ins" not in view.today.recommendation_sources


def test_current_poor_sleep_keeps_its_reason_when_history_cannot_be_verified():
    # Refresh uses the actual answers, not the old recommendation labels.
    store = _store_with_plan(FailingRecentCheckinsStore)
    store.upsert_today_checkin(
        ATHLETE,
        {
            **_checkin_payload(sleep="poor"),
            "training_day": NOW.date().isoformat(),
            "athlete_timezone": "",
            "recommendation_state": "modify",
            "recommendation_reason": "Session reduced.\nPoor sleep.",
            "recommendation_triggers": ["poor_sleep", "session_risk_high", "phase_spp"],
        },
    )

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=NOW)

    assert view.today.recommendation_state == "modify"
    assert view.today.recommendation_trigger_labels == ["Poor sleep"]
    assert "Hard session planned" not in view.today.recommendation_context_labels
    assert view.today.recommendation_confidence == "moderate"
    assert "refresh your recent check-ins" in view.today.recommendation_confidence_note


def test_a_complete_context_refreshes_the_explanation_from_current_inputs():
    store = _store_with_plan()
    _seed_stored_green_with_explanation(store)

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=NOW)

    assert view.today.recommendation_state == "train_as_planned"
    # The stored TAPER explanation does not match this GPP check-in/plan.
    # The unresolved session uses the existing moderate confidence band.
    assert view.today.recommendation_confidence == "moderate"
    assert "session isn't resolved yet" in view.today.recommendation_confidence_note
    assert "Fight week" not in view.today.recommendation_context_labels


def test_an_equal_band_still_adopts_the_live_failure_reason():
    # Two different problems both rank moderate: an athlete with little history,
    # and an athlete whose history could not be loaded. Keeping the stored wording
    # at an equal band told someone with a failed read that they had "no recent
    # days to compare" — untrue, and the wrong remedy, since checking in tomorrow
    # cannot fix a read that broke.
    store = _store_with_plan(FailingRecentCheckinsStore)
    store.upsert_today_checkin(
        ATHLETE,
        {
            **_checkin_payload(),
            "training_day": NOW.date().isoformat(),
            "athlete_timezone": "",
            "recommendation_state": "modify",
            "recommendation_reason": "Session reduced.\nPoor sleep.",
            "recommendation_triggers": ["poor_sleep", "sparse_history"],
        },
    )

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=NOW)

    assert view.today.recommendation_confidence == "moderate"
    assert "couldn't load your recent check-ins" in view.today.recommendation_confidence_note
    assert "no recent days to compare" not in view.today.recommendation_confidence_note


def test_a_re_check_never_claims_to_have_used_what_it_could_not_reload():
    # The old decision used history, but this fresh evaluation could not load
    # it. Its sources and confidence must describe the current evaluation.
    store = _store_with_plan(FailingRecentCheckinsStore)
    store.upsert_today_checkin(
        ATHLETE,
        {
            **_checkin_payload(),
            "training_day": NOW.date().isoformat(),
            "athlete_timezone": "",
            "recommendation_state": "modify",
            "recommendation_reason": "Session reduced.\nPoor sleep for 3 days.",
            "recommendation_triggers": ["poor_sleep_3_day_streak", "repeated_poor_readiness"],
        },
    )

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=NOW)

    # A fresh failure must never claim the old history as a current source.
    assert view.today.recommendation_confidence == "moderate"
    assert "couldn't load your recent check-ins" in view.today.recommendation_confidence_note
    assert "your last few check-ins" not in view.today.recommendation_sources


def test_a_replaced_decision_is_made_now_and_stays_present_tense():
    # Nothing historical here: the fail-safe made this call in this request, and
    # its sources were rebuilt to match. Past tense would be wrong.
    store = _store_with_plan(FailingRecentCheckinsStore)
    _seed_stored_green_with_explanation(store)

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=NOW)

    assert "couldn't load your recent check-ins" in view.today.recommendation_confidence_note


def test_standalone_rehab_can_be_declined_on_a_rest_day_without_an_injury_read():
    store = _store_with_plan(FailingInjuryFlagsStore)
    day = today_service.resolve_training_day("", now=NOW)
    store.plans[PLAN]["structured_plan"] = {"weeks": [{"days": [{"date": day, "day_type": "rest", "sessions": []}]}]}
    row = upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="", now=NOW,
        payload={"plan_id": PLAN, "session_id": f"rehab-{day}", "status": "skipped", "modification_reason": "Resting today"})
    assert row["status"] == "skipped" and not row.get("prescription_snapshot")


def test_stopped_logging_for_owned_frozen_session_survives_failed_injury_read():
    store = _store_with_plan(FailingInjuryFlagsStore)
    day = today_service.resolve_training_day("", now=NOW)
    store.upsert_session_completion(ATHLETE, {
        "plan_id": PLAN, "session_id": "session-1", "training_day": day,
        "status": "started", "started_at": NOW.isoformat(),
        "prescription_snapshot": {"plan_id": PLAN, "session": {"session_id": "session-1"}},
    })
    payload = {"plan_id": PLAN, "session_id": "session-1", "status": "modified",
               "rehab_performance": "stopped", "modification_reason": "Stopped when symptoms changed"}
    row = upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="", payload=payload, now=NOW)
    assert row["status"] == "modified" and row["rehab_performance"] == "stopped"
    with pytest.raises(HTTPException) as refused:
        upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="", payload={**payload, "session_id": "unstarted"}, now=NOW)
    assert refused.value.status_code == 503


def test_unavailable_context_holds_frozen_rehab_even_with_conservative_checkin():
    view = boundary_core.CommandView(today={"training_day": "2026-06-18",
        "recommendation_state": "modify", "decision_tier": "modify"},
        live_prescription={"revision": "a" * 64, "frozen": True, "safety_hold": False,
                           "session": {"session_id": "rehab", "session_type": "rehab"}})
    health = boundary_core.ReadinessContextHealth()
    health.record("injury_flags", RuntimeError("unavailable"))
    held = boundary_core._apply_fail_safe_to_command_view(view, health)
    assert held.live_prescription["safety_hold"]
    assert held.live_prescription["revision"] == "a" * 64
    assert held.today.decision_tier == "stop"
