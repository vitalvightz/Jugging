"""Regression checks for the 15 combat core additions and targeted safety fixes."""
from fightcamp.goal_preservation import trunk_dose_adequate
from fightcamp.strength import _core_development_candidates, get_exercise_bank
from fightcamp.strength_session_quality import trunk_function, trunk_load_tier
from fightcamp.training_catalogue import build_catalogue
from fightcamp.training_context import known_equipment, normalize_equipment_list

NEW_NAMES = (
    "Bear Plank Dumbbell Drag",
    "Renegade Row",
    "Partner-Perturbed Guard Hold",
    "Standing Cable Side Bend",
    "Kettlebell Windmill",
    "Lateral Med-Ball Catch and Stick",
    "Slider Body Saw",
    "Swiss-Ball Pike",
    "Tall-Kneeling Cable Pulldown Brace",
    "Reverse Crunch with Pelvic Lift",
    "Seated Pike Compression Lift-Off",
    "Kneeling Cable Crunch",
    "Weighted 45° Back Extension",
    "Low-to-High Cable Lift",
    "Rotational Med-Ball Catch and Stick",
)


def test_all_15_are_distinct_and_keep_strict_catalogue_ids():
    names = [row["name"] for row in get_exercise_bank()]
    assert all(names.count(name) == 1 for name in NEW_NAMES)
    catalogue = {row["name"]: row for row in build_catalogue(strict=True)
                 if row["source"] == "exercise_bank.json"}
    assert all(catalogue[name]["catalogue_id"] for name in NEW_NAMES)


def test_catches_and_side_bends_are_distinct_trunk_functions():
    rows = {row["name"]: row for row in get_exercise_bank()}
    catch = rows["Rotational Med-Ball Catch and Stick"]
    assert trunk_function(catch) == "rotational_braking"
    assert trunk_load_tier(catch) == "loaded"
    assert trunk_function(rows["Standing Cable Side Bend"]) == "lateral_flexion"
    assert trunk_function(rows["Lateral Med-Ball Catch and Stick"]) == "anti_lateral_flexion"


def test_low_rep_braking_counts_without_lowering_other_core_thresholds():
    witness = {"sets": 3, "reps": 5, "trunk_function": "rotational_braking",
               "trunk_load_tier": "loaded"}
    assert trunk_dose_adequate(witness)
    assert not trunk_dose_adequate({**witness, "reps": 2})
    assert not trunk_dose_adequate({**witness, "trunk_function": "anti_rotation"})


def test_core_candidates_obey_guard_before_filling_function_quota():
    rows = {row["name"]: row for row in get_exercise_bank()}
    scored = [(rows[name], 10 - i, {}) for i, name in enumerate(
        ("Renegade Row", "Cable Pallof Press", "Slider Body Saw"))]
    candidates = _core_development_candidates(
        scored, is_safe=lambda row: row["name"] != "Renegade Row")
    selected = [entry["exercise"]["name"] for entry in candidates]
    assert "Renegade Row" not in selected
    assert "Cable Pallof Press" in selected


def test_back_extension_requires_its_own_bench_not_generic_bench():
    rows = {row["name"]: row for row in get_exercise_bank()}
    equipment = normalize_equipment_list(rows["Weighted 45° Back Extension"]["equipment"])
    assert equipment == ["back_extension_bench", "plate"]
    assert "back_extension_bench" in known_equipment
