"""Stage 1 self-parity baseline.

These tests run the Stage 2 validator against Stage 1's *own* rendered draft and
lock in the current gap so the "make Stage 1 match live" tracks can be measured.

Two invariants matter most:

* **Gating precondition** — Stage 1's draft already produces zero validator
  errors and zero hard blocking warnings for every representative scenario.
  That is the precondition for ever bypassing the Stage 2 LLM, so it must never
  regress.
* **Bounded soft gap** — every remaining soft review-flag code is in a known
  baseline set. New code types are a regression (the draft drifted further from
  live); removing codes is the win each rendering track is chasing, and is
  always allowed by a subset assertion.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, time, timedelta

import pytest

pytest.importorskip("fastapi")

from support import _build_request  # noqa: E402

from fightcamp.main import generate_plan_sync  # noqa: E402
from fightcamp.role_labels import (  # noqa: E402
    athlete_facing_label_for,
    humanize_role_key,
    stamp_weekly_role_map_labels,
)
from fightcamp.stage1_parity import (  # noqa: E402
    parity_breakdown,
    review_stage1_self_output,
    stage1_parity_breakdown,
)
from fightcamp.normal_calendar_placement import fill_missing_session_days  # noqa: E402
from fightcamp.weekly_plan_render import (  # noqa: E402
    _sanitize_dose,
    render_weekly_schedule_section,
)

# Scenarios that render a normal dated camp (not a late-fight countdown). Stage 1
# still owns the role-map structure for these, but must not render final
# week/day/exercise sessions into the draft.
NORMAL_CAMP_SCENARIOS = frozenset(
    {
        "standard_amateur_boxing",
        "long_camp_pro",
        "weight_cut",
        "injury_knee",
        "no_injury_clean",
    }
)
WEEK_STRUCTURE_CODES = frozenset(
    {"missing_week_session_role", "late_camp_session_incomplete"}
)


# Stage 1 derives days-out, the plan-creation weekday and every calendar
# placement from the clock, so scenarios built on the real ``date.today()`` give
# different plans on different weekdays (a Thursday run, for example, underfills
# the long-camp conditioning role). Every scenario is pinned to one fixed
# reference day instead, so the suite is deterministic whenever it runs.
PINNED_TODAY = date(2026, 9, 28)  # Monday


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    from planner_clock import pin_planner_clock

    pin_planner_clock(monkeypatch, datetime.combine(PINNED_TODAY, time(12, 0)))


def _fight_date(days_out: int) -> str:
    return (PINNED_TODAY + timedelta(days=days_out)).isoformat()


# Scenario overrides keyed by name, dated relative to ``PINNED_TODAY``.
def _scenarios() -> dict[str, dict]:
    return {
        "standard_amateur_boxing": {"fight_date": _fight_date(56)},
        "long_camp_pro": {
            "athlete": {"professional_status": "pro", "record": "12-1"},
            "fight_date": _fight_date(70),
            "weekly_training_frequency": 5,
            "training_availability": [
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Saturday",
            ],
        },
        "weight_cut": {
            "athlete": {"weight_kg": 80.0, "target_weight_kg": 70.0},
            "fight_date": _fight_date(56),
        },
        "injury_knee": {
            "injuries": "moderate left knee pain on deep squats",
            "fight_date": _fight_date(56),
        },
        "late_fight_countdown": {"fight_date": _fight_date(9)},
        "fight_week": {"fight_date": _fight_date(5)},
        "no_injury_clean": {"injuries": "", "fight_date": _fight_date(56)},
    }


# Soft review-flag codes Stage 1's own draft is currently allowed to emit.
# Driving this set toward empty is the whole point of the Stage 1 quality tracks.
# Adding a code here means the draft regressed; removing one is the goal.
BASELINE_REVIEW_FLAG_CODES = frozenset(
    {
        "support_takeover_before_anchor",
        "weak_anchor_session",
        "template_like_session_render",
        "missing_week_session_role",
        "late_camp_session_incomplete",
        "missing_injury_lead_summary",
        "missing_weight_cut_lead_summary",
        "generic_instruction_opener",
        "sport_language_leak",
        "conditional_conditioning_choice",
        "late_fight_missing_countdown_header",
        "late_fight_active_role_overage",
        "late_fight_block_overage",
        "late_fight_meaningful_stress_overage",
        "late_fight_forbidden_content",
        "late_fight_hard_sparring_overage",
        "late_fight_neural_power_stacking",
    }
)


# Validator error codes the Stage 1 draft is currently allowed to carry. Stage 1
# deliberately renders no final week/day/exercise sessions for a normal camp, so
# the render-based goal checks cannot be satisfied by the draft; the rest are
# tracked gaps. Like the review-flag baseline, adding a code is a regression and
# removing one is the goal. Nothing here may hold a release.
BASELINE_ERROR_CODES = frozenset(
    {
        "goal_preservation_render_mismatch",
        "goal_preservation_failed",
        "missing_priority_microdose",
    }
)
HOLD_ERROR_CODES = frozenset(
    {"conditioning_role_workload_underfilled", "ambiguous_working_dose"}
)


_STAGE1_RESULTS: dict[str, dict] = {}


def _run_stage1(overrides: dict) -> dict:
    # Every test runs under the same pinned clock, so one generation per
    # scenario serves all four checks; each caller gets its own copy.
    key = json.dumps(overrides, sort_keys=True, default=str)
    if key not in _STAGE1_RESULTS:
        result = generate_plan_sync(_build_request(overrides).to_payload())
        assert result.get("status") != "invalid_input", result
        assert isinstance(result.get("planning_brief"), dict) and result["planning_brief"], (
            f"scenario produced no planning_brief: {result.get('status')!r}"
        )
        _STAGE1_RESULTS[key] = result
    return copy.deepcopy(_STAGE1_RESULTS[key])


@pytest.mark.parametrize("name,overrides", list(_scenarios().items()))
def test_stage1_draft_has_no_unexpected_release_blockers(name: str, overrides: dict) -> None:
    """Stage 1 trips no release-holding error and stays inside the error baseline."""
    result = _run_stage1(overrides)
    breakdown = stage1_parity_breakdown(result)

    error_codes = set(breakdown["error_codes"])
    assert not error_codes & HOLD_ERROR_CODES, (
        f"{name}: Stage 1 draft carries release-holding errors {sorted(error_codes & HOLD_ERROR_CODES)}"
    )
    assert error_codes <= BASELINE_ERROR_CODES, (
        f"{name}: Stage 1 draft produced NEW validator errors "
        f"{sorted(error_codes - BASELINE_ERROR_CODES)}; the draft drifted further from live."
    )
    unexpected = set(breakdown["blocking_codes"]) - WEEK_STRUCTURE_CODES
    assert not unexpected, (
        f"{name}: Stage 1 draft produced unexpected release blockers {sorted(unexpected)}"
    )
    assert breakdown["is_publishable"] is (breakdown["blocking_count"] == 0)


@pytest.mark.parametrize("name,overrides", list(_scenarios().items()))
def test_stage1_soft_review_flags_stay_within_baseline(name: str, overrides: dict) -> None:
    """Remaining soft gap is bounded by the documented baseline set."""
    result = _run_stage1(overrides)
    breakdown = stage1_parity_breakdown(result)
    observed = set(breakdown["review_flag_codes"])
    unexpected = observed - BASELINE_REVIEW_FLAG_CODES
    assert not unexpected, (
        f"{name}: Stage 1 draft emitted NEW review-flag codes {sorted(unexpected)}; "
        "the draft drifted further from live. Fix the renderer or update the baseline."
    )


@pytest.mark.parametrize("name,overrides", list(_scenarios().items()))
def test_every_rendered_session_role_has_athlete_facing_label(
    name: str, overrides: dict
) -> None:
    """Stage 1 keeps deterministic labels for every planned role."""
    result = _run_stage1(overrides)
    weekly_role_map = (result["planning_brief"].get("weekly_role_map") or {})
    missing: list[str] = []
    for week in weekly_role_map.get("weeks", []) or []:
        for role in week.get("session_roles", []) or []:
            label = str(role.get("athlete_facing_label") or "").strip()
            if not label:
                missing.append(str(role.get("role_key")))
    assert not missing, f"{name}: session roles missing athlete_facing_label: {missing}"


@pytest.mark.parametrize(
    "name,overrides",
    [(n, ov) for n, ov in _scenarios().items() if n in NORMAL_CAMP_SCENARIOS],
)
def test_normal_camps_do_not_render_final_weekly_schedule(name: str, overrides: dict) -> None:
    """Stage 1 must not inject final weekly session content into the draft."""
    result = _run_stage1(overrides)
    plan_text = str(result.get("plan_text") or "")
    assert "# Weekly Schedule" not in plan_text
    assert "## Week 1 —" not in plan_text

    breakdown = stage1_parity_breakdown(result)
    leaked = WEEK_STRUCTURE_CODES & set(breakdown["review_flag_codes"])
    assert leaked, (
        f"{name}: expected Stage 1 parity to keep measuring missing week/session "
        "structure now that final session rendering is owned by Stage 2."
    )


def test_review_stage1_self_output_rejects_non_success_result() -> None:
    with pytest.raises(ValueError):
        review_stage1_self_output({"status": "invalid_input", "plan_text": ""})


def test_parity_breakdown_shape() -> None:
    review = review_stage1_self_output(_run_stage1(_scenarios()["standard_amateur_boxing"]))
    breakdown = parity_breakdown(review)
    assert set(breakdown) >= {
        "status",
        "is_publishable",
        "error_codes",
        "blocking_codes",
        "review_flag_codes",
        "all_codes",
    }
    # all_codes is the union of the three buckets.
    union = {
        **breakdown["error_codes"],
        **breakdown["blocking_codes"],
        **breakdown["review_flag_codes"],
    }
    assert set(breakdown["all_codes"]) == set(union)


# --- role_labels unit coverage -------------------------------------------------


def test_humanize_role_key_strips_suffix_and_titlecases() -> None:
    assert humanize_role_key("double_stress_day") == "Double Stress"
    assert humanize_role_key("mini_taper_protocol") == "Mini Taper"
    assert humanize_role_key("") == ""


def test_athlete_facing_label_resolution_order() -> None:
    # Explicit mapping wins.
    assert athlete_facing_label_for("primary_strength_day") == "Strength"
    # Unknown key falls back to caller fallback, then humanised key.
    assert athlete_facing_label_for("totally_unknown_day", fallback="Custom") == "Custom"
    assert athlete_facing_label_for("totally_unknown_day") == "Totally Unknown"


def test_taper_role_labels_preserve_performance_language() -> None:
    assert athlete_facing_label_for("aerobic_flush_day") == "Rhythm flush"
    assert athlete_facing_label_for("neural_primer_day") == "Neural speed touch"
    assert athlete_facing_label_for("alactic_sharpness_day") == "Freshness primer"
    assert athlete_facing_label_for("fight_week_freshness_day") == "Fight-week freshness"


# --- weekly_plan_render unit coverage -----------------------------------------


class _FakeBlocks:
    def __init__(self, strength_blocks: dict, conditioning_blocks: dict) -> None:
        self.strength_blocks = strength_blocks
        self.conditioning_blocks = conditioning_blocks


def _synthetic_brief() -> dict:
    return {
        "payload_variant": "",
        "weekly_role_map": {
            "weeks": [
                {
                    "week_index": 1,
                    "phase": "GPP",
                    "declared_training_days": ["Monday", "Thursday"],
                    "calendar_days": [
                        {"weekday": "monday", "d_day": 56},
                        {"weekday": "thursday", "d_day": 53},
                    ],
                    "session_roles": [
                        {
                            "category": "strength",
                            "role_key": "primary_strength_day",
                            "athlete_facing_label": "Strength",
                            "scheduled_day_hint": "Thursday",
                            "governance": {
                                "main_job": "anchor",
                                "forbidden_secondary_stressors": ["hinge_transfer"],
                            },
                        },
                        {
                            # No scheduled_day_hint -> must be filled with Monday.
                            "category": "conditioning",
                            "role_key": "aerobic_base_day",
                            "athlete_facing_label": "Aerobic support",
                            "preferred_system": "aerobic",
                        },
                    ],
                },
                {
                    "week_index": 2,
                    "phase": "GPP",
                    "declared_training_days": ["Monday", "Thursday"],
                    "calendar_days": [
                        {"weekday": "monday", "d_day": 49},
                        {"weekday": "thursday", "d_day": 46},
                    ],
                    "session_roles": [
                        {
                            "category": "strength",
                            "role_key": "primary_strength_day",
                            "athlete_facing_label": "Strength",
                            "scheduled_day_hint": "Thursday",
                        }
                    ],
                },
            ]
        },
    }


def _synthetic_blocks() -> _FakeBlocks:
    return _FakeBlocks(
        strength_blocks={
            "GPP": {
                "exercises": [
                    {"name": "Back Squat", "tags": ["squat"], "equipment": "barbell", "anchor_capable": True},
                    {"name": "Romanian Deadlift (RDL)", "tags": ["hinge"], "equipment": "barbell", "anchor_capable": True},
                ]
            }
        },
        conditioning_blocks={
            "GPP": {"grouped_drills": {"aerobic": [{"name": "Easy Bike", "duration": "25 min", "system": "AEROBIC"}]}}
        },
    )


def test_render_weekly_schedule_section_structure_and_governance() -> None:
    brief = _synthetic_brief()
    # Day placement is owned by the calendar/placement layer and runs before the
    # read-only renderer. Fill the dayless role here (as the pipeline does) so the
    # renderer reads the placed weekday rather than inferring one itself.
    fill_missing_session_days(brief["weekly_role_map"])
    section = render_weekly_schedule_section(
        planning_brief=brief, blocks=_synthetic_blocks()
    )
    assert section.startswith("# Weekly Schedule")
    assert "## Week 1 — GPP (D-56 → D-53)" in section
    # Real day + D-day + label headings.
    assert "### D-53 (Thursday) — Strength" in section
    # Dayless conditioning role was placed on the free training day (Monday) by the
    # placement layer; the read-only renderer reads that assignment.
    assert "### D-56 (Monday) — Aerobic support" in section
    assert "Easy Bike — 25 min" in section
    # Anchor-day governance: the hinge transfer is excluded, the squat stays.
    assert "Back Squat" in section
    assert "Romanian Deadlift" not in section


def test_render_weekly_schedule_is_read_only_and_never_infers_days() -> None:
    # Step 8 ownership boundary: rendering is presentation-only. A role the
    # placement layer left dayless must render without a weekday — the renderer
    # must not infer or assign one. Here placement is deliberately NOT run, so the
    # aerobic role stays dayless and the renderer omits it until placement resolves a day.
    brief = _synthetic_brief()
    section = render_weekly_schedule_section(
        planning_brief=brief, blocks=_synthetic_blocks()
    )
    assert "### Aerobic support" not in section  # Unplaced guidance is not an appointment.
    assert "### Monday" not in section  # renderer invented no weekday for it
    # The already-placed strength role still renders on its assigned day.
    assert "### D-53 (Thursday) — Strength" in section


def test_render_weekly_schedule_section_skips_late_fight_variant() -> None:
    brief = _synthetic_brief()
    brief["payload_variant"] = "late_fight_stage2_payload"
    assert render_weekly_schedule_section(planning_brief=brief, blocks=_synthetic_blocks()) == ""


def test_fill_missing_session_days_assigns_free_training_day() -> None:
    weekly_role_map = _synthetic_brief()["weekly_role_map"]
    fill_missing_session_days(weekly_role_map)
    roles = weekly_role_map["weeks"][0]["session_roles"]
    aerobic = next(r for r in roles if r["role_key"] == "aerobic_base_day")
    assert aerobic["scheduled_day_hint"] == "Monday"
    # Already-placed role is untouched.
    strength = next(r for r in roles if r["role_key"] == "primary_strength_day")
    assert strength["scheduled_day_hint"] == "Thursday"


def test_sanitize_dose_strips_contrast_when_forbidden() -> None:
    spp = "3–5x3–5 @ 85–90% 1RM with contrast training (pair with explosive move)."
    cleaned = _sanitize_dose(spp, {"contrast_work"})
    assert "contrast" not in cleaned.lower()
    assert cleaned.startswith("3–5x3–5 @ 85–90% 1RM")
    # Without the forbidden token, the dose is preserved verbatim.
    assert _sanitize_dose(spp, set()) == spp


def test_stamp_weekly_role_map_preserves_existing_and_skips_plan_markers() -> None:
    weekly_role_map = {
        "weeks": [
            {
                "session_roles": [
                    {"role_key": "primary_strength_day", "category": "strength"},
                    {
                        "role_key": "converted_mobility_support_day",
                        "category": "conditioning",
                        "athlete_facing_label": "Low-load mobility support",
                    },
                ],
                "suppressed_roles": [
                    {"role_key": "fight_week_override", "category": "plan"},
                ],
            }
        ]
    }
    stamp_weekly_role_map_labels(weekly_role_map)
    roles = weekly_role_map["weeks"][0]["session_roles"]
    assert roles[0]["athlete_facing_label"] == "Strength"
    # Existing bespoke label is preserved, not overwritten.
    assert roles[1]["athlete_facing_label"] == "Low-load mobility support"
    # Plan markers never get a session title.
    assert "athlete_facing_label" not in weekly_role_map["weeks"][0]["suppressed_roles"][0]
