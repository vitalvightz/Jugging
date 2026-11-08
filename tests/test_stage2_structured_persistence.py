"""Stage 2 structured-plan persistence, mapping, and automator integration.

These tests cover the additive structured_plan path end-to-end without breaking
the legacy raw plan_text flow:

* persistence: ``SupabaseAppStore.create_plan`` writes the new columns.
* mapping: ``_map_plan_detail`` surfaces a valid structured plan, falls back to
  plan_text when it is missing/malformed, and exposes admin debug status.
* automator: ``OpenAIStage2Automator.finalize`` attempts structured generation
  (incl. one repair retry) when enabled, and never blocks the raw plan.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


import api.stage2_automation as stage2_module
import api.services.admin_stage2_service as admin_stage2_service
from api.plan_mappers import _map_plan_detail
from api.services.admin_stage2_service import (
    _APPROVAL_STRUCTURED_PLAN_BUDGET_SECONDS,
    _admin_approved_result,
    _approval_structured_budget_seconds,
    approve_review_required_plan,
    backfill_structured_plans,
    list_structured_plan_backfill_candidates,
    prewarm_structured_plan,
    prepare_structured_plan_rebuild,
    run_structured_plan_post_processing,
    self_heal_orphaned_structured_cards,
    should_prewarm_review_plan_row,
    submit_manual_stage2,
)
from api.structured_card_lifecycle import (
    STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY,
    has_fresh_structured_card_attempt,
)
from api.stage2_automation import (
    OpenAIStage2Automator,
    attempt_structured_plan_for_result,
)
from api.store import SupabaseAppStore
from api.structured_plan_generation import StructuredPlanOutcome, build_structured_plan_outcome
from api.structured_plan_models import SCHEMA_VERSION

from support import FakeOpenAIClient as _FakeClient
from support import FakeStore, _build_request, _now
from test_structured_plan_models import _valid_plan as _schema_valid_plan


def _valid_plan():
    plan = _schema_valid_plan()
    # Persistence fixtures carry actionable authored coaching rather than the
    # schema example's bare alternate exercise with unknown equipment.
    for week in plan["weeks"]:
        for day in week["days"]:
            for session in day["sessions"]:
                for block in session["blocks"]:
                    block["regression_options"] = ["Use a shallower pain-free squat."]
    return plan


class _StructuredAutomator:
    """Minimal automator exposing only the structured conversion hook."""

    def __init__(self, outcome: StructuredPlanOutcome, costs=None):
        self.outcome = outcome
        self.costs = costs or []
        self.calls: list[dict] = []

    async def _attempt_structured_plan(self, *, final_plan_text, planning_brief, source, log_context=None):
        self.calls.append(
            {"final_plan_text": final_plan_text, "planning_brief": planning_brief, "source": source}
        )
        return self.outcome, list(self.costs)


def _faithful_source(plan: dict) -> str:
    """Markdown faithful to ``plan`` so the faithfulness gate returns a clean card.

    The faithfulness gate rejects a countdown-claiming card whose source text has
    no D-day marker, so a placeholder like ``# final plan`` would degrade the
    outcome to ``invalid_fallback_used``. Derive a real countdown source from the
    plan: each week's bounds plus, per day, a D-day header with its exercise names.
    """
    lines = ["# FIGHT CAMP PLAN", ""]
    for week in plan.get("weeks") or []:
        lines.append(
            f"## Week: SPP ({week.get('countdown_start')} to {week.get('countdown_end')})"
        )
        lines.append("")
        for day in week.get("days") or []:
            lines.append(f"### Day ({day.get('countdown_label') or ''}): Session")
            for session in day.get("sessions") or []:
                for block in session.get("blocks") or []:
                    name = block.get("display_name")
                    if name:
                        lines.append(f"- {name}")
            lines.append("")
    return "\n".join(lines)


def _valid_outcome(raw_markdown: str = "") -> StructuredPlanOutcome:
    # The card must be a faithful projection of its source for
    # build_structured_plan_outcome to return a clean (valid) card, so derive the
    # source from the plan itself. A caller-supplied label is kept as a leading
    # heading purely for readability of which flow built the outcome.
    plan = _valid_plan()
    source = _faithful_source(plan)
    if raw_markdown.strip():
        source = f"{raw_markdown}\n\n{source}"
    return build_structured_plan_outcome(plan, raw_markdown=source)


# A Stage 2 first-pass markdown faithful to ``_valid_plan()`` so the structured
# conversion's faithfulness gate returns a clean card (the card-first publish gate
# then keeps the plan ready). Used by finalize tests that expect a valid card.
# Stripped because the automator trims model output before persisting plan_text.
_FAITHFUL_FINAL_PLAN = _faithful_source(_valid_plan()).strip()


# ---------------------------------------------------------------------------
# Persistence: create_plan writes structured_plan + schema_version
# ---------------------------------------------------------------------------


def _capture_create_plan(result: dict) -> dict:
    """Run create_plan against a mock client and return the inserted payload."""
    store = SupabaseAppStore(client=MagicMock(), admin_emails=set())
    captured: dict = {}

    def _insert(payload: dict):
        captured["payload"] = payload
        handle = MagicMock()
        handle.execute.return_value = MagicMock(data=[{"id": "plan-1", **payload}])
        return handle

    store.client.table.return_value.insert.side_effect = _insert
    store.create_plan(
        athlete_id="athlete-1",
        intake_id="intake-1",
        request=_build_request(),
        result=result,
    )
    return captured["payload"]


# A. Existing raw-only Stage 2 plan still saves (structured columns stay NULL).
def test_create_plan_raw_only_leaves_structured_columns_null():
    payload = _capture_create_plan(
        {"status": "ready", "plan_text": "# raw", "final_plan_text": "# raw"}
    )
    assert payload["plan_text"] == "# raw"
    assert payload["structured_plan"] is None
    assert payload["schema_version"] is None


# B. Valid structured output is persisted beside plan_text.
def test_create_plan_persists_valid_structured_plan():
    structured = _valid_plan()
    payload = _capture_create_plan(
        {
            "status": "ready",
            "plan_text": "# raw",
            "final_plan_text": "# raw",
            "structured_plan": structured,
            "schema_version": SCHEMA_VERSION,
        }
    )
    assert payload["plan_text"] == "# raw"  # raw fallback kept
    assert payload["structured_plan"] == structured
    assert payload["schema_version"] == SCHEMA_VERSION


def test_plan_creation_persists_the_same_derived_ids_every_reader_uses():
    from uuid import UUID
    from api.services.effective_structured_plan import resolve_effective_structured_plan

    structured = _valid_plan()
    session = structured["weeks"][0]["days"][0]["sessions"][0]
    session["session_id"] = None
    payload = _capture_create_plan({"status": "ready", "structured_plan": structured})
    UUID(payload["id"])
    saved = payload["structured_plan"]["weeks"][0]["days"][0]["sessions"][0]["session_id"]
    assert saved.startswith(f"ses-{payload['id'][:8]}-")
    assert resolve_effective_structured_plan(payload) == payload["structured_plan"]
    assert session["session_id"] is None  # input remains untouched


def test_stage2_and_artifact_writes_persist_missing_ids_with_existing_plan_scope():
    from api.services.effective_structured_plan import ensure_structured_session_ids

    structured = _valid_plan()
    structured["weeks"][0]["days"][0]["sessions"][0]["session_id"] = None
    plan_id = "11111111-2222-3333-4444-555555555555"
    expected = ensure_structured_session_ids(structured, plan_id=plan_id)
    store = SupabaseAppStore(client=MagicMock(), admin_emails=set())
    stage2 = store._build_plan_stage2_payload({"id": plan_id, "status": "ready"},
                                             {"status": "ready", "structured_plan": structured})
    assert stage2["structured_plan"] == expected
    store.get_plan = MagicMock(return_value={"id": plan_id, "structured_plan": expected})
    store.update_plan_structured_artifacts(plan_id, structured_plan=structured,
                                          schema_version=SCHEMA_VERSION, stage2_validator_report={})
    written = store.client.table.return_value.update.call_args.args[0]
    assert written["structured_plan"] == expected


# ---------------------------------------------------------------------------
# Mapping: _map_plan_detail
# ---------------------------------------------------------------------------


def _plan_row(**overrides) -> dict:
    row = {
        "id": "plan-1",
        "athlete_id": "athlete-1",
        "full_name": "Ari Mensah",
        "status": "generated",
        "plan_text": "# raw plan",
        "stage2_validator_report": {"errors": [], "warnings": []},
    }
    row.update(overrides)
    return row


def test_parsing_metadata_comes_from_its_own_column_not_the_stage2_payload():
    """The stage2_payload fallback for parsing_metadata was dead and is gone.

    Stage 1 sets ``stage2_payload["input_parsing_metadata"]`` and the top-level
    ``parsing_metadata`` from the same ``plan_input.parsing_metadata`` in the same
    result (fightcamp/main.py), so the fallback could only ever repeat what the
    column already held. Rows predating the column predate ``stage2_payload`` too
    (both added by 20260427120000, neither backfilled), so there was no legacy era
    where it supplied anything either.

    Pinning it: a row whose column is empty maps to empty, even when the payload
    carries a value. Anything relying on the old fallback was relying on a value
    that, in production, was always equal to the column.
    """
    detail = _map_plan_detail(
        _plan_row(
            parsing_metadata={},
            stage2_payload={"input_parsing_metadata": {"athlete_timezone": "stale"}},
        ),
        include_admin=True,
    )

    assert detail.admin_outputs.parsing_metadata == {}
    # The payload itself is still surfaced verbatim for admin debugging.
    assert detail.admin_outputs.stage2_payload == {
        "input_parsing_metadata": {"athlete_timezone": "stale"}
    }


def test_parsing_metadata_column_is_surfaced_when_present():
    detail = _map_plan_detail(
        _plan_row(parsing_metadata={"athlete_timezone": {"source": "profile"}}),
        include_admin=True,
    )

    assert detail.admin_outputs.parsing_metadata == {
        "athlete_timezone": {"source": "profile"}
    }


# H (valid) + B mapping: structured plan surfaces in outputs.
def test_map_plan_detail_returns_structured_plan_when_valid():
    detail = _map_plan_detail(
        _plan_row(structured_plan=_valid_plan()), include_admin=True
    )
    assert detail.outputs.plan_text == "# raw plan"
    assert detail.outputs.structured_plan is not None
    assert detail.outputs.schema_version == SCHEMA_VERSION
    assert detail.admin_outputs.structured_schema_version == SCHEMA_VERSION


def test_map_plan_detail_collapses_a_stale_tactical_watch_shell_on_read():
    structured = _valid_plan()
    day = structured["weeks"][0]["days"][0]
    canonical = day["sessions"][0]
    canonical["title"] = "Tactical Focus"
    day["sessions"].insert(
        0,
        {
            **canonical,
            "session_id": "watch-shell",
            "title": "Barbell Back Squat",
            "blocks": [],
        },
    )
    planning_brief = {
        "weeks": [{"session_roles": [{
            "scheduled_countdown_label": "D-15",
            "athlete_facing_label": "Tactical Focus",
            "governance": {
                "selected_drill_locked": True,
                "selected_drill_name": "Barbell Back Squat",
            },
            "tactical_watch": {
                "name": "Barbell Back Squat",
                "why": "Rehearse the selected tactical response.",
                "duration_min": 8,
                "instructions": ["Review the chosen sequence."],
                "mindset": {
                    "intent": "Stay calm.",
                    "focus": "See the sequence.",
                    "reset": "Reset and review.",
                    "anchor": "Stay precise.",
                },
                "progress": "Keep the review concise.",
            },
        }]}],
    }

    detail = _map_plan_detail(
        _plan_row(structured_plan=structured, planning_brief=planning_brief),
        include_admin=False,
    )

    sessions = detail.outputs.structured_plan.weeks[0].days[0].sessions
    assert [session.title for session in sessions] == ["Tactical Focus"]
    assert sessions[0].blocks[0].display_name == "Barbell Back Squat"
    # The read repair is intentionally non-persistent: it changes only the
    # returned card, never the stored training-plan payload.
    assert [session["title"] for session in day["sessions"]] == [
        "Barbell Back Squat",
        "Tactical Focus",
    ]


# A + H (none): legacy row with no structured_plan returns plan_text only.
def test_map_plan_detail_falls_back_to_plan_text_when_missing():
    detail = _map_plan_detail(_plan_row(), include_admin=True)
    assert detail.outputs.structured_plan is None
    assert detail.outputs.schema_version is None
    assert detail.outputs.plan_text == "# raw plan"
    assert detail.admin_outputs.structured_plan_status == "not_attempted"


# H (malformed): a malformed structured_plan is dropped, plan_text still returns.
def test_map_plan_detail_drops_malformed_structured_plan():
    detail = _map_plan_detail(
        _plan_row(structured_plan={"plan_metadata": "not-an-object"}),
        include_admin=True,
    )
    assert detail.outputs.structured_plan is None
    assert detail.outputs.plan_text == "# raw plan"


# C (admin debug): invalid structured attempt records status + errors for admin.
def test_map_plan_detail_exposes_invalid_structured_debug():
    row = _plan_row(
        structured_plan=None,
        stage2_validator_report={
            "errors": [],
            "warnings": [],
            "structured_plan": {
                "status": "invalid_fallback_used",
                "errors": ["plan_metadata: field required"],
                "schema_version": None,
            },
        },
    )
    detail = _map_plan_detail(row, include_admin=True)
    assert detail.outputs.structured_plan is None  # invalid never exposed
    assert detail.outputs.plan_text == "# raw plan"  # fallback preserved
    assert detail.admin_outputs.structured_plan_status == "invalid_fallback_used"
    assert detail.admin_outputs.structured_plan_errors == ["plan_metadata: field required"]


# ---------------------------------------------------------------------------
# Automator integration
# ---------------------------------------------------------------------------


def _response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_test",
        output_text=text,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
    )


def _stage1_result() -> dict:
    return {
        "plan_text": "# Stage 1 Draft",
        "coach_notes": "### Coach Review",
        "pdf_url": None,
        "why_log": {},
        "stage2_payload": {"ok": True},
        "planning_brief": {"schema_version": "planning_brief.v1", "fight_date": "2026-06-13"},
        "stage2_handoff_text": "handoff",
    }


def _pass_review(**_):
    return {
        "status": "PASS",
        "needs_retry": False,
        "validator_report": {"errors": [], "warnings": [], "review_flag_count": 0},
    }


def _fail_review(*codes: str):
    error_codes = codes or ("true_internal_system_leak",)

    def _review(**_):
        return {
            "status": "FAIL",
            "needs_retry": True,
            "validator_report": {
                "errors": [{"code": code} for code in error_codes],
                "warnings": [],
                "blocking_warnings": [],
                "review_flag_count": 0,
            },
        }

    return _review


def test_finalize_skips_structured_when_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _pass_review)
    client = _FakeClient([_response("# final plan")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    # Only the single plan-text call; no structured calls when disabled.
    assert len(client.responses.calls) == 1
    assert result["structured_plan"] is None
    assert result["schema_version"] is None
    assert result["stage2_validator_report"]["structured_plan"]["status"] == "not_attempted"
    assert result["plan_text"] == "# final plan"


def test_finalize_attaches_valid_structured_plan(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _pass_review)
    client = _FakeClient([_response(_FAITHFUL_FINAL_PLAN), _response(json.dumps(_valid_plan()))])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert len(client.responses.calls) == 2  # plan + structured first pass
    assert result["plan_text"] == _FAITHFUL_FINAL_PLAN  # raw fallback untouched
    assert result["schema_version"] == SCHEMA_VERSION
    assert isinstance(result["structured_plan"], dict)
    assert result["stage2_validator_report"]["structured_plan"]["status"] == "valid"


def _locked_body_attack_repair_fixture():
    source = """D-17 (Monday) - Strength
