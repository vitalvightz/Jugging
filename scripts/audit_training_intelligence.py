"""Reproduce the training audit without network calls or production data.

Run: python -m scripts.audit_training_intelligence --output <directory>
The output includes full deterministic plans, finalizer packets and summaries.
It does not call the AI provider or publish plans.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from api.structured_plan_deterministic_fallback import build_deterministic_structured_plan
from api.structured_plan_models import StructuredTrainingPlan
from fightcamp.calendar_context import role_d_day
from fightcamp.goal_preservation import collect_goal_evidence, validate_goal_preservation
from fightcamp.input_parsing import PlanInput
from fightcamp.plan_pipeline_blocks import generate_plan_blocks
from fightcamp.plan_pipeline_rendering import build_stage2_outputs, render_plan_bundle
from fightcamp.plan_pipeline_runtime import build_runtime_context, prime_plan_banks
from fightcamp.programme_fidelity import reconcile_authoritative_programme, programme_fidelity_findings
from fightcamp.stage2_finalizer_packet import build_stage2_finalizer_packet
from fightcamp.stage2_validator import validate_stage2_output
from fightcamp.stage2_pipeline import review_stage2_output
from fightcamp.strength import get_exercise_bank, _classify_prescription_type
from fightcamp.strength_session_quality import classify_strength_item, strength_adaptation_relevance
from fightcamp.training_catalogue import build_catalogue


NOW = dt.datetime(2026, 10, 16, 12)
BASE_FIELDS = {
    "Full name": "Synthetic audit athlete", "Age": "28", "Weight (kg)": "77",
    "Target Weight (kg)": "77", "Height (cm)": "180", "Stance": "Orthodox",
    "Fighting Style (Technical)": "mma", "Professional Status": "amateur",
    "Rounds x Minutes": "3x5", "Weekly Training Frequency": "5",
    "Fatigue Level": "low",
    "Equipment Access": "barbell, dumbbells, kettlebells, medicine_ball, cable_machine, pull_up_bar, heavy_bag, bodyweight",
    "Training Availability": "Monday, Tuesday, Thursday, Friday, Saturday",
    "Hard Sparring Days": "Tuesday, Thursday",
    "What are your key performance goals?": "power, strength",
    "Primary goal": "power",
    "Where do you feel weakest right now?": "power, trunk_strength",
    "Primary weak area": "power",
}
# Each variant passes through the real intake parser, selectors, calendar,
# composition, effective dosing, goal reconciliation and rendering.
SCENARIOS = {
    "mma_27d": {},
    "mma_monday_capacity": {"Weekly Training Frequency": "6", "Training Availability": "Monday, Tuesday, Wednesday, Thursday, Friday, Saturday"},
    "strength_primary": {"Primary goal": "strength"},
    "speed_primary": {"Primary goal": "speed", "What are your key performance goals?": "speed, strength"},
    "power_trunk": {"Primary weak area": "trunk_strength"},
    "high_fatigue": {"Fatigue Level": "high"},
    "short_notice": {"days": 10},
    "eight_weeks": {"days": 56},
    "twelve_weeks": {"days": 84},
    "injury_restricted": {"Current injuries": "Mild left shoulder impingement; avoid overhead loading"},
    "weight_cut": {"Target Weight (kg)": "70"},
    "limited_capacity": {"Equipment Access": "bodyweight, resistance_bands", "Training Availability": "Monday, Thursday, Saturday", "Weekly Training Frequency": "3", "Hard Sparring Days": "Thursday"},
    "competing_goals": {"What are your key performance goals?": "power, strength, conditioning, speed", "Where do you feel weakest right now?": "power, trunk_strength, gas_tank"},
    "late_transition": {"days": 18},
    "boxing": {"Fighting Style (Technical)": "boxing", "Rounds x Minutes": "3x3"},
    "bjj": {"Fighting Style (Technical)": "bjj", "Rounds x Minutes": "1x10"},
}


def generate_scenario(name: str, *, seed: int = 1) -> dict:
    overrides = dict(SCENARIOS[name])
    days = overrides.pop("days", 27)
    fields = {**BASE_FIELDS, **overrides, "When is your next fight?": (NOW.date() + dt.timedelta(days=days)).isoformat()}
    logger = logging.getLogger(__name__)
    prime_plan_banks(logger=logger)
    with patch("fightcamp.input_parsing._utc_now", return_value=NOW):
        plan_input = PlanInput.from_payload({"data": {"fields": [{"label": k, "value": v} for k, v in fields.items()]}})
    assert plan_input.days_until_fight == days
    context = build_runtime_context(plan_input=plan_input, random_seed=seed, logger=logger)
    blocks = generate_plan_blocks(context=context, logger=logger, record_timing=lambda *a, **kw: None)
    rendered = render_plan_bundle(context=context, blocks=blocks, logger=logger)
    payload, brief, handoff = build_stage2_outputs(context=context, blocks=blocks, rendered=rendered)
    plan = reconcile_authoritative_programme(brief, rendered.fight_plan_text)["text"]
    packet = build_stage2_finalizer_packet(stage2_payload=payload, planning_brief=brief)
    structured = build_deterministic_structured_plan(brief, plan)
    assert structured is not None, "Generated plan must reach athlete-facing cards"
    StructuredTrainingPlan.model_validate(structured)
    evidence = collect_goal_evidence(brief)
    weeks = []
    for week in (brief.get("weekly_role_map") or {}).get("weeks") or []:
        roles = []
        for role in week.get("session_roles") or []:
            if role.get("category") not in {"strength", "conditioning", "sparring"}:
                continue
            roles.append({
                "day": role_d_day(week, role), "weekday": role.get("scheduled_day_hint"),
                "role": role.get("role_key"), "category": role.get("category"),
                "exercises": [{"name": m.get("name"), "dose": m.get("effective_prescription") or m.get("base_prescription")}
                              for m in role.get("selected_exercise_assignments") or []],
            })
        weeks.append({"week": week.get("week_index"), "phase": week.get("phase"), "sessions": roles,
                      "counts": week.get("session_count_summary"),
                      "unused": week.get("intentionally_unused_days")})
    summary = {
        "scenario": name, "seed": seed, "days": days,
        "goals": brief.get("goal_preservation"),
        "strength_exposures": [e for e in evidence if "meaningful_strength" in e["intents"]],
        "goal_findings": validate_goal_preservation(brief),
        "fidelity_findings": programme_fidelity_findings(brief, plan),
        "raw_validator": validate_stage2_output(planning_brief=brief, final_plan_text=plan),
        "validator": review_stage2_output(planning_brief=brief, final_plan_text=plan)["validator_report"],
        "weeks": weeks,
    }
    return {"summary": summary, "brief": brief, "packet": packet, "plan": plan, "handoff": handoff,
            "structured": structured}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    args.output.mkdir(parents=True, exist_ok=True)
    catalogue, strength = build_catalogue(), get_exercise_bank()
    library = {
        "records": len(catalogue),
        "by_source": dict(Counter(r["source"] for r in catalogue)),
        "by_class": dict(Counter(r["content_class"] for r in catalogue)),
        "source_hashes": {r["source"]: r["source_sha256"] for r in catalogue},
        "strength_records": len(strength),
        "strength_quality_classes": dict(Counter(classify_strength_item(e)["quality_class"] for e in strength)),
        "strength_prescription_types": dict(Counter(_classify_prescription_type(e) for e in strength)),
        "capability_without_strength_dose": [
            {"name": e["name"], "prescription_type": _classify_prescription_type(e)} for e in strength
            if "strength" in strength_adaptation_relevance(e)
            and "strength" not in strength_adaptation_relevance(e, prescription_type=_classify_prescription_type(e))
        ],
    }
    (args.output / "library.json").write_text(json.dumps(library, indent=2), encoding="utf-8")
    summaries = []
    for name in args.scenario or SCENARIOS:
        result = generate_scenario(name, seed=args.seed)
        summaries.append(result["summary"])
        for kind in ("brief", "packet", "summary", "structured"):
            (args.output / f"{name}.{kind}.json").write_text(json.dumps(result[kind], indent=2, default=str), encoding="utf-8")
        (args.output / f"{name}.md").write_text(result["plan"], encoding="utf-8")
        print(f"{name}: strength D-days={sorted({e['d_day'] for e in result['summary']['strength_exposures']}, reverse=True)}; goal findings={len(result['summary']['goal_findings'])}", flush=True)
    (args.output / "summary.json").write_text(json.dumps(summaries, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
