"""Under-18 athletes never receive weight-cut, dehydration or water-cut guidance.

docs/children-age-appropriate-use-policy.md: "Do not provide aggressive
weight-cut, dehydration or automated water-cut protocols" to under-18s.
docs/terms-of-use.md repeats it as a user-facing promise.

The guard has three layers and each is exercised here: the Stage 1 payload never
carries the cut inputs, the deterministic pipeline produces no cut for a minor,
and anything an AI stage adds on top is caught on the way out.
"""

from __future__ import annotations

from datetime import date

import pytest

from api.minor_safety import (
    MINOR_WEIGHT_CUT_NOTE,
    strip_minor_target_weight,
    blocked_guidance_reasons,
    contains_blocked_minor_guidance,
    detect_minor_guidance_leakage,
    minor_safe_stage1_payload,
    scrub_minor_guidance,
    scrub_minor_guidance_tree,
)
from api.structured_plan_safety import audit_structured_plan, is_blocking_finding
from fightcamp.input_parsing import PlanInput
from fightcamp.nutrition import compute_nutrition_targets, generate_nutrition_block
from fightcamp.stage2_payload import build_computed_support
from tests.support import DEFAULT_ATHLETE_USER, _build_client, _build_request

ATHLETE = {"Authorization": "Bearer athlete-token"}


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Begin water loading 5 days out, then cut water for the final 24 hours.",
        "Target 3% dehydration by weigh-in.",
        "Rehydration protocol: 150% of fluid lost.",
        "Two sauna sessions on fight week.",
        "Wear a sweat suit for the last round.",
        "Use a mild diuretic if the scale is not moving.",
        "Acute cut begins Wednesday.",
        "Restrict fluids from Thursday evening.",
        "Sodium loading then sodium depletion across the week.",
        "Refeed with 10 g/kg carbohydrate after the weigh-in.",
    ],
)
def test_blocked_protocols_are_detected(text):
    assert contains_blocked_minor_guidance(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Drink water steadily across the day.",
        "Hydration: 0.03-0.04 l/kg -> 2100-2900 ml/day.",
        "Protein intake: 1.7-2.2 g/kg.",
        "Three core meals and two snacks daily.",
        "Take a full rest day if soreness is still high tomorrow.",
    ],
)
def test_ordinary_hydration_and_nutrition_advice_is_not_blocked(text):
    # Telling a child to drink is the safe outcome; only fluid *restriction*
    # protocols are prohibited. A pattern that swallowed plain hydration advice
    # would make the guard actively harmful.
    assert contains_blocked_minor_guidance(text) is False


def test_reasons_name_the_rule_that_matched():
    assert "water_cut" in blocked_guidance_reasons("water cut on Friday")
    assert "sweat_protocol" in blocked_guidance_reasons("sauna after training")


# ---------------------------------------------------------------------------
# Layer 1: the Stage 1 payload
# ---------------------------------------------------------------------------


def test_minor_payload_drops_the_target_weight_and_sets_the_flag():
    payload = {
        "data": {
            "fields": [
                {"label": "Weight (kg)", "value": "72.5"},
                {"label": "Target Weight (kg)", "value": "66.0"},
            ]
        }
    }

    guarded = minor_safe_stage1_payload(payload)

    assert guarded["is_minor"] is True
    values = {field["label"]: field["value"] for field in guarded["data"]["fields"]}
    # Current bodyweight survives — it drives macros and hydration, which
    # under-18 athletes still get.
    assert values["Weight (kg)"] == "72.5"
    assert values["Target Weight (kg)"] == ""
    # The caller's payload is not mutated in place.
    assert payload["data"]["fields"][1]["value"] == "66.0"


def test_plan_input_reads_the_server_supplied_minor_flag():
    payload = {
        "is_minor": True,
        "data": {
            "fields": [
                {"label": "Full name", "value": "Junior Athlete"},
                {"label": "Weekly Training Frequency", "value": "3"},
                {"label": "Training Availability", "value": "Monday, Wednesday, Friday"},
            ]
        },
    }

    assert PlanInput.from_payload(payload).is_minor is True
    assert PlanInput.from_payload({**payload, "is_minor": False}).is_minor is False
    # Absent means adult: only the backend ever sets this, and it always sets it
    # for a minor.
    assert PlanInput.from_payload({"data": payload["data"]}).is_minor is False


