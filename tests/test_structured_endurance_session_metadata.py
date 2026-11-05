"""Session metadata must not disappear when exercise titles are generic."""
import pytest

from api.structured_plan_safety import audit_structured_plan
from fightcamp.session_workload import UNSAFE_STACKING_CODE, structured_stacking_findings
from tests.test_substantial_endurance_stacking import _structured


@pytest.mark.parametrize("layout", [
    "block_duration", "block_modality", "session_modality", "conditioning_type", "session_only",
])
def test_generic_endurance_cards_preserve_session_and_block_evidence(layout):
    plan = _structured("Aerobic support")
    session = plan["weeks"][0]["days"][0]["sessions"][1]
    block = session["blocks"][0]
    block["block_type"] = "conditioning"
    if layout == "block_duration":
        block.update(modality="run", duration={"value": 45, "unit": "minutes"})
    else:
        session["planned_duration"] = {"value": 45, "unit": "minutes"}
        if layout == "block_modality":
            block["modality"] = "run"
        elif layout == "session_modality":
            session["modality"] = "run"
        elif layout == "session_only":
            session["blocks"] = []
    assert structured_stacking_findings(plan)
    assert any(UNSAFE_STACKING_CODE in finding for finding in audit_structured_plan(plan))


@pytest.mark.parametrize("layout", ["short_override", "intervals", "technical", "strength"])
def test_session_budget_does_not_replace_current_or_non_endurance_work(layout):
    plan = _structured("Aerobic support")
    session = plan["weeks"][0]["days"][0]["sessions"][1]
    session["planned_duration"] = {"value": 45, "unit": "minutes"}
    block = session["blocks"][0]
    if layout == "short_override":
        session["modality"] = "run"
        block.update(block_type="conditioning", duration={"value": 10, "unit": "minutes"})
    elif layout == "intervals":
        session["modality"] = "run"
        block.update(block_type="conditioning", rounds=6, work={"value": 30, "unit": "seconds"})
    elif layout == "technical":
        session["session_type"] = "skill"
        block["block_type"] = "skill"
    else:
        session["session_type"] = "strength_power"
        block.update(display_name="Barbell Row", block_type="strength")
    assert not structured_stacking_findings(plan)
    assert not any(UNSAFE_STACKING_CODE in finding for finding in audit_structured_plan(plan))
