"""PR5: goal and weakness delivery measured on the finished programme.

F01 anatomical involvement is not adaptation; F02/F03 trunk work must reach the
final schedule and be checked there; F04 heavy implements are not low-load
support. Pipeline tests run the real Stage 1 entry point with a frozen clock and
a fixed seed; they make no model call. The two saved generations are replayed
from the exact intakes stored for them.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json

import pytest

from api.models import PlanRequest
from fightcamp.calendar_context import role_d_day
from fightcamp.goal_preservation import _sets_reps, collect_goal_evidence, dose_volume, validate_goal_preservation
from fightcamp.main import generate_plan_sync
from fightcamp.prescription_resolver import _pre_hard_verified_low_cost
from fightcamp.session_composition import _candidate_records, _select_bounded_records, slot_adaptations
from fightcamp.strength import get_exercise_bank, score_exercise, _classify_prescription_type
from fightcamp.strength_session_quality import classify_strength_item, strength_adaptation_relevance
from fightcamp.tag_maps import (
    ADAPTATION_PRIORITY_KEYS,
    ANATOMICAL_INVOLVEMENT_TAGS,
    GOAL_PRIORITY_TAG_MAP,
    WEAKNESS_PRIORITY_TAG_MAP,
)
from fightcamp.priority_profile import build_priority_profile
from planner_clock import pin_planner_clock

_GUIDED_BLISTER = {
    "area": "Core", "zone": "core", "avoid": "", "notes": "[training_impact:not_limiting]", "trend": "stable",
    "cleared": "", "severity": "low", "timeframe": "", "open_wound": "yes", "injury_type": "surface_injury",
    "surface_type": "blister", "impact_related": "", "sensitive_area": "", "bleeding_status": "",
    "infection_signs": [], "injury_subtypes": ["surface_injury:blister"],
}
# The intakes behind the 12 October saved generations (Supabase plans
# 51514874-... "Vito B" and 35af9e70-... "King"), unchanged.
_VITO_B = {
    "notes": "", "athlete": {"age": 23, "sex": "male", "record": "4-2", "stance": "Orthodox", "full_name": "Vito B",
                             "height_cm": 190, "weight_kg": 80, "athlete_locale": "", "tactical_style": ["counter_striker"],
                             "technical_style": ["boxing"], "athlete_timezone": "Europe/London", "target_weight_kg": None,
                             "professional_status": "amateur"},
    "injuries": "Core (low, stable). Type: surface_injury. Surface: blister. Open wound: yes. Notes: [training_impact:not_limiting]",
    "key_goals": ["speed", "conditioning"], "fight_date": "2026-11-08", "weak_areas": ["strength", "speed"],
    "primary_goal": "", "fatigue_level": "low", "guided_injury": _GUIDED_BLISTER, "rounds_format": "5 x 5",
    "guided_injuries": [_GUIDED_BLISTER], "open_camp_weeks": 12,
    "equipment_access": ["trap_bar", "sled", "barbell", "heavy_bag", "bench", "foam_roller", "plate", "bosu_ball",
                         "atlas_stone", "log", "step_mill", "sledgehammer", "pool", "agility_ladder", "jump_rope", "trx",
                         "pullup_bar", "weight_belt", "water_jug", "bulgarian_bag"],
    "primary_weak_area": "strength", "support_work_days": ["Monday"], "hard_sparring_days": ["Thursday"],
    "mindset_challenges": "", "no_scheduled_fight": False, "training_preference": "",
    "training_availability": ["Thursday", "Saturday", "Tuesday", "Wednesday", "Monday", "Friday"],
    "reduced_contact_requested": True, "weekly_training_frequency": 5, "goal_weakness_collision_tags": ["speed"],
    "goal_weakness_collision_detail": "", "goal_weakness_collision_details": [{"tag": "speed", "label": "Speed", "detail": ""}],
}
_KING = {
    "notes": "", "athlete": {"age": 23, "sex": "male", "record": "0-0", "stance": "Orthodox", "full_name": "King",
                             "height_cm": 175, "weight_kg": 68, "athlete_locale": "", "tactical_style": ["pressure_fighter"],
                             "technical_style": ["boxing"], "athlete_timezone": "Europe/London", "target_weight_kg": 65,
                             "professional_status": "amateur"},
    "injuries": "", "key_goals": ["speed", "conditioning"], "fight_date": "2026-11-08", "weak_areas": ["trunk_strength"],
    "primary_goal": "speed", "fatigue_level": "low", "guided_injury": None, "rounds_format": "3 x 3",
    "guided_injuries": [], "open_camp_weeks": 12,
    "equipment_access": ["plate", "box", "treadmill", "weight_vest", "bench", "hurdles", "pads", "bosu_ball", "kettlebells",
                         "partner", "bulgarian_bag", "dumbbells", "barbell", "trap_bar", "assault_bike", "sled", "pool",
                         "heavy_bag", "jump_rope", "medicine_ball", "weight_belt", "sandbag", "bands", "stable_support",
                         "chair", "table", "towel"],
    "primary_weak_area": "trunk_strength", "support_work_days": ["Monday", "Friday"], "hard_sparring_days": ["Thursday"],
    "mindset_challenges": "", "no_scheduled_fight": False, "training_preference": "",
    "training_availability": ["Monday", "Tuesday", "Saturday", "Wednesday", "Thursday", "Friday"],
    "reduced_contact_requested": True, "weekly_training_frequency": 4, "goal_weakness_collision_tags": [],
    "goal_weakness_collision_detail": "", "goal_weakness_collision_details": [],
}
_SAVED_AT = {"vito_b": dt.datetime(2026, 10, 12, 22, 31, 46), "king": dt.datetime(2026, 10, 12, 21, 3, 30)}
_OVERRIDE = {"approved": True, "allowed_modes": ["needs_review", "restricted_rehab_only"]}

_NOW = dt.datetime(2026, 1, 4, 12)
_FULL_GYM = ("barbell", "dumbbells", "kettlebells", "medicine_ball", "heavy_bag", "bodyweight", "pull_up_bar", "bands",
             "bench", "skipping_rope")
_FORMS = {
    "boxer28_strength_fullgym": {},
    "boxer28_pool_bike": {"goals": ("conditioning", "speed"), "weak": ("strength", "gas_tank"),
                          "equipment": _FULL_GYM + ("pool", "assault_bike", "sled")},
    "boxer35_bodyweight_strength": {"days": 35, "equipment": ("bodyweight",), "goals": ("strength", "power")},
    "boxer28_trunk_bodyweight": {"goals": ("speed", "conditioning"), "weak": ("trunk_strength",), "equipment": ("bodyweight",)},
    "boxer28_high_fatigue_cut": {"fatigue": "high", "weight": "72", "target": "66"},
    "boxer28_knee_injury": {"guided": [{"area": "Left knee", "severity": "moderate", "trend": "stable",
                                        "injury_type": "pain", "timeframe": "last_month"}]},
    "boxer10_trunk_short": {"days": 10, "goals": ("speed",), "weak": ("trunk_strength",)},
    "mma42_strength_power": {"days": 42, "sport": "mma", "goals": ("strength", "power"), "weak": ("strength", "gas_tank"),
                             "hard": ("Tuesday", "Thursday"), "frequency": 5,
                             "equipment": _FULL_GYM + ("sled", "sandbag", "yoke")},
    "muaythai28_power_trunk": {"sport": "muay thai", "goals": ("power", "conditioning"), "weak": ("trunk_strength", "balance"),
                               "frequency": 5, "hard": ("Wednesday", "Saturday"), "equipment": _FULL_GYM + ("sled", "pool")},
}
# PR6 exposure scenarios: generated on demand only, never in the ``_ALL`` sweeps.
_EXPOSURE_FORMS = {
    "boxer28_trunk_fullgym": {"goals": ("speed", "conditioning"), "weak": ("trunk_strength",)},
    "boxer14_speed": {"days": 14, "goals": ("speed", "conditioning"), "weak": ("power",)},
    "boxer10_speed": {"days": 10, "goals": ("speed", "conditioning"), "weak": ("power",)},
    "boxer3_speed": {"days": 3, "goals": ("speed", "conditioning"), "weak": ("power",)},
}


def _saved_payload(intake: dict, *, override: bool) -> dict:
    payload = PlanRequest(**copy.deepcopy(intake)).to_payload()
    payload["random_seed"] = 7
    if override:
        payload["_triage_resume_override"] = dict(_OVERRIDE)
    return payload


def _form_payload(key: str) -> dict:
    kw = {"days": 28, "sport": "boxing", "goals": ("strength", "conditioning"), "weak": ("strength",), "frequency": 4,
          "equipment": _FULL_GYM, "hard": ("Thursday",), "weight": "70", "target": "", "fatigue": "low", "guided": None,
          **{**_FORMS, **_EXPOSURE_FORMS}[key]}
    fields = {
        "Full name": "Delivery", "Age": "24", "Weight (kg)": kw["weight"], "Target Weight (kg)": kw["target"],
        "Height (cm)": "175", "Stance": "Orthodox", "Fighting Style (Technical)": kw["sport"],
        "Professional Status": "amateur", "Current Record": "3-1", "Rounds x Minutes": "3x3",
        "Weekly Training Frequency": str(kw["frequency"]), "Fatigue Level": kw["fatigue"],
        "Equipment Access": ", ".join(kw["equipment"]),
        "Training Availability": "Monday, Tuesday, Wednesday, Thursday, Friday, Saturday",
        "Hard Sparring Days": ", ".join(kw["hard"]),
        "What are your key performance goals?": ", ".join(kw["goals"]), "Primary goal": kw["goals"][0],
        "Where do you feel weakest right now?": ", ".join(kw["weak"]), "Primary weak area": kw["weak"][0],
        "When is your next fight?": (_NOW.date() + dt.timedelta(days=kw["days"])).isoformat(),
        "Athlete Time Zone": "UTC",
    }
    data = {"data": {"fields": [{"label": k, "value": v} for k, v in fields.items()]}, "random_seed": 7}
    if kw["guided"]:
        data["guided_injuries"] = kw["guided"]
        data["_triage_resume_override"] = dict(_OVERRIDE)
    return data


def _scenario(key: str) -> tuple[dt.datetime, dict]:
    if key == "vito_b":
        return _SAVED_AT[key], _saved_payload(_VITO_B, override=True)
    if key == "vito_b_healthy":
        intake = {**_VITO_B, "injuries": "", "guided_injury": None, "guided_injuries": []}
        return _SAVED_AT["vito_b"], _saved_payload(intake, override=False)
    if key == "king":
        return _SAVED_AT[key], _saved_payload(_KING, override=False)
    if key == "king_no_cut":
        intake = copy.deepcopy(_KING)
        intake["athlete"]["target_weight_kg"] = None
        return _SAVED_AT["king"], _saved_payload(intake, override=False)
    return _NOW, _form_payload(key)


def _generate(key: str) -> dict:
    now, payload = _scenario(key)
    mp = pytest.MonkeyPatch()
    pin_planner_clock(mp, now)
    try:
        return generate_plan_sync(payload)
    finally:
        mp.undo()


_CACHE: dict[str, dict] = {}


def _brief(key: str) -> dict:
    if key not in _CACHE:
        _CACHE[key] = _generate(key)["planning_brief"]
    return _CACHE[key]


_ALL = ["vito_b", "vito_b_healthy", "king", "king_no_cut", *_FORMS]


def _roles(brief: dict):
    if "late_fight_session_sequence" in brief:
        return [({}, role) for role in brief["late_fight_session_sequence"]]
    return [(week, role) for week in brief["weekly_role_map"]["weeks"] for role in week["session_roles"]]


def _members(role: dict) -> list[dict]:
    return [m for m in role.get("selected_exercise_assignments") or [] if isinstance(m, dict)]


def _entry(brief: dict, goal: str) -> dict:
    return next(e for e in brief["goal_preservation"] if e["goal"] == goal)


_BANK = {exercise["name"]: exercise for exercise in get_exercise_bank()}


# ---------------------------------------------------------------------------
# F01 - anatomical involvement never establishes an adaptation
# ---------------------------------------------------------------------------

def test_adaptation_priorities_never_match_on_anatomical_involvement():
    for tag_map in (GOAL_PRIORITY_TAG_MAP, WEAKNESS_PRIORITY_TAG_MAP):
        for key, tags in tag_map.items():
            if key in ADAPTATION_PRIORITY_KEYS:
                assert not set(tags) & ANATOMICAL_INVOLVEMENT_TAGS, key
    # A strength priority is matched on the exercise's own evidence only.
    assert WEAKNESS_PRIORITY_TAG_MAP["strength"] == [] and GOAL_PRIORITY_TAG_MAP["strength"] == []
    # Regional priorities keep their region: there the region is the point.
    assert "upper_body" in WEAKNESS_PRIORITY_TAG_MAP["shoulders"]
    assert "posterior_chain" in WEAKNESS_PRIORITY_TAG_MAP["posterior_chain"]


@pytest.mark.parametrize("key", ["vito_b", "boxer28_pool_bike", "king"])
def test_no_conditioning_candidate_earns_a_priority_through_a_body_part(key):
    """The saved Vito B plan scored swimming +2.5 for priority_primary_weakness_match:upper_body."""
    for pool in _brief(key)["candidate_pools"].values():
        for slot in pool.get("conditioning_slots") or []:
            for option in [slot.get("selected") or {}, *(slot.get("alternates") or [])]:
                for code in option.get("reason_codes") or []:
                    if code.startswith(("priority_primary_", "priority_secondary_")):
                        assert code.rsplit(":", 1)[-1] not in ANATOMICAL_INVOLVEMENT_TAGS, (option.get("name"), code)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Back Squat", {"strength"}),
        ("Romanian Deadlift (RDL)", {"strength"}),
        ("Z Press", {"strength"}),
        ("Heavy RDL → Broad Jump", {"strength", "power"}),  # a genuine hybrid keeps both
        ("Sled Push (Board)", {"power", "speed"}),
        ("Sled Pull Backward", set()),  # strength_conditioning drag: work capacity, not strength
        ("Sled Push (Heavy)", set()),
        ("Yoke Walk", set()),
        ("Farmer Carry (Heavy)", set()),
        ("Sledgehammer Strikes", set()),
        ("Pallof Press", {"trunk_strength"}),
        ("Plank (Extended Hold)", {"trunk_strength"}),
        ("Single-Arm Chest Press (Floor)", {"strength"}),  # anti-rotation tag on a press is involvement
        ("Copenhagen Adduction", set()),  # adductor hold with a core tag is not trunk training
    ],
)
def test_adaptation_relevance_comes_from_method_and_loading(name, expected):
    assert set(strength_adaptation_relevance(_BANK[name])) == expected


def test_strength_priority_is_scored_through_evidence_and_keeps_its_hierarchy():
    def bonus(item, *, weak_areas, primary_weak_area, goals=("speed",), primary_goal="speed"):
        profile = build_priority_profile({"key_goals": list(goals), "primary_goal": primary_goal,
                                          "weak_areas": list(weak_areas), "primary_weak_area": primary_weak_area})
        score, reasons = score_exercise(
            exercise_tags=item["tags"], weakness_tags=list(weak_areas), goal_tags=[], style_tags=[], must_have_tags=[],
            phase_tags=[], current_phase="GPP", fatigue_level="low", available_equipment=["barbell"],
            required_equipment=[], is_rehab=False, priority_profile=profile,
            adaptation_tags=strength_adaptation_relevance(item),
        )
        return score, reasons["reason_codes"]

    squat, sled = _BANK["Back Squat"], _BANK["Sled Pull Backward"]
    primary, primary_codes = bonus(squat, weak_areas=("strength", "speed"), primary_weak_area="strength")
    secondary, _ = bonus(squat, weak_areas=("speed", "strength"), primary_weak_area="speed")
    none, _ = bonus(squat, weak_areas=("speed",), primary_weak_area="speed")
    assert "priority_primary_weakness_match:strength" in primary_codes
    assert primary > secondary > none
    _, sled_codes = bonus(sled, weak_areas=("strength",), primary_weak_area="strength")
    assert not [code for code in sled_codes if "strength" in code]


def test_authored_body_category_overrides_a_word_hint_family():
    """'Pull' made a sled drag and a rack-pull isometric upper-body pulling."""
    for name in ("Sled Pull Backward", "Rack Pull Isometric (115% 1RM @ Knees)", "Isometric Mid-Thigh Pull"):
        categories = set(classify_strength_item(_BANK[name])["base_categories"])
        assert "upper_body_push_pull" not in categories and "lower_body_loaded" in categories, name
    assert not set(classify_strength_item(_BANK["Pallof Press"])["base_categories"]) & {
        "upper_body_push_pull", "lower_body_loaded"}


def test_a_strength_requirement_is_met_by_a_strength_lift_not_a_pattern():
    """A clap push-up sits in the upper-strength family by its push pattern only."""
    def slot(name, index):
        exercise = _BANK[name]
        profile = classify_strength_item(exercise)
        adaptations = sorted(strength_adaptation_relevance(exercise))
        return {"slot_id": f"t_{index}", "session_index": 1, "selected": {
            "name": name, "quality_class": profile["quality_class"], "base_categories": profile["base_categories"],
            "support_only": profile["support_only"], "meaningful_stress": exercise.get("meaningful_stress"),
            "movement_patterns": [exercise["movement"], *exercise["tags"]], "prescription": "3 x 5",
            "selection_metadata": {"primary_adaptation": adaptations[0] if adaptations else "unknown",
                                   "secondary_adaptations": adaptations[1:]}}}

    records = _candidate_records([slot("Clap Push-Up", 0), slot("Alternating Skater Hops", 1),
                                  slot("Cossack Squat (Dynamic)", 2)])
    selected, _ = _select_bounded_records(records, role_key="transfer_strength_day", cap=2, pressure=0)
    assert "Cossack Squat (Dynamic)" in [record["name"] for record in selected]


# ---------------------------------------------------------------------------
# F04 - heavy implements are not low-load support
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["Sled Push (Heavy)", "Sled Push (Incline)", "Sled Drag (Side)", "Yoke Walk",
                                  "Farmer Carry (Heavy)", "Trap Bar Carry (Frame Neutral)", "Sledgehammer Strikes",
                                  "Bear Hug Carry"])
def test_authored_meaningful_stress_is_never_low_cost_core_support(name):
    assert classify_strength_item(_BANK[name])["core_balance_support"] is False


@pytest.mark.parametrize("name", ["Plank", "Pallof Press", "Dead Bug (Isometric)", "Hollow Body Hold"])
def test_genuinely_low_cost_trunk_work_stays_available(name):
    assert classify_strength_item(_BANK[name])["core_balance_support"] is True


def test_a_heavy_sled_is_not_the_low_cost_item_before_hard_contact():
    def slot(name):
        exercise = _BANK[name]
        profile = classify_strength_item(exercise)
        return {"selected": {"name": name, "quality_class": profile["quality_class"],
                             "support_only": exercise.get("support_only"), "meaningful_stress": exercise.get("meaningful_stress"),
                             **{field: exercise.get(field) for field in ("impact_cost", "landing_cost", "eccentric_cost",
                                                                         "soreness_risk")}}}

    assert _pre_hard_verified_low_cost(slot("Sled Push (Heavy)")) is False
    assert _pre_hard_verified_low_cost(slot("Plank")) is True


@pytest.mark.parametrize("key", _ALL)
def test_no_heavy_implement_is_embedded_as_low_load_trunk_support(key):
    for _, role in _roles(_brief(key)):
        for member in _members(role):
            if member.get("support_dose_category") == "low_load_trunk":
                exercise = _BANK[member["name"]]
                assert exercise.get("meaningful_stress") is not True, member["name"]
                assert "trunk_strength" in strength_adaptation_relevance(exercise), member["name"]


# ---------------------------------------------------------------------------
# The saved generations, end to end
# ---------------------------------------------------------------------------

def _development_strength_sessions(brief: dict) -> list[tuple[int, dict]]:
    return [(role_d_day(week, role), role) for week, role in _roles(brief)
            if role.get("category") == "strength" and not role.get("late_fight_tail_owned")
            and isinstance(role_d_day(week, role), int) and role_d_day(week, role) > 13]


def test_vito_b_strength_weakness_is_trained_by_real_lifts():
    brief = _brief("vito_b")
    entry = _entry(brief, "strength")
    assert (entry["priority"], entry["state"], entry["satisfied"]) == ("primary_weakness", "build", True)
    lifted = {e["name"] for e in entry["evidence"]}
    assert lifted and all("strength" in strength_adaptation_relevance(_BANK[name]) for name in lifted)
    assert not any(e["goal"] == "strength" for e in validate_goal_preservation(brief))
    # Every development session holds real strength, power or speed work;
    # the weakness contract above still requires strength in each owed window.
    for d_day, role in _development_strength_sessions(brief):
        assert any({"strength", "power", "speed"} & strength_adaptation_relevance(_BANK[m["name"]]) for m in _members(role)), d_day


def test_a_stable_blister_is_not_global_strength_pressure():
    """A covered not-limiting blister cost every strength session one exercise."""
    brief, healthy = _brief("vito_b"), _brief("vito_b_healthy")
    # Main (#2867) drops an approved not-limiting injury from planning; when an
    # injury survives it must be the surface-only kind. Either way: no pressure.
    snapshot = brief["athlete_snapshot"]
    assert snapshot.get("surface_injury_only") is True or not snapshot.get("has_active_injury")
    for d_day, role in _development_strength_sessions(brief):
        policy = role["strength_composition_policy"]
        assert (policy["injury_pressure"], policy["pressure"]) == (0, 0), d_day
    shipped = [[m["name"] for m in _members(r)] for _, r in _development_strength_sessions(brief)]
    assert shipped == [[m["name"] for m in _members(r)] for _, r in _development_strength_sessions(healthy)]


def test_a_significant_injury_still_reduces_strength_capacity():
    for _, role in _development_strength_sessions(_brief("boxer28_knee_injury")):
        policy = role["strength_composition_policy"]
        assert policy["injury_restricted"] is True and policy["injury_pressure"] == 1


@pytest.mark.parametrize("key", ["vito_b", "boxer28_strength_fullgym", "boxer28_pool_bike"])
def test_a_fresh_standalone_primary_strength_day_can_carry_up_to_six_complementary_lifts(key):
    days = [(d, r) for d, r in _development_strength_sessions(_brief(key)) if r["role_key"] == "primary_strength_day"]
    assert days
    for d_day, role in days:
        names = [m["name"] for m in _members(role)]
        assert 4 <= len(names) <= 6 and len(set(names)) == len(names), (d_day, names)
        assert role["strength_composition_policy"]["pressure"] == 0
        assert role["strength_composition_policy"]["base_exercise_cap"] == 6


def test_king_trunk_weakness_is_checked_against_delivered_trunk_work():
    brief = _brief("king")
    disposition = brief["primary_weakness_disposition"]
    assert disposition["resolution"] == "delegated" and disposition["required_intent"] == "trunk_training"
    entry = _entry(brief, "trunk_strength")
    assert entry["initial_state"] == "build"
    # Recomposition after a conflicting neighbour is removed restores safe
    # trunk work inside the existing readiness cap. The cut no longer supplies
    # a causal reason to demote this achievable build obligation.
    assert (entry["state"], entry["satisfied"]) == ("build", True)
    assert entry["constraints"] == []
    assert entry["missing_coverage"] == []
    for evidence in entry["evidence"]:
        assert "trunk_strength" in strength_adaptation_relevance(_BANK[evidence["name"]])
        assert "sled" not in evidence["name"].lower()
    assert not any(e["goal"] == "trunk_strength" for e in validate_goal_preservation(brief))


@pytest.mark.parametrize("key", ["king", "king_no_cut", "boxer28_trunk_bodyweight", "muaythai28_power_trunk"])
def test_every_conditioning_day_says_why_it_carries_no_trunk_support(key):
    skipped = [
        (role_d_day(week, role), role["conditioning_composition_policy"]["embedded_trunk_support_skip_reason"])
        for week, role in _roles(_brief(key))
        if not (role.get("conditioning_composition_policy") or {}).get("embedded_trunk_support", True)
    ]
    assert all(reason for _, reason in skipped), skipped
    if key == "king":
        assert any(r.get("category") == "conditioning" and _members(r)
                   for week, r in _roles(_brief(key))
                   if isinstance(role_d_day(week, r), int) and 14 <= role_d_day(week, r) <= 20)



def test_without_the_cut_king_builds_trunk_strength_every_week():
    entry = _entry(_brief("king_no_cut"), "trunk_strength")
    assert (entry["state"], entry["satisfied"], entry["constraints"]) == ("build", True, [])


@pytest.mark.parametrize("key", ["boxer28_trunk_bodyweight", "muaythai28_power_trunk"])
def test_trunk_weakness_reaches_the_finished_programme(key):
    brief = _brief(key)
    entry = _entry(brief, "trunk_strength")
    assert entry["satisfied"] and entry["evidence"]
    final = {(role_d_day(week, role), m["name"]) for week, role in _roles(brief) for m in _members(role)}
    for evidence in entry["evidence"]:
        assert (evidence["d_day"], evidence["name"]) in final


def test_embedded_trunk_support_is_maintenance_not_development():
    embedded = [e for e in collect_goal_evidence(_brief("boxer28_trunk_bodyweight"))
                if e.get("dose_authority") == "embedded_trunk_support"]
    assert embedded
    assert all(e["intents"] == ["trunk_training"] and e["development_quality"] is False for e in embedded)


def test_a_short_camp_keeps_its_trunk_weakness_through_the_late_tail():
    brief = _brief("boxer10_trunk_short")
    entry = _entry(brief, "trunk_strength")
    assert entry["state"] in {"maintain", "defer"}
    assert entry["satisfied"] or entry["constraints"]
    assert not any(e["goal"] == "trunk_strength" for e in validate_goal_preservation(brief))


@pytest.mark.parametrize("key", _ALL)
def test_goal_evidence_names_only_final_members_at_their_final_day(key):
    """Candidates, dropped records and unused alternates never evidence any
    goal: every witness is a member of its role on the day it is cited."""
    brief = _brief(key)
    final = {(role_d_day(week, role), m["name"]) for week, role in _roles(brief) for m in _members(role)}
    for entry in brief["goal_preservation"]:
        for evidence in entry["evidence"]:
            if evidence.get("d_day") is not None and evidence.get("name"):
                if evidence.get("dose_authority") == "scheduled_support_dose":
                    from fightcamp.render_authority import authoritative_render_for_role
                    role = next(r for week, r in _roles(brief)
                                if role_d_day(week, r) == evidence["d_day"] and r["role_key"] == evidence["role_key"])
                    render = authoritative_render_for_role(role)
                    assert render and any(evidence["name"] in line for line in render["body_lines"])
                    assert role["prescribed_duration_min"] == evidence["duration_min"]
                    continue
                assert (evidence["d_day"], evidence["name"]) in final, (entry["goal"], evidence["name"])


@pytest.mark.parametrize("key", _ALL)
def test_no_selected_goal_newly_loses_its_contract(key):
    """Strength and trunk obligations resolve to build, maintain or a cited defer."""
    brief = _brief(key)
    for entry in brief["goal_preservation"]:
        if entry["goal"] in {"strength", "trunk_strength"}:
            assert entry["state"] in {"build", "maintain", "defer"}
            if entry["state"] == "defer":
                assert entry["constraints"]


def test_swimming_remains_available_on_its_aerobic_merit():
    """No blacklist: with a pool, swimming may still own an aerobic slot."""
    from fightcamp.session_composition import compose_normal_conditioning_assignments
    brief = _brief("vito_b")
    swims = [s for s in brief["candidate_pools"]["GPP"]["conditioning_slots"]
             if "swim" in s["selected"]["name"].lower()]
    assert swims
    role = {"role_key": "aerobic_support_day", "category": "conditioning", "preferred_system": "aerobic",
            "scheduled_countdown_label": "D-56", "scheduled_day_hint": "Monday"}
    compose_normal_conditioning_assignments(weekly_role_map={"weeks": [{"phase": "GPP", "session_roles": [role]}]},
        candidate_pools={"GPP": {"conditioning_slots": swims}})
    assert any("swim" in m["name"].lower() for m in _members(role))



def test_contrast_rounds_are_read_as_working_sets():
    assert _sets_reps("3–4 rounds: heavy movement 2–3 reps @ 85–92% 1RM → explosive movement") == (3, 2)
    assert _sets_reps("3 rounds; heavy RDL 2 reps @ 85% 1RM") == (3, 2)
    # A hold is timed and a carry is a distance: neither is a rep count.
    assert _sets_reps("1-2 controlled sets x 15-20 sec hold; stop before fatigue") == (1, 0)
    assert dose_volume("1-2 controlled sets x 15-20 sec hold") == {"sets": 1, "amount": 15, "unit": "sec"}
    assert dose_volume("2 x 20 m carry") == {"sets": 2, "amount": 20, "unit": "m"}
    assert _sets_reps("5 rounds x 3 min") == (0, 0)


def _fingerprint(key: str) -> str:
    """A fresh generation's digest. Never the shared ``_brief`` cache, which
    ledger reads in other tests may annotate in place."""
    brief = _generate(key)["planning_brief"]
    return hashlib.sha256(json.dumps(brief, sort_keys=True, default=str).encode()).hexdigest()


def test_unchanged_inputs_generate_identical_programmes():
    assert _fingerprint("vito_b") == _fingerprint("vito_b")


# ---------------------------------------------------------------------------
# Find a Drill reviewed Goals: confidence, never a ranking authority
# ---------------------------------------------------------------------------

def test_reviewed_strength_bank_goals_agree_with_programming_evidence():
    """The discovery reviews are an independent oracle for the classifier.

    ``project_entry`` validates each review against the exact catalogue identity
    and row SHA256 and raises on a stale review.
    """
    from fightcamp.training_catalogue import build_catalogue
    from fightcamp.training_metadata import load_projection, project_entry

    projection = load_projection()
    reviewed = 0
    for entry in build_catalogue():
        if entry["source"] != "exercise_bank.json":
            continue
        for value in project_entry(entry, projection):
            if value.get("status") != "reviewed_goal":
                continue
            goal = {"maximal_strength_maintenance": "strength"}.get(value["canonical_id"], value["canonical_id"])
            assert goal in strength_adaptation_relevance(entry["raw"]), (entry["name"], goal)
            reviewed += 1
    assert reviewed >= 8


def test_an_unreviewed_lift_competes_exactly_like_a_reviewed_one():
    """Back Squat carries a reviewed Strength Goal; the RDL does not."""
    for name in ("Back Squat", "Romanian Deadlift (RDL)"):
        assert strength_adaptation_relevance(_BANK[name]) == {"strength"}
    vito = _brief("vito_b")
    reasons = {
        slot["selected"]["name"]: set(slot["selected"]["reason_codes"])
        for pool in vito["candidate_pools"].values() for slot in pool.get("strength_slots") or []
    }
    assert "priority_primary_weakness_match:strength" in reasons["Back Squat"]
    assert "priority_primary_weakness_match:strength" in reasons["Romanian Deadlift (RDL)"]


def test_reviewed_goal_metadata_cannot_change_the_programme(monkeypatch):
    """Generation with every reviewed Goal removed is identical: no bonus, no
    shortlist and no eligibility gap follows from review status."""
    from shared import contracts

    original = contracts.load_shared_contract

    def without_goal_reviews(name, *args, **kwargs):
        contract = original(name, *args, **kwargs)
        if name == "training-metadata.json":
            contract = copy.deepcopy(contract)
            contract["catalogue_projection"]["goal_reviews"] = []
        return contract

    monkeypatch.setattr(contracts, "load_shared_contract", without_goal_reviews)
    disabled = _fingerprint("vito_b")
    monkeypatch.undo()
    assert disabled == _fingerprint("vito_b")


def test_stamped_adaptation_relevance_is_what_composition_reads():
    for pool in _brief("vito_b")["candidate_pools"].values():
        for slot in pool.get("strength_slots") or []:
            name = slot["selected"]["name"]
            expected = strength_adaptation_relevance(_BANK[name], prescription_type=_classify_prescription_type(_BANK[name]))
            assert slot_adaptations(slot) == expected or not expected, name