# ---------------------------------------------------------------------------
# Layer 2: the deterministic nutrition module
# ---------------------------------------------------------------------------


def _flags(**overrides) -> dict:
    base = {
        "weight": 72.5,
        "phase": "TAPER",
        "fatigue": "low",
        "weight_cut_risk": True,
        "weight_cut_pct": 6.5,
        "days_until_fight": 10,
    }
    base.update(overrides)
    return base


def test_an_adult_with_an_active_cut_still_gets_cut_guidance():
    block = generate_nutrition_block(flags=_flags())

    assert "Weight Cut Protocol Triggered" in block
    assert MINOR_WEIGHT_CUT_NOTE not in block


def test_a_minor_never_gets_the_cut_protocol_even_if_a_cut_flag_leaks_through():
    # Belt and braces: the pipeline already forces weight_cut_risk false for a
    # minor. This asserts the module itself would still not emit the alarm-tier
    # protocol section body if that upstream guard were bypassed.
    block = generate_nutrition_block(flags=_flags(is_minor=True, weight_cut_risk=False))

    assert MINOR_WEIGHT_CUT_NOTE in block
    assert "Weight Cut Protocol Triggered" not in block
    assert contains_blocked_minor_guidance(block) is False


def test_computed_targets_mark_the_cut_as_blocked_for_a_minor():
    targets = compute_nutrition_targets(flags=_flags(is_minor=True, weight_cut_risk=False))

    weight_cut = targets["weight_cut"]
    assert weight_cut["active"] is False
    assert weight_cut["blocked"] is True
    assert weight_cut["blocked_reason"] == "under_18"
    # No coach-gated acute-cut protocol is produced at all.
    assert "acute_cut_protocol" not in targets.get("coach_gated", {})


def test_computed_support_carries_the_minor_safeguard_to_stage_2():
    support = build_computed_support(flags=_flags(is_minor=True, weight_cut_risk=False))

    assert support["safeguards"]["is_minor"] is True


def test_the_pipeline_forces_the_cut_flag_off_for_a_minor():
    import logging

    from fightcamp.plan_pipeline_runtime import build_runtime_context

    def _payload(is_minor: bool) -> dict:
        return {
            "is_minor": is_minor,
            "data": {
                "fields": [
                    {"label": "Full name", "value": "Junior Athlete"},
                    {"label": "Age", "value": "16" if is_minor else "27"},
                    {"label": "Weight (kg)", "value": "72.5"},
                    {"label": "Target Weight (kg)", "value": "66.0"},
                    {"label": "Fighting Style (Technical)", "value": "boxing"},
                    {"label": "Weekly Training Frequency", "value": "3"},
                    {"label": "Training Availability", "value": "Monday, Wednesday, Friday"},
                    {"label": "Fatigue Level", "value": "low"},
                ]
            },
            "no_scheduled_fight": True,
        }

    def _context(is_minor: bool):
        return build_runtime_context(
            plan_input=PlanInput.from_payload(_payload(is_minor)),
            random_seed=1,
            logger=logging.getLogger(__name__),
        )

    adult = _context(False)
    minor = _context(True)

    # ~9% is a serious cut for an adult and is scored as one.
    assert adult.weight_cut_risk_flag is True
    assert adult.weight_cut_pct_val > 0

    assert minor.weight_cut_risk_flag is False
    assert minor.weight_cut_pct_val == 0.0
    assert minor.training_context.is_minor is True
    assert minor.training_context.to_flags()["is_minor"] is True


# ---------------------------------------------------------------------------
# Layer 3: output scrubbing and the structured-plan audit
# ---------------------------------------------------------------------------


