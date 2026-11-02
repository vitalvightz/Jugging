"""Characterization gate for the final-calendar integrity refactor (Stage 3A).

Purpose
-------
This module is a *pre-Stage-3B behaviour characterization gate*. It proves that
the current deterministic planner still produces the **known pre-governor
semantic calendar** for a set of representative fixtures, by comparing the live
projection against **explicit frozen baselines** captured from Main / PR #2396's
base (the pre-Stage-3B planner).

It deliberately does more than prove determinism. Comparing two runs of the same
code (``first == second``) only shows the code is stable; it cannot catch Stage
3B accidentally moving, restoring, or removing an *unrelated* session. Freezing
an explicit expected baseline can:

    actual = _semantic_projection(_run(...))
    assert actual == EXPECTED_BASELINE   # loaded from tests/fixtures/...

so any semantic drift Stage 3B introduces shows up as an exact diff against the
committed baseline.

What is frozen (semantic projection only)
-----------------------------------------
Only the semantic planner state Stage 3B must preserve unless a shared
calendar-policy violation explicitly requires a change:

* role rows      -> (week_index, phase, role_key, category, weekday, d_day,
                     late_fight_tail_owned, late_camp_role_morph,
                     late_camp_strength_morph, stress_class, cost_class)
* contact rows   -> (week_index, weekday, status, effective_load)
* suppression rows -> (week_index, role_key, category, replacement_role_key,
                       compression_reason_codes)
* generator_mode, payload_variant, and the D-14/D-13 tail handoff metadata

What is intentionally *not* frozen: exercise names, athlete-facing copy, HTML,
render formatting, LLM wording, or other presentation/irrelevant metadata.

Scope
-----
Stage 3A is tests-only. This module does not import or exercise any production
change and, importantly, does **not** mutate process-global state. It notably
does NOT globally disable logging at import time — an earlier version did, which
silenced every log-assertion test in the whole pytest process (for example
``tests/test_logging_utils.py``) and made unrelated tests order-dependent.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pytest

from planner_clock import pin_planner_clock
from fightcamp.input_parsing import PlanInput
from fightcamp.plan_pipeline_blocks import generate_plan_blocks
from fightcamp.plan_pipeline_rendering import build_stage2_outputs
from fightcamp.plan_pipeline_runtime import (
    RenderedPlanBundle,
    build_runtime_context,
)

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "calendar_integrity"
_LOGGER = logging.getLogger("calendar-integrity-characterization")

FIGHT_DATE = dt.date(2026, 1, 30)  # Friday
_EQUIPMENT = (
    "bands, partner, kettlebells, dumbbells, cable, barbell, pullup_bar, "
    "heavy_bag, neck_harness, plate, towel, weight_belt, box, trap_bar, "
    "landmine, foam_roller, assault_bike, weight_vest, rower, pool, hurdles"
)


def _fields(**overrides) -> list[dict]:
    return [
        {"label": "Full name", "value": "Calendar Characterization"},
        {"label": "Age", "value": "30"},
        {"label": "Weight (kg)", "value": overrides.get("weight", "88")},
        {"label": "Target Weight (kg)", "value": overrides.get("target_weight", "85")},
        {"label": "Height (cm)", "value": "185"},
        {"label": "Fighting Style (Technical)", "value": "boxing"},
        {"label": "Fighting Style (Tactical)", "value": "counter_striker"},
        {"label": "Stance", "value": "Orthodox"},
        {"label": "Professional Status", "value": "professional"},
        {"label": "Current Record", "value": "9-2"},
        {"label": "Athlete Time Zone", "value": "Europe/London"},
        {"label": "Rounds x Minutes", "value": "12x3"},
        {"label": "Weekly Training Frequency", "value": overrides.get("frequency", "4")},
        {"label": "Fatigue Level", "value": overrides.get("fatigue", "low")},
        {"label": "Equipment Access", "value": _EQUIPMENT},
        {
            "label": "Training Availability",
            "value": overrides.get(
                "availability",
                "Monday, Tuesday, Wednesday, Thursday, Friday, Sunday",
            ),
        },
        {"label": "Hard Sparring Days", "value": overrides.get("hard_sparring", "")},
        {"label": "Support Work Days", "value": overrides.get("support", "Monday")},
        {"label": "What are your key performance goals?", "value": overrides.get("key_goals", "power, speed")},
        {"label": "Primary goal", "value": overrides.get("primary_goal", "power")},
        {"label": "Where do you feel weakest right now?", "value": overrides.get("weak", "coordination, speed")},
        {"label": "Primary weak area", "value": overrides.get("primary_weak", "coordination")},
        {"label": "When is your next fight?", "value": FIGHT_DATE.strftime("%Y-%m-%d")},
        {"label": "Any injuries or areas you need to work around?", "value": overrides.get("injuries", "")},
    ]


def _run(days: int, monkeypatch: pytest.MonkeyPatch, **overrides) -> dict:
    """Run the real deterministic planner for a fixed ``days``-out fight.

    Both clocks the planner reads are patched (function-scoped monkeypatch,
    auto-restored) so the countdown *and* the plan-creation weekday are fully
    deterministic; no process-global state is mutated. Patching only
    ``input_parsing._utc_now`` leaves the weekday on the real clock.
    """
    fixed_now = dt.datetime.combine(
        FIGHT_DATE - dt.timedelta(days=days),
        dt.time(12, 0),
    )
    pin_planner_clock(monkeypatch, fixed_now)
    plan_input = PlanInput.from_payload({"data": {"fields": _fields(**overrides)}})
    assert plan_input.days_until_fight == days
    context = build_runtime_context(
        plan_input=plan_input,
        random_seed=1,
        logger=_LOGGER,
    )
    blocks = generate_plan_blocks(
        context=context,
        logger=_LOGGER,
        record_timing=lambda *args, **kwargs: None,
    )
    rendered = RenderedPlanBundle(
        fight_plan_text="",
        coach_notes="",
        reason_log={},
        html="",
    )
    _payload, brief, _handoff = build_stage2_outputs(
        context=context,
        blocks=blocks,
        rendered=rendered,
    )
    return brief


def _role_d_day(week: dict, role: dict) -> int | None:
    weekday = str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").strip().lower()
    for day in week.get("calendar_days", []) or []:
        if not isinstance(day, dict):
            continue
        if str(day.get("weekday") or "").strip().lower() == weekday and isinstance(day.get("d_day"), int):
            return day["d_day"]
    for key in ("scheduled_countdown_label", "countdown_label"):
        label = str(role.get(key) or "").strip().upper()
        if label.startswith("D-") and label[2:].isdigit():
            return int(label[2:])
    return None


def _semantic_projection(brief: dict) -> dict:
    """Project the brief down to the semantic calendar state Stage 3B must keep.

    Exercise names, athlete-facing copy, HTML, and render formatting are all
    dropped on purpose (see module docstring). Rows are sorted so the projection
    is stable regardless of internal ordering.
    """
    role_rows: list[tuple] = []
    contact_rows: list[tuple] = []
    suppression_rows: list[tuple] = []
    weekly_role_map = brief.get("weekly_role_map") or {}
    for week in weekly_role_map.get("weeks", []) or []:
        if not isinstance(week, dict):
            continue
        week_index = int(week.get("week_index") or 0)
        for role in week.get("session_roles", []) or []:
            if not isinstance(role, dict):
                continue
            role_rows.append(
                (
                    week_index,
                    str(role.get("phase") or week.get("phase") or ""),
                    str(role.get("role_key") or ""),
                    str(role.get("category") or ""),
                    str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").lower(),
                    _role_d_day(week, role),
                    bool(role.get("late_fight_tail_owned")),
                    bool(role.get("late_camp_role_morph")),
                    bool(role.get("late_camp_strength_morph")),
                    str(role.get("stress_class") or ""),
                    str(role.get("cost_class") or ""),
                )
            )
        for entry in week.get("hard_sparring_plan", []) or []:
            if not isinstance(entry, dict):
                continue
            contact_rows.append(
                (
                    week_index,
                    str(entry.get("day") or "").lower(),
                    str(entry.get("status") or ""),
                    str(entry.get("effective_load") or ""),
                )
            )
        for entry in week.get("suppressed_roles", []) or []:
            if isinstance(entry, dict):
                suppression_rows.append(
                    (
                        week_index,
                        str(entry.get("role_key") or entry.get("role") or ""),
                        str(entry.get("category") or ""),
                        str(entry.get("replacement_role_key") or ""),
                        tuple(sorted(str(c) for c in (entry.get("compression_reason_codes") or []))),
                    )
                )
    return {
        "generator_mode": brief.get("generator_mode"),
        "payload_variant": brief.get("payload_variant"),
        "roles": sorted(role_rows),
        "contacts": sorted(contact_rows),
        "suppressed": sorted(suppression_rows),
        "tail_handoff": weekly_role_map.get("late_fight_tail_handoff"),
    }


def _normalize(projection: dict) -> dict:
    """Round-trip through JSON so tuples become lists, matching the fixtures."""
    result = json.loads(json.dumps(projection))
    # Retired mental cadence has its own governor tests. Keep this historical
    # snapshot strict for every physical row, contact and suppression field.
    mental = {"fight_visualization", "neural_visualization", "tactical_watch", "tactical_cue_card", "self_review"}
    result["roles"] = [row for row in result["roles"] if row[2] not in mental]
    return result


def _load_baseline(name: str) -> dict:
    return _normalize(json.loads((_FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# Representative scenarios. Each planner run is executed once (module scope)   #
# and shared by the baseline tests and the architecture-invariant tests.      #
# --------------------------------------------------------------------------- #
_SCENARIOS: dict[str, tuple[int, dict]] = {
    # A. Clean D-24 normal camp, no declared hard contact.
    "d24_no_contact": (24, {"hard_sparring": ""}),
    # B. D-24, one resolved hard-contact day.
    "d24_one_hard": (24, {"hard_sparring": "Thursday"}),
    # C. D-24, two declared hard-contact days.
    "d24_two_hard": (24, {"hard_sparring": "Tuesday, Friday"}),
    # D. D-16, declared contact converted away from effective hard.
    "d16_technical_conversion": (16, {"hard_sparring": "Tuesday, Friday"}),
    # E. D-14 boundary (last day the normal planner owns).
    "d14_boundary": (14, {"hard_sparring": "Thursday"}),
    # F. D-13 direct late-fight plan.
    "d13_direct": (13, {"hard_sparring": "Thursday"}),
    # G. D-28 long camp with a finished D-13 -> D-1 tail.
    "d28_finished_tail": (28, {"hard_sparring": "Thursday"}),
    # H. High-fatigue / compressed week.
    "d24_high_fatigue": (24, {"hard_sparring": "Tuesday, Friday", "fatigue": "high"}),
}


@pytest.fixture(scope="module")
def briefs() -> dict[str, dict]:
    """Run every scenario once and cache the resulting planning briefs.

    Uses a self-contained MonkeyPatch context (not the function-scoped fixture)
    so this module-scoped fixture leaves no global state behind.
    """
    out: dict[str, dict] = {}
    with pytest.MonkeyPatch.context() as mp:
        for name, (days, overrides) in _SCENARIOS.items():
            out[name] = _run(days, mp, **overrides)
    return out


def _all_roles(brief: dict):
    for week in (brief.get("weekly_role_map") or {}).get("weeks", []) or []:
        for role in week.get("session_roles", []) or []:
            if isinstance(role, dict):
                yield week, role


def _all_contact_entries(brief: dict):
    for week in (brief.get("weekly_role_map") or {}).get("weeks", []) or []:
        for entry in week.get("hard_sparring_plan", []) or []:
            if isinstance(entry, dict):
                yield week, entry


# --------------------------------------------------------------------------- #
# 1. Exact baseline preservation                                              #
#    assert _semantic_projection(...) == EXPECTED_BASELINE                     #
# --------------------------------------------------------------------------- #

def test_baseline_d24_no_contact(briefs):
    """A. Clean D-24 normal camp: pre-governor role/day/D-day structure."""
    actual = _normalize(_semantic_projection(briefs["d24_no_contact"]))
    assert actual == _load_baseline("d24_no_contact")


def test_baseline_d24_one_resolved_hard_contact(briefs):
    """B. D-24, one resolved hard-contact day (Thursday)."""
    actual = _normalize(_semantic_projection(briefs["d24_one_hard"]))
    assert actual == _load_baseline("d24_one_hard")


def test_baseline_d24_two_hard_contacts(briefs):
    """C. D-24, two declared hard-contact days (Tuesday, Friday).

    This fixture intentionally captures the pre-governor baseline for two
    declared hard-contact days, defects included. It is NOT asserted to be
    desirable behaviour: the current planner leaves week-1 Friday as effective
    ``hard`` while converting the other declared days to ``technical``,
    compresses week-1 strength away, and even splices a duplicate week-2 Friday
    ``hard_sparring_day`` from the finished tail.

    Stage 3B's calendar-integrity governor is expected to change ONLY the
    policy-forbidden placements while preserving the rest of this semantic
    projection. The baseline exists so that whatever Stage 3B changes shows up
    as an exact, reviewable diff against this file — nothing more, nothing less.
    """
    actual = _normalize(_semantic_projection(briefs["d24_two_hard"]))
    assert actual == _load_baseline("d24_two_hard")


def test_baseline_d16_technical_conversion(briefs):
    """D. D-16, declared contact converted away from effective hard load."""
    actual = _normalize(_semantic_projection(briefs["d16_technical_conversion"]))
    assert actual == _load_baseline("d16_technical_conversion")


def test_baseline_d14_boundary(briefs):
    """E. D-14 boundary: normal planner still owns the calendar at D-14."""
    actual = _normalize(_semantic_projection(briefs["d14_boundary"]))
    assert actual == _load_baseline("d14_boundary")


def test_baseline_d13_direct(briefs):
    """F. D-13 direct plan: still uses the finished late-fight path."""
    actual = _normalize(_semantic_projection(briefs["d13_direct"]))
    assert actual == _load_baseline("d13_direct")


def test_baseline_d28_finished_tail(briefs):
    """G. D-28 long camp: finished D-13 -> D-1 tail remains immutable state."""
    actual = _normalize(_semantic_projection(briefs["d28_finished_tail"]))
    assert actual == _load_baseline("d28_finished_tail")


def test_baseline_d24_high_fatigue_suppression(briefs):
    """H. High-fatigue compressed week: suppression semantics are frozen."""
    actual = _normalize(_semantic_projection(briefs["d24_high_fatigue"]))
    assert actual == _load_baseline("d24_high_fatigue")


# --------------------------------------------------------------------------- #
# 2. Architecture invariants                                                  #
#    These are broad structural guarantees that must hold regardless of the   #
#    exact frozen values. They are kept alongside — not instead of — the      #
#    explicit baselines above.                                                #
# --------------------------------------------------------------------------- #

def test_invariant_no_contact_fixture_has_no_resolved_contact(briefs):
    """A no-declared-contact camp must not invent a resolved contact day."""
    assert list(_all_contact_entries(briefs["d24_no_contact"])) == []


def test_invariant_one_declared_hard_contact_resolves_to_hard(briefs):
    """A single declared hard day resolves to an effective-hard contact."""
    entries = [entry for _week, entry in _all_contact_entries(briefs["d24_one_hard"])]
    assert entries
    assert any(
        str(entry.get("status") or "") == "hard_as_planned"
        or str(entry.get("effective_load") or "") == "hard"
        for entry in entries
    )


def test_invariant_two_declared_hard_contacts_own_two_contact_days(briefs):
    """Both declared contact weekdays keep calendar ownership at D-24."""
    weekdays = {
        str(entry.get("day") or "").strip().lower()
        for _week, entry in _all_contact_entries(briefs["d24_two_hard"])
    }
    assert {"tuesday", "friday"} <= weekdays


def test_invariant_d16_has_no_effective_hard_contact(briefs):
    """Declared contact != effective hard load at D-16: nothing stays hard."""
    entries = [
        entry
        for _week, entry in _all_contact_entries(briefs["d16_technical_conversion"])
    ]
    assert entries
    assert not any(
        str(entry.get("status") or "") == "hard_as_planned"
        or str(entry.get("effective_load") or "") == "hard"
        for entry in entries
    )


def test_invariant_no_normal_role_on_unavailable_saturday(briefs):
    """Saturday is unavailable: no normal app role may be scheduled there."""
    for name, brief in briefs.items():
        for _week, role in _all_roles(brief):
            # The Fight Visualisation countdown protocol is zero-load and owns its
            # D-day unconditionally, so it is not subject to day availability.
            if role.get("role_key") == "fight_visualization":
                continue
            assert str(role.get("scheduled_day_hint") or "").strip().lower() != "saturday", (
                name,
                role.get("role_key"),
                role.get("scheduled_countdown_label") or role.get("countdown_display_label"),
            )


def test_invariant_d14_d13_planner_boundary(briefs):
    """Normal planner owns D-14; the D-13 direct path switches architecture."""
    d14 = briefs["d14_boundary"]
    d13 = briefs["d13_direct"]
    assert d14.get("generator_mode") == "deterministic_planner_plus_ai_finalizer"
    assert d14.get("payload_variant") is None
    assert d13.get("payload_variant") == "late_fight_stage2_payload"


def test_invariant_d13_direct_uses_late_fight_payload(briefs):
    """The D-13 direct path must use the finished late-fight Stage 2 payload."""
    assert briefs["d13_direct"].get("payload_variant") == "late_fight_stage2_payload"


def test_invariant_long_camp_tail_owned_roles_confined_to_late_camp(briefs):
    """Finished-tail-owned roles must be placed and confined to the final weeks.

    Note the pre-governor reality this characterizes: the ``late_fight_tail_owned``
    flag currently spans the whole late-camp transition, not a clean D-13..D-1
    window (in these fixtures it reaches D-15/D-14 at the top and D-0 at the
    bottom). The exact D-days are pinned by the frozen baselines; this invariant
    only guards the structural claim that the finished tail stays in the final
    two calendar weeks and never leaks back into the early GPP weeks.
    """
    for name in ("d24_one_hard", "d28_finished_tail"):
        brief = briefs[name]
        weeks = (brief.get("weekly_role_map") or {}).get("weeks", []) or []
        max_week = max((int(w.get("week_index") or 0) for w in weeks), default=0)
        owned = [
            (week, role)
            for week, role in _all_roles(brief)
            if role.get("late_fight_tail_owned")
        ]
        assert owned, f"{name}: expected finished-tail-owned roles"
        for week, role in owned:
            assert _role_d_day(week, role) is not None, f"{name}: tail role missing d_day"
            assert int(week.get("week_index") or 0) >= max_week - 1


def test_invariant_long_camp_hands_normal_planner_through_d14(briefs):
    """The long-camp tail handoff records the normal planner owning through D-14."""
    handoff = (briefs["d28_finished_tail"].get("weekly_role_map") or {}).get(
        "late_fight_tail_handoff"
    )
    assert handoff == {
        "active": True,
        "normal_planner_through_d": 14,
        "late_fight_planner_from_d": 13,
        "source": "finished_existing_late_fight_path",
    }


def test_invariant_high_fatigue_week_suppresses_work(briefs):
    """A high-fatigue camp must carry suppression state (work intentionally removed).

    Freezing this protects against Stage 3B accidentally *restoring* sessions the
    upstream planner deliberately dropped.
    """
    projection = _semantic_projection(briefs["d24_high_fatigue"])
    assert projection["suppressed"], "expected suppressed roles under high fatigue"


# --------------------------------------------------------------------------- #
# 3. Contamination guard                                                      #
# --------------------------------------------------------------------------- #

def test_characterization_module_does_not_disable_logging_globally() -> None:
    """Regression guard for the Stage 3A fix.

    An earlier version of this module globally disabled logging at import time
    (a module-level ``logging`` ``disable`` call), which silenced every
    log-assertion test in the pytest process (for example
    ``tests/test_logging_utils.py``) and made unrelated tests order-dependent.
    The needle is assembled at runtime so this guard does not match itself.
    """
    needle = "logging." + "disable("
    source = Path(__file__).read_text(encoding="utf-8")
    assert needle not in source
