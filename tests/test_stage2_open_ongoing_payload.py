import fightcamp.stage2_payload_open_ongoing as open_ongoing
from fightcamp.stage2_payload_open_ongoing import (
    _uses_open_ongoing_payload,
    build_open_ongoing_payload,
)
from fightcamp.tactical_watch_library import (
    TacticalWatchBankExhausted,
    extract_tactical_style,
    select_tactical_watch,
)


def test_open_ongoing_route_when_no_fight_date():
    athlete = {"sport": "boxing", "days_until_fight": None, "fight_date": None, "next_fight_date": None}
    assert _uses_open_ongoing_payload(athlete) is True
    payload = build_open_ongoing_payload(athlete_model=athlete)
    assert payload["payload_mode"] == "open_ongoing_payload"
    assert payload["render_mode"] == "open_ongoing_system"
    assert isinstance(payload.get("open_plan_spec"), dict)


def test_open_ongoing_spec_required_sections_and_banned_terms():
    payload = build_open_ongoing_payload(athlete_model={"days_until_fight": None})
    spec = payload["open_plan_spec"]
    required = [
        "Immediate Coach Summary",
        "Current Training Rules",
        "Weekly Rhythm",
        "Session Cards",
        "4-Week Development Block",
        "Progression Rules",
        "Priority Hierarchy",
        "Adjustment Rules",
        "Rehab / Red Flags",
        "4-Week Reassessment Gate",
    ]
    assert spec.get("structure") == required
    forbidden = set(spec.get("forbidden_terms") or [])
    for token in ("GPP", "SPP", "TAPER", "D-", "fight week", "fight-day", "countdown"):
        assert token in forbidden


def test_no_scheduled_fight_does_not_override_real_fight_date():
    athlete = {
        "no_scheduled_fight": True,
        "fight_date": "2026-06-01",
        "days_until_fight": 17,
    }
    assert _uses_open_ongoing_payload(athlete) is False


def test_numeric_string_days_until_fight_does_not_route_open():
    athlete = {
        "days_until_fight": "17",
        "fight_date": "",
        "next_fight_date": "",
    }
    assert _uses_open_ongoing_payload(athlete) is False


def test_open_plan_carries_weekly_tactical_watch_rotation():
    payload = build_open_ongoing_payload(
        athlete_model={
            "sport": "boxing",
            "fighting_style": "counter striker",
            "days_until_fight": None,
        }
    )
    watch_spec = payload["open_plan_spec"]["tactical_watch"]
    assert watch_spec["label"] == "Tactical Focus"
    assert watch_spec["placement"] == "session_cards"
    assert watch_spec["zero_physical_load"] is True

    rotation = watch_spec["weekly_rotation"]
    assert [entry["week"] for entry in rotation] == [1, 3]
    # One distinct watch per week of the renewable block.
    assert len({entry["tactical_watch_key"] for entry in rotation}) == 2
    for entry in rotation:
        assert entry["zero_physical_load"] is True
        assert entry["duration_min"] > 0
        assert entry["display_text"].startswith("Why: ")
        assert entry["name"] in entry["display_text"]
        assert entry["tactical_watch"]["instructions"]


def test_open_plan_tactical_watch_hides_phase_and_camp_vocabulary():
    payload = build_open_ongoing_payload(
        athlete_model={"sport": "mma", "fighting_style": "pressure fighter"}
    )
    spec = payload["open_plan_spec"]
    forbidden = [term.lower() for term in spec["forbidden_terms"]]
    for entry in spec["tactical_watch"]["weekly_rotation"]:
        visible = entry["display_text"].lower()
        assert "camp" not in visible
        for term in forbidden:
            assert term not in visible
        block = entry["tactical_watch"]
        # Selection internals never travel with an athlete-facing open-plan card.
        for internal in ("key", "style", "phase", "sports", "fallback_reason"):
            assert internal not in block
        assert "context" not in block["mindset"]


def test_open_plan_render_rules_cover_tactical_watch():
    rules = " ".join(
        build_open_ongoing_payload(athlete_model={})["open_plan_spec"]["render_rules"]
    )
    assert "weekly_rotation is the complete Tactical Watch set" in rules
    assert "never invent, repeat or fill a week it does not cover" in rules
    assert "zero physical load" in rules.lower()
    # The contract must never name a fixed week range the rotation may not reach.
    assert "Week 2-4" not in rules


def test_open_plan_render_rules_never_promise_weeks_the_rotation_lacks(monkeypatch):
    """A short bank must not leave the finalizer told to render missing weeks.

    The rotation is capped by the bank, so the render contract is driven by
    weekly_rotation/weeks_covered alone: nothing in it may instruct the model to
    produce a Tactical Watch for a week the deterministic side did not select.
    """
    _stub_bank(monkeypatch, limit=2)
    spec = build_open_ongoing_payload(athlete_model={"sport": "boxing"})["open_plan_spec"]
    assert spec["tactical_watch"]["weeks_covered"] == 2
    for rule in spec["render_rules"]:
        for absent_week in ("Week 3", "Week 4", "Week 2-4"):
            assert absent_week not in rule


