"""F08: scheduled physical sessions with no eligible member; F14: low-load
support days carrying high-cost work.

Pipeline tests run the real Stage 1 entry point with a frozen clock and a
fixed seed; they make no model call.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from fightcamp import input_parsing
from fightcamp.calendar_context import role_d_day
from fightcamp.goal_preservation import collect_goal_evidence
from fightcamp.main import generate_plan_sync
from fightcamp.physical_session_frequency import _explained_offsets, is_physical_session_role, role_countdown_offset
from fightcamp.session_composition import (
    EMPTY_PHYSICAL_SESSION_REASON,
    _meets_low_load_support_contract,
    compose_normal_conditioning_assignments,
    suppress_unfilled_physical_roles,
)
from fightcamp.stage2_payload import late_fight_sequence_budget

_NOW = dt.datetime(2026, 1, 4, 12)
_REPO = Path(__file__).resolve().parents[1]
_FULL_GYM = ("barbell", "dumbbells", "kettlebells", "medicine_ball", "heavy_bag", "bodyweight",
             "pull_up_bar", "bands", "bench", "skipping_rope")
_SHOULDER = [{"area": "Right shoulder", "severity": "low", "trend": "improving", "injury_type": "pain",
              "timeframe": "last_month"}]
_SCENARIOS = {
    # Known case: D-15 glycolytic with a declared hard day.
    "boxer21": {},
    "boxer21_freq6": {"frequency": 6},
    "boxer21_bodyweight": {"equipment": ("bodyweight",)},
    "kick10_late": {"days": 10, "sport": "kickboxing", "goals": ("speed",)},
    "bjj35_shoulder_rehab_friendly": {"days": 35, "sport": "bjj", "goals": ("strength",), "target": "68",
                                      "guided": _SHOULDER},
    "bjj35_shoulder_mobility": {"days": 35, "sport": "bjj", "goals": ("strength", "mobility"), "target": "68",
                                "hard": ("Tuesday", "Thursday"), "guided": _SHOULDER},
}
_CACHE: dict[str, dict] = {}


def _plan(key):
    if key not in _CACHE:
        kw = {"days": 21, "sport": "boxing", "goals": ("speed", "conditioning"), "frequency": 4,
              "equipment": _FULL_GYM, "hard": ("Thursday",), "target": "65", "guided": None, **_SCENARIOS[key]}
        fields = {
            "Full name": "Regression", "Age": "24", "Weight (kg)": "68", "Target Weight (kg)": kw["target"],
            "Height (cm)": "175", "Stance": "Orthodox", "Fighting Style (Technical)": kw["sport"],
            "Professional Status": "amateur", "Current Record": "0-0", "Rounds x Minutes": "3x3",
            "Weekly Training Frequency": str(kw["frequency"]), "Fatigue Level": "low",
            "Equipment Access": ", ".join(kw["equipment"]),
            "Training Availability": "Monday, Tuesday, Wednesday, Thursday, Friday, Saturday",
            "Hard Sparring Days": ", ".join(kw["hard"]),
            "What are your key performance goals?": ", ".join(kw["goals"]), "Primary goal": kw["goals"][0],
            "Where do you feel weakest right now?": "trunk_strength", "Primary weak area": "trunk_strength",
            "When is your next fight?": (_NOW.date() + dt.timedelta(days=kw["days"])).isoformat(),
            "Athlete Time Zone": "UTC",
        }
        data = {"data": {"fields": [{"label": k, "value": v} for k, v in fields.items()]}, "random_seed": 7}
        if kw["guided"]:
            data["guided_injuries"] = kw["guided"]
        mp = pytest.MonkeyPatch()
        mp.setattr(input_parsing, "_utc_now", lambda: _NOW)
        try:
            _CACHE[key] = generate_plan_sync(data)
        finally:
            mp.undo()
    return _CACHE[key]["planning_brief"]


def _surfaces(brief):
    """(week, roles, suppressed) on the brief's authoritative calendar."""
    if "late_fight_session_sequence" in brief:
        return [({}, brief["late_fight_session_sequence"], brief["late_fight_plan_spec"].get("suppressed_roles") or [])]
    return [(w, w["session_roles"], w.get("suppressed_roles") or []) for w in brief["weekly_role_map"]["weeks"]]


def _empty_suppressions(brief):
    return [s for _, _, rows in _surfaces(brief) for s in rows if s.get("reason_code") == EMPTY_PHYSICAL_SESSION_REASON]


