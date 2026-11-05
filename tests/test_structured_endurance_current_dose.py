"""Current structured doses must take precedence over bank prescriptions."""
import pytest

from api.structured_plan_safety import audit_structured_plan
from fightcamp.session_workload import UNSAFE_STACKING_CODE, structured_stacking_findings
from tests.test_substantial_endurance_stacking import _structured


@pytest.mark.parametrize("prescription", ["10 min easy run", "1 km continuous", "6 x 800 m"])
def test_current_prescription_does_not_reuse_the_bank_five_k(prescription):
    plan = _structured()
    plan["weeks"][0]["days"][0]["sessions"][1]["blocks"][0]["prescription_text"] = prescription
    assert not structured_stacking_findings(plan)
    assert not any(UNSAFE_STACKING_CODE in finding for finding in audit_structured_plan(plan))


@pytest.mark.parametrize("rounds,conflict", [(1, True), (6, False)])
def test_round_count_distinguishes_continuous_work_from_intervals(rounds, conflict):
    plan = _structured()
    plan["weeks"][0]["days"][0]["sessions"][1]["blocks"][0]["rounds"] = rounds
    assert bool(structured_stacking_findings(plan)) is conflict
    assert any(UNSAFE_STACKING_CODE in finding for finding in audit_structured_plan(plan)) is conflict