- Sled Push (Speed). 3 x 2 pushes.

D-16 (Tuesday) - Technical-only combat
Technical-only contact today - no hard sparring.
"""
    display_text = """Why: Find safe body attacks that reward pressure without making the entry predictable.
- Body Attack Opportunity: 10 minutes, tactical review only. No physical load.
  Step 1: Identify the clearest body opening the opponent gives away.
  Step 2: Choose the punch or feint that creates that opening.
  Intent: Attack the body when the position earns it.
  Focus: Watch for high guards, separated elbows and upright posture.
  Reset: If the body line closes, go back upstairs rather than forcing it.
  Anchor: Open it before you take it.
  Purpose: SPP body-attack selection for a pressure fighter.
  Progress: Use the body attack only after the chosen setup appears in technical work."""
    brief = {
        "weeks": [{
            "session_roles": [{
                "scheduled_countdown_label": "D-17",
                "display_text": display_text,
                "mandatory_tactical_watch": True,
                "governance": {
                    "selected_drill_locked": True,
                    "selected_drill_name": "Body Attack Opportunity",
                    "render_selected_drill_exactly": True,
                    "mandatory": True,
                },
                "tactical_watch": {
                    "name": "Body Attack Opportunity",
                    "why": "Find safe body attacks that reward pressure without making the entry predictable.",
                    "duration_min": 10,
                    "instructions": [
                        "Identify the clearest body opening the opponent gives away.",
                        "Choose the punch or feint that creates that opening.",
                    ],
                    "mindset": {
                        "intent": "Attack the body when the position earns it.",
                        "focus": "Watch for high guards, separated elbows and upright posture.",
                        "reset": "If the body line closes, go back upstairs rather than forcing it.",
                        "anchor": "Open it before you take it.",
                        "context": "SPP body-attack selection for a pressure fighter.",
                    },
                    "progress": "Use the body attack only after the chosen setup appears in technical work.",
                },
            }]
        }]
    }
    return source, brief


def test_structured_attempt_repairs_missing_locked_watch_source_before_model_call(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    source, brief = _locked_body_attack_repair_fixture()

    def _pass_repair_review(**_):
        return {
            "status": "PASS",
            "needs_retry": False,
            "validator_report": {
                "errors": [],
                "warnings": [],
                "blocking_warnings": [],
                "review_flag_count": 0,
                "release_decision": "publish",
            },
        }

    monkeypatch.setattr(stage2_module, "review_stage2_output", _pass_repair_review)
    result = {
        "status": "ready",
        "plan_text": source,
        "final_plan_text": source,
        "stage2_validator_report": {"errors": [], "warnings": []},
    }
    automator = _StructuredAutomator(StructuredPlanOutcome(status="invalid_fallback_used"))

    asyncio.run(
        attempt_structured_plan_for_result(
            result,
            planning_brief=brief,
            automator=automator,
            source="test",
        )
    )

    assert len(automator.calls) == 1
    sent_text = automator.calls[0]["final_plan_text"]
    assert "D-17 (Monday) — Tactical Focus" in sent_text
    assert "Body Attack Opportunity" in sent_text
    assert result["final_plan_text"] == sent_text
    audit = result["stage2_validator_report"]["source_repair"]["locked_tactical_watch"]
    assert audit["status"] == "applied"
    assert audit["applied"] == ["D-17: Body Attack Opportunity"]
    assert audit["unresolved_count"] == 0
    assert audit["revalidated_status"] == "PASS"
    assert audit["revalidated_release_decision"] == "publish"


def test_structured_attempt_rejects_repair_when_revalidation_holds(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    source, brief = _locked_body_attack_repair_fixture()

    def _hold_repair_review(**_):
        return {
            "status": "FAIL",
            "needs_retry": True,
            "validator_report": {
                "errors": [{"code": "restriction_violation"}],
                "warnings": [],
                "blocking_warnings": [],
                "review_flag_count": 0,
                "release_decision": "hold",
            },
        }

    monkeypatch.setattr(stage2_module, "review_stage2_output", _hold_repair_review)
    result = {
        "status": "ready",
        "plan_text": source,
        "final_plan_text": source,
        "stage2_validator_report": {"errors": [], "warnings": []},
    }
    automator = _StructuredAutomator(StructuredPlanOutcome(status="invalid_fallback_used"))

    asyncio.run(
        attempt_structured_plan_for_result(
            result,
            planning_brief=brief,
            automator=automator,
            source="test",
        )
    )

    assert len(automator.calls) == 1
    assert automator.calls[0]["final_plan_text"] == source
    assert result["final_plan_text"] == source
    assert result["plan_text"] == source
    assert "source_repair" not in result["stage2_validator_report"]


def test_structured_attempt_preserves_failed_stage2_status_when_repair_still_not_publishable(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    source, brief = _locked_body_attack_repair_fixture()

    def _nonpublishable_repair_review(**_):
        return {
            "status": "WARN",
            "needs_retry": True,
            "validator_report": {
                "errors": [],
                "warnings": [{"code": "weekly_session_overage"}],
                "blocking_warnings": [{"code": "weekly_session_overage"}],
                "review_flag_count": 0,
                "release_decision": "manual_review",
            },
        }

    monkeypatch.setattr(stage2_module, "review_stage2_output", _nonpublishable_repair_review)
    result = {
        "status": "ready",
        "plan_text": source,
        "final_plan_text": source,
        "stage2_status": "stage2_failed",
        "stage2_validator_report": {
            "errors": [{"code": "prior_failure"}],
            "blocking_warnings": [{"code": "prior_blocker"}],
        },
    }
    automator = _StructuredAutomator(StructuredPlanOutcome(status="invalid_fallback_used"))

    asyncio.run(
        attempt_structured_plan_for_result(
            result,
            planning_brief=brief,
            automator=automator,
            source="test",
        )
    )

    assert result["stage2_status"] == "stage2_failed"
    assert result["status"] == "ready"
    audit = result["stage2_validator_report"]["source_repair"]["locked_tactical_watch"]
    assert audit["previous_stage2_status"] == "stage2_failed"
    assert audit["previous_app_status"] == "ready"
    assert audit["previous_error_count"] == 1
    assert audit["previous_blocking_warning_count"] == 1
    assert audit["revalidated_status"] == "WARN"
    assert audit["revalidated_release_decision"] == "manual_review"


def _fail_review(*codes: str):
    error_codes = codes or ("true_internal_system_leak",)

    def _review(**_):
        return {
            "status": "FAIL",
            "needs_retry": True,
            "validator_report": {
                "errors": [{"code": code} for code in error_codes],
                "warnings": [],
                "blocking_warnings": [],
                "review_flag_count": 0,
            },
        }

    return _review


def test_finalize_soft_blocker_released_with_flags_and_clean_card(monkeypatch: pytest.MonkeyPatch):
    # A non-safety error (internal scaffolding leak) no longer holds the plan.
    # It releases with flags and the structured card still builds normally.
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _fail_review("true_internal_system_leak"))
    client = _FakeClient([_response(_FAITHFUL_FINAL_PLAN), _response(json.dumps(_valid_plan()))])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == _FAITHFUL_FINAL_PLAN
    assert isinstance(result["structured_plan"], dict)
    assert result["stage2_validator_report"]["structured_plan"]["status"] == "valid"
    # The original error is still recorded for admin visibility.
    assert result["stage2_validator_report"]["errors"] == [{"code": "true_internal_system_leak"}]


def test_finalize_soft_blocking_warning_released_with_flags(monkeypatch: pytest.MonkeyPatch):
    def _warn_review(**_):
        return {
            "status": "WARN",
            "needs_retry": True,
            "validator_report": {
                "errors": [],
                "warnings": [{"code": "generic_filler_phrase", "severity": "blocker"}],
                "blocking_warnings": [{"code": "generic_filler_phrase", "severity": "blocker"}],
                "review_flag_count": 0,
            },
        }

    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _warn_review)
    client = _FakeClient([_response(_FAITHFUL_FINAL_PLAN), _response(json.dumps(_valid_plan()))])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == _FAITHFUL_FINAL_PLAN
    assert result["structured_plan"] is not None
    assert result["stage2_validator_report"]["structured_plan"]["status"] == "valid"
    assert result["stage2_validator_report"]["blocking_warnings"] == [
        {"code": "generic_filler_phrase", "severity": "blocker"}
    ]


def test_finalize_low_risk_quality_warning_releases_with_flags(monkeypatch: pytest.MonkeyPatch):
    # An explicitly allowlisted low-risk quality flag does not hold the athlete.
    # The structured call has no mocked response, so it must fail cleanly and
    # leave an auditable generation_failed marker instead of masking the release.
    def _warn_review(**_):
        return {
            "status": "WARN",
            "needs_retry": True,
            "validator_report": {
                "errors": [],
                "warnings": [{"code": "template_like_session_render", "severity": "blocker"}],
                "blocking_warnings": [{"code": "template_like_session_render", "severity": "blocker"}],
                "review_flag_count": 0,
            },
        }

    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _warn_review)
    client = _FakeClient([_response("# final plan")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == "# final plan"
    assert result["structured_plan"] is None
    assert result["stage2_validator_report"]["structured_plan"]["status"] == "generation_failed"
    assert result["stage2_validator_report"]["quality_review_flags"] == [
        {"code": "template_like_session_render", "severity": "blocker"}
    ]
    assert result["stage2_validator_report"]["release_decision"] == "publish_with_flags"
    assert result["stage2_validator_report"]["is_publishable"] is True


def test_finalize_still_releases_when_card_invalid(monkeypatch: pytest.MonkeyPatch):
    # The card never validates. The plan still ships on the raw plan_text
    # fallback; the failed conversion is recorded, not escalated.
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _fail_review("true_internal_system_leak"))
    client = _FakeClient(
        [
            _response("# final plan"),
            _response(json.dumps(["not", "a", "plan"])),  # invalid first pass
            _response(json.dumps(["still", "broken"])),  # repair still invalid
        ]
    )
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == "# final plan"
    assert result["final_plan_text"] == "# final plan"
    assert result["structured_plan"] is None
    debug = result["stage2_validator_report"]["structured_plan"]
    assert debug["status"] == "invalid_fallback_used"
    assert debug["errors"]


def test_finalize_safety_error_releases_with_flags_and_builds_card(monkeypatch: pytest.MonkeyPatch):
    # A safety error (restriction violation) is flagged for admins but no longer
    # holds the plan, so the release path runs in full — including the structured
    # card conversion. Both provider responses are supplied so the card really is
    # built rather than silently failing on an exhausted queue.
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _fail_review("restriction_violation"))
    client = _FakeClient([_response(_FAITHFUL_FINAL_PLAN), _response(json.dumps(_valid_plan()))])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == _FAITHFUL_FINAL_PLAN
    assert result["stage2_validator_report"]["errors"] == [{"code": "restriction_violation"}]
    # The card was actually converted: plan-text pass + structured pass.
    assert len(client.responses.calls) == 2
    assert isinstance(result["structured_plan"], dict)
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["stage2_validator_report"]["structured_plan"]["status"] == "valid"


def test_finalize_soft_blocker_released_when_structured_disabled(monkeypatch: pytest.MonkeyPatch):
    # With structured generation off there is no card at all; the plan still
    # releases with flags on one model call.
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _fail_review("true_internal_system_leak"))
    client = _FakeClient([_response("# final plan")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == "# final plan"
    assert len(client.responses.calls) == 1


def test_finalize_uses_one_repair_retry_when_opted_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_REPAIR", "1")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _pass_review)
    client = _FakeClient(
        [
            _response(_FAITHFUL_FINAL_PLAN),
            _response(json.dumps(["not", "a", "plan"])),  # invalid first structured pass
            _response(json.dumps(_valid_plan())),  # repaired structured pass
        ]
    )
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert len(client.responses.calls) == 3  # plan + first + repair
    assert isinstance(result["structured_plan"], dict)
    assert result["schema_version"] == SCHEMA_VERSION
    assert (
        result["stage2_validator_report"]["structured_plan"]["status"]
        == "repair_attempted_valid"
    )


def test_finalize_keeps_raw_plan_when_structured_invalid_after_repair(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_REPAIR", "1")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _pass_review)
    client = _FakeClient(
        [
            _response("# final plan"),
            _response(json.dumps(["not", "a", "plan"])),  # invalid
            _response(json.dumps(["still", "broken"])),  # repair still invalid
        ]
    )
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    # Structured-card failure alone should not create admin review work. With no
    # release blockers, the raw Stage 2 plan remains athlete-visible.
    assert result["status"] == "ready"
    assert result["plan_text"] == "# final plan"
    assert result["final_plan_text"] == "# final plan"
    assert result["structured_plan"] is None  # invalid never persisted
    debug = result["stage2_validator_report"]["structured_plan"]
    assert debug["status"] == "invalid_fallback_used"
    assert debug["errors"]


def test_finalize_makes_one_card_call_and_no_repair_by_default(monkeypatch: pytest.MonkeyPatch):
    # The repair rescued ~1 in 12 failed cards in production at 150-200 s
    # each; a rejected card is served as the validated deterministic card.
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.delenv("UNLXCK_STAGE2_STRUCTURED_REPAIR", raising=False)
    monkeypatch.setattr(stage2_module, "review_stage2_output", _pass_review)
    client = _FakeClient(
        [_response("# final plan"), _response(json.dumps(["not", "a", "plan"]))]
    )
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert len(client.responses.calls) == 2  # plan + one card conversion
    assert result["status"] == "ready"
    assert result["structured_plan"] is None
    report = result["stage2_validator_report"]
    assert report["structured_plan"]["status"] == "invalid_fallback_used"
    calls = report["stage2_model_calls"]
    assert [call["label"] for call in calls] == ["first_pass", "structured_first"]
    assert calls[0]["outcome"] == report["release_decision"]
    assert calls[1]["outcome"] == "invalid_fallback_used"
    for call in calls:
        assert call["prompt_chars"] > 0 and call["response_chars"] > 0
        assert set(call) <= {
            "label", "elapsed_seconds", "input_tokens", "output_tokens", "reasoning_tokens",
            "source", "prompt_chars", "response_chars", "outcome", "reason", "retry_reason",
        }


def test_finalize_releases_plan_when_card_blocked_by_safety_audit(
    monkeypatch: pytest.MonkeyPatch,
):
    # Safety findings on the card (coach_gated leakage / deterministic conflict /
    # audit crash) discard the card, but the plan still publishes on the
    # plan_text fallback. The blocked card status stays on the report.
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _pass_review)
    import api.structured_plan_generation as generation_module

    monkeypatch.setattr(
        generation_module,
        "audit_structured_plan",
        lambda *_a, **_k: [
            "LEAKAGE: coach_gated dosing token 'bicarbonate' surfaced athlete-facing"
        ],
    )
    client = _FakeClient(
        [_response(_FAITHFUL_FINAL_PLAN), _response(json.dumps(_valid_plan()))]
    )
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["structured_plan"] is None  # blocked card is never persisted
    debug = result["stage2_validator_report"]["structured_plan"]
    assert debug["status"] == "blocked_by_safety_audit"
    assert any(error.startswith("LEAKAGE") for error in debug["errors"])
    assert result["status"] == "ready"
    assert result["plan_text"] == _FAITHFUL_FINAL_PLAN


def test_finalize_accumulates_structured_call_costs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_REPAIR", "1")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _pass_review)
    # plan pass (10/5) + structured first (10/5) + structured repair (10/5).
    client = _FakeClient(
        [
            _response("# final plan"),
            _response(json.dumps(["not", "a", "plan"])),  # invalid -> triggers repair
            _response(json.dumps(_valid_plan())),
        ]
    )
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    cost = result["stage2_cost"]
    # 3 calls summed: input 3*10, output 3*5, total 3*15.
    assert cost["stage2_input_tokens"] == 30
    assert cost["stage2_output_tokens"] == 15
    assert cost["stage2_total_tokens"] == 45
    calls = result["stage2_validator_report"]["stage2_model_calls"]
    assert [call["label"] for call in calls] == ["first_pass", "structured_first", "structured_repair"]
    # The repair records why it ran and what it achieved.
    assert calls[1]["outcome"] == "invalid_fallback_used"
    assert calls[2]["reason"] == "invalid_fallback_used"
    assert calls[2]["outcome"] == result["stage2_validator_report"]["structured_plan"]["status"]


def test_finalize_cost_unchanged_when_structured_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _pass_review)
    client = _FakeClient([_response("# final plan")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    # Single plan-text call only: no double counting of the merge step.
    assert result["stage2_cost"]["stage2_input_tokens"] == 10
    assert result["stage2_cost"]["stage2_output_tokens"] == 5
    assert result["stage2_cost"]["stage2_total_tokens"] == 15


def test_finalize_does_not_crash_when_structured_model_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setattr(stage2_module, "review_stage2_output", _pass_review)
    # Second call (structured) raises; the raw plan must still come back.
    client = _FakeClient([_response("# final plan"), RuntimeError("boom")])

    async def create(**request):
        client.responses.calls.append(request)
        output = client.responses.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output

    client.responses.create = create  # type: ignore[assignment]
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    # The raw plan must survive the structured crash and remain athlete-visible
    # when Stage 2 validation found no release blockers.
    assert result["status"] == "ready"
    assert result["plan_text"] == "# final plan"
    assert result["final_plan_text"] == "# final plan"
    assert result["structured_plan"] is None
    assert result["stage2_validator_report"]["structured_plan"]["status"] == "generation_failed"


# ---------------------------------------------------------------------------
# Centralized trigger: attempt_structured_plan_for_result
# ---------------------------------------------------------------------------


def test_helper_attaches_structured_on_displayable_result(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    automator = _StructuredAutomator(_valid_outcome())
    result = {"status": "ready", "final_plan_text": "# final plan", "stage2_validator_report": {}}

    out, _costs = asyncio.run(
        attempt_structured_plan_for_result(
            result, planning_brief={}, automator=automator, source="admin_stage2"
        )
    )

    assert len(automator.calls) == 1
    assert out["structured_plan"] is not None
    assert out["schema_version"] == SCHEMA_VERSION
    assert out["stage2_validator_report"]["structured_plan"]["status"] == "valid"


def test_helper_marks_worker_result_in_flight_then_clears_on_terminal_outcome(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    result = {"status": "ready", "final_plan_text": "# final plan", "stage2_validator_report": {}}

    async def _exercise() -> None:
        started = asyncio.Event()
        finish = asyncio.Event()

        class _BlockingAutomator(_StructuredAutomator):
            async def _attempt_structured_plan(self, **kwargs):
                started.set()
                await finish.wait()
                return await super()._attempt_structured_plan(**kwargs)

        automator = _BlockingAutomator(_valid_outcome())
        task = asyncio.create_task(
            attempt_structured_plan_for_result(
                result,
                planning_brief={},
                automator=automator,
                source="worker",
            )
        )
        await started.wait()
        assert STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY in result["stage2_validator_report"]
        finish.set()
        await task

    asyncio.run(_exercise())

    assert STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY not in result["stage2_validator_report"]
    assert result["stage2_validator_report"]["structured_plan"]["status"] == "valid"


def test_helper_attaches_for_publishable_with_flags(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    automator = _StructuredAutomator(_valid_outcome())
    result = {
        "status": "publishable_with_flags",
        "final_plan_text": "# final plan",
        "stage2_validator_report": {},
    }
    out, _costs = asyncio.run(
        attempt_structured_plan_for_result(result, planning_brief={}, automator=automator, source="x")
    )
    assert len(automator.calls) == 1
    assert out["structured_plan"] is not None


def test_helper_skips_non_displayable_status(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    automator = _StructuredAutomator(_valid_outcome())
    result = {"status": "review_required", "final_plan_text": "# x", "stage2_validator_report": {}}

    out, _costs = asyncio.run(
        attempt_structured_plan_for_result(result, planning_brief={}, automator=automator, source="x")
    )

    assert automator.calls == []  # no model call for a non-displayable plan
    assert out["structured_plan"] is None
    assert out["stage2_validator_report"]["structured_plan"]["status"] == "not_attempted"


def test_helper_skips_when_env_disabled(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")
    automator = _StructuredAutomator(_valid_outcome())
    result = {"status": "ready", "final_plan_text": "# x", "stage2_validator_report": {}}
    out, _costs = asyncio.run(
        attempt_structured_plan_for_result(result, planning_brief={}, automator=automator, source="x")
    )
    assert automator.calls == []
    assert out["structured_plan"] is None


def test_helper_skips_when_automator_has_no_converter(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    result = {"status": "ready", "final_plan_text": "# x", "stage2_validator_report": {}}
    out, _costs = asyncio.run(
        attempt_structured_plan_for_result(result, planning_brief={}, automator=object(), source="x")
    )
    assert out["structured_plan"] is None
    assert out["stage2_validator_report"]["structured_plan"]["status"] == "not_attempted"


# ---------------------------------------------------------------------------
# Admin approval is fast/DB-only; structured conversion is deferred
# ---------------------------------------------------------------------------


def _seed_held_plan(store: FakeStore, *, plan_id: str = "plan-1") -> str:
    store.profiles["athlete-1"] = {"id": "athlete-1", "full_name": "Ari Mensah"}
    store.plans[plan_id] = {
        "id": plan_id,
        "athlete_id": "athlete-1",
        "full_name": "Ari Mensah",
        "status": "held_for_review",
        "plan_text": "",
        "final_plan_text": "# approved plan",
        "draft_plan_text": "# draft plan",
        "planning_brief": None,
        "stage2_validator_report": {},
        "stage2_status": "stage2_failed",
        "stage2_attempt_count": 1,
        "created_at": _now(),
    }
    return plan_id


@pytest.mark.parametrize(
    "raw",
    ["", "not-a-number", "0", "-5", "inf", "Infinity", "-inf", "nan"],
)
def test_approval_budget_falls_back_to_default_for_unusable_values(monkeypatch, raw):
    # Non-finite / non-positive / unparseable env values must not be used as a
    # timeout: inf would make wait_for() block forever, nan compares False.
    monkeypatch.setenv("UNLXCK_APPROVAL_STRUCTURED_PLAN_BUDGET_SECONDS", raw)
    assert _approval_structured_budget_seconds() == _APPROVAL_STRUCTURED_PLAN_BUDGET_SECONDS


def test_approval_budget_honours_finite_positive_override(monkeypatch):
    monkeypatch.setenv("UNLXCK_APPROVAL_STRUCTURED_PLAN_BUDGET_SECONDS", "12.5")
    assert _approval_structured_budget_seconds() == 12.5


def test_structured_card_attempt_is_stale_at_exactly_twenty_five_minutes():
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    report = {
        STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY: (now - timedelta(minutes=25)).isoformat()
    }

    assert has_fresh_structured_card_attempt(report, now=now) is False


# ---------------------------------------------------------------------------
# Pre-warm: build the held plan's card before approval so approval is instant
# ---------------------------------------------------------------------------


def test_prewarm_builds_and_persists_card_for_held_plan(monkeypatch):
    """A held plan with no card gets one built and persisted ahead of approval."""
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    automator = _StructuredAutomator(_valid_outcome("# approved plan"))

    asyncio.run(prewarm_structured_plan(plan_id=plan_id, store=store, stage2=automator))

    row = store.plans[plan_id]
    assert row["structured_plan"] is not None
    assert row["schema_version"] == SCHEMA_VERSION
    # The plan is still held — pre-warming the card must not release it.
    assert row["status"] == "held_for_review"
    # The card was converted from the approved/final text.
    assert automator.calls[0]["final_plan_text"] == "# approved plan"
    assert automator.calls[0]["source"] == "admin_prewarm"
    assert STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY not in row["stage2_validator_report"]


def test_prewarm_persists_build_marker_before_model_call(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    marker_seen: list[str] = []

    class _InspectingAutomator(_StructuredAutomator):
        async def _attempt_structured_plan(self, **kwargs):
            report = store.plans[plan_id]["stage2_validator_report"]
            marker_seen.append(str(report.get(STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY) or ""))
            return await super()._attempt_structured_plan(**kwargs)

    asyncio.run(
        prewarm_structured_plan(
            plan_id=plan_id,
            store=store,
            stage2=_InspectingAutomator(_valid_outcome("# approved plan")),
        )
    )

    assert marker_seen and marker_seen[0]


def test_prewarm_is_noop_when_card_already_present(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    store.plans[plan_id]["structured_plan"] = _valid_plan()

    automator = _StructuredAutomator(_valid_outcome("# approved plan"))
    asyncio.run(prewarm_structured_plan(plan_id=plan_id, store=store, stage2=automator))

    assert automator.calls == []  # no redundant model call


def test_prewarm_is_noop_without_env_flag(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")
    store = FakeStore()
    plan_id = _seed_held_plan(store)

    automator = _StructuredAutomator(_valid_outcome("# approved plan"))
    asyncio.run(prewarm_structured_plan(plan_id=plan_id, store=store, stage2=automator))

    assert automator.calls == []
    assert store.plans[plan_id].get("structured_plan") is None


def test_prewarm_skips_when_no_text_to_convert(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    store.plans[plan_id]["final_plan_text"] = ""
    store.plans[plan_id]["plan_text"] = ""

    automator = _StructuredAutomator(_valid_outcome("# approved plan"))
    asyncio.run(prewarm_structured_plan(plan_id=plan_id, store=store, stage2=automator))

    assert automator.calls == []
    assert store.plans[plan_id].get("structured_plan") is None


def test_prewarm_skips_when_status_no_longer_prewarmable(monkeypatch):
    """A plan approved/rejected/archived before the task runs wastes no LLM call."""
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    # The plan left the review queue (e.g. it was approved) after the queue load
    # scheduled this pre-warm but before the background task ran.
    store.plans[plan_id]["status"] = "ready"

    automator = _StructuredAutomator(_valid_outcome("# approved plan"))
    asyncio.run(prewarm_structured_plan(plan_id=plan_id, store=store, stage2=automator))

    assert automator.calls == []  # no conversion for a plan that left the queue


def test_approval_reuses_prewarmed_card_without_a_model_call(monkeypatch):
    """The slow win: once pre-warmed, approval ships the card with no conversion."""
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)

    # Pre-warm builds and persists the card while the plan is still held.
    prewarm_automator = _StructuredAutomator(_valid_outcome("# approved plan"))
    asyncio.run(prewarm_structured_plan(plan_id=plan_id, store=store, stage2=prewarm_automator))
    assert store.plans[plan_id]["structured_plan"] is not None

    # Approval must reuse that card rather than paying for a second conversion.
    approve_automator = _StructuredAutomator(_valid_outcome("# approved plan"))
    detail = asyncio.run(
        approve_review_required_plan(plan_id=plan_id, store=store, stage2=approve_automator)
    )

    assert detail.status == "ready"
    assert detail.outputs.structured_plan is not None
    assert approve_automator.calls == []  # no inline conversion — the card was reused


def test_approval_does_not_reuse_card_built_from_different_text(monkeypatch):
    """Reuse is refused when the approved text isn't what the card was built from.

    Guards the text-match contract: if the text being approved differs from the
    text the row's card was converted from, the card is a stale projection and a
    fresh conversion must run instead of shipping it.
    """
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    # Exercise the inline conversion path so "reuse refused → fresh conversion"
    # is observable within the approval call itself.
    monkeypatch.setenv("UNLXCK_STAGE2_INLINE_APPROVAL_CARD", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    row = store.plans[plan_id]
    # Approval will approve the draft text, but the card on the row was built from
    # a different plan_text, so the two no longer correspond.
    row["final_plan_text"] = ""
    row["draft_plan_text"] = "# draft text being approved"
    row["plan_text"] = "# different text the card came from"
    row["structured_plan"] = _valid_plan()
    row["stage2_validator_report"] = {"structured_plan": {"status": "valid"}}

    approve_automator = _StructuredAutomator(_valid_outcome("# draft text being approved"))
    asyncio.run(
        approve_review_required_plan(plan_id=plan_id, store=store, stage2=approve_automator)
    )

    # Reuse is refused (text mismatch) so a fresh conversion runs.
    assert approve_automator.calls != []


def test_should_prewarm_review_plan_row_gates_on_approvable_held_status():
    assert should_prewarm_review_plan_row({"id": "p1", "status": "review_required"}) is True
    assert should_prewarm_review_plan_row({"id": "p1", "status": "held_for_review"}) is True
    assert should_prewarm_review_plan_row({"id": "p1", "status": "needs_review"}) is True
    # Safety-gated and already-displayable states are never pre-warmed.
    assert should_prewarm_review_plan_row({"id": "p1", "status": "triage_blocked"}) is False
    assert should_prewarm_review_plan_row({"id": "p1", "status": "medical_hold"}) is False
    assert should_prewarm_review_plan_row({"id": "p1", "status": "publishable_with_flags"}) is False
    # A row with no id can't be scheduled.
    assert should_prewarm_review_plan_row({"status": "review_required"}) is False


def test_should_prewarm_review_plan_row_ignores_not_attempted_debug():
    """A recorded ``not_attempted`` outcome must not block pre-warm.

    Held plans routinely carry {status: not_attempted} in their validator report
    (the worker records it because a held plan is not displayable). Treating that
    record as "already attempted" disabled pre-warm for exactly the plans it
    exists to serve, so every admin approval paid the slow inline conversion.
    """
    row = {
        "id": "p1",
        "status": "held_for_review",
        "stage2_validator_report": {"structured_plan": {"status": "not_attempted"}},
    }
    assert should_prewarm_review_plan_row(row) is True

    # A conversion that actually RAN (any terminal status) still skips pre-warm.
    for status in ("valid", "repair_attempted_valid", "invalid_fallback_used", "blocked_by_safety_audit"):
        row = {
            "id": "p1",
            "status": "held_for_review",
            "stage2_validator_report": {"structured_plan": {"status": status}},
        }
        assert should_prewarm_review_plan_row(row) is False, status


def test_attempt_structured_plan_records_reason_when_converter_unavailable(monkeypatch):
    """An automator without a converter must say WHY the card was not built.

    A DisabledStage2Automator (e.g. missing OPENAI_API_KEY in the process doing
    approvals) previously recorded a bare ``not_attempted`` — every plan silently
    stayed on the markdown fallback with an unexplained admin diagnostic.
    """
    from api.stage2_automation import DisabledStage2Automator, attempt_structured_plan_for_result

    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    result = {
        "status": "ready",
        "final_plan_text": "# approved plan",
        "stage2_validator_report": {},
        "structured_plan": None,
    }
    automator = DisabledStage2Automator(reason="OPENAI_API_KEY is required for automated Stage 2 finalization.")

    updated, costs = asyncio.run(
        attempt_structured_plan_for_result(
            result,
            planning_brief={},
            automator=automator,
            source="admin_stage2",
        )
    )

    assert costs == []
    debug = updated["stage2_validator_report"]["structured_plan"]
    assert debug["status"] == "not_attempted"
    assert debug["errors"], "the diagnostic must carry the unavailability reason"
    assert "OPENAI_API_KEY" in debug["errors"][0]


def test_admin_approval_revalidation_preserves_structured_debug_and_build_marker(monkeypatch):
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    row = store.plans[plan_id]
    row["planning_brief"] = {"schema_version": "planning_brief.v1"}
    row["stage2_validator_report"] = {
        "structured_plan": {
            "status": "invalid_fallback_used",
            "errors": ["bad shape"],
        },
        STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY: "2026-07-11T10:00:00+00:00",
    }

    monkeypatch.setattr(
        "fightcamp.stage2_pipeline.review_stage2_output",
        lambda **_kwargs: {"validator_report": {"errors": [], "warnings": []}},
    )

    result = _admin_approved_result(row)

    report = result["stage2_validator_report"]
    assert report["structured_plan"] == row["stage2_validator_report"]["structured_plan"]
    assert report[STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY] == "2026-07-11T10:00:00+00:00"


def test_admin_approval_write_does_not_drop_prior_structured_debug(monkeypatch):
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    row = store.plans[plan_id]
    row["planning_brief"] = {"schema_version": "planning_brief.v1"}
    row["stage2_validator_report"] = {
        "structured_plan": {
            "status": "invalid_fallback_used",
            "errors": ["bad shape"],
        }
    }
    monkeypatch.setattr(
        "fightcamp.stage2_pipeline.review_stage2_output",
        lambda **_kwargs: {"validator_report": {"errors": [], "warnings": []}},
    )

    asyncio.run(approve_review_required_plan(plan_id=plan_id, store=store, stage2=None))

    debug = store.plans[plan_id]["stage2_validator_report"]["structured_plan"]
    assert debug["status"] == "invalid_fallback_used"
    assert debug["errors"] == ["bad shape"]


def test_admin_approve_defers_conversion_to_background_by_default(monkeypatch):
    """By default approval runs NO fresh conversion inline — one call, deferred.

    The inline fresh conversion near-always times out for the configured model and
    the background task then redoes the whole conversion (~1.5x cost). With the
    inline attempt off (default), approval persists only the durable in-flight
    marker and releases; the deferred background task does the single conversion.
    """
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.delenv("UNLXCK_STAGE2_INLINE_APPROVAL_CARD", raising=False)
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    automator = _StructuredAutomator(_valid_outcome("# approved plan"))

    detail = asyncio.run(
        approve_review_required_plan(plan_id=plan_id, store=store, stage2=automator)
    )

    assert detail.status == "ready"
    assert detail.outputs.plan_text == "# approved plan"  # raw fallback retained
    # No inline model call was made; the card comes from the deferred task.
    assert automator.calls == []
    assert detail.outputs.structured_plan is None
    assert store.plans[plan_id].get("structured_plan") is None
    # But the durable "building" marker IS persisted so the admin UI shows the
    # in-flight state and the self-heal sweep can recover an orphaned build.
    assert (
        STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY
        in store.plans[plan_id]["stage2_validator_report"]
    )


def test_admin_approve_attaches_structured_card_inline(monkeypatch):
    """With the inline flag on, approval ships the live card when it is fast.

    A fast inline conversion is attached to the approval response (and persisted)
    so the athlete sees the structured card immediately, with plan_text retained
    as the raw-markdown fallback. Off by default; opt in per env.
    """
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setenv("UNLXCK_STAGE2_INLINE_APPROVAL_CARD", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    automator = _StructuredAutomator(_valid_outcome("# approved plan"))

    detail = asyncio.run(
        approve_review_required_plan(plan_id=plan_id, store=store, stage2=automator)
    )

    assert detail.status == "ready"
    assert detail.outputs.plan_text == "# approved plan"  # raw fallback retained
    # The inline conversion ran once and the card is on both the response and row.
    assert len(automator.calls) == 1
    assert detail.outputs.structured_plan is not None
    assert store.plans[plan_id]["structured_plan"] is not None
    assert store.plans[plan_id]["schema_version"] == SCHEMA_VERSION


def test_admin_approve_falls_back_to_text_when_inline_card_times_out(monkeypatch):
    """A slow inline conversion must not stall approval - text releases instantly."""
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setenv("UNLXCK_STAGE2_INLINE_APPROVAL_CARD", "1")
    monkeypatch.setenv("UNLXCK_APPROVAL_STRUCTURED_PLAN_BUDGET_SECONDS", "0.01")
    store = FakeStore()
    plan_id = _seed_held_plan(store)

    class _SlowAutomator(_StructuredAutomator):
        async def _attempt_structured_plan(self, **kwargs):
            await asyncio.sleep(0.2)  # blow past the tiny budget
            return await super()._attempt_structured_plan(**kwargs)

    automator = _SlowAutomator(_valid_outcome("# approved plan"))

    detail = asyncio.run(
        approve_review_required_plan(plan_id=plan_id, store=store, stage2=automator)
    )

    # Released immediately with text; no card persisted by the inline attempt.
    assert detail.status == "ready"
    assert detail.outputs.plan_text == "# approved plan"
    assert detail.outputs.structured_plan is None
    assert store.plans[plan_id].get("structured_plan") is None
    # The timeout is not a blank/unknown state: approval persists the in-flight
    # marker that the deferred background conversion will finish (or that will
    # age into an explicit stale-build failure after the lifecycle timeout).
    assert (
        STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY
        in store.plans[plan_id]["stage2_validator_report"]
    )


def test_admin_stage2_service_wraps_sync_store_calls_in_to_thread(monkeypatch):
    """Async admin approval must not call sync DB helpers on the event loop."""
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    calls: list[str] = []
    real_to_thread = asyncio.to_thread

    async def _tracked_to_thread(func, /, *args, **kwargs):
        calls.append(getattr(func, "__name__", repr(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(admin_stage2_service.asyncio, "to_thread", _tracked_to_thread)

    detail = asyncio.run(
        approve_review_required_plan(plan_id=plan_id, store=store, stage2=None)
    )

    assert detail.status == "ready"
    assert calls.count("get_plan") >= 1
    assert "update_plan_stage2_if_unchanged" in calls
    assert "update_plan_stage2" not in calls
    assert "_lookup_plan_source" in calls


def test_approve_review_required_plan_uses_atomic_stage2_update(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    calls = {"atomic": 0}
    original_atomic = store.update_plan_stage2_if_unchanged

    def _atomic(plan_id_arg, result, expected_snapshot):
        calls["atomic"] += 1
        return original_atomic(plan_id_arg, result, expected_snapshot)

    store.update_plan_stage2_if_unchanged = _atomic  # type: ignore[method-assign]

    detail = asyncio.run(approve_review_required_plan(plan_id=plan_id, store=store, stage2=None))

    assert detail.status == "ready"
    assert calls == {"atomic": 1}


def test_submit_manual_stage2_uses_atomic_stage2_update(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    calls = {"atomic": 0}
    original_atomic = store.update_plan_stage2_if_unchanged

    def _atomic(plan_id_arg, result, expected_snapshot):
        calls["atomic"] += 1
        return original_atomic(plan_id_arg, result, expected_snapshot)

    store.update_plan_stage2_if_unchanged = _atomic  # type: ignore[method-assign]

    asyncio.run(submit_manual_stage2(plan_id=plan_id, final_plan_text="# manual plan", store=store, stage2=None))

    assert calls == {"atomic": 1}


def test_atomic_stage2_update_rejects_edit_between_read_and_write(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    original_atomic = store.update_plan_stage2_if_unchanged

    def _racing_atomic(plan_id_arg, result, expected_snapshot):
        store.plans[plan_id_arg]["status"] = "archived"
        store.plans[plan_id_arg]["plan_text"] = ""
        store.plans[plan_id_arg]["stage2_status"] = "admin_rejected"
        return original_atomic(plan_id_arg, result, expected_snapshot)

    store.update_plan_stage2_if_unchanged = _racing_atomic  # type: ignore[method-assign]

    with pytest.raises(Exception) as exc_info:
        asyncio.run(approve_review_required_plan(plan_id=plan_id, store=store, stage2=None))

    assert getattr(exc_info.value, "status_code", None) == 409
    assert store.plans[plan_id]["status"] == "archived"
    assert store.plans[plan_id]["plan_text"] == ""
    assert store.plans[plan_id]["stage2_status"] == "admin_rejected"


def test_structured_post_processing_converts_when_inline_was_skipped(monkeypatch):
    """The background fallback finishes the card when approval did not (stage2=None)."""
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)

    # Approve without an automator: no inline conversion, plan releases as text.
    asyncio.run(approve_review_required_plan(plan_id=plan_id, store=store, stage2=None))
    assert store.plans[plan_id].get("structured_plan") is None

    structured = _valid_plan()
    structured["plan_notes"] = [
        {
            "category": "injury",
            "label": "Achilles",
            "text": "Stop if Achilles pain rises during the session.",
        }
    ]
    automator = _StructuredAutomator(
        build_structured_plan_outcome(
            structured,
            raw_markdown=f"# approved plan\n\n{_faithful_source(structured)}",
        )
    )
    asyncio.run(
        run_structured_plan_post_processing(plan_id=plan_id, store=store, stage2=automator)
    )

    # Conversion ran exactly once and is persisted, without disturbing the live
    # released plan (status/plan_text intact).
    assert len(automator.calls) == 1
    assert store.plans[plan_id]["structured_plan"] is not None
    assert store.plans[plan_id]["schema_version"] == SCHEMA_VERSION
    assert store.plans[plan_id]["structured_plan"]["plan_notes"] == structured["plan_notes"]
    assert store.plans[plan_id]["status"] == "ready"
    assert store.plans[plan_id]["plan_text"] == "# approved plan"

    # The athlete-facing mapper used by Active Notes reads the same persisted
    # plan_notes rather than reconstructing them from markdown.
    detail = _map_plan_detail(store.plans[plan_id], include_admin=False)
    assert detail.outputs.structured_plan is not None
    assert [note.model_dump(mode="json") for note in detail.outputs.structured_plan.plan_notes] == structured[
        "plan_notes"
    ]


def test_structured_post_processing_persists_marker_before_conversion_and_clears_it(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    asyncio.run(approve_review_required_plan(plan_id=plan_id, store=store, stage2=None))
    marker_seen: list[str] = []

    class _InspectingAutomator(_StructuredAutomator):
        async def _attempt_structured_plan(self, **kwargs):
            report = store.plans[plan_id]["stage2_validator_report"]
            marker_seen.append(str(report.get(STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY) or ""))
            return await super()._attempt_structured_plan(**kwargs)

    asyncio.run(
        run_structured_plan_post_processing(
            plan_id=plan_id,
            store=store,
            stage2=_InspectingAutomator(_valid_outcome("# approved plan")),
        )
    )

    assert marker_seen and marker_seen[0]
    assert (
        STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY
        not in store.plans[plan_id]["stage2_validator_report"]
    )


def test_structured_post_processing_uses_exact_source_text_for_narrow_write_guard(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    asyncio.run(approve_review_required_plan(plan_id=plan_id, store=store, stage2=None))
    store.plans[plan_id]["final_plan_text"] = "# approved plan\n"
    store.plans[plan_id]["plan_text"] = "# approved plan\n"

    asyncio.run(
        run_structured_plan_post_processing(
            plan_id=plan_id,
            store=store,
            stage2=_StructuredAutomator(
                StructuredPlanOutcome(status="invalid_fallback_used", errors=["bad shape"])
            ),
        )
    )

    report = store.plans[plan_id]["stage2_validator_report"]
    assert report["structured_plan"]["status"] == "invalid_fallback_used"
    assert STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY not in report


def test_explicit_rebuild_retries_held_safety_failure_without_releasing_plan(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    store.plans[plan_id]["stage2_validator_report"] = {
        "structured_plan": {
            "status": "blocked_by_safety_audit",
            "errors": ["deterministic safety conflict"],
        }
    }

    decision = asyncio.run(prepare_structured_plan_rebuild(plan_id=plan_id, store=store))
    blocked = StructuredPlanOutcome(
        status="blocked_by_safety_audit",
        errors=["deterministic safety conflict remains"],
    )
    asyncio.run(
        run_structured_plan_post_processing(
            plan_id=plan_id,
            store=store,
            stage2=_StructuredAutomator(blocked),
            continue_existing_attempt=True,
            rebuild=True,
        )
    )

    row = store.plans[plan_id]
    assert decision == {"queued": True, "plan_id": plan_id}
    assert row["status"] == "held_for_review"
    assert row["plan_text"] == ""
    assert row.get("structured_plan") is None
    assert row["stage2_validator_report"]["structured_plan"]["status"] == "blocked_by_safety_audit"
    assert STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY not in row["stage2_validator_report"]


def test_explicit_rebuild_requeues_a_stale_build_marker(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    stale = "2020-01-01T00:00:00Z"
    store.plans[plan_id]["stage2_validator_report"] = {
        "structured_plan": {"status": "not_attempted", "errors": ["worker stopped"]},
        STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY: stale,
    }

    decision = asyncio.run(prepare_structured_plan_rebuild(plan_id=plan_id, store=store))

    assert decision == {"queued": True, "plan_id": plan_id}
    marker = store.plans[plan_id]["stage2_validator_report"][
        STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY
    ]
    assert marker != stale


def test_structured_post_processing_skips_when_card_already_present(monkeypatch):
    """Once a card exists (e.g. from the inline approval), the fallback is a no-op."""
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setenv("UNLXCK_STAGE2_INLINE_APPROVAL_CARD", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)

    # Inline approval produces the card.
    asyncio.run(
        approve_review_required_plan(
            plan_id=plan_id, store=store, stage2=_StructuredAutomator(_valid_outcome("# approved plan"))
        )
    )
    assert store.plans[plan_id]["structured_plan"] is not None

    # The background fallback must not pay for a redundant conversion.
    fallback = _StructuredAutomator(_valid_outcome("# approved plan"))
    asyncio.run(
        run_structured_plan_post_processing(plan_id=plan_id, store=store, stage2=fallback)
    )
    assert fallback.calls == []


def test_structured_post_processing_failure_keeps_plan_text_and_ready(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    failed = StructuredPlanOutcome(status="invalid_fallback_used", errors=["bad shape"])
    automator = _StructuredAutomator(failed)

    # Simulate the real fallback path: approval released text before a structured
    # card existed, then deferred post-processing tried and failed to make one.
    asyncio.run(
        approve_review_required_plan(plan_id=plan_id, store=store, stage2=None)
    )
    assert "structured_plan" not in store.plans[plan_id].get("stage2_validator_report", {})

    asyncio.run(
        run_structured_plan_post_processing(plan_id=plan_id, store=store, stage2=automator)
    )

    # A structured failure leaves the released raw markdown plan intact.
    assert store.plans[plan_id]["status"] == "ready"
    assert store.plans[plan_id]["plan_text"] == "# approved plan"
    assert store.plans[plan_id].get("structured_plan") is None
    debug = store.plans[plan_id]["stage2_validator_report"]["structured_plan"]
    assert debug["status"] == "invalid_fallback_used"
    assert debug["errors"] == ["bad shape"]


def test_structured_post_processing_normalizes_non_dict_validator_report(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    asyncio.run(approve_review_required_plan(plan_id=plan_id, store=store, stage2=None))
    store.plans[plan_id]["stage2_validator_report"] = "bad-report"
    failed = StructuredPlanOutcome(status="invalid_fallback_used", errors=["bad shape"])

    asyncio.run(
        run_structured_plan_post_processing(
            plan_id=plan_id,
            store=store,
            stage2=_StructuredAutomator(failed),
        )
    )

    report = store.plans[plan_id]["stage2_validator_report"]
    assert isinstance(report, dict)
    assert report["structured_plan"]["status"] == "invalid_fallback_used"


class _ConcurrentMutationAutomator(_StructuredAutomator):
    """Automator that runs a concurrent admin action *during* the slow conversion.

    Simulates the race the narrow structured writer guards against: the model
    conversion takes seconds, and a second admin rewrites the plan row in the
    meantime (reject, archive, rename, manual Stage 2 edit).
    """

    def __init__(self, outcome, *, on_convert, costs=None):
        super().__init__(outcome, costs=costs)
        self._on_convert = on_convert

    async def _attempt_structured_plan(self, **kwargs):
        self._on_convert()
        return await super()._attempt_structured_plan(**kwargs)


def _track_full_stage2_writes(store: FakeStore) -> dict[str, int]:
    """Spy on ``update_plan_stage2`` so tests can prove the narrow path is used."""
    counter = {"calls": 0}
    original = store.update_plan_stage2

    def _tracked(plan_id, result):
        counter["calls"] += 1
        return original(plan_id, result)

    store.update_plan_stage2 = _tracked  # type: ignore[method-assign]
    return counter


def test_structured_post_processing_failure_does_not_clear_concurrent_card(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    asyncio.run(approve_review_required_plan(plan_id=plan_id, store=store, stage2=None))

    def _concurrent_card_write():
        row = store.plans[plan_id]
        row["structured_plan"] = _valid_plan()
        row["schema_version"] = SCHEMA_VERSION
        row["stage2_validator_report"] = {
            "structured_plan": {
                "status": "valid",
                "errors": [],
                "warnings": [],
                "schema_version": SCHEMA_VERSION,
            }
        }

    failed = StructuredPlanOutcome(status="invalid_fallback_used", errors=["bad shape"])
    automator = _ConcurrentMutationAutomator(failed, on_convert=_concurrent_card_write)

    asyncio.run(
        run_structured_plan_post_processing(plan_id=plan_id, store=store, stage2=automator)
    )

    row = store.plans[plan_id]
    assert row["structured_plan"] is not None
    assert row["schema_version"] == SCHEMA_VERSION
    assert row["stage2_validator_report"]["structured_plan"]["status"] == "valid"


def test_structured_post_processing_does_not_overwrite_concurrent_manual_edit(monkeypatch):
    """A concurrent manual Stage 2 edit must survive the deferred conversion.

    The edit keeps the plan athlete-visible (``ready``) but rewrites plan_text /
    stage2_status / stage2_retry_text / attempt count. The background writer must
    only touch the structured-output columns, so the manual edit is preserved.
    The full ``update_plan_stage2`` writer (which rebuilds all of those fields
    from the stale pre-conversion snapshot) must never run here.
    """
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)

    # Skip the inline attempt so the conversion under test happens only in the
    # background fallback, where the concurrent-mutation race is exercised.
    asyncio.run(approve_review_required_plan(plan_id=plan_id, store=store, stage2=None))

    def _concurrent_manual_edit():
        row = store.plans[plan_id]
        row["status"] = "ready"
        row["plan_text"] = "# manually edited plan"
        row["final_plan_text"] = "# manually edited plan"
        row["stage2_status"] = "manual_stage2_pass"
        row["stage2_retry_text"] = "tweak the taper"
        row["stage2_attempt_count"] = 2

    stage2_calls = _track_full_stage2_writes(store)
    automator = _ConcurrentMutationAutomator(
        _valid_outcome("# approved plan"), on_convert=_concurrent_manual_edit
    )

    asyncio.run(
        run_structured_plan_post_processing(plan_id=plan_id, store=store, stage2=automator)
    )

    row = store.plans[plan_id]
    # The newer manual edit is fully preserved — no field regressed to the stale
    # pre-conversion snapshot.
    assert row["status"] == "ready"
    assert row["plan_text"] == "# manually edited plan"
    assert row["final_plan_text"] == "# manually edited plan"
    assert row["stage2_status"] == "manual_stage2_pass"
    assert row["stage2_retry_text"] == "tweak the taper"
    assert row["stage2_attempt_count"] == 2
    # The card was converted from the pre-edit text, so it is now a stale
    # projection of superseded text: the narrow writer rejects it rather than
    # publishing a card that contradicts the manually edited plan_text.
    assert row.get("structured_plan") is None
    assert row.get("schema_version") is None
    # The full Stage 2 writer was never used either.
    assert stage2_calls["calls"] == 0


def test_structured_post_processing_does_not_overwrite_concurrent_reject(monkeypatch):
    """A concurrent reject/archive must not be resurrected by the conversion.

    The plan leaves the athlete-visible state mid-conversion (status flips to
    ``archived``, plan_text cleared). The deferred structured write must leave
    that newer terminal state untouched rather than re-releasing the plan.
    """
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)

    # Skip the inline attempt so the conversion under test happens only in the
    # background fallback, where the concurrent-reject race is exercised.
    asyncio.run(approve_review_required_plan(plan_id=plan_id, store=store, stage2=None))

    def _concurrent_reject():
        row = store.plans[plan_id]
        row["status"] = "archived"
        row["plan_text"] = ""
        row["final_plan_text"] = ""
        row["stage2_status"] = "admin_rejected"
        row["stage2_retry_text"] = "rejected by reviewer"

    stage2_calls = _track_full_stage2_writes(store)
    automator = _ConcurrentMutationAutomator(
        _valid_outcome("# approved plan"), on_convert=_concurrent_reject
    )

    asyncio.run(
        run_structured_plan_post_processing(plan_id=plan_id, store=store, stage2=automator)
    )

    row = store.plans[plan_id]
    # The concurrent reject/archive survives intact.
    assert row["status"] == "archived"
    assert row["plan_text"] == ""
    assert row["final_plan_text"] == ""
    assert row["stage2_status"] == "admin_rejected"
    assert row["stage2_retry_text"] == "rejected by reviewer"
    # The card was converted from the pre-reject text; the stale-write guard
    # rejects it so an archived plan is never re-released with a structured card.
    assert row.get("structured_plan") is None
    # The full Stage 2 writer was never used (it would have failed the
    # archived->ready transition or otherwise clobbered the row).
    assert stage2_calls["calls"] == 0


def test_structured_post_processing_persists_narrow_fields_without_regression(monkeypatch):
    """Structured output is persisted; every non-structured field is unchanged."""
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_held_plan(store)

    # Skip the inline attempt so post-processing is the sole structured writer
    # under test here.
    asyncio.run(approve_review_required_plan(plan_id=plan_id, store=store, stage2=None))
    # Snapshot the released row immediately after approval.
    approved = dict(store.plans[plan_id])

    stage2_calls = _track_full_stage2_writes(store)
    asyncio.run(
        run_structured_plan_post_processing(
            plan_id=plan_id, store=store, stage2=_StructuredAutomator(_valid_outcome("# approved plan"))
        )
    )

    row = store.plans[plan_id]
    # Structured output produced and persisted.
    assert row["structured_plan"] is not None
    assert row["schema_version"] == SCHEMA_VERSION
    # No regression on any lifecycle/text field.
    for field in (
        "status",
        "plan_text",
        "draft_plan_text",
        "final_plan_text",
        "stage2_status",
        "stage2_retry_text",
        "stage2_attempt_count",
    ):
        assert row[field] == approved[field], field
    # Only the narrow writer touched the row.
    assert stage2_calls["calls"] == 0


def test_admin_approve_without_env_flag_skips_structured(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    automator = _StructuredAutomator(_valid_outcome("# approved plan"))

    detail = asyncio.run(
        approve_review_required_plan(plan_id=plan_id, store=store, stage2=automator)
    )
    # Even the background step is a no-op when the env flag is off.
    asyncio.run(
        run_structured_plan_post_processing(plan_id=plan_id, store=store, stage2=automator)
    )

    assert detail.status == "ready"
    assert detail.outputs.structured_plan is None
    assert store.plans[plan_id].get("structured_plan") is None
    assert automator.calls == []


def _seed_released_plan(
    store: FakeStore,
    *,
    plan_id: str,
    structured: dict | None = None,
    status: str = "ready",
) -> str:
    store.profiles["athlete-1"] = {"id": "athlete-1", "full_name": "Ari Mensah"}
    store.plans[plan_id] = {
        "id": plan_id,
        "athlete_id": "athlete-1",
        "full_name": "Ari Mensah",
        "status": status,
        "plan_text": "# released plan",
        "final_plan_text": "# released plan",
        "draft_plan_text": "# draft plan",
        "planning_brief": None,
        "stage2_validator_report": {},
        "stage2_status": "stage2_pass",
        "stage2_attempt_count": 1,
        "structured_plan": structured,
        "created_at": _now(),
    }
    return plan_id


def test_backfill_candidates_only_displayable_plans_without_a_card(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    _seed_released_plan(store, plan_id="needs-card")  # ready, no card -> candidate
    _seed_released_plan(store, plan_id="has-card", structured={"weeks": []})  # carded -> skip
    _seed_held_plan(store, plan_id="held")  # not displayable -> skip

    candidates = asyncio.run(list_structured_plan_backfill_candidates(store=store, limit=25))

    assert candidates == ["needs-card"]


def test_backfill_converts_released_plans_missing_a_card(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    _seed_released_plan(store, plan_id="needs-card")
    automator = _StructuredAutomator(_valid_outcome("# released plan"))

    candidates = asyncio.run(list_structured_plan_backfill_candidates(store=store, limit=25))
    asyncio.run(backfill_structured_plans(store=store, stage2=automator, plan_ids=candidates))

    assert len(automator.calls) == 1
    assert store.plans["needs-card"]["structured_plan"] is not None
    assert store.plans["needs-card"]["schema_version"] == SCHEMA_VERSION


def test_backfill_is_noop_without_automator():
    store = FakeStore()
    _seed_released_plan(store, plan_id="needs-card")

    asyncio.run(backfill_structured_plans(store=store, stage2=None, plan_ids=["needs-card"]))

    assert store.plans["needs-card"]["structured_plan"] is None


# ---------------------------------------------------------------------------
# Startup self-heal: recover card builds orphaned by a deploy/restart
# ---------------------------------------------------------------------------


def _seed_orphaned_build(store: FakeStore, *, plan_id: str) -> str:
    """A released plan carrying an in-flight marker but no card (build orphaned)."""
    _seed_released_plan(store, plan_id=plan_id)
    store.plans[plan_id]["stage2_validator_report"] = {
        STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY: _now()
    }
    return plan_id


def test_orphaned_query_selects_only_displayable_carded_marker_rows():
    store = FakeStore()
    _seed_orphaned_build(store, plan_id="orphaned")  # ready + marker + no card -> hit
    _seed_released_plan(store, plan_id="no-marker")  # ready, no marker -> skip
    _seed_released_plan(
        store, plan_id="carded", structured={"weeks": []}
    )  # has a card -> skip
    _seed_held_plan(store, plan_id="held")  # not displayable -> skip
    # A held plan that also carries a marker is still not displayable -> skip.
    store.plans["held"]["stage2_validator_report"] = {
        STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY: _now()
    }

    rows = store.list_plans_with_orphaned_structured_card_attempt(limit=25)

    assert [row["id"] for row in rows] == ["orphaned"]


def test_self_heal_requeues_orphaned_build(monkeypatch):
    """A build orphaned by a restart is re-queued and finishes on the next boot."""
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    plan_id = _seed_orphaned_build(store, plan_id="orphaned")
    automator = _StructuredAutomator(_valid_outcome("# released plan"))

    healed = asyncio.run(
        self_heal_orphaned_structured_cards(store=store, stage2=automator)
    )

    assert healed == 1
    assert len(automator.calls) == 1
    row = store.plans[plan_id]
    assert row["structured_plan"] is not None
    assert row["schema_version"] == SCHEMA_VERSION
    # The terminal outcome cleared the in-flight marker, so the plan is no longer
    # stuck "building".
    assert STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY not in row["stage2_validator_report"]


def test_self_heal_ignores_plans_without_an_orphaned_marker(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    _seed_released_plan(store, plan_id="no-marker")  # healthy: no marker, no card
    automator = _StructuredAutomator(_valid_outcome("# released plan"))

    healed = asyncio.run(
        self_heal_orphaned_structured_cards(store=store, stage2=automator)
    )

    # A plain cardless plan is backfill's job, not self-heal's — no marker means
    # no interrupted build to recover, so it is left untouched.
    assert healed == 0
    assert automator.calls == []
    assert store.plans["no-marker"].get("structured_plan") is None


def test_self_heal_noop_when_no_orphans(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    _seed_released_plan(store, plan_id="carded", structured={"weeks": []})
    automator = _StructuredAutomator(_valid_outcome())

    healed = asyncio.run(
        self_heal_orphaned_structured_cards(store=store, stage2=automator)
    )

    assert healed == 0
    assert automator.calls == []


def test_self_heal_survives_a_bad_plan_and_continues(monkeypatch):
    """One failing re-queue must not abort the rest of the sweep."""
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    store = FakeStore()
    _seed_orphaned_build(store, plan_id="bad")
    _seed_orphaned_build(store, plan_id="good")
    automator = _StructuredAutomator(_valid_outcome("# released plan"))

    original = admin_stage2_service.run_structured_plan_post_processing

    async def _flaky(*, plan_id, **kwargs):
        if plan_id == "bad":
            raise RuntimeError("boom")
        return await original(plan_id=plan_id, **kwargs)

    monkeypatch.setattr(
        admin_stage2_service, "run_structured_plan_post_processing", _flaky
    )

    healed = asyncio.run(
        self_heal_orphaned_structured_cards(store=store, stage2=automator)
    )

    # "bad" raised but was counted as attempted; "good" still converted.
    assert healed == 1
    assert store.plans["good"]["structured_plan"] is not None


# ---------------------------------------------------------------------------
# Stale-write guard: a card converted from now-superseded text is not written
# ---------------------------------------------------------------------------


def test_structured_artifacts_write_skipped_when_text_changed():
    """The narrow writer rejects a card whose source text no longer matches.

    Models an async conversion/backfill that read the plan text, produced a card,
    and tried to persist it after a concurrent edit changed final_plan_text. The
    card is now a stale projection of superseded text, so the write is skipped and
    the row keeps its raw-markdown fallback.
    """
    store = FakeStore()
    plan_id = _seed_held_plan(store)
    store.plans[plan_id]["final_plan_text"] = "# edited after conversion started"

    returned = store.update_plan_structured_artifacts(
        plan_id,
        structured_plan={"schema_version": SCHEMA_VERSION},
        schema_version=SCHEMA_VERSION,
        stage2_validator_report={},
        expected_final_plan_text="# approved plan",  # the pre-edit text
    )

    assert returned.get("structured_plan") is None
    assert store.plans[plan_id].get("structured_plan") is None


def test_structured_artifacts_write_proceeds_when_text_unchanged():
    """The card is persisted when the source text still matches at write time."""
    store = FakeStore()
    plan_id = _seed_held_plan(store)  # final_plan_text == "# approved plan"

    store.update_plan_structured_artifacts(
        plan_id,
        structured_plan={"schema_version": SCHEMA_VERSION},
        schema_version=SCHEMA_VERSION,
        stage2_validator_report={},
        expected_final_plan_text="# approved plan",
    )

    assert store.plans[plan_id]["structured_plan"] == {"schema_version": SCHEMA_VERSION}
    assert store.plans[plan_id]["schema_version"] == SCHEMA_VERSION
