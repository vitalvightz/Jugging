"""Current seconds and explicit session types outrank a session time budget."""
import pytest

from fightcamp.session_workload import structured_stacking_findings
from tests.test_substantial_endurance_stacking import _structured


@pytest.mark.parametrize("budget_field", ["duration", "planned_duration"])
@pytest.mark.parametrize("seconds,conflict", [(30, False), (1800, True)])
def test_current_work_seconds_are_not_replaced_by_the_session_budget(budget_field, seconds, conflict):
    plan = _structured("Running support")
    session = plan["weeks"][0]["days"][0]["sessions"][1]
    session[budget_field] = {"value": 45, "unit": "minutes"}
    block = session["blocks"][0]
    for field in ("duration", "prescription_text", "work", "distance"):
        block.pop(field, None)
    block.update(work_sec=seconds, modality="run")
    assert bool(structured_stacking_findings(plan)) is conflict


@pytest.mark.parametrize("session_type", ["strength", "strength_power", "skill"])
def test_non_endurance_session_does_not_inherit_endurance_from_modality_metadata(session_type):
    plan = _structured()
    session = plan["weeks"][0]["days"][0]["sessions"][1]
    session.update(session_type=session_type, title="Strength session", modality="run",
                   planned_duration={"value": 45, "unit": "minutes"}, blocks=[])
    assert not structured_stacking_findings(plan)