@pytest.mark.parametrize("key", sorted(_SCENARIOS))
def test_no_completed_plan_ships_an_empty_strength_or_conditioning_session(key):
    brief = _plan(key)
    empty = [(r["role_key"], role_d_day(w, r)) for w, roles, _ in _surfaces(brief) for r in roles
             if r.get("category") in {"strength", "conditioning"} and not r.get("selected_exercise_assignments")]
    assert empty == []


def test_known_d15_glycolytic_session_is_a_recorded_suppression():
    rows = _empty_suppressions(_plan("boxer21"))
    assert [(s["role_key"], s["preferred_system"], s["original_d_day"]) for s in rows] == [
        ("fight_pace_repeatability_day", "glycolytic", 15)
    ]
    assert rows[0]["reason"] and rows[0]["authority"] == "session_composition"


def test_late_fight_plan_suppresses_only_on_its_authoritative_sequence():
    brief = _plan("kick10_late")
    assert all(s["category"] in {"strength", "conditioning"} for s in _empty_suppressions(brief))
    # The stale weekly mirror is not rewritten into false "no eligible" claims.
    assert not any(s.get("reason_code") == EMPTY_PHYSICAL_SESSION_REASON
                   for w in brief["weekly_role_map"]["weeks"] for s in w.get("suppressed_roles") or [])


@pytest.mark.parametrize("key", sorted(_SCENARIOS))
def test_suppressed_sessions_carry_no_goal_evidence(key):
    brief = _plan(key)
    removed = {(s["role_key"], s["original_d_day"]) for s in _empty_suppressions(brief)}
    assert not {(e.get("role_key"), e.get("d_day")) for e in collect_goal_evidence(brief)} & removed


def test_recovery_and_zero_load_support_roles_are_untouched():
    brief = _plan("boxer21")
    keys = {r["role_key"] for _, roles, _ in _surfaces(brief) for r in roles}
    assert {"fight_week_freshness_day", "tactical_watch", "fight_visualization"} <= keys
    assert all(s["category"] in {"strength", "conditioning"} for s in _empty_suppressions(brief))


def test_no_new_sessions_are_added():
    brief = _plan("boxer21_freq6")
    for week, roles, _ in _surfaces(brief):
        declared = {d.lower() for d in week.get("declared_training_days") or []}
        assert all(str(r.get("scheduled_day_hint") or "").lower() in declared | {""} for r in roles
                   if r.get("category") in {"strength", "conditioning"})


# ---------------------------------------------------------------------------
# F14 - low-load support days
# ---------------------------------------------------------------------------

def _bank(name):
    rows = json.loads((_REPO / "data/conditioning_bank.json").read_text())
    return next(row for row in rows if row["name"] == name)


_LOW_LOAD_ROLE = {
    "governance": {"authority": "unused_day_low_load_support_upgrade"},
    "blocked_systems": ["glycolytic", "ATP-PCr"],
    "blocked_intensities": ["high", "max"],
    "blocked_tags": ["mech_cns_high", "high_cns", "sprint", "plyometric", "high_impact_lower", "mech_landing_impact"],
}


def test_the_kettlebell_swing_emom_breaks_the_low_load_contract():
    assert not _meets_low_load_support_contract(_LOW_LOAD_ROLE, _bank("Kettlebell Swings (EMOM Aerobic)"))


def test_zone_two_shadowboxing_meets_the_low_load_contract():
    assert _meets_low_load_support_contract(_LOW_LOAD_ROLE, _bank("Tempo Shadowboxing"))


@pytest.mark.parametrize("key", ["bjj35_shoulder_rehab_friendly", "bjj35_shoulder_mobility"])
def test_low_load_support_days_hold_only_low_load_work_or_are_suppressed(key):
    brief = _plan(key)
    support = [(w, r) for w, roles, _ in _surfaces(brief) for r in roles
               if (r.get("governance") or {}).get("authority") == "unused_day_low_load_support_upgrade"]
    assert support, "scenario must exercise a converted low-load day"
    for _, role in support:
        names = [m["name"] for m in role["selected_exercise_assignments"]]
        assert names and "Kettlebell Swings (EMOM Aerobic)" not in names
        for name in names:
            assert _meets_low_load_support_contract(role, _bank(name))
    # #2984/#3108 changed eligible content and resolved-contact calendars, so
    # a real converted day need not be empty. Keep the suppression check by
    # explicitly emptying one such day; keep all real low-load checks above.
    empty = copy.deepcopy(brief)
    week, role = next((w, r) for w, roles, _ in _surfaces(empty) for r in roles
                     if (r.get("governance") or {}).get("authority") == "unused_day_low_load_support_upgrade")
    role_key, d_day = role["role_key"], role_d_day(week, role)
    role["selected_exercise_assignments"] = []
    suppress_unfilled_physical_roles(empty)
    assert any(s["role_key"] == role_key and s["original_d_day"] == d_day
               for s in _empty_suppressions(empty))


