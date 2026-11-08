from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

import api.stage2_automation as stage2_module
from api.stage2_automation import OpenAIStage2Automator, Stage2AutomationError
from fightcamp.stage2_policy import (
    ADMIN_REVIEW_BLOCKING_CODES,
    ATHLETE_RELEASE_WITH_FLAGS_CODES,
    apply_stage2_release_policy,
)
from support import FakeOpenAIClient as FakeClient

STRUCTURAL_INTEGRITY_CODES = {
    "phase_section_missing",
    "missing_week_session_role",
    "late_camp_session_incomplete",
    "late_fight_missing_required_countdown_session",
}


@pytest.fixture(autouse=True)
def _structured_plan_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin structured-plan generation OFF for this module.

    These tests exercise the single-call plan-text finalize/retry flow and
    assert exact provider call counts. Structured generation is on by default
    now (a second conversion call), so disable it here; the structured path has
    dedicated coverage in test_stage2_structured_persistence.py.
    """
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")


def _response(text: str, *, input_tokens: int = 10, output_tokens: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_test",
        output_text=text,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


def _incomplete_response() -> SimpleNamespace:
    # Mirrors an OpenAI Responses result truncated by the output-token budget
    # (reasoning tokens + plan text exceeded max_output_tokens).
    return SimpleNamespace(
        id="resp_incomplete",
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output_text="# Fight Camp Plan\n\nWeek 1 of a cut-off pl",
        usage=SimpleNamespace(input_tokens=10, output_tokens=24000, total_tokens=24010),
    )


def _review(status_value: str) -> dict:
    errors = [{"code": "restriction_violation"}] if status_value == "FAIL" else []
    warnings = [{"code": "generic_filler_phrase", "blocking": True}] if status_value == "WARN" else []
    review_flags = [{"code": "generic_filler_phrase"}] if status_value == "PASS_WITH_FLAGS" else []
    return {
        "status": "PASS" if status_value == "PASS_WITH_FLAGS" else status_value,
        "needs_retry": status_value not in {"PASS", "PASS_WITH_FLAGS"},
        "validator_report": {
            "errors": errors,
            "warnings": warnings + review_flags,
            "review_flag_count": len(review_flags),
        },
    }


def _stage1_result() -> dict:
    return {
        "plan_text": "# Stage 1 Draft",
        "coach_notes": "### Coach Review",
        "pdf_url": "https://example.com/stage1.pdf",
        "why_log": {"strength": {}},
        "stage2_payload": {"ok": True},
        "planning_brief": {"schema_version": "planning_brief.v1", "main_limiter": "conditioning"},
        "stage2_handoff_text": "handoff",
    }


@pytest.mark.parametrize(
    "findings",
    [
        [
            {
                "code": "goal_preservation_failed",
                "goal": "speed",
                "satisfied": False,
                "missing_coverage": ["D14-D20"],
            }
        ],
        [
            {"code": "goal_preservation_failed", "goal": "speed"},
            {"code": "goal_preservation_failed", "goal": "strength"},
            {"code": "goal_preservation_failed", "goal": "conditioning"},
        ],
    ],
)
def test_goal_findings_require_planner_regeneration_without_renderer_retry(monkeypatch, findings):
    monkeypatch.setattr(stage2_module, "validate_goal_preservation", lambda _: findings)
    monkeypatch.setattr(
        stage2_module, "review_stage2_output", lambda **_: _review("PASS")
    )
    client = FakeClient([_response("# Usable camp")])
    result = asyncio.run(
        OpenAIStage2Automator(client=client, model="test").finalize(
            stage1_result=_stage1_result()
        )
    )
    assert len(client.responses.calls) == 1
    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == "# Usable camp"
    assert result["final_plan_text"] == "# Usable camp"
    assert result["stage2_status"] == "stage2_pass"
    assert result["stage2_retry_text"] == ""
    assert result["requires_planner_regeneration"] is True
    report = result["stage2_validator_report"]
    assert report["errors"] == findings
    assert report["is_athlete_releasable"] is True
    assert report["release_decision"] == "publish_with_flags"


def test_goal_witness_loss_releases_without_validator_retry(monkeypatch):
    finding = {"code": "goal_preservation_render_mismatch", "goal": "strength"}
    monkeypatch.setattr(
        stage2_module,
        "review_stage2_output",
        lambda **_: {
            "status": "FAIL",
            "needs_retry": True,
            "validator_report": {"errors": [finding], "warnings": []},
        },
    )
    client = FakeClient([_response("# Usable camp")])
    result = asyncio.run(
        OpenAIStage2Automator(client=client, model="test").finalize(
            stage1_result=_stage1_result()
        )
    )
    assert len(client.responses.calls) == 1
    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == "# Usable camp"
    assert result["stage2_attempt_count"] == 1
    assert result["stage2_retry_text"] == ""
    assert result["stage2_validator_report"]["errors"] == [finding]


def test_missing_conditioning_is_source_repaired_without_extra_model_call():
    stage1 = _stage1_result()
    stage1["planning_brief"] = {
        "weekly_role_map": {
            "weeks": [
                {
                    "phase": "SPP",
                    "calendar_days": [{"weekday": "thursday", "d_day": 16}],
                    "session_roles": [
                        {
                            "category": "conditioning",
                            "role_key": "fight_pace_repeatability_day",
                            "scheduled_day_hint": "thursday",
                            "scheduled_countdown_label": "D-16",
                            "selected_exercise_assignments": [
                                {
                                    "name": "Plyo Step-Up Intervals",
                                    "effective_prescription": "2 x 30 sec work; 90 sec rest; RPE 8",
                                },
                                {
                                    "name": "Kettlebell Swing Intervals",
                                    "effective_prescription": "3 x 20 sec work; 100 sec rest; RPE 7",
                                },
                                {
                                    "name": "Assault Bike Repeat",
                                    "effective_prescription": "4 x 15 sec work; 75 sec rest; RPE 8",
                                },
                            ],
                        }
                    ],
                }
            ]
        }
    }
    client = FakeClient([
        _response(
            "D-16 (Thursday) — Conditioning\n"
            "- Plyo Step-Up Intervals: 2 x 30 sec work; 90 sec rest; RPE 8\n"
        )
    ])

    result = asyncio.run(OpenAIStage2Automator(client=client, model="test").finalize(stage1_result=stage1))

    assert len(client.responses.calls) == 1
    assert result["stage2_attempt_count"] == 1
    assert result["stage2_retry_text"] == ""
    assert "Kettlebell Swing Intervals: 3 x 20 sec work; 100 sec rest; RPE 7" in result["final_plan_text"]
    assert "Assault Bike Repeat: 4 x 15 sec work; 75 sec rest; RPE 8" in result["final_plan_text"]
    audit = result["stage2_validator_report"]["conditioning_render_repair"]
    assert audit["status"] == "applied"
    assert audit["model_call_used"] is False
    assert result["stage2_validator_report"]["repair_source_report"]


def test_conditioning_repair_preserves_goal_failure_and_holds_publication(monkeypatch):
    goal_finding = {"code": "goal_preservation_failed", "goal": "conditioning"}
    monkeypatch.setattr(stage2_module, "validate_goal_preservation", lambda _: [goal_finding])
    stage1 = _stage1_result()
    stage1["planning_brief"] = {
        "weekly_role_map": {
            "weeks": [
                {
                    "phase": "SPP",
                    "calendar_days": [{"weekday": "thursday", "d_day": 16}],
                    "session_roles": [
                        {
                            "category": "conditioning",
                            "role_key": "fight_pace_repeatability_day",
                            "scheduled_day_hint": "thursday",
                            "scheduled_countdown_label": "D-16",
                            "selected_exercise_assignments": [
                                {"name": "Zone 2 Run", "effective_prescription": "20 min at RPE 4"},
                                {"name": "Easy Bike", "effective_prescription": "15 min at RPE 3"},
                            ],
                        }
                    ],
                }
            ]
        }
    }
    client = FakeClient([
        _response("D-16 (Thursday) — Conditioning\n- Zone 2 Run: 20 min at RPE 4\n")
    ])

    result = asyncio.run(OpenAIStage2Automator(client=client, model="test").finalize(stage1_result=stage1))

    assert len(client.responses.calls) == 1
    assert "Easy Bike: 15 min at RPE 3" in result["final_plan_text"]
    assert result["status"] == "review_required"
    assert result["plan_text"] == ""
    assert result["requires_planner_regeneration"] is True
    report = result["stage2_validator_report"]
    assert goal_finding in report["errors"]
    assert report["release_decision"] == "hold"
    assert report["conditioning_render_repair"]["status"] == "applied"


@pytest.mark.parametrize("reconciliation", ["unresolved", "raises"])
def test_unresolved_conditioning_render_is_held_without_a_second_model_call(
    monkeypatch, reconciliation
):
    # Production (job b2106af0): the render-repair prompt carried the whole
    # planning brief, came to 1,257,368 chars against the 400,000 budget, and
    # failed the build. An unrepaired render must be held, never re-rendered.
    finding = {
        "code": "missing_selected_conditioning_assignment",
        "scheduled_d_day": 16,
        "exercise": "Easy Bike",
        "effective_prescription": "15 min at RPE 3",
    }
    monkeypatch.setattr(
        stage2_module,
        "review_stage2_output",
        lambda **_: {
            "status": "FAIL",
            "needs_retry": True,
            "validator_report": {"errors": [finding], "warnings": []},
        },
    )
    monkeypatch.setattr(stage2_module, "validate_goal_preservation", lambda _: [])
    monkeypatch.setattr(
        stage2_module,
        "build_stage2_retry",
        lambda **_: {"needs_retry": True, "repair_prompt": "x" * 1_257_368},
    )

    first_pass = "D-16 — Conditioning\n- Zone 2 Run: 20 min at RPE 4"

    def _reconcile(**_: object) -> dict:
        if reconciliation == "raises":
            raise RuntimeError("synthetic reconciliation failure")
        return {"text": first_pass, "applied": [], "unresolved": []}

    monkeypatch.setattr(stage2_module, "reconcile_selected_conditioning_assignments", _reconcile)
    client = FakeClient([_response(first_pass)])

    result = asyncio.run(OpenAIStage2Automator(client=client, model="test").finalize(
        stage1_result=_stage1_result()
    ))

    assert len(client.responses.calls) == 1
    assert result["stage2_attempt_count"] == 1
    assert result["stage2_retry_text"] == ""
    assert result["status"] == "review_required"
    assert result["plan_text"] == ""
    assert result["final_plan_text"] == first_pass
    report = result["stage2_validator_report"]
    assert report["release_decision"] == "hold"
    assert report["conditioning_render_hold"] is True
    assert report["conditioning_render_repair"]["status"] == "unresolved"
    assert report["conditioning_render_repair"]["model_call_used"] is False


def test_first_pass_pass_returns_ready_with_one_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stage2_module, "review_stage2_output", lambda **_: _review("PASS")
    )
    client = FakeClient([_response("# final plan")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert len(client.responses.calls) == 1
    assert result["status"] == "ready"
    assert result["plan_text"] == "# final plan"
    assert result["final_plan_text"] == "# final plan"
    assert result["stage2_status"] == "stage2_pass"
    assert result["stage2_attempt_count"] == 1
    assert result["stage2_retry_text"] == ""


def test_first_pass_omits_max_output_tokens_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNLXCK_STAGE2_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("PASS"))
    client = FakeClient([_response("# final plan")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    # Default is 0 (no cap) so the model is never truncated mid-plan.
    assert "max_output_tokens" not in client.responses.calls[0]


def test_first_pass_honors_max_output_tokens_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNLXCK_STAGE2_MAX_OUTPUT_TOKENS", "12000")
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("PASS"))
    client = FakeClient([_response("# final plan")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert client.responses.calls[0]["max_output_tokens"] == 12000


def test_first_pass_omits_max_output_tokens_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNLXCK_STAGE2_MAX_OUTPUT_TOKENS", "0")
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("PASS"))
    client = FakeClient([_response("# final plan")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert "max_output_tokens" not in client.responses.calls[0]


def test_structured_calls_request_json_object_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # The structured-card conversion calls must ask the provider for JSON output
    # mode so the response is always syntactically valid JSON.
    import json

    monkeypatch.delenv("UNLXCK_STAGE2_STRUCTURED_JSON_MODE", raising=False)
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_REPAIR", "0")  # single call keeps it simple
    client = FakeClient([_response(json.dumps([1, 2, 3]))])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    asyncio.run(
        automator._generate_structured_outcome(
            final_plan_text="# plan", planning_brief={}, source="test", costs=[]
        )
    )

    assert client.responses.calls[0]["text"] == {"format": {"type": "json_object"}}


def test_structured_calls_omit_json_mode_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_JSON_MODE", "0")
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_REPAIR", "0")
    client = FakeClient([_response(json.dumps([1, 2, 3]))])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    asyncio.run(
        automator._generate_structured_outcome(
            final_plan_text="# plan", planning_brief={}, source="test", costs=[]
        )
    )

    assert "text" not in client.responses.calls[0]


def test_structured_calls_use_strict_json_schema_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    # Opt-in schema mode sends the strict json_schema format instead of plain
    # JSON object mode, so the provider guarantees schema conformance.
    import json

    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_SCHEMA_MODE", "1")
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_REPAIR", "0")
    client = FakeClient([_response(json.dumps([1, 2, 3]))])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    asyncio.run(
        automator._generate_structured_outcome(
            final_plan_text="# plan", planning_brief={}, source="test", costs=[]
        )
    )

    fmt = client.responses.calls[0]["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert fmt["name"] == "structured_training_plan"
    assert fmt["schema"]["additionalProperties"] is False


def test_schema_mode_off_by_default_uses_json_object(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.delenv("UNLXCK_STAGE2_STRUCTURED_SCHEMA_MODE", raising=False)
    monkeypatch.delenv("UNLXCK_STAGE2_STRUCTURED_JSON_MODE", raising=False)
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_REPAIR", "0")
    client = FakeClient([_response(json.dumps([1, 2, 3]))])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    asyncio.run(
        automator._generate_structured_outcome(
            final_plan_text="# plan", planning_brief={}, source="test", costs=[]
        )
    )

    assert client.responses.calls[0]["text"]["format"] == {"type": "json_object"}


def test_schema_build_failure_degrades_to_json_object_not_free_form(monkeypatch: pytest.MonkeyPatch) -> None:
    # Schema mode on, JSON-object mode explicitly off: a schema-build failure must
    # still return json_object (valid JSON), never None (free-form).
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_SCHEMA_MODE", "1")
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_JSON_MODE", "0")

    def _boom() -> dict:
        raise RuntimeError("schema build broke")

    monkeypatch.setattr(stage2_module, "build_strict_structured_plan_schema", _boom)
    assert stage2_module._structured_response_format() == {"type": "json_object"}


def test_plan_text_first_pass_omits_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # The markdown plan-text pass must stay free-form (never JSON output mode),
    # regardless of the structured JSON-mode flag.
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_JSON_MODE", "1")
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("PASS"))
    client = FakeClient([_response("# final plan")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert "text" not in client.responses.calls[0]


def test_first_pass_incomplete_response_releases_with_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Partial Stage 2 text is still a usable plan; the provider audit stays visible.
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("PASS"))
    client = FakeClient([_incomplete_response()])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))
    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"].startswith("# Fight Camp Plan")
    assert result["stage2_status"] == "stage2_pass"
    assert any(
        item.get("code") == "stage2_output_truncated"
        for item in result["stage2_validator_report"]["errors"]
    )


def test_first_pass_pass_with_review_flags_returns_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("PASS_WITH_FLAGS"))
    client = FakeClient([_response("# final plan with minor flags")])
    automator = OpenAIStage2Automator(client=client, model="test-model")
    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))
    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == "# final plan with minor flags"


@pytest.mark.parametrize("code", sorted(ATHLETE_RELEASE_WITH_FLAGS_CODES))
def test_first_pass_low_risk_quality_codes_publish_with_flags(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    def _quality_flagged_review(**_: object) -> dict:
        finding = {"code": code, "phase": "SPP"}
        return {
            "status": "PASS",
            "needs_retry": False,
            "validator_report": {
                "errors": [],
                "warnings": [finding],
                "review_flags": [finding],
                "review_flag_count": 1,
            },
        }

    monkeypatch.setattr(stage2_module, "review_stage2_output", _quality_flagged_review)
    client = FakeClient([_response("# final plan with quality flag")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert len(client.responses.calls) == 1
    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == "# final plan with quality flag"
    assert result["final_plan_text"] == "# final plan with quality flag"
    assert result["stage2_status"] == "stage2_pass"
    assert result["stage2_validator_report"]["quality_review_flags"] == [
        {"code": code, "phase": "SPP"}
    ]
    assert result["stage2_validator_report"]["release_decision"] == "publish_with_flags"
    assert result["stage2_validator_report"]["is_athlete_releasable"] is True
    assert result["stage2_validator_report"]["is_publishable"] is True


@pytest.mark.parametrize("code", sorted(ADMIN_REVIEW_BLOCKING_CODES - STRUCTURAL_INTEGRITY_CODES))
def test_first_pass_context_or_programme_codes_publish_with_flags(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    finding = {"code": code, "phase": "SPP"}
    monkeypatch.setattr(
        stage2_module,
        "review_stage2_output",
        lambda **_: {
            "status": "PASS",
            "needs_retry": False,
            "validator_report": {
                "errors": [],
                "warnings": [finding],
                "review_flags": [finding],
                "review_flag_count": 1,
            },
        },
    )
    automator = OpenAIStage2Automator(
        client=FakeClient([_response("# unsafe or incomplete plan")]),
        model="test-model",
    )

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == "# unsafe or incomplete plan"
    assert result["stage2_status"] == "stage2_pass"
    report = result["stage2_validator_report"]
    assert report["release_decision"] == "publish_with_flags"
    assert report["is_athlete_releasable"] is True
    assert report["is_publishable"] is True
    assert report["admin_review_blocking_flags"] == [finding]


@pytest.mark.parametrize("code", sorted(ADMIN_REVIEW_BLOCKING_CODES & STRUCTURAL_INTEGRITY_CODES))
def test_first_pass_structural_integrity_codes_hold_after_unresolved_repair(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    finding = {"code": code, "phase": "SPP"}
    monkeypatch.setattr(
        stage2_module,
        "review_stage2_output",
        lambda **_: {
            "status": "PASS",
            "needs_retry": False,
            "validator_report": {
                "errors": [],
                "warnings": [finding],
                "review_flags": [finding],
                "review_flag_count": 1,
                "release_decision": "publish_with_flags",
                "is_athlete_releasable": True,
                "is_publishable": True,
            },
        },
    )
    automator = OpenAIStage2Automator(
        client=FakeClient([_response("# structurally incomplete plan")]),
        model="test-model",
    )

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["status"] == "review_required"
    assert result["plan_text"] == ""
    assert result["final_plan_text"] == "# structurally incomplete plan"
    assert result["stage2_status"] == "stage2_failed"
    report = result["stage2_validator_report"]
    assert report["release_decision"] == "hold"
    assert report["is_athlete_releasable"] is False
    assert report["is_publishable"] is False
    assert report["errors"][-1]["code"] == "structural_integrity_failure"


def test_first_pass_mixed_quality_and_blocking_codes_publish_with_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings = [{"code": "option_overload"}, {"code": "missing_required_element"}]
    monkeypatch.setattr(
        stage2_module,
        "review_stage2_output",
        lambda **_: {
            "status": "PASS",
            "needs_retry": False,
            "validator_report": {
                "errors": [],
                "warnings": findings,
                "review_flags": findings,
                "review_flag_count": 2,
            },
        },
    )
    automator = OpenAIStage2Automator(client=FakeClient([_response("# mixed plan")]), model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == "# mixed plan"
    assert result["stage2_validator_report"]["quality_review_flags"] == findings
    assert result["stage2_validator_report"]["admin_review_blocking_flags"] == [
        {"code": "missing_required_element"}
    ]


def test_first_pass_unknown_blocking_code_publishes_with_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown = {"code": "brand_new_warning_code"}
    monkeypatch.setattr(
        stage2_module,
        "review_stage2_output",
        lambda **_: {
            "status": "WARN",
            "needs_retry": True,
            "validator_report": {
                "errors": [],
                "warnings": [unknown],
                "blocking_warnings": [unknown],
                "review_flags": [],
            },
        },
    )
    automator = OpenAIStage2Automator(client=FakeClient([_response("# unknown plan")]), model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == "# unknown plan"
    assert result["stage2_validator_report"]["blocking_warnings"] == [unknown]
    assert result["stage2_validator_report"]["release_decision"] == "publish_with_flags"
    assert result["stage2_validator_report"]["is_publishable"] is True


def test_first_pass_hard_failure_publishes_with_flags_with_one_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("FAIL"))
    client = FakeClient([_response("# first pass needs review")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert len(client.responses.calls) == 1
    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == "# first pass needs review"
    assert result["final_plan_text"] == "# first pass needs review"
    assert result["stage2_status"] == "stage2_pass"
    assert result["stage2_attempt_count"] == 1
    assert result["stage2_retry_text"] == ""
    report = dict(result["stage2_validator_report"])
    report.pop("structured_plan")
    expected = apply_stage2_release_policy(_review("FAIL")["validator_report"])
    assert report["errors"] == expected["errors"]
    assert report["blocking_warnings"] == expected["blocking_warnings"]
    assert report["release_decision"] == "publish_with_flags"


def test_effective_dose_failure_does_not_force_renderer_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finding = {
        "code": "late_camp_effective_prescription_exceeded",
        "severity": "blocker",
        "line": "Barbell Back Squat: 3 sets x 5 reps",
        "scheduled_d_day": 16,
        "exercise": "Back Squat",
        "effective_max_sets": 3,
        "effective_max_reps": 3,
        "effective_rpe_cap": 7,
        "violations": ["reps 5 > effective max 3"],
    }
    monkeypatch.setattr(
        stage2_module,
        "review_stage2_output",
        lambda **_: {
            "status": "FAIL",
            "needs_retry": True,
            "validator_report": {"errors": [finding], "warnings": []},
        },
    )
    client = FakeClient([
        _response("# D-16\nBack Squat: 3 x 5", input_tokens=11, output_tokens=7),
    ])
    result = asyncio.run(OpenAIStage2Automator(client=client, model="test-model").finalize(
        stage1_result=_stage1_result()
    ))

    assert len(client.responses.calls) == 1
    assert result["stage2_attempt_count"] == 1
    assert result["stage2_status"] == "stage2_pass"
    assert result["status"] == "publishable_with_flags"
    assert result["final_plan_text"].endswith("Back Squat: 3 x 5")
    assert result["stage2_retry_text"] == ""
    assert result["stage2_cost"]["stage2_input_tokens"] == 11
    assert result["stage2_cost"]["stage2_output_tokens"] == 7
    assert result["stage2_cost"]["stage2_total_tokens"] == 18


def test_first_pass_non_pass_with_findings_returns_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("WARN"))
    client = FakeClient([_response("# first pass clean enough to release")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert len(client.responses.calls) == 1
    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == "# first pass clean enough to release"
    assert result["final_plan_text"] == "# first pass clean enough to release"
    assert result["stage2_status"] == "stage2_pass"
    assert result["stage2_attempt_count"] == 1
    assert result["stage2_retry_text"] == ""


def test_build_stage2_retry_is_not_called_during_automatic_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("FAIL"))

    def _unexpected_retry(**_: object) -> dict:
        raise AssertionError("automatic Stage 2 finalization must not build a repair prompt")

    monkeypatch.setattr(
        stage2_module,
        "build_stage2_retry",
        _unexpected_retry,
        raising=False,
    )
    client = FakeClient([_response("# failed first pass")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert len(client.responses.calls) == 1
    assert result["status"] == "publishable_with_flags"


def test_retry_pass_is_never_sent_during_automatic_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("FAIL"))
    client = FakeClient([_response("# failed first pass"), _response("# retry should stay unused")])
    automator = OpenAIStage2Automator(client=client, model="test-model")
    original_generate_text = automator._generate_text
    seen_attempts: list[str] = []

    async def _record_attempt(
        prompt: str,
        *,
        attempt_label: str,
        source: str,
        log_context: dict | None = None,
        timeout: float | None = None,
        response_format: dict | None = None,
    ) -> tuple[str, dict]:
        seen_attempts.append(attempt_label)
        if attempt_label == "retry_pass":
            raise AssertionError("automatic Stage 2 finalization must not send retry_pass")
        return await original_generate_text(
            prompt,
            attempt_label=attempt_label,
            source=source,
            log_context=log_context,
            timeout=timeout,
            response_format=response_format,
        )

    monkeypatch.setattr(automator, "_generate_text", _record_attempt)

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["status"] == "publishable_with_flags"
    assert seen_attempts == ["first_pass"]
    assert len(client.responses.calls) == 1


def test_first_pass_over_limit_blocks_before_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNLXCK_STAGE2_MAX_FIRST_PASS_CHARS", "10")
    stage1 = _stage1_result()
    stage1["stage2_handoff_text"] = "x" * 11
    client = FakeClient([_response("# should not be called")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    with pytest.raises(Stage2AutomationError, match="first_pass prompt too large"):
        asyncio.run(automator.finalize(stage1_result=stage1))

    assert client.responses.calls == []


def test_first_pass_default_limit_is_400k_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNLXCK_STAGE2_MAX_FIRST_PASS_CHARS", raising=False)
    stage1 = _stage1_result()
    stage1["stage2_handoff_text"] = "x" * 400_001
    client = FakeClient([_response("# should not be called")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    with pytest.raises(Stage2AutomationError, match="chars > 400000"):
        asyncio.run(automator.finalize(stage1_result=stage1))

    assert client.responses.calls == []


def test_quota_error_stops_after_single_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(
        [
            RuntimeError(
                'Error code: 429 - {"error":{"message":"Too Many Requests","code":"insufficient_quota"}}'
            )
        ]
    )
    automator = OpenAIStage2Automator(client=client, model="test-model")

    with pytest.raises(Stage2AutomationError, match="quota/rate limit hit"):
        asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert len(client.responses.calls) == 1


def test_generic_provider_failure_raises_sanitized_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-quota provider failure must not surface raw exception text (which can
    # carry request payloads/provider internals) in the raised/stored error.
    raw = "boom connecting to https://api.openai.com with key sk-secret-payload-12345"
    client = FakeClient([RuntimeError(raw)])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    with pytest.raises(Stage2AutomationError) as exc_info:
        asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    message = str(exc_info.value)
    assert message == "Stage 2 model request failed. Check server logs."
    assert "sk-secret-payload-12345" not in message
    assert "api.openai.com" not in message
    assert len(client.responses.calls) == 1


def test_first_pass_pass_records_token_cost_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("PASS"))
    client = FakeClient([_response("# final plan", input_tokens=123, output_tokens=456)])
    automator = OpenAIStage2Automator(client=client, model="gpt-5-mini")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    cost = result["stage2_cost"]
    assert cost["stage2_model"] == "gpt-5-mini"
    assert cost["stage2_input_tokens"] == 123
    assert cost["stage2_output_tokens"] == 456
    assert cost["stage2_total_tokens"] == 579
    assert cost["stage2_attempt_count"] == 1
    assert cost["stage2_response_id"] == "resp_test"
    assert cost["stage2_cost_recorded_at"]
    assert isinstance(cost["stage2_estimated_cost_usd"], float)


def test_flagged_release_also_carries_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("FAIL"))
    client = FakeClient([_response("# first pass needs review", input_tokens=7, output_tokens=9)])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    assert result["status"] == "publishable_with_flags"
    assert result["stage2_cost"]["stage2_input_tokens"] == 7
    assert result["stage2_cost"]["stage2_output_tokens"] == 9


def test_missing_usage_does_not_crash_and_falls_back_to_estimates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An OpenAI response with no ``usage`` field must not crash generation; cost
    # falls back to char-based estimates so the row is still populated.
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("PASS"))
    response = SimpleNamespace(id="resp_no_usage", output_text="# final plan")
    client = FakeClient([response])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    cost = result["stage2_cost"]
    assert result["status"] == "ready"
    assert cost["stage2_input_tokens"] >= 1
    assert cost["stage2_output_tokens"] >= 1
    assert cost["stage2_response_id"] == "resp_no_usage"


def test_incomplete_response_release_carries_actual_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("PASS"))
    client = FakeClient([_incomplete_response()])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))
    cost = result["stage2_cost"]
    assert result["status"] == "publishable_with_flags"
    assert cost is not None
    assert cost["stage2_model"] == "test-model"
    assert cost["stage2_input_tokens"] == 10
    assert cost["stage2_output_tokens"] == 24000
    assert cost["stage2_response_id"] == "resp_incomplete"


def test_request_failure_carries_estimated_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    # No response means no actual usage; the failed attempt still records an
    # estimated input cost and leaves genuinely-unknown fields as None.
    client = FakeClient([RuntimeError("boom")])
    automator = OpenAIStage2Automator(client=client, model="test-model")

    with pytest.raises(Stage2AutomationError) as exc_info:
        asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    cost = exc_info.value.stage2_cost
    assert cost is not None
    assert cost["stage2_model"] == "test-model"
    assert cost["stage2_input_tokens"] >= 1
    assert cost["stage2_output_tokens"] is None
    assert cost["stage2_total_tokens"] is None
    assert cost["stage2_response_id"] is None


def test_from_env_disables_openai_sdk_retries_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, object] = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.update(kwargs)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("UNLXCK_STAGE2_OPENAI_MAX_RETRIES", raising=False)

    automator = OpenAIStage2Automator.from_env()

    assert isinstance(automator, OpenAIStage2Automator)
    assert captured_kwargs["max_retries"] == 0


def test_from_env_invalid_timeout_falls_back_to_210(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, object] = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.update(kwargs)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("UNLXCK_STAGE2_TIMEOUT_SECONDS", "not-a-number")

    automator = OpenAIStage2Automator.from_env()

    assert isinstance(automator, OpenAIStage2Automator)
    assert captured_kwargs["timeout"] == 210.0


# --- Structural source repair: fail-closed release + audit -------------------

_STRUCTURAL_REPAIR_RENDERED_WEEK_ONE = (
    "## PHASE 1: GPP\n"
    "### Week 1\n"
    "#### Mon (D-28) — Strength build\n"
    "- Landmine Press - 4x5\n"
)


def _structural_repair_brief() -> dict:
    return {
        "athlete_model": {"sport": "boxing"},
        "restrictions": [],
        "phase_strategy": {},
        "candidate_pools": {},
        "weekly_role_map": {
            "weeks": [
                {
                    "week_index": 1,
                    "phase": "GPP",
                    "calendar_days": [{"weekday": "Mon", "d_day": 28}],
                    "session_roles": [
                        {
                            "role_key": "strength_day",
                            "category": "strength",
                            "athlete_facing_label": "Strength build",
                            "scheduled_day_hint": "Mon",
                            "scheduled_countdown_label": "D-28",
                            "display_text": "- Landmine Press - 4x5",
                        }
                    ],
                },
                {
                    "week_index": 2,
                    "phase": "SPP",
                    "calendar_days": [{"weekday": "Mon", "d_day": 21}],
                    "session_roles": [
                        {
                            "role_key": "conditioning_day",
                            "category": "conditioning",
                            "athlete_facing_label": "Alactic conditioning",
                            "scheduled_day_hint": "Mon",
                            "scheduled_countdown_label": "D-21",
                            "selected_exercise_assignments": [
                                {
                                    "name": "Air Bike Sprint",
                                    "effective_prescription": {"display": "6 x 6 sec / 90 sec easy"},
                                }
                            ],
                        }
                    ],
                },
            ]
        },
    }


def _structural_repair_result(final_plan_text: str, structural_finding: dict, *, extra_errors=None) -> dict:
    return {
        "status": "publishable_with_flags",
        "plan_text": final_plan_text,
        "final_plan_text": final_plan_text,
        "stage2_status": "stage2_pass",
        "stage2_validator_report": {
            "errors": list(extra_errors or []),
            "warnings": [structural_finding],
            "review_flags": [structural_finding],
            "release_decision": "publish_with_flags",
            "is_publishable": True,
            "is_athlete_releasable": True,
        },
    }


def test_structural_source_repair_publishes_when_repair_clears_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_text = _STRUCTURAL_REPAIR_RENDERED_WEEK_ONE + "\n## PHASE 2: SPP\n### Week 2\n"
    finding = {"code": "missing_week_session_role", "week_index": 2, "role_key": "conditioning_day"}
    result = _structural_repair_result(original_text, finding)

    monkeypatch.setattr(
        stage2_module,
        "review_stage2_output",
        lambda **_: {
            "status": "PASS",
            "validator_report": {
                "errors": [],
                "warnings": [{"code": "option_overload"}],
                "review_flags": [{"code": "option_overload"}],
                "release_decision": "publish_with_flags",
                "is_publishable": True,
                "is_athlete_releasable": True,
            },
        },
    )

    stage2_module._apply_structural_source_repair_and_hold(
        result, planning_brief=_structural_repair_brief(), source="test"
    )

    assert result["status"] == "publishable_with_flags"
    assert result["stage2_status"] == "stage2_pass"
    assert "- Air Bike Sprint — 6 x 6 sec / 90 sec easy" in result["final_plan_text"]
    assert result["plan_text"] == result["final_plan_text"]
    report = result["stage2_validator_report"]
    assert report["release_decision"] == "publish_with_flags"
    audit = report["source_repair"]["structural_source_repair"]
    assert audit["status"] == "applied"
    assert audit["unresolved_count"] == 0
    assert any(entry["role_key"] == "conditioning_day" for entry in audit["applied"])
    assert audit["previous_release_decision"] == "publish_with_flags"


def test_structural_source_repair_holds_and_preserves_original_on_unresolved() -> None:
    # Week 1 is not rendered, so its mandatory strength day cannot be restored in
    # place; the repair reports it unresolved and the plan must hold with the
    # ORIGINAL text (never a partial repair) and the original failure preserved.
    original_text = "## PHASE 2: SPP\n### Week 2\n"
    finding = {"code": "phase_section_missing", "phase": "GPP"}
    result = _structural_repair_result(
        original_text, finding, extra_errors=[{"code": "restriction_violation", "line": "x"}]
    )

    stage2_module._apply_structural_source_repair_and_hold(
        result, planning_brief=_structural_repair_brief(), source="test"
    )

    assert result["status"] == "review_required"
    assert result["plan_text"] == ""
    assert result["final_plan_text"] == original_text
    assert result["stage2_status"] == "stage2_failed"
    report = result["stage2_validator_report"]
    assert report["release_decision"] == "hold"
    assert report["is_publishable"] is False
    assert report["errors"][-1]["code"] == "structural_integrity_failure"
    # An unrelated failure is preserved, not erased by the repair.
    assert any(err["code"] == "restriction_violation" for err in report["errors"])
    audit = report["source_repair"]["structural_source_repair"]
    assert audit["status"] == "unresolved_structural_loss"
    assert audit["unresolved_count"] >= 1


def test_structural_source_repair_holds_when_revalidation_still_structural(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_text = _STRUCTURAL_REPAIR_RENDERED_WEEK_ONE + "\n## PHASE 2: SPP\n### Week 2\n"
    finding = {"code": "missing_week_session_role", "week_index": 2, "role_key": "conditioning_day"}
    result = _structural_repair_result(original_text, finding)

    # The repair changes the text, but revalidation still reports structural loss:
    # fail closed to a hold and keep the original plan.
    monkeypatch.setattr(
        stage2_module,
        "review_stage2_output",
        lambda **_: {
            "status": "WARN",
            "validator_report": {
                "errors": [],
                "warnings": [finding],
                "review_flags": [finding],
                "release_decision": "publish_with_flags",
                "is_publishable": True,
                "is_athlete_releasable": True,
            },
        },
    )

    stage2_module._apply_structural_source_repair_and_hold(
        result, planning_brief=_structural_repair_brief(), source="test"
    )

    assert result["status"] == "review_required"
    assert result["plan_text"] == ""
    assert result["final_plan_text"] == original_text
    report = result["stage2_validator_report"]
    assert report["release_decision"] == "hold"
    audit = report["source_repair"]["structural_source_repair"]
    assert audit["status"] == "unresolved_after_repair"


def test_structural_source_repair_holds_when_revalidation_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_text = _STRUCTURAL_REPAIR_RENDERED_WEEK_ONE + "\n## PHASE 2: SPP\n### Week 2\n"
    finding = {"code": "missing_week_session_role", "week_index": 2, "role_key": "conditioning_day"}
    result = _structural_repair_result(original_text, finding)

    def _boom(**_):
        raise RuntimeError("revalidation exploded")

    monkeypatch.setattr(stage2_module, "review_stage2_output", _boom)

    stage2_module._apply_structural_source_repair_and_hold(
        result, planning_brief=_structural_repair_brief(), source="test"
    )

    assert result["status"] == "review_required"
    assert result["plan_text"] == ""
    assert result["final_plan_text"] == original_text
    report = result["stage2_validator_report"]
    assert report["release_decision"] == "hold"
    audit = report["source_repair"]["structural_source_repair"]
    assert audit["status"] == "revalidation_failed"


def test_structural_source_repair_noop_without_structural_findings() -> None:
    original_text = "## PHASE 2: SPP\n### Week 2\n- Air Bike Sprint\n"
    result = _structural_repair_result(original_text, {"code": "option_overload"})

    stage2_module._apply_structural_source_repair_and_hold(
        result, planning_brief=_structural_repair_brief(), source="test"
    )

    # No structural loss -> the repair is inert and the plan is untouched.
    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"] == original_text
    assert "source_repair" not in result["stage2_validator_report"]


def _preflight_finding(code: str) -> dict:
    return {
        "code": code,
        "severity": "info" if code == "late_physical_role_safely_omitted" else "blocker",
        "role_key": "secondary_strength_day",
        "countdown_label": "D-11",
    }


@pytest.mark.parametrize(
    "code",
    # Neither is a release-hold code: the safe omission is informational, and a
    # missing exposure is routed to admin review by the same authority. Both
    # used to suppress the Stage 2 model call entirely.
    ["late_physical_role_safely_omitted", "late_physical_role_missing_assignment"],
)
def test_non_holding_preflight_finding_does_not_skip_stage2(monkeypatch, code):
    """Production case: a non-holding finding must not suppress the model call."""
    finding = _preflight_finding(code)
    monkeypatch.setattr(
        stage2_module,
        "build_stage2_package",
        lambda **_: {
            "planner_preflight_findings": [finding],
            "planning_brief": {"schema_version": "planning_brief.v1"},
            "stage2_payload": {"ok": True},
            "handoff_text": "handoff",
            "draft_plan_text": "# Stage 1 Draft",
        },
    )
    monkeypatch.setattr(stage2_module, "validate_goal_preservation", lambda _: [])
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("PASS"))
    client = FakeClient([_response("# Usable camp")])
    result = asyncio.run(
        OpenAIStage2Automator(client=client, model="test").finalize(
            stage1_result=_stage1_result()
        )
    )

    assert len(client.responses.calls) == 1
    assert result["stage2_attempt_count"] >= 1
    assert result["final_plan_text"] == "# Usable camp"
    assert result["plan_text"] == "# Usable camp"
    assert result["plan_text"] != result["draft_plan_text"]
    assert result["stage2_status"] == "stage2_pass"
    report = result["stage2_validator_report"]
    assert finding in report["review_flags"]
    assert report.get("planner_preflight_failed") is not True


def test_blocking_preflight_finding_still_prevents_stage2(monkeypatch):
    finding = _preflight_finding("selected_loaded_exercise_forbidden")
    monkeypatch.setattr(
        stage2_module,
        "build_stage2_package",
        lambda **_: {
            "planner_preflight_findings": [finding],
            "planning_brief": {"schema_version": "planning_brief.v1"},
            "stage2_payload": {"ok": True},
            "handoff_text": "handoff",
            "draft_plan_text": "# Stage 1 Draft",
        },
    )
    client = FakeClient([_response("# Usable camp")])
    result = asyncio.run(
        OpenAIStage2Automator(client=client, model="test").finalize(
            stage1_result=_stage1_result()
        )
    )

    assert client.responses.calls == []
    assert result["stage2_attempt_count"] == 0
    assert result["final_plan_text"] == ""
    assert result["plan_text"] == ""
    assert result["stage2_validator_report"]["planner_preflight_failed"] is True


def test_cost_records_reasoning_tokens_elapsed_time_and_call_breakdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage2_module, "review_stage2_output", lambda **_: _review("PASS"))
    response = SimpleNamespace(
        id="resp_reasoning",
        output_text="# final plan",
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=900,
            total_tokens=1000,
            output_tokens_details=SimpleNamespace(reasoning_tokens=700),
        ),
    )
    client = FakeClient([response])
    automator = OpenAIStage2Automator(client=client, model="gpt-5-mini")

    result = asyncio.run(automator.finalize(stage1_result=_stage1_result()))

    cost = result["stage2_cost"]
    assert cost["stage2_reasoning_tokens"] == 700
    assert cost["stage2_elapsed_seconds"] is not None
    assert cost["stage2_elapsed_seconds"] >= 0
    assert cost["stage2_calls"] == [
        {
            "label": "first_pass",
            "elapsed_seconds": cost["stage2_elapsed_seconds"],
            "input_tokens": 100,
            "output_tokens": 900,
            "reasoning_tokens": 700,
            "source": "unknown",
            "prompt_chars": len(_stage1_result()["stage2_handoff_text"]),
            "response_chars": len("# final plan"),
            "outcome": result["stage2_validator_report"]["release_decision"],
        }
    ]
    assert result["stage2_validator_report"]["stage2_model_calls"] == cost["stage2_calls"]


def test_merged_cost_sums_reasoning_and_elapsed_and_concatenates_calls() -> None:
    first = {
        "stage2_input_tokens": 10,
        "stage2_output_tokens": 20,
        "stage2_total_tokens": 30,
        "stage2_reasoning_tokens": 15,
        "stage2_elapsed_seconds": 1.5,
        "stage2_calls": [{"label": "first_pass"}],
    }
    second = {
        "stage2_input_tokens": 1,
        "stage2_output_tokens": 2,
        "stage2_total_tokens": 3,
        "stage2_reasoning_tokens": None,
        "stage2_elapsed_seconds": 2.25,
        "stage2_calls": [{"label": "structured_first"}],
    }

    merged = stage2_module._merge_stage2_costs(first, second)

    assert merged["stage2_reasoning_tokens"] == 15
    assert merged["stage2_elapsed_seconds"] == 3.75
    assert merged["stage2_calls"] == [{"label": "first_pass"}, {"label": "structured_first"}]
    unknown = stage2_module._merge_stage2_costs(
        {"stage2_reasoning_tokens": None}, {"stage2_reasoning_tokens": None}
    )
    assert unknown["stage2_reasoning_tokens"] is None