def test_scrubbing_removes_a_blocked_section_and_leaves_the_rest():
    text = (
        "Nutrition Module\n"
        "- 3 core meals + 2-3 snacks daily\n"
        "- Hydration: 0.03-0.04 l/kg\n"
        "\n"
        "**Weight Cut Protocol Triggered:**\n"
        "- Active weight cut (~6.5%): risk band SEVERE\n"
        "- Rehydrate steadily with fluids + electrolytes\n"
        "\n"
        "**Meal Timing Guidelines:**\n"
        "- Pre-training: light carbs 30-60 min before\n"
    )

    scrubbed = scrub_minor_guidance(text)

    assert "3 core meals" in scrubbed
    assert "Meal Timing Guidelines" in scrubbed
    assert "Pre-training" in scrubbed
    # The heading takes its whole body with it — leaving the heading behind
    # would still tell a child a cut protocol exists.
    assert "Weight Cut Protocol Triggered" not in scrubbed
    assert "Rehydrate" not in scrubbed
    assert MINOR_WEIGHT_CUT_NOTE in scrubbed
    assert contains_blocked_minor_guidance(scrubbed) is False


def test_scrubbing_clean_text_changes_nothing():
    text = "Nutrition Module\n- Hydration: 0.03-0.04 l/kg\n"

    assert scrub_minor_guidance(text) == text


def test_scrubbing_a_payload_tree_keeps_safe_sentences():
    node = {
        "notes": [
            "Keep the pace conversational. Cut water for 24 hours before weigh-in. Sleep eight hours.",
        ],
        "title": "Taper week",
    }

    scrubbed = scrub_minor_guidance_tree(node)

    assert scrubbed["title"] == "Taper week"
    assert "Keep the pace conversational." in scrubbed["notes"][0]
    assert "Sleep eight hours." in scrubbed["notes"][0]
    assert contains_blocked_minor_guidance(scrubbed["notes"][0]) is False


def test_a_wholly_blocked_string_becomes_the_safe_note():
    assert scrub_minor_guidance_tree("Cut water for 24 hours.") == MINOR_WEIGHT_CUT_NOTE


def test_the_audit_blocks_a_card_that_shows_a_minor_cut_guidance():
    structured_plan = {
        "nutrition": {"fight_week_guidance": "Begin the water cut on Thursday evening."},
    }
    support = {"safeguards": {"is_minor": True}}

    findings = detect_minor_guidance_leakage(structured_plan, is_minor=True)
    assert findings
    assert all(is_blocking_finding(finding) for finding in findings)

    # And the same result through the top-level audit the publisher calls.
    audited = audit_structured_plan(structured_plan, support)
    assert any("under-18" in finding for finding in audited)
    assert any(is_blocking_finding(finding) for finding in audited)


def test_the_audit_leaves_an_adults_card_alone():
    structured_plan = {
        "nutrition": {"fight_week_guidance": "Begin the water cut Thursday."},
    }

    assert audit_structured_plan(structured_plan, {"safeguards": {"is_minor": False}}) == []
    # An absent safeguards block means "not a minor" — Stage 1 always supplies it.
    assert audit_structured_plan(structured_plan, {}) == []


# ---------------------------------------------------------------------------
# End to end through the API
# ---------------------------------------------------------------------------


def _minor_dob() -> str:
    today = date.today()
    return today.replace(year=today.year - 15).isoformat()


def _seed_plan(store, plan_text: str) -> dict:
    request = _build_request()
    intake = store.create_intake(DEFAULT_ATHLETE_USER.user_id, request)
    return store.create_plan(
        athlete_id=DEFAULT_ATHLETE_USER.user_id,
        intake_id=intake["id"],
        request=request,
        result={"plan_text": plan_text, "status": "generated"},
    )


