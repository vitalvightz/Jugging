"""Session guidance remains visible without preventing an athlete from starting."""
import pytest

from tests import test_clinician_clearance_today as clearance_tests
from tests.test_clinician_clearance_today import (
    REHAB, TRAIN, execute, report, session, set_sessions, view,
)


# Register the shared fixture without shadowing an imported name in the test.
context = clearance_tests.context


@pytest.mark.parametrize("scopes", [REHAB, TRAIN])
def test_clinician_guidance_does_not_lock_session_start(context, scopes):
    set_sessions(context, [session("sparring", "Hard sparring", [])])
    report(context, scopes)
    current = view(context)
    live = current.live_prescription
    assert live["safety_hold"]
    assert live["safety_hold_reason"]
    started = execute(context, live)
    assert started["status"] == "started"
    assert started["prescription_snapshot"]["safety_hold"]
