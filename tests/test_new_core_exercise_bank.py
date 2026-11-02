"""Coverage for the 15 additional combat-specific core/trunk exercises."""
from fightcamp.strength import get_exercise_bank, _classify_prescription_type
from fightcamp.strength_session_quality import strength_adaptation_relevance, trunk_function
from fightcamp.training_catalogue import build_catalogue
from fightcamp.training_context import normalize_equipment_list

NEW_NAMES = [
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
  "Rotational Med-Ball Catch and Stick"
]


def test_all_new_core_entries_have_doses_and_are_trunk_selectable():
    entries = {e["name"]: e for e in get_exercise_bank()}
    for name in NEW_NAMES:
        row = entries[name]
        assert row["category"] == "core" and {"GPP", "SPP"} <= set(row["phases"])
        assert row["prescription"].startswith("3 x ") and row["notes"]
        assert row["mechanical_risk_tags"]
        assert "trunk_strength" in strength_adaptation_relevance(row, prescription_type=_classify_prescription_type(row))
        assert trunk_function(row)


def test_equipment_is_explicit_and_aligned_with_user_intake():
    entries = {e["name"]: e for e in get_exercise_bank()}
    assert normalize_equipment_list(entries["Partner-Perturbed Guard Hold"]["equipment"]) == ["bands", "partner"]
    assert normalize_equipment_list(entries["Rotational Med-Ball Catch and Stick"]["equipment"]) == ["medicine_ball", "partner"]
    assert normalize_equipment_list(entries["Slider Body Saw"]["equipment"]) == ["sliders"]
    assert normalize_equipment_list(entries["Weighted 45° Back Extension"]["equipment"]) == ["back_extension_bench", "plate"]


def test_trunk_classification_does_not_confuse_dynamic_and_static_functions():
    entries = {e["name"]: e for e in get_exercise_bank()}
    expected = {
        "Bear Plank Dumbbell Drag": "anti_rotation",
        "Renegade Row": "anti_rotation",
        "Partner-Perturbed Guard Hold": "reactive_bracing",
        "Standing Cable Side Bend": "lateral_flexion",
        "Kettlebell Windmill": "anti_lateral_flexion",
        "Lateral Med-Ball Catch and Stick": "anti_lateral_flexion",
        "Slider Body Saw": "anti_extension",
        "Swiss-Ball Pike": "anti_extension",
        "Tall-Kneeling Cable Pulldown Brace": "anti_extension",
        "Kneeling Cable Crunch": "flexion",
        "Weighted 45° Back Extension": "trunk_extension",
        "Low-to-High Cable Lift": "rotation",
        "Rotational Med-Ball Catch and Stick": "rotational_braking",
    }
    for name, function in expected.items():
        assert trunk_function(entries[name]) == function, name


def test_training_catalogue_binding_is_strict_and_complete():
    entries = {e["name"]: e for e in build_catalogue(strict=True) if e["source"] == "exercise_bank.json"}
    assert all(entries[name]["catalogue_id"] for name in NEW_NAMES)
