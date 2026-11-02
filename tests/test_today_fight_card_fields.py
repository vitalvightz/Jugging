"""Home fight countdown card fields read from the plan's intake."""
from api.services.today_service import _athlete_is_adult, _fight_card_fields_from_intake


class _Store:
    def __init__(self, profile=None, fail=False):
        self.profile, self.fail = profile, fail

    def get_profile_compliance(self, athlete_id):
        if self.fail:
            raise RuntimeError("read failed")
        return self.profile


def test_event_name_and_target_weight_come_from_the_intake():
    fields = _fight_card_fields_from_intake(
        {"event_name": "  London   Regionals ", "athlete": {"target_weight_kg": 90.7, "weight_kg": 95}}
    )
    assert fields == {"event_name": "London Regionals", "target_weight_kg": 90.7}


def test_missing_or_invalid_values_are_omitted():
    assert _fight_card_fields_from_intake({}) == {}
    assert _fight_card_fields_from_intake({"event_name": "  ", "athlete": {"target_weight_kg": None}}) == {}
    for bad in (0, -3, "abc", True, float("nan")):
        assert _fight_card_fields_from_intake({"athlete": {"target_weight_kg": bad}}) == {}


def test_target_weight_is_only_for_confirmed_adults():
    assert _athlete_is_adult(_Store({"date_of_birth": "1995-04-02"}), "a") is True
    assert _athlete_is_adult(_Store({"date_of_birth": "2014-04-02"}), "a") is False
    # A missing profile or failed read fails closed.
    assert _athlete_is_adult(_Store(None), "a") is False
    assert _athlete_is_adult(_Store(fail=True), "a") is False
