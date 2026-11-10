import json

import pytest

from fightcamp.stage2_llm_boundary import build_stage2_llm_planning_brief
from fightcamp.stage2_payload import build_stage2_handoff_text


def _planning_brief(*, tactical_styles: list[str], preferred_tags: list[str]) -> dict:
    return {
        "athlete_snapshot": {
            "sport": "boxing",
            "status": "amateur",
            "stance": "Hybrid",
            "tactical_styles": tactical_styles,
        },
        "weekly_role_map": {
            "weeks": [
                {
                    "week_index": 1,
                    "phase": "SPP",
                    "session_roles": [
                        {
                            "role_key": "tactical_watch_day",
                            "category": "technical",
                            "scheduled_day_hint": "wednesday",
                            "preferred_tags": preferred_tags,
                            "display_text": "SPP pocket planning for a pressure fighter.",
                        }
                    ],
                }
            ]
        },
    }


def _json_section(handoff: str, heading: str) -> dict:
    block = handoff.split(f"{heading}\n", 1)[1].split("\n\n---\n\n", 1)[0]
    body = block.removeprefix("```json\n").removesuffix("\n```")
    return json.loads(body)


def test_stage2_llm_projection_preserves_internal_model_and_hides_parent_family_tags():
    planning_brief = _planning_brief(
        tactical_styles=["pressure fighter", "hybrid"],
        preferred_tags=["tactical_watch", "brawler", "SPP"],
    )

    llm_brief = build_stage2_llm_planning_brief(planning_brief)

    assert planning_brief["athlete_snapshot"]["tactical_styles"] == [
        "pressure fighter",
        "hybrid",
    ]
    assert planning_brief["weekly_role_map"]["weeks"][0]["session_roles"][0][
        "preferred_tags"
    ] == ["tactical_watch", "brawler", "SPP"]

    assert llm_brief is not planning_brief
    assert llm_brief["athlete_snapshot"]["tactical_styles"] == ["Pressure Fighter"]
    assert llm_brief["weekly_role_map"]["weeks"][0]["session_roles"][0][
        "preferred_tags"
    ] == ["tactical_watch", "SPP"]


def test_stage2_llm_projection_sanitizes_athlete_model_without_mutating_source():
    planning_brief = {
        "athlete_model": {
            "sport": "boxing",
            "stance": "Hybrid",
            "tactical_styles": ["pressure fighter", "hybrid"],
        }
    }

    llm_brief = build_stage2_llm_planning_brief(planning_brief)

    assert planning_brief["athlete_model"]["tactical_styles"] == [
        "pressure fighter",
        "hybrid",
    ]
    assert llm_brief["athlete_model"]["tactical_styles"] == ["Pressure Fighter"]


def test_pressure_fighter_hybrid_stance_stage2_handoff_sanitizes_raw_planning_brief():
    planning_brief = _planning_brief(
        tactical_styles=["pressure fighter", "hybrid"],
        preferred_tags=["tactical_watch", "brawler", "SPP"],
    )

    handoff = build_stage2_handoff_text(
        stage2_payload={
            "athlete_model": {
                "sport": "boxing",
                "stance": "Hybrid",
                "tactical_styles": ["pressure fighter", "hybrid"],
            }
        },
        plan_text="# Stage 1 Draft\n- Pressure Fighter tactical watch",
        planning_brief=planning_brief,
    )

    packet = _json_section(handoff, "FINALIZER PACKET")
    athlete_profile = {**packet["athlete_model"], **_json_section(handoff, "ATHLETE PROFILE")}

    assert planning_brief["athlete_snapshot"]["tactical_styles"] == [
        "pressure fighter",
        "hybrid",
    ]
    assert planning_brief["weekly_role_map"]["weeks"][0]["session_roles"][0][
        "preferred_tags"
    ] == ["tactical_watch", "brawler", "SPP"]

    assert packet["athlete_model"]["tactical_styles"] == ["Pressure Fighter"]
    assert packet["render_guards"]["declared_tactical_styles"] == ["Pressure Fighter"]
    assert athlete_profile["tactical_styles"] == ["Pressure Fighter"]
    assert athlete_profile["stance"] == "Hybrid"
    assert "brawler" not in handoff.lower()
    assert '"tactical_styles":["pressure fighter","hybrid"]' not in handoff.lower()

    role = packet["selected_plan"]["weekly_role_map"]["weeks"][0]["session_roles"][0]
    assert role["preferred_tags"] == ["tactical_watch", "SPP"]


@pytest.mark.parametrize(
    ("raw_styles", "expected"),
    [
        (["brawler"], ["Brawler"]),
        (["hybrid"], ["Hybrid"]),
        (["counter striker"], ["Counter Striker"]),
    ],
)
def test_stage2_llm_projection_preserves_declared_style_label(raw_styles, expected):
    planning_brief = _planning_brief(
        tactical_styles=raw_styles,
        preferred_tags=["tactical_watch", "counter_striker", "SPP"],
    )

    llm_brief = build_stage2_llm_planning_brief(planning_brief)

    assert llm_brief["athlete_snapshot"]["tactical_styles"] == expected
    assert llm_brief["weekly_role_map"]["weeks"][0]["session_roles"][0][
        "preferred_tags"
    ] == ["tactical_watch", "SPP"]
