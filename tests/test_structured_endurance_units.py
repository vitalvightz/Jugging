"""Structured quantities and authored text retain their physical units."""
import pytest

from api.structured_plan_safety import audit_structured_plan
from fightcamp.session_workload import UNSAFE_STACKING_CODE, substantial_endurance, structured_stacking_findings
from tests.test_substantial_endurance_stacking import _structured


@pytest.mark.parametrize("name,value,unit,conflict", [
    ("Continuous run", 5, "mi", True),
    ("Continuous run", 3, "kilometer", True),
    ("Continuous run", 3, "kilometre", True),
    ("Continuous swim", 1000, "yd", False),
    ("Continuous swim", 1000, "yard", False),
    ("Continuous swim", 1000, "yards", False),
])
def test_supported_distance_units_preserve_the_endurance_threshold(name, value, unit, conflict):
    plan = _structured(name, distance={"value": value, "unit": unit})
    assert bool(structured_stacking_findings(plan)) is conflict
    assert any(UNSAFE_STACKING_CODE in finding for finding in audit_structured_plan(plan)) is conflict


@pytest.mark.parametrize("unit", ["second", "secs"])
def test_short_second_quantities_are_not_interpreted_as_minutes(unit):
    plan = _structured(duration={"value": 600, "unit": unit})
    assert not structured_stacking_findings(plan)


def test_current_mile_prescription_is_also_measured_outside_structured_cards():
    assert substantial_endurance({"name": "Continuous run", "effective_prescription": "5 mi continuous"})


def test_duration_minute_alias_is_not_reinterpreted_as_metres():
    plan = _structured("Continuous run", duration={"value": 45, "unit": "m"})
    assert structured_stacking_findings(plan)