def test_a_served_plan_is_scrubbed_for_a_minor_but_not_for_an_adult():
    from api.models import PlanDetail, PlanOutputs, PlanSafetyState
    from api.services.minor_plan_guard import apply_minor_plan_guard

    plan = PlanDetail(
        plan_id="plan-1",
        athlete_id="athlete-1",
        full_name="Junior Athlete",
        created_at="2026-08-01T00:00:00+00:00",
        outputs=PlanOutputs(
            plan_text=(
                "Camp plan\n"
                "- Hydration: 0.03-0.04 l/kg\n"
                "- Cut water for the final 24 hours before weigh-in\n"
            )
        ),
        safety_state=PlanSafetyState(
            state="plan_ready", status_chip="Ready", header="Ready", subtext=""
        ),
    )

    adult_view = apply_minor_plan_guard(plan, is_minor=False)
    assert adult_view.outputs.plan_text == plan.outputs.plan_text

    minor_view = apply_minor_plan_guard(plan, is_minor=True)
    assert "Hydration" in minor_view.outputs.plan_text
    assert "Cut water" not in minor_view.outputs.plan_text
    assert MINOR_WEIGHT_CUT_NOTE in minor_view.outputs.plan_text


def test_a_minor_reading_a_stored_plan_does_not_receive_blocked_guidance():
    client, store, _ = _build_client()
    store.record_compliance_acceptance(
        DEFAULT_ATHLETE_USER.user_id,
        date_of_birth=_minor_dob(),
        accept_terms=True,
        health_data_consent=True,
    )
    plan = _seed_plan(
        store,
        "Camp plan\n"
        "- Hydration: 0.03-0.04 l/kg\n"
        "- Cut water for the final 24 hours before weigh-in\n",
    )

    response = client.get(f"/api/plans/{plan['id']}", headers=ATHLETE)

    assert response.status_code == 200
    plan_text = response.json()["outputs"]["plan_text"]
    assert contains_blocked_minor_guidance(plan_text) is False
    assert MINOR_WEIGHT_CUT_NOTE in plan_text


def test_an_adult_reading_the_same_plan_still_sees_the_cut_guidance():
    client, store, _ = _build_client()
    plan = _seed_plan(
        store, "Camp plan\n- Cut water for the final 24 hours before weigh-in\n"
    )

    response = client.get(f"/api/plans/{plan['id']}", headers=ATHLETE)

    assert response.status_code == 200
    assert "Cut water" in response.json()["outputs"]["plan_text"]


def test_an_admin_reviewing_a_minors_plan_sees_it_unscrubbed():
    # Admins review what was actually generated. They have no date of birth on
    # file, which fails safe to "minor" — that must not redact their review view.
    client, store, _ = _build_client()
    store.record_compliance_acceptance(
        DEFAULT_ATHLETE_USER.user_id,
        date_of_birth=_minor_dob(),
        accept_terms=True,
        health_data_consent=True,
    )
    plan = _seed_plan(store, "Camp plan\n- Cut water for the final 24 hours\n")

    response = client.get(
        f"/api/plans/{plan['id']}", headers={"Authorization": "Bearer admin-token"}
    )

    assert response.status_code == 200
    assert "Cut water" in response.json()["outputs"]["plan_text"]


# ---------------------------------------------------------------------------
# Data minimisation: no target weight collected from an under-18
# ---------------------------------------------------------------------------


def test_target_weight_is_stripped_from_a_minors_intake_payload():
    payload = {
        "athlete": {"full_name": "Junior", "weight_kg": 62.0, "target_weight_kg": 57.0},
        "shared_camp_context": {"target_weight_kg": 57.0, "target_weight_range_kg": [56.0, 57.5]},
    }

    cleaned = strip_minor_target_weight(payload)

    assert "target_weight_kg" not in cleaned["athlete"]
    assert "target_weight_kg" not in cleaned["shared_camp_context"]
    assert "target_weight_range_kg" not in cleaned["shared_camp_context"]
    # Current bodyweight survives: it drives macros and hydration, which
    # under-18 athletes still receive, so it has a purpose of its own.
    assert cleaned["athlete"]["weight_kg"] == 62.0
    # The caller's payload is untouched.
    assert payload["athlete"]["target_weight_kg"] == 57.0


def test_stripping_leaves_an_unrelated_payload_alone():
    payload = {"athlete": {"full_name": "Junior", "weight_kg": 62.0}}
    assert strip_minor_target_weight(payload) == payload
    assert strip_minor_target_weight(None) is None