def _stub_bank(monkeypatch, *, limit: int) -> None:
    """Make the Tactical Watch bank run dry after ``limit`` distinct selections."""

    def _limited(style, phase, used_keys=None):
        if len(set(used_keys or ())) >= limit:
            raise TacticalWatchBankExhausted("stubbed exhaustion")
        return select_tactical_watch(style, phase, used_keys)

    monkeypatch.setattr(open_ongoing, "select_tactical_watch", _limited)


def test_open_plan_tactical_watch_stops_at_bank_exhaustion(monkeypatch):
    _stub_bank(monkeypatch, limit=2)
    spec = build_open_ongoing_payload(
        athlete_model={"sport": "boxing", "fighting_style": "counter striker"}
    )["open_plan_spec"]["tactical_watch"]

    rotation = spec["weekly_rotation"]
    # Short bank: fewer weeks, never a repeated or invented watch to pad to four.
    assert [entry["week"] for entry in rotation] == [1, 3]
    assert spec["weeks_covered"] == 2
    assert len({entry["tactical_watch_key"] for entry in rotation}) == 2


def test_open_plan_tolerates_a_bank_with_nothing_for_the_athlete(monkeypatch):
    _stub_bank(monkeypatch, limit=0)
    spec = build_open_ongoing_payload(athlete_model={"sport": "boxing"})["open_plan_spec"]

    # No watch is a rendering no-op, not a generation failure: the rest of the
    # open plan still builds.
    assert spec["tactical_watch"]["weekly_rotation"] == []
    assert spec["tactical_watch"]["weeks_covered"] == 0
    assert spec["structure"][0] == "Immediate Coach Summary"


def test_weeks_covered_matches_the_rotation_length():
    spec = build_open_ongoing_payload(
        athlete_model={"sport": "mma", "fighting_style": "grappler"}
    )["open_plan_spec"]["tactical_watch"]
    assert spec["weeks_covered"] == len(spec["weekly_rotation"])


def test_style_selection_is_used_for_the_open_plan_rotation():
    athlete = {"sport": "boxing", "fighting_style": "counter striker"}
    expected = select_tactical_watch(extract_tactical_style(athlete), "GPP", set())
    first = build_open_ongoing_payload(athlete_model=athlete)["open_plan_spec"][
        "tactical_watch"
    ]["weekly_rotation"][0]
    assert first["tactical_watch_key"] == expected.key


def test_open_ongoing_render_mode_prompt_defers_to_the_rotation():
    from fightcamp.stage2_payload import _OPEN_ONGOING_RENDER_MODE_INSTRUCTIONS

    prompt = _OPEN_ONGOING_RENDER_MODE_INSTRUCTIONS
    assert "does not always cover all four weeks" in prompt
    assert "Never invent, repeat or fill in a Tactical Watch" in prompt
    # The prompt is the other place a fixed week range could over-promise.
    assert "Week 2-4" not in prompt


def test_open_plan_render_contract_demands_one_exercise_and_authorised_progress_only():
    """Source lines must name one exercise; Progress appears only where work advances.

    An "A or B" main-work line leaves the athlete choosing mid-session and the
    dose ambiguous. A forced "Progress: none this block" line is no longer
    needed to stop the conversion recycling Easier as a progression: source
    projection owns progression_rule (see test_stage2_luna_coaching).
    """
    from fightcamp.stage2_payload import _OPEN_ONGOING_RENDER_MODE_INSTRUCTIONS

    rules = " ".join(
        build_open_ongoing_payload(athlete_model={})["open_plan_spec"]["render_rules"]
    )
    prompt = _OPEN_ONGOING_RENDER_MODE_INSTRUCTIONS

    for text in (rules, prompt):
        assert "one exercise per main-work line" in text.lower()
        assert '"A + B"' in text
        assert "Progress:" in text
        assert "none this block" not in text
        assert "Omit" in text
    assert "9 words" not in prompt
    # The swap/safety split must stay explicit in the authoring prompt.
    assert 'belongs on `Easier:` or `Stop:`, never on `Progress:`' in prompt


def test_prioritised_open_plan_uses_weekly_cadence():
    spec = build_open_ongoing_payload(athlete_model={"mental_prep_priority": True})["open_plan_spec"]["tactical_watch"]
    assert [entry["week"] for entry in spec["weekly_rotation"]] == [1, 2, 3, 4]
    assert spec["attach_to_existing_training"] is True