def _low_load_week(alternate_authority):
    role = {"role_key": "converted_rehab_friendly_support_day", "category": "conditioning",
            "preferred_system": "aerobic", "scheduled_day_hint": "Monday", **_LOW_LOAD_ROLE}
    pools = {"SPP": {"conditioning_slots": [{
        "slot_id": "spp_aerobic_1", "role": "aerobic",
        "selected": {**_bank("Kettlebell Swings (EMOM Aerobic)"), "fulfillment_authority": True},
        "alternates": [{**_bank("Tempo Shadowboxing"), "fulfillment_authority": alternate_authority}],
    }]}}
    week = {"week_index": 1, "phase": "SPP", "calendar_days": [{"weekday": "monday", "d_day": 25}],
            "session_roles": [role]}
    compose_normal_conditioning_assignments(weekly_role_map={"weeks": [week]}, candidate_pools=pools)
    return [m["name"] for m in role["selected_exercise_assignments"]]


def test_a_low_load_alternate_without_fulfilment_authority_is_never_scheduled():
    assert _low_load_week(alternate_authority=False) == []


def test_a_low_load_alternate_with_fulfilment_authority_replaces_the_failing_drill():
    assert _low_load_week(alternate_authority=True) == ["Tempo Shadowboxing"]


def test_dedicated_rehab_roles_are_not_rewritten(monkeypatch):
    from fightcamp.rehab_clinical import load_clinical_policies
    policy = next(p for p in load_clinical_policies() if p.policy_id == "ankle_sprain")
    assert policy.activation == "live"
    # #2984: use a specific published injury decision, not legacy drills for
    # generic shoulder pain. This is the server-attached live episode context.
    owned = {"id": "ankle-1", "episode_id": "episode-1", "athlete_id": "athlete-1",
             "side": "left", "injury_type": "sprain", "canonical_location": "ankle",
             "body_region": "ankle", "severity": "moderate", "status": "monitoring",
             "latest_reported_status": "improving", "rehab_stage": "restore"}
    context = {"rehab_stage": "restore", "injury_id": owned["id"], "episode_id": owned["episode_id"],
               "athlete_id": owned["athlete_id"], "available_equipment": ["stable_support"],
               "policy_injury": owned}
    monkeypatch.setitem(_SCENARIOS, "published_ankle", {
        "days": 35, "sport": "bjj", "goals": ("strength",), "target": "68",
        "equipment": _FULL_GYM + ("stable_support",),
        "guided": [{"area": "Left ankle", "severity": "moderate", "trend": "improving",
                    "injury_type": "sprain", "timeframe": "last_month", "rehab_generation_context": context}],
    })
    brief = _plan("published_ankle")
    published_ids = {rx.drill_id for rx in policy.prescriptions}
    # #2984 publishes candidates; a generic mobility support insert sharing
    # this role key is not a dedicated clinical appointment. Exercise the
    # suppression contract with an explicit, populated clinical role.
    for phase in ("GPP", "SPP", "TAPER"):
        slots = brief["candidate_pools"][phase]["rehab_slots"]
        assert slots
        assignments = [copy.deepcopy(slot["selected"]) for slot in slots]
        assert all(a["rehab_drill_id"] in published_ids and a["prescription"] for a in assignments)
        role = {"role_key": "mobility_rehab", "category": "rehab", "preferred_pool": "rehab_slots",
                "selected_exercise_assignments": assignments}
        dedicated = {"weekly_role_map": {"weeks": [{**_week(role), "phase": phase}]}}
        before = copy.deepcopy(dedicated)
        suppress_unfilled_physical_roles(dedicated)
        assert dedicated == before


@pytest.mark.parametrize("key", sorted(_SCENARIOS))
def test_every_day_emptied_by_suppression_is_explained(key):
    brief = _plan(key)
    weeks = brief["weekly_role_map"]["weeks"]
    explained = {offset for w in weeks for offset in _explained_offsets(w)}
    occupied = {role_countdown_offset(r) for _, roles, _ in _surfaces(brief) for r in roles if is_physical_session_role(r)}
    for row in _empty_suppressions(brief):
        assert row["original_d_day"] in occupied | explained