def test_a_minors_generation_request_never_stores_a_target_weight():
    client, store, _ = _build_client()
    store.record_compliance_acceptance(
        DEFAULT_ATHLETE_USER.user_id,
        date_of_birth=_minor_dob(),
        accept_terms=True,
        health_data_consent=True,
    )

    response = client.post(
        "/api/plans/generate",
        headers=ATHLETE,
        json=_build_request().model_dump(mode="json"),
    )

    assert response.status_code == 202
    # _build_request carries target_weight_kg=70.0; it must not reach the stored
    # generation payload or the intake derived from it.
    job = store.generation_jobs[response.json()["job_id"]]
    assert job["request_payload"]["athlete"]["target_weight_kg"] is None
    intake = store.get_latest_intake(DEFAULT_ATHLETE_USER.user_id)
    assert intake["intake"]["athlete"]["target_weight_kg"] is None


def test_an_adults_generation_request_keeps_the_target_weight():
    client, store, _ = _build_client()

    response = client.post(
        "/api/plans/generate",
        headers=ATHLETE,
        json=_build_request().model_dump(mode="json"),
    )

    assert response.status_code == 202
    job = store.generation_jobs[response.json()["job_id"]]
    assert job["request_payload"]["athlete"]["target_weight_kg"] == 70.0


def test_a_minors_onboarding_draft_never_stores_a_target_weight():
    client, store, _ = _build_client()
    store.record_compliance_acceptance(
        DEFAULT_ATHLETE_USER.user_id,
        date_of_birth=_minor_dob(),
        accept_terms=True,
        health_data_consent=True,
    )

    response = client.patch(
        "/api/onboarding/draft",
        headers=ATHLETE,
        json={
            "onboarding_draft": {
                "current_step": 2,
                "athlete": {"full_name": "Junior", "weight_kg": 62.0, "target_weight_kg": 57.0},
            }
        },
    )

    assert response.status_code == 200
    draft = store.profiles[DEFAULT_ATHLETE_USER.user_id]["onboarding_draft"]
    assert "target_weight_kg" not in draft["athlete"]
    assert draft["athlete"]["weight_kg"] == 62.0


# ---------------------------------------------------------------------------
# The refusal note, reworded by Stage 2, must not sink an under-18 card
# ---------------------------------------------------------------------------


def test_a_shortened_refusal_note_is_not_read_as_cut_guidance():
    first_sentence = MINOR_WEIGHT_CUT_NOTE.split(". ")[0] + "."

    assert blocked_guidance_reasons(first_sentence) == []
    assert blocked_guidance_reasons(first_sentence.rstrip(".").lower()) == []
    # Re-wrapped across lines, as a card label or markdown bullet may be.
    assert blocked_guidance_reasons(first_sentence.replace(" and ", "\nand ")) == []
    # The note does not launder real guidance that sits next to it.
    assert blocked_guidance_reasons(f"{first_sentence} Sit in the sauna Friday.") == [
        "sweat_protocol"
    ]


def _minor_outcome(summary: str):
    from api.structured_plan_generation import build_structured_plan_outcome
    from test_structured_plan_models import _valid_plan
    from test_structured_plan_safety import _faithful_source

    support = build_computed_support(flags=_flags(is_minor=True, weight_cut_risk=False))
    plan = _valid_plan()
    plan["nutrition"]["summary"] = summary
    return build_structured_plan_outcome(
        plan, raw_markdown=_faithful_source(plan), computed_support=support
    )


def test_a_minors_card_carrying_a_shortened_refusal_note_publishes():
    first_sentence = MINOR_WEIGHT_CUT_NOTE.split(". ")[0] + "."

    outcome = _minor_outcome(f"Eat to fuel training. {first_sentence}")

    assert outcome.status != "blocked_by_safety_audit", outcome.errors
    assert outcome.structured_plan is not None


def test_a_minors_card_with_real_cut_guidance_is_still_blocked():
    # Fail closed: genuine protocol wording is never scrubbed into a
    # publishable card — the whole card is blocked.
    outcome = _minor_outcome("Eat to fuel training. Sit in the sauna Friday.")

    assert outcome.status == "blocked_by_safety_audit"
    assert outcome.structured_plan is None
    assert any("sweat_protocol" in error for error in outcome.errors), outcome.errors


