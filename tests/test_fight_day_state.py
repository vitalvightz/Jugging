"""Fight day (D-0) is a state, not a session.

Covers the Today command view on the fight date, the completion gate, the
post-weigh-in weight status, and the injury hold naming a conflict only when one
was actually found.
"""

from datetime import date
from uuid import uuid4

import pytest

from api.contracts.injury_policy import reconcile_session_prescription, resolve_injury_policy
from api.models import NutritionBodyweightLogEntry
from api.nutrition_workspace import _fight_day_cut_status
from fightcamp.rehab_clinical import load_clinical_policies
from fightcamp.rehab_protocols import get_rehab_bank
from tests.support import _build_client

ATHLETE = {"Authorization": "Bearer athlete-token"}
PLAN_ID = "11111111-1111-1111-1111-111111111111"


def _seed_fight_day_plan(client, store) -> str:
    store.plans[PLAN_ID] = {
        "id": PLAN_ID,
        "athlete_id": "athlete-1",
        "status": "ready",
        "plan_name": "Camp A",
        "created_at": "2026-06-01T00:00:00+00:00",
    }
    store.set_active_plan_id("athlete-1", PLAN_ID)
    training_day = client.get("/api/today", headers=ATHLETE).json()["today"]["training_day"]
    store.plans[PLAN_ID]["fight_date"] = training_day
    return training_day


class TestFightDayCommandView:
    def test_fight_day_has_no_session_and_its_own_guidance(self):
        client, store, _ = _build_client()
        _seed_fight_day_plan(client, store)
        today = client.get("/api/today", headers=ATHLETE).json()
        assert today["today"]["fight_day"] is True
        assert today["today"]["session_scope"] == "none"
        assert today["today"]["next_session"] == {}
        assert today["live_prescription"] is None
        assert today["session_hold"] is None
        assert today["today"]["decision_tier"] == "green"
        assert today["today"]["recommendation_reason"].splitlines()[0] == "Fight day"
        assert today["today"]["recommendation_context_labels"] == ["Fight day"]

    def test_other_days_are_not_fight_day(self):
        client, store, _ = _build_client()
        _seed_fight_day_plan(client, store)
        store.plans[PLAN_ID]["fight_date"] = "2099-01-01"
        assert client.get("/api/today", headers=ATHLETE).json()["today"]["fight_day"] is False

    @pytest.mark.parametrize("status", ["started", "done", "skipped"])
    def test_no_session_can_be_logged_on_fight_day(self, status):
        client, store, _ = _build_client()
        _seed_fight_day_plan(client, store)
        resp = client.post("/api/today/session-completion", headers=ATHLETE,
                           json={"plan_id": PLAN_ID, "session_id": "s1", "status": status,
                                 **({"modification_reason": "other"} if status == "skipped" else {})})
        assert resp.status_code == 409
        assert "fight day" in resp.json()["detail"]


def _entry(day: str, kg: float, time: str | None = None) -> NutritionBodyweightLogEntry:
    return NutritionBodyweightLogEntry(date=day, weight_kg=kg, time=time)


class TestFightDayWeightStatus:
    FIGHT = date(2026, 11, 22)
    LIVE = ("on_track", 1.0, None)

    def status(self, weigh_in_type, entries, days=0, weigh_in_time="18:00"):
        return _fight_day_cut_status(self.LIVE, days_until_fight=days, weigh_in_type=weigh_in_type,
                                     weigh_in_time=weigh_in_time, fight_date=self.FIGHT,
                                     target_weight_kg=65.0, entries=entries)

    def test_before_fight_day_the_cut_is_live(self):
        assert self.status("day_before", [], days=1) == self.LIVE

    def test_same_day_weigh_in_keeps_the_cut_live_on_fight_day(self):
        assert self.status("same_day", []) == self.LIVE

    def test_any_weigh_in_day_reading_on_target_is_made_weight(self):
        entries = [_entry("2026-11-21", 66.1, "07:00"), _entry("2026-11-21", 64.8, "18:05"), _entry("2026-11-22", 66.0)]
        assert self.status("day_before", entries) == ("made_weight", 0.0, None)

    def test_a_reading_at_the_weigh_in_time_over_target_is_missed_weight(self):
        entries = [_entry("2026-11-21", 65.4, "18:10")]
        assert self.status("day_before", entries) == ("missed_weight", 0.4, None)

    def test_a_morning_reading_before_the_weigh_in_is_never_missed_weight(self):
        entries = [_entry("2026-11-21", 66.1, "07:00")]
        assert self.status("day_before", entries) == ("weigh_in_done", None, None)

    @pytest.mark.parametrize("weigh_in_time", [None, "", "evening"])
    def test_without_a_weigh_in_time_an_over_target_reading_is_never_missed_weight(self, weigh_in_time):
        entries = [_entry("2026-11-21", 65.4, "18:10")]
        assert self.status("day_before", entries, weigh_in_time=weigh_in_time) == ("weigh_in_done", None, None)

    def test_an_untimed_reading_is_never_missed_weight(self):
        assert self.status("day_before", [_entry("2026-11-21", 65.4)]) == ("weigh_in_done", None, None)

    @pytest.mark.parametrize("weigh_in_type", [None, "informal", "day_before"])
    def test_without_a_weigh_in_reading_the_cut_is_finished(self, weigh_in_type):
        # A rehydrated fight-day reading is never counted as kg still to lose.
        assert self.status(weigh_in_type, [_entry("2026-11-22", 66.0)]) == ("weigh_in_done", None, None)


def _shoulder_decision():
    row = dict(id=str(uuid4()), episode_id=str(uuid4()), athlete_id="a", body_area="Shoulder",
               description="Left shoulder tightness", body_region="shoulder", canonical_location="shoulder",
               injury_type="tightness", severity="mild", status="open", side="left")
    return row, resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(), phase="TAPER")


@pytest.mark.parametrize(("blocks", "expected"), [
    ([], "The injury-loading details for your planned training are unavailable."),
    ([{"block_id": "b", "block_type": "protocol", "display_name": "Fight day"}],
     "The injury-loading details for your planned training are unavailable."),
    ([{"block_id": "b", "block_type": "strength", "display_name": "Press", "mechanical_load_regions": ["shoulder"],
       "tags": [], "contact_level": "none"}],
     "Your planned training conflicts with the current injury restrictions."),
])
def test_hold_names_a_conflict_only_when_one_was_found(blocks, expected):
    row, decision = _shoulder_decision()
    assert decision["outcome"] == "prescribed_rehab"
    session = {"session_id": "s", "session_type": "strength", "title": "Session", "blocks": blocks}
    live = reconcile_session_prescription(session, decisions=[decision], plan_id="p", training_day="2026-11-22",
                                          injuries=[row])
    assert live["training_hold_reason"] == expected