def test_late_fight_counts_and_caps_match_the_sequence_after_suppression():
    # This kickboxing countdown used to lose a primer to empty membership on its
    # own; the late selector now fills it, so the removal is created explicitly
    # and run through the real suppression pass.
    brief = copy.deepcopy(_plan("kick10_late"))
    target = next(r for r in brief["late_fight_session_sequence"]
                  if r.get("category") in {"strength", "conditioning"} and r.get("selected_exercise_assignments"))
    target["selected_exercise_assignments"] = []
    suppress_unfilled_physical_roles(brief)
    spec = brief["late_fight_plan_spec"]
    assert _empty_suppressions(brief), "scenario must remove a late-fight session"
    expected = late_fight_sequence_budget(brief["late_fight_session_sequence"], spec["role_budget"])
    assert {key: spec[key] for key in expected} == expected
    suppressed = {s["role_key"] for s in _empty_suppressions(brief)}
    remaining = {r["role_key"] for r in brief["late_fight_session_sequence"]}
    assert not set(spec["visible_session_roles"]) & (suppressed - remaining)


# ---------------------------------------------------------------------------
# Suppression pass on hand-built briefs
# ---------------------------------------------------------------------------

def _week(*roles):
    return {"week_index": 1, "calendar_days": [{"weekday": "monday", "d_day": 20}], "session_roles": list(roles)}


def test_pass_removes_empty_strength_and_conditioning_and_keeps_everything_else():
    keep = [
        {"role_key": "primary_strength_day", "category": "strength", "selected_exercise_assignments": [{"name": "Squat"}]},
        {"role_key": "fight_week_freshness_day", "category": "recovery"},
        {"role_key": "hard_sparring_day", "category": "sparring"},
        {"role_key": "tactical_watch", "category": "support_insert"},
    ]
    drop = [
        {"role_key": "strength_touch_day", "category": "strength", "selected_exercise_assignments": []},
        {"role_key": "fight_pace_repeatability_day", "category": "conditioning", "preferred_system": "glycolytic",
         "scheduled_day_hint": "Monday"},
    ]
    brief = {"weekly_role_map": {"weeks": [_week(*keep, *drop)]}}
    suppress_unfilled_physical_roles(brief)
    week = brief["weekly_role_map"]["weeks"][0]
    assert week["session_roles"] == keep
    assert [(s["role_key"], s["reason_code"], s["original_d_day"]) for s in week["suppressed_roles"]] == [
        ("strength_touch_day", EMPTY_PHYSICAL_SESSION_REASON, None),
        ("fight_pace_repeatability_day", EMPTY_PHYSICAL_SESSION_REASON, 20),
    ]


def test_pass_acts_on_the_late_fight_sequence_and_reconciles_its_counts():
    mirror = {"weeks": [{**_week({"role_key": "neural_primer_day", "category": "strength"}),
                         "countdown_span": {"start_day": 13, "end_day": 1}}]}
    sequence = [{"role_key": "neural_primer_day", "category": "strength", "scheduled_countdown_label": "D-7",
                 "countdown_offset": 7, "scheduled_day_hint": "Friday", "stress_class": "meaningful_stress"},
                {"role_key": "strength_touch_day", "category": "strength", "scheduled_countdown_label": "D-12",
                 "countdown_offset": 12, "stress_class": "meaningful_stress",
                 "selected_exercise_assignments": [{"name": "Band-Resisted Punch"}]}]
    spec = {"role_budget": {"max_meaningful_stress_exposures": 2, "selected_active_roles": 2,
                            "selected_meaningful_stress_exposures": 2},
            "visible_session_cap": 2, "max_active_roles": 2,
            "visible_session_roles": ["neural_primer_day", "strength_touch_day"]}
    brief = {"weekly_role_map": mirror, "late_fight_session_sequence": sequence, "late_fight_plan_spec": spec}
    suppress_unfilled_physical_roles(brief)
    assert [r["role_key"] for r in sequence] == ["strength_touch_day"]
    assert [s["role_key"] for s in spec["suppressed_roles"]] == ["neural_primer_day"]
    assert (spec["visible_session_cap"], spec["max_active_roles"], spec["visible_session_roles"]) == (
        1, 1, ["strength_touch_day"])
    assert spec["role_budget"]["selected_meaningful_stress_exposures"] == 1
    assert spec["role_budget"]["max_meaningful_stress_exposures"] == 2
    # The emptied D-7 is explained; the stale mirror's roles are not rewritten.
    assert [(e["countdown_offset"], e["reason_code"]) for e in mirror["weeks"][0]["intentionally_unused_days"]] == [
        (7, EMPTY_PHYSICAL_SESSION_REASON)]
    assert len(mirror["weeks"][0]["session_roles"]) == 1