# ---------------------------------------------------------------------------
# The conversion model never sees cut wording; the note is added in code
# ---------------------------------------------------------------------------


def _conversion_prompt(*, is_minor: bool, weight: float | None = 72.5) -> str:
    from api.structured_plan_generation import build_structured_plan_prompt

    # _flags defaults to the TAPER phase, where the weight-making wording lived.
    flags = _flags(is_minor=is_minor, weight_cut_risk=False, weight=weight)
    markdown = "# PLAN\n\n**Nutrition:**\n" + generate_nutrition_block(flags=flags)
    return build_structured_plan_prompt(
        plan_markdown=markdown,
        planning_brief={"computed_support": build_computed_support(flags=flags)},
    )


@pytest.mark.parametrize("weight", [72.5, None])
def test_a_minors_conversion_prompt_carries_no_cut_wording(weight):
    from api.structured_plan_generation import (
        _MINOR_CONVERSION_RULE,
        _ROOT_SKELETON,
        _STRUCTURED_PLAN_RULES,
    )

    prompt = _conversion_prompt(is_minor=True, weight=weight)

    assert _MINOR_CONVERSION_RULE in prompt
    assert MINOR_WEIGHT_CUT_NOTE.split(". ")[0] not in prompt
    assert "coach_gated" not in prompt.replace(_STRUCTURED_PLAN_RULES, "")
    # Everything the model is asked to convert is free of cut vocabulary; only
    # the fixed rules (and the rule forbidding it) name it.
    variable = (
        prompt.replace(_STRUCTURED_PLAN_RULES, "")
        .replace(_ROOT_SKELETON, "")
        .replace(_MINOR_CONVERSION_RULE, "")
    )
    assert blocked_guidance_reasons(variable) == []
    # blocked_guidance_reasons only knows acute-cut protocols; weight-making
    # wording and the weight_cut key are checked directly.
    lowered = variable.lower()
    for phrase in ("weight making", "making weight", "weigh-in", "reduce fiber", "weight_cut"):
        assert phrase not in lowered, phrase


def test_an_adults_conversion_prompt_is_unchanged():
    from api.structured_plan_generation import _MINOR_CONVERSION_RULE, _STRUCTURED_PLAN_RULES

    prompt = _conversion_prompt(is_minor=False)

    assert _MINOR_CONVERSION_RULE not in prompt
    # Nutrition guidance (and its coach-gated dosing) never reaches Stage 2;
    # an adult still gets the weight-cut risk bands.
    assert "coach_gated" not in prompt.replace(_STRUCTURED_PLAN_RULES, "")
    assert '"weight_cut":{"by_phase"' in prompt
    # An adult's taper still gets its weight-making guidance.
    assert "weight making" in prompt
    assert "days before weigh-in" in prompt
    assert "reduce fiber 1-2 days out" in prompt


def test_a_published_minor_card_carries_the_note_verbatim_once():
    outcome = _minor_outcome("Eat to fuel training.")

    assert outcome.status != "blocked_by_safety_audit", outcome.errors
    summary = outcome.structured_plan["nutrition"]["summary"]
    assert summary == f"Eat to fuel training. {MINOR_WEIGHT_CUT_NOTE}"

    already_noted = _minor_outcome(f"Eat to fuel training. {MINOR_WEIGHT_CUT_NOTE}")
    assert already_noted.structured_plan["nutrition"]["summary"].count(
        MINOR_WEIGHT_CUT_NOTE
    ) == 1


def test_an_adults_card_gets_no_minor_note():
    from api.structured_plan_generation import build_structured_plan_outcome
    from test_structured_plan_models import _valid_plan
    from test_structured_plan_safety import _faithful_source

    plan = _valid_plan()
    outcome = build_structured_plan_outcome(
        plan,
        raw_markdown=_faithful_source(plan),
        computed_support=build_computed_support(
            flags=_flags(is_minor=False, weight_cut_risk=False)
        ),
    )

    assert outcome.structured_plan is not None
    assert MINOR_WEIGHT_CUT_NOTE not in outcome.structured_plan["nutrition"]["summary"]
