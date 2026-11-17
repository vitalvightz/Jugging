"""Contract-to-storage integration tests for the Today/Overview service.

These exercise api/services/today_service.py against the in-memory FakeStore with
an injected ``now``, so training-day boundaries and recommendation validity are
deterministic without a live clock or database.
"""

import copy
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace
from unittest import mock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from api.models import WeeklyDayEntry, WeeklySchedule
from api.services.plan_schedule import resolve_today_and_next
from api.contracts.completion import completion_landing_state, completion_status_of
from api.services import today_service as today_service_module
from api.services.today_service import (
    _scan_forward_for_next_training,
    build_today_command_view,
    resolve_today_landing,
    submit_today_checkin,
    submit_today_injury_checkin,
    upsert_session_completion,
)
from tests.support import FakeStore

NY = "America/New_York"
ATHLETE = "athlete-1"
PLAN = "11111111-1111-1111-1111-111111111111"
OTHER_PLAN = "22222222-2222-2222-2222-222222222222"


def _store_with_plan(
    plan_id: str = PLAN,
    athlete_id: str = ATHLETE,
    *,
    store_type=FakeStore,
) -> FakeStore:
    store = store_type()
    store.plans[plan_id] = {
        "id": plan_id,
        "athlete_id": athlete_id,
        "status": "ready",
        "plan_name": "Camp A",
        "created_at": "2026-06-01T00:00:00+00:00",
    }
    # The active-plan pointer is authoritative (see api/services/active_plan.py):
    # seeding plans[...] alone leaves the athlete with no active plan, so the
    # command view degrades to the no-plan shape.
    store.set_active_plan_id(athlete_id, plan_id)
    return store


def _attach_intake(
    store: FakeStore,
    intake: dict,
    *,
    plan_id: str = PLAN,
    athlete_id: str = ATHLETE,
    intake_id: str = "intake-1",
) -> None:
    store.intakes.setdefault(athlete_id, []).append(
        {
            "id": intake_id,
            "athlete_id": athlete_id,
            "intake": intake,
            "created_at": "2026-06-01T00:00:00+00:00",
        }
    )
    store.plans[plan_id]["intake_id"] = intake_id


def _taper_planning_brief() -> dict:
    return {
        "weekly_role_map": {
            "weeks": [
                {
                    "phase": "TAPER",
                    "hard_sparring_plan": [],
                }
            ]
        }
    }


def _calendar_training_brief(*, active_offsets: list[int]) -> dict:
    today = date(2026, 6, 18)
    monday = today - timedelta(days=today.weekday())
    calendar_days = [
        {
            "weekday": (monday + timedelta(days=offset)).strftime("%A"),
            "calendar_date": (monday + timedelta(days=offset)).isoformat(),
            "d_day": 30 - offset,
        }
        for offset in range(7)
    ]
    hard_sparring_plan = []
    for offset in active_offsets:
        training_date = today + timedelta(days=offset)
        hard_sparring_plan.append(
            {
                "day": training_date.strftime("%A"),
                "hard_day_class": "managed_hard",
                "effective_load": "technical",
                "status": "technical_skill",
                "reason": "Plan card for the matched training day.",
                "coach_note": "Keep it sharp and clean.",
                "reason_codes": [],
            }
        )
    return {
        "fight_date": "2026-07-18",
        "weekly_role_map": {
            "weeks": [
                {
                    "phase": "SPP",
                    "calendar_days": calendar_days,
                    "hard_sparring_plan": hard_sparring_plan,
                }
            ]
        },
    }


def _monday_rest_tuesday_training_brief() -> dict:
    monday = date(2026, 6, 22)
    calendar_days = [
        {
            "weekday": (monday + timedelta(days=offset)).strftime("%A"),
            "calendar_date": (monday + timedelta(days=offset)).isoformat(),
            "d_day": 25 - offset,
        }
        for offset in range(7)
    ]
    return {
        "fight_date": "2026-07-17",
        "weekly_role_map": {
            "weeks": [
                {
                    "phase": "GPP",
                    "calendar_days": calendar_days,
                    "hard_sparring_plan": [
                        {
                            "day": "Tuesday",
                            "hard_day_class": "primary_hard",
                            "effective_load": "hard",
                            "status": "hard_as_planned",
                            "reason": "Tuesday is the next real session.",
                            "coach_note": "Hard sparring.",
                            "reason_codes": [],
                        }
                    ],
                }
            ]
        },
    }


def _monday_strength_structured_plan() -> dict:
    return {
        "weeks": [
            {
                "phase_label": "GPP",
                "days": [
                    {
                        "date": "2026-06-22",
                        "countdown_label": "D-25",
                        "day_type": "high",
                        "today_card": {
                            "headline": "Posterior chain strength + control",
                        },
                        "sessions": [
                            {
                                "session_id": "2026-06-22-strength",
                                "session_type": "strength",
                                "title": "Posterior chain strength + control",
                                "objective": "Rebuild single-leg control and hinge strength.",
                                "blocks": [],
                            }
                        ],
                    },
                    {
                        "date": "2026-06-23",
                        "countdown_label": "D-24",
                        "day_type": "high",
                        "sessions": [
                            {
                                "session_id": "2026-06-23-sparring",
                                "session_type": "sparring",
                                "title": "Hard sparring",
                                "blocks": [],
                            }
                        ],
                    },
                ],
            }
        ]
    }


def _combined_contact_and_app_structured_plan() -> dict:
    return {
        "weeks": [
            {
                "phase_label": "SPP",
                "days": [
                    {
                        "date": "2026-06-18",
                        "countdown_label": "D-18",
                        "day_type": "high",
                        "today_card": {"headline": "Power Transfer Touch"},
                        "sessions": [
                            {
                                "session_id": "2026-06-18-contact",
                                "session_type": "sparring",
                                "title": "Coach-led boxing - technical only",
                                "blocks": [],
                            },
                            {
                                "session_id": "2026-06-18-app",
                                "session_type": "skill",
                                "title": "Power Transfer Touch",
                                "objective": "Preserve punch speed without fatigue.",
                                "blocks": [
                                    {
                                        "block_type": "skill",
                                        "display_name": "Band-Resisted Jab-Cross Primer",
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "date": "2026-06-25",
                        "countdown_label": "D-11",
                        "day_type": "high",
                        "sessions": [
                            {
                                "session_id": "2026-06-25-hard",
                                "session_type": "sparring",
                                "title": "Hard sparring",
                                "blocks": [],
                            }
                        ],
                    },
                ],
            }
        ]
    }


def _out_of_order_countdown_structured_plan() -> dict:
    return {
        "weeks": [
            {
                "phase_label": "TAPER",
                "days": [
                    {
                        "date": "2026-06-23",
                        "countdown_label": "D-9",
                        "day_type": "high",
                        "sessions": [
                            {
                                "session_id": "2026-06-23-app",
                                "session_type": "strength",
                                "title": "Tuesday app session",
                                "blocks": [],
                            }
                        ],
                    },
                    {
                        "date": "2026-06-27",
                        "countdown_label": "D-5",
                        "day_type": "high",
                        "sessions": [
                            {
                                "session_id": "2026-06-27-sparring",
                                "session_type": "sparring",
                                "title": "Saturday technical sparring",
                                "blocks": [],
                            }
                        ],
                    },
                    {
                        "date": "2026-06-26",
                        "countdown_label": "D-6",
                        "day_type": "high",
                        "sessions": [
                            {
                                "session_id": "2026-06-26-app",
                                "session_type": "app_session",
                                "title": "Friday app session",
                                "blocks": [],
                            }
                        ],
                    },
                ],
            }
        ]
    }


def _tuesday_today_saturday_technical_brief() -> dict:
    tuesday = date(2026, 6, 23)
    monday = tuesday - timedelta(days=tuesday.weekday())
    calendar_days = [
        {
            "weekday": (monday + timedelta(days=offset)).strftime("%A"),
            "calendar_date": (monday + timedelta(days=offset)).isoformat(),
            "d_day": 9 - offset,
        }
        for offset in range(7)
    ]
    return {
        "fight_date": "2026-07-02",
        "weekly_role_map": {
            "weeks": [
                {
                    "phase": "SPP",
                    "calendar_days": calendar_days,
                    "hard_sparring_plan": [
                        {
                            "day": "Tuesday",
                            "hard_day_class": "managed_hard",
                            "effective_load": "reduced",
                            "status": "technical_skill",
                            "reason": "Tuesday app session.",
                            "coach_note": "Light power and balance.",
                            "reason_codes": [],
                        },
                        {
                            "day": "Saturday",
                            "hard_day_class": "technical",
                            "effective_load": "technical",
                            "status": "convert_to_technical_suggested",
                            "reason": "Saturday technical only.",
                            "coach_note": "Technical work.",
                            "reason_codes": [],
                        },
                    ],
                }
            ]
        },
    }


class SummaryActiveStore(FakeStore):
    """Mirror production auto-active selection, where list_user_plans is summary-only."""

    def list_user_plans(self, athlete_id: str) -> list[dict]:
        rows = super().list_user_plans(athlete_id)
        return [
            {
                key: value
                for key, value in row.items()
                if key not in {"structured_plan", "planning_brief", "stage2_payload"}
            }
            for row in rows
        ]


class NullCheckinListStore(FakeStore):
    def list_today_checkins_for_day(self, athlete_id: str, training_day: str):
        return None


class TestDailyScheduleResolver:
    def test_next_session_uses_earliest_future_calendar_date(self):
        week = WeeklySchedule(
            plan_id=PLAN,
            week_index=0,
            week_count=1,
            days=[
                WeeklyDayEntry(
                    weekday="Tue",
                    calendar_date="2026-06-23",
                    effective_load="hard",
                    status="app_session",
                ),
                WeeklyDayEntry(
                    weekday="Sat",
                    calendar_date="2026-06-27",
                    effective_load="technical",
                    status="technical_sparring",
                ),
                WeeklyDayEntry(
                    weekday="Fri",
                    calendar_date="2026-06-26",
                    effective_load="reduced",
                    status="app_session",
                ),
            ],
        )

        today_entry, next_entry = resolve_today_and_next(week, today=date(2026, 6, 23))

        assert today_entry is not None
        assert today_entry.calendar_date == "2026-06-23"
        assert next_entry is not None
        assert next_entry.weekday == "Fri"
        assert next_entry.calendar_date == "2026-06-26"

    def test_undated_open_schedule_returns_next_recurring_weekday(self):
        week = WeeklySchedule(
            plan_id=PLAN,
            week_index=0,
            week_count=4,
            phase="GPP",
            days=[
                WeeklyDayEntry(
                    weekday="Mon",
                    title="Mon training",
                    effective_load="reduced",
                    status="open_plan_session",
                ),
                WeeklyDayEntry(
                    weekday="Wed",
                    title="Wed coach-led sparring",
                    effective_load="hard",
                    status="hard_as_planned",
                ),
            ],
        )

        today_entry, next_entry = resolve_today_and_next(week, today=date(2026, 7, 12))

        assert today_entry is None
        assert next_entry is not None
        assert next_entry.weekday == "Mon"
        assert next_entry.title == "Mon training"


def _multi_week_taper_brief() -> dict:
    """A two-week taper where each week has a single training day.

    Week 0 trains on Mon (2026-06-15); week 1 trains on Wed (2026-06-24). Every
    other day is rest, mirroring real taper/late-camp weeks. Used to prove the
    "Next session" lookup crosses the week boundary instead of stopping at the
    end of the current week.
    """
    week0_monday = date(2026, 6, 15)

    def _calendar_days(monday: date, d_day_for_monday: int) -> list[dict]:
        return [
            {
                "weekday": (monday + timedelta(days=offset)).strftime("%A"),
                "calendar_date": (monday + timedelta(days=offset)).isoformat(),
                "d_day": d_day_for_monday - offset,
            }
            for offset in range(7)
        ]

    def _training_day(weekday: str) -> dict:
        return {
            "day": weekday,
            "hard_day_class": "managed_hard",
            "effective_load": "technical",
            "status": "technical_skill",
            "reason": "Plan card for the matched training day.",
            "coach_note": "Keep it sharp and clean.",
            "reason_codes": [],
        }

    return {
        "fight_date": "2026-07-11",
        "weekly_role_map": {
            "weeks": [
                {
                    "phase": "TAPER",
                    "calendar_days": _calendar_days(week0_monday, 26),
                    "hard_sparring_plan": [_training_day("Monday")],
                },
                {
                    "phase": "TAPER",
                    "calendar_days": _calendar_days(week0_monday + timedelta(days=7), 19),
                    "hard_sparring_plan": [_training_day("Wednesday")],
                },
            ]
        },
    }


def _checkin_payload(**overrides) -> dict:
    base = {
        "plan_id": PLAN,
        "sleep": "good",
        "body": "normal",
        "pain": "none",
        "phase": "GPP",
        "active_injury": "none",
        "previous_session": "none",
        "sharp_pain": False,
        "instability": False,
        "swelling": False,
        "neurological_symptoms": False,
        "illness_symptoms": False,
        "cannot_warm_into_movement": False,
        "worse_next_day_pain": False,
    }
    return {**base, **overrides}


def _checked_in_completion(store, *, athlete_id=ATHLETE, athlete_timezone="", payload, now=None, **kwargs):
    """Starting or logging today's session needs today's check-in (session_hold)."""
    submit_today_checkin(store, athlete_id=athlete_id, athlete_timezone=athlete_timezone,
                         payload=_checkin_payload(plan_id=payload.get("plan_id", PLAN)), now=now)
    return upsert_session_completion(store, athlete_id=athlete_id, athlete_timezone=athlete_timezone,
                                     payload=payload, now=now, **kwargs)


class TestTrainingDayPersistence:
    def test_0259_local_stores_previous_training_day(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 2, 59, tzinfo=ZoneInfo(NY))
        row = submit_today_checkin(
            store, athlete_id=ATHLETE, athlete_timezone=NY, payload=_checkin_payload(), now=now
        )
        assert row["training_day"] == "2026-06-17"

    def test_0300_local_stores_current_training_day(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 3, 0, tzinfo=ZoneInfo(NY))
        row = submit_today_checkin(
            store, athlete_id=ATHLETE, athlete_timezone=NY, payload=_checkin_payload(), now=now
        )
        assert row["training_day"] == "2026-06-18"

    def test_missing_timezone_fallback_does_not_crash(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 6, 0, tzinfo=timezone.utc)
        row = submit_today_checkin(
            store, athlete_id=ATHLETE, athlete_timezone=None, payload=_checkin_payload(), now=now
        )
        assert row["training_day"] == "2026-06-18"


class TestCheckinSubmit:
    def test_checkin_and_recommendation_persist(self):
        store = _store_with_plan()
        row = submit_today_checkin(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=_checkin_payload(sleep="poor")
        )
        assert store.today_checkins[ATHLETE], "check-in row must persist"
        assert row["recommendation_state"] == "modify"
        assert row["recommendation_reason"].splitlines() == [
            "Session reduced.",
            "Poor sleep means your body has less room to recover today.",
            "Cut 1 round and do not add extra conditioning.",
        ]
        assert "poor_sleep" in row["recommendation_triggers"]

    def test_same_day_duplicate_upserts_single_row(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        submit_today_checkin(store, athlete_id=ATHLETE, athlete_timezone="", payload=_checkin_payload(), now=now)
        second = submit_today_checkin(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=_checkin_payload(sleep="poor"), now=now
        )
        assert len(store.today_checkins[ATHLETE]) == 1
        assert second["recommendation_state"] == "modify"
        assert second["warnings"] == []

    def test_client_supplied_recommendation_is_ignored(self):
        store = _store_with_plan()
        # Client tries to force train_as_planned, but pain=high is a hard override.
        payload = _checkin_payload(pain="high", recommendation_state="train_as_planned")
        row = submit_today_checkin(store, athlete_id=ATHLETE, athlete_timezone="", payload=payload)
        assert row["recommendation_state"] == "pull_back"

    def test_same_day_other_plan_checkin_updates_single_daily_row(self):
        store = _store_with_plan()
        store.plans[OTHER_PLAN] = {
            "id": OTHER_PLAN,
            "athlete_id": ATHLETE,
            "status": "ready",
            "plan_name": "Camp B",
            "created_at": "2026-06-02T00:00:00+00:00",
        }
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)

        submit_today_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=_checkin_payload(plan_id=OTHER_PLAN),
            now=now,
        )
        row = submit_today_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=_checkin_payload(plan_id=PLAN),
            now=now,
        )

        assert len(store.today_checkins[ATHLETE]) == 1
        assert row["warnings"] == []

    def test_same_day_warning_handles_null_lister_and_immutable_row(self):
        store = NullCheckinListStore()
        store.plans[PLAN] = {
            "id": PLAN,
            "athlete_id": ATHLETE,
            "status": "ready",
            "plan_name": "Camp A",
            "created_at": "2026-06-01T00:00:00+00:00",
        }
        original_upsert = store.upsert_today_checkin

        def immutable_upsert(athlete_id: str, fields: dict) -> MappingProxyType:
            return MappingProxyType(original_upsert(athlete_id, fields))

        store.upsert_today_checkin = immutable_upsert  # type: ignore[method-assign]

        row = submit_today_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=_checkin_payload(),
            now=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        )

        assert row["warnings"] == []
        assert row["plan_id"] == PLAN


class TestPlanOwnership:
    def _seed_other(self, store):
        store.plans[OTHER_PLAN] = {
            "id": OTHER_PLAN,
            "athlete_id": "someone-else",
            "status": "ready",
            "plan_name": "Other",
            "created_at": "2026-06-01T00:00:00+00:00",
        }

    def test_checkin_rejected_when_plan_not_owned(self):
        store = _store_with_plan()
        self._seed_other(store)
        with pytest.raises(HTTPException) as exc:
            submit_today_checkin(
                store,
                athlete_id=ATHLETE,
                athlete_timezone="",
                payload=_checkin_payload(plan_id=OTHER_PLAN),
            )
        assert exc.value.status_code == 404
        assert not store.today_checkins.get(ATHLETE)

    def test_completion_rejected_when_plan_not_owned(self):
        store = _store_with_plan()
        self._seed_other(store)
        with pytest.raises(HTTPException) as exc:
            upsert_session_completion(
                store,
                athlete_id=ATHLETE,
                athlete_timezone="",
                payload={"plan_id": OTHER_PLAN, "session_id": "s1", "status": "started"},
            )
        assert exc.value.status_code == 404
        assert not store.session_completions.get(ATHLETE)


class TestPlanIdValidation:
    def test_checkin_rejects_malformed_plan_id(self):
        store = _store_with_plan()
        with pytest.raises(HTTPException) as exc:
            submit_today_checkin(
                store,
                athlete_id=ATHLETE,
                athlete_timezone="",
                payload=_checkin_payload(plan_id="not-a-uuid"),
            )
        assert exc.value.status_code == 422
        assert not store.today_checkins.get(ATHLETE)

    def test_completion_rejects_malformed_plan_id(self):
        store = _store_with_plan()
        with pytest.raises(HTTPException) as exc:
            upsert_session_completion(
                store,
                athlete_id=ATHLETE,
                athlete_timezone="",
                payload={"plan_id": "not-a-uuid", "session_id": "s1", "status": "started"},
            )
        assert exc.value.status_code == 422
        assert not store.session_completions.get(ATHLETE)


class TestRecommendationValidity:
    def test_same_training_day_recommendation_is_live(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        submit_today_checkin(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=_checkin_payload(sleep="poor"), now=now
        )
        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=now)
        assert view.today.recommendation_state == "modify"
        assert view.today.recommendation_reason

    def test_previous_training_day_returns_not_checked_in(self):
        store = _store_with_plan()
        submit_today_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=_checkin_payload(sleep="poor"),
            now=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        )
        # Next day: the prior recommendation has expired.
        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc),
        )
        assert view.today.recommendation_state == "not_checked_in"
        assert view.today.recommendation_reason is None


class TestStructuredFillerDayResolution:
    """Filler/support days must resolve as sessions, not vanish as rest days.

    Athletes reported Today only ever surfacing sparring/strength days: a
    recovery filler scheduled on a ``rest``/``travel`` day_type was zeroed to
    effective_load "none" and dropped, and a headline-only filler day
    ("Rhythm flush") failed the old sparring-vocabulary allowlist.
    """

    NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    DAY = "2026-07-10"

    def _plan_row(self, day: dict) -> dict:
        return {
            "id": PLAN,
            "athlete_id": ATHLETE,
            "status": "ready",
            "structured_plan": {"weeks": [{"phase_label": "TAPER", "days": [day]}]},
        }

    def test_filler_session_on_rest_day_type_still_resolves(self):
        entry = today_service_module._structured_today_session_entry(
            self._plan_row(
                {
                    "date": self.DAY,
                    "countdown_label": "D-19",
                    "day_type": "rest",
                    "today_card": {"headline": "Rhythm flush"},
                    "sessions": [
                        {
                            "session_id": "2026-07-10-flush",
                            "session_type": "recovery",
                            "title": "Rhythm flush",
                            "objective": "Easy fight-pace touches without building fatigue.",
                            "blocks": [],
                        }
                    ],
                }
            ),
            self.DAY,
        )
        assert entry is not None
        assert entry["title"] == "Rhythm flush"
        # A day that schedules work is never load "none" — that made
        # has_scheduled_day_content() drop it from Today entirely.
        assert entry["effective_load"] == "reduced"
        from api.services.plan_schedule import has_scheduled_day_content

        assert has_scheduled_day_content(entry) is True

    def test_headline_only_filler_day_resolves_as_session(self):
        entry = today_service_module._structured_today_session_entry(
            self._plan_row(
                {
                    "date": self.DAY,
                    "countdown_label": "D-19",
                    "day_type": "recovery",
                    "today_card": {"headline": "Rhythm flush"},
                    "sessions": [],
                }
            ),
            self.DAY,
        )
        assert entry is not None
        assert entry["title"] == "Rhythm flush"

    def test_headline_only_rest_day_stays_rest(self):
        for headline in (
            "Rest day.",
            "Full rest",
            "Off day",
            "Travel day",
            "No training today",
            # A day that opens by declaring rest is a rest day even when the
            # sentence goes on to name recovery work — that is what the athlete
            # MAY do, not a session to start.
            "Rest or active recovery",
            "Rest and light mobility if wanted",
        ):
            entry = today_service_module._structured_today_session_entry(
                self._plan_row(
                    {
                        "date": self.DAY,
                        "countdown_label": "D-19",
                        "day_type": "rest",
                        "today_card": {"headline": headline},
                        "sessions": [],
                    }
                ),
                self.DAY,
            )
            assert entry is None, headline

    def test_command_view_surfaces_filler_day_as_today_session(self):
        store = _store_with_plan()
        store.plans[PLAN]["structured_plan"] = {
            "weeks": [
                {
                    "phase_label": "TAPER",
                    "days": [
                        {
                            "date": self.DAY,
                            "countdown_label": "D-19",
                            "day_type": "rest",
                            "today_card": {"headline": "Rhythm flush"},
                            "sessions": [
                                {
                                    "session_id": "2026-07-10-flush",
                                    "session_type": "recovery",
                                    "title": "Rhythm flush",
                                    "objective": "Easy movement, no fatigue.",
                                    "blocks": [],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        view = build_today_command_view(
            store, athlete_id=ATHLETE, athlete_timezone="", now=self.NOW
        )
        assert view.today.next_session.get("title") == "Rhythm flush"
        assert view.today.session_scope == "today"


class TestOpenPlanRestDayResolution:
    """An open plan's REST day must not be presented as today's session.

    The card is weekday-only (its date projection is unavailable whenever the
    card and the saved weekly template disagree), so the server used to find no
    row for today and fall back to the intake template's generic weekly rhythm —
    which calls every configured training weekday a session. Today then offered
    "Start session" on a day whose own card read "Rest or active recovery".
    """

    # 2026-07-31 is a Friday.
    NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    DAY = "2026-07-31"

    @staticmethod
    def _planning_brief() -> dict:
        return {
            "open_plan_spec": {
                "plan_type": "open_ongoing_system",
                "weekly_template": {
                    "training_days": ["Monday", "Wednesday", "Friday", "Saturday"],
                    "hard_sparring_days": [],
                    "coach_owned_days": {},
                },
                "development_block": {
                    "week_1": "Baseline",
                    "week_2": "Progress",
                    "week_3": "Highest controlled week",
                    "week_4": "Deload and reassess",
                },
            },
            "stage1_selection_summary": {"current_phase": "GPP"},
        }

    @staticmethod
    def _structured_plan() -> dict:
        def app_day(weekday: str, title: str) -> dict:
            return {
                "date": "",
                "weekday": weekday,
                "day_type": "moderate",
                "today_card": {"headline": title},
                "sessions": [{"title": title, "blocks": [{"display_name": "Main work"}]}],
            }

        days = [
            app_day("Mon", "Support strength"),
            app_day("Tue", "Technical rhythm"),
            app_day("Wed", "Power transfer"),
            {
                "date": "",
                "weekday": "Fri",
                "day_type": "rest",
                "today_card": {"headline": "Rest or active recovery"},
                "sessions": [],
            },
            app_day("Sat", "Conditioning"),
        ]
        # Five authored rows against four configured training days: the card and
        # the template disagree, so the projection refuses to guess dates and the
        # rows stay weekday-only.
        return {
            "weeks": [
                {"week_index": index, "days": [copy.deepcopy(day) for day in days]}
                for index in range(1, 5)
            ]
        }

    def _store(self) -> FakeStore:
        store = _store_with_plan()
        store.plans[PLAN].update(
            {
                "created_at": "2026-07-01T09:00:00+00:00",
                "fight_date": None,
                "planning_brief": self._planning_brief(),
                "structured_plan": self._structured_plan(),
            }
        )
        return store

    def test_weekday_only_card_resolves_todays_row(self):
        plan_row = self._store().plans[PLAN]
        matched = today_service_module._structured_day_for_training_day(plan_row, self.DAY)
        assert matched is not None
        day, _week = matched
        assert day["weekday"] == "Fri"
        assert day["today_card"]["headline"] == "Rest or active recovery"

    def test_rest_row_resolves_to_no_session_today(self):
        plan_row = self._store().plans[PLAN]
        assert today_service_module._structured_today_session_entry(plan_row, self.DAY) is None

    def test_command_view_does_not_present_a_rest_day_as_todays_session(self):
        view = build_today_command_view(
            self._store(), athlete_id=ATHLETE, athlete_timezone="", now=self.NOW
        )
        # The weekly template calls Friday a training day; the authored card says
        # rest, and the card owns today.
        assert view.today.next_session.get("title") != "Fri training"
        assert view.today.session_scope != "today"

    def test_completion_writes_are_rejected_on_a_rest_day(self):
        """The UI not offering it is not the guard — the write path is.

        A direct call (or a client on a stale card) would otherwise persist a
        session state the plan never prescribed, and that record is what then
        drives "Resume" on a rest day.
        """
        store = self._store()
        for status_value in ("started", "done", "modified", "skipped"):
            with pytest.raises(HTTPException) as exc:
                upsert_session_completion(
                    store,
                    athlete_id=ATHLETE,
                    athlete_timezone="",
                    payload={
                        "plan_id": PLAN,
                        "session_id": "Fri",
                        "status": status_value,
                        "modification_reason": "swapped to recovery",
                        "notes": "logged anyway",
                    },
                    now=self.NOW,
                )
            assert exc.value.status_code == 409, status_value
            assert "rest day" in str(exc.value.detail)

    def test_a_rest_day_completion_can_still_be_reverted(self):
        """``not_started`` stays open so an already-written record can be cleared."""
        store = self._store()
        row = upsert_session_completion(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={"plan_id": PLAN, "session_id": "Fri", "status": "not_started"},
            now=self.NOW,
        )
        assert row["status"] == "not_started"

    def test_landing_ignores_a_stale_completion_on_a_rest_day(self):
        store = self._store()
        # A record written before the write path was guarded (or against a plan
        # that has since changed) must not land the athlete on "Resume".
        store.session_completions.setdefault(ATHLETE, []).append(
            {
                "athlete_id": ATHLETE,
                "plan_id": PLAN,
                "session_id": "Fri",
                "training_day": self.DAY,
                "status": "started",
                "started_at": "2026-07-31T09:00:00+00:00",
            }
        )

        decision = resolve_today_landing(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            has_interacted=True,
            now=self.NOW,
        )

        assert decision.target != "resume_session"

    def test_a_card_row_with_work_still_resolves_through_the_weekly_schedule(self):
        """Only the rest decision is taken from a weekday-only row.

        A row that schedules work carries no date to key a session on, so it
        still resolves through the weekly schedule — session identity (and every
        completion already logged against it) is unchanged.
        """
        store = self._store()
        for week in store.plans[PLAN]["structured_plan"]["weeks"]:
            week["days"][3] = {
                "date": "",
                "weekday": "Fri",
                "day_type": "moderate",
                "today_card": {"headline": "Power transfer"},
                "sessions": [{"title": "Power transfer", "blocks": [{"display_name": "Main work"}]}],
            }

        view = build_today_command_view(
            store, athlete_id=ATHLETE, athlete_timezone="", now=self.NOW
        )

        assert view.today.next_session.get("title") == "Fri training"
        assert view.today.session_scope == "today"


class TestCommandView:
    def test_no_active_plan_returns_intake_cta(self):
        store = FakeStore()  # no plan seeded
        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")
        assert view.active_plan == {}
        assert [a.id for a in view.quick_actions] == ["complete_intake"]

    def test_active_plan_without_checkin_is_not_checked_in(self):
        store = _store_with_plan()
        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")
        assert view.active_plan.get("id") == PLAN
        assert view.today.recommendation_state == "not_checked_in"

    def test_missing_structured_plan_does_not_crash(self):
        # Minimal plan row (no planning_brief) must degrade, not raise.
        store = _store_with_plan()
        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")
        assert view.today.next_session == {}
        assert view.today.completion_status == "not_started"

    def test_guided_intake_injury_seeds_open_injury_flag(self):
        store = _store_with_plan()
        _attach_intake(
            store,
            {
                "guided_injuries": [
                    {
                        "area": "Left shoulder",
                        "zone": "l_shoulder",
                        "severity": "high",
                        "trend": "same",
                        "surface_type": "bruise",
                        "cleared": "",
                    }
                ]
            },
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        assert len(view.open_injuries) == 1
        seeded = view.open_injuries[0]
        assert seeded["source"] == "intake"
        assert seeded["plan_id"] == PLAN
        assert seeded["body_area"] == "Left shoulder"
        assert seeded["severity"] == "severe"
        assert seeded["status"] == "open"
        assert "bruise" in seeded["description"]

    def test_guided_intake_description_carries_no_taxonomy_tokens(self):
        """The description is athlete-facing, so the routing keys stay internal.

        Guided intake stores the family (``surface_injury``) and its
        ``family:specific`` pair alongside the real condition word. Both used to
        land in the description and render on the injury card as "Right
        shoulder: blister. surface injury. surface injury:blister".
        """
        store = _store_with_plan()
        _attach_intake(
            store,
            {
                "guided_injuries": [
                    {
                        "area": "Right shoulder",
                        "severity": "moderate",
                        "trend": "same",
                        "injury_type": "surface_injury",
                        "surface_type": "blister",
                        "injury_subtypes": ["surface_injury:blister"],
                    }
                ]
            },
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        description = view.open_injuries[0]["description"]
        assert description == "Right shoulder: blister"
        # The condition word survives, so the scorer still reads it as a wound.
        assert view.open_injuries[0]["label"] == "Right shoulder blister"

    def test_guided_intake_taxonomy_check_is_case_insensitive(self):
        """Nothing guarantees the casing a stored enum comes back in."""
        store = _store_with_plan()
        _attach_intake(
            store,
            {
                "guided_injuries": [
                    {
                        "area": "Right shoulder",
                        "severity": "moderate",
                        "injury_type": "Surface_Injury",
                        "surface_type": "Blister",
                        "injury_subtypes": ["Surface_Injury:Blister"],
                    }
                ]
            },
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        assert view.open_injuries[0]["description"] == "Right shoulder: Blister"

    def test_guided_intake_description_leaves_out_the_timeframe(self):
        """When it happened is structured intake data, not the athlete's words.

        "last month" / "one to three months" / "old cleared" are enum answers,
        and they read as planner vocabulary sitting in the middle of the note.
        """
        store = _store_with_plan()
        _attach_intake(
            store,
            {
                "guided_injuries": [
                    {
                        "area": "Left shoulder",
                        "severity": "moderate",
                        "surface_type": "bruise",
                        "timeframe": "last_month",
                        "notes": "sore on overhead work",
                    }
                ]
            },
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        assert view.open_injuries[0]["description"] == (
            "Left shoulder: bruise. sore on overhead work"
        )

    def test_guided_intake_keeps_a_type_with_no_specific_word(self):
        """A non-surface type is the only word available, so it is kept."""
        store = _store_with_plan()
        _attach_intake(
            store,
            {
                "guided_injuries": [
                    {
                        "area": "Left knee",
                        "severity": "moderate",
                        "injury_type": "tendon_ligament",
                        "injury_subtypes": ["sprain"],
                    }
                ]
            },
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        assert view.open_injuries[0]["description"] == "Left knee: tendon ligament. sprain"

    def test_guided_intake_description_does_not_repeat_body_area(self):
        store = _store_with_plan()
        _attach_intake(
            store,
            {
                "guided_injuries": [
                    {
                        "area": "Left shoulder",
                        "zone": "l_shoulder",
                        "severity": "moderate",
                        "trend": "same",
                        "notes": "Left shoulder bruise",
                    }
                ]
            },
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        assert view.open_injuries[0]["description"] == "Left shoulder bruise"

    def test_guided_intake_injury_bootstrap_is_idempotent(self):
        store = _store_with_plan()
        _attach_intake(
            store,
            {
                "guided_injuries": [
                    {
                        "area": "Left shoulder",
                        "zone": "l_shoulder",
                        "severity": "moderate",
                        "trend": "same",
                    }
                ]
            },
        )

        build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")
        build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        assert len(store.injury_flags[ATHLETE]) == 1

    def test_intake_bootstrap_write_failure_does_not_break_today(self):
        class FailingBootstrapStore(FakeStore):
            def create_injury_flag(self, athlete_id: str, fields: dict) -> dict:
                raise RuntimeError("temporary write failure")

        store = FailingBootstrapStore()
        store.plans[PLAN] = _store_with_plan().plans[PLAN]
        store.set_active_plan_id(ATHLETE, PLAN)
        _attach_intake(
            store,
            {
                "guided_injuries": [
                    {
                        "area": "Left shoulder",
                        "zone": "l_shoulder",
                        "severity": "high",
                        "trend": "same",
                    }
                ]
            },
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        assert view.active_plan.get("id") == PLAN
        assert view.open_injuries == []

    def test_cleared_guided_intake_injury_is_not_seeded(self):
        store = _store_with_plan()
        _attach_intake(
            store,
            {
                "guided_injuries": [
                    {
                        "area": "Left shoulder",
                        "zone": "l_shoulder",
                        "severity": "high",
                        "trend": "improving",
                        "cleared": "yes",
                    }
                ],
                "injuries": "left shoulder bruise",
            },
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        assert view.open_injuries == []
        assert store.injury_flags.get(ATHLETE, []) == []

    def test_cleared_intake_seeded_injury_stays_cleared(self):
        """Clearing an intake-seeded injury must not re-seed it on the next load."""
        store = _store_with_plan()
        _attach_intake(
            store,
            {
                "guided_injuries": [
                    {
                        "area": "Left shoulder",
                        "zone": "l_shoulder",
                        "severity": "high",
                        "trend": "same",
                    }
                ]
            },
        )

        # First load seeds the open flag from intake.
        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")
        assert len(view.open_injuries) == 1
        flag_id = view.open_injuries[0]["id"]

        # Athlete presses "Cleared" on the daily check-in.
        result = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={"injuries": [{"flag_id": flag_id, "status": "resolved"}]},
        )
        assert result["open_injuries"] == []

        # The injury must not come back the next time Today loads, even though the
        # intake payload still lists it.
        view_after = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")
        assert view_after.open_injuries == []

    @pytest.mark.parametrize(
        ("guided_severity", "flag_severity"),
        [("low", "mild"), ("moderate", "moderate"), ("high", "severe"), ("", "moderate")],
    )
    def test_guided_intake_severity_maps_to_flag_severity(
        self,
        guided_severity: str,
        flag_severity: str,
    ):
        store = _store_with_plan()
        _attach_intake(
            store,
            {
                "guided_injury": {
                    "area": "Left knee",
                    "zone": "l_knee",
                    "severity": guided_severity,
                    "trend": "improving",
                }
            },
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        assert view.open_injuries[0]["severity"] == flag_severity
        assert view.open_injuries[0]["status"] == "monitoring"

    def test_legacy_intake_injury_text_seeds_conservative_flag(self):
        store = _store_with_plan()
        _attach_intake(store, {"injuries": "Shoulder bruise after sparring"})

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        assert len(view.open_injuries) == 1
        seeded = view.open_injuries[0]
        assert seeded["source"] == "intake"
        assert seeded["severity"] == "moderate"
        assert seeded["status"] == "open"
        assert seeded["body_area"] == "Shoulder"
        assert seeded["description"] == "bruise after sparring"

    def test_intake_injury_dedupes_against_existing_open_flag(self):
        store = _store_with_plan()
        store.create_injury_flag(
            ATHLETE,
            {
                "source": "manual",
                "plan_id": PLAN,
                "body_area": "Left shoulder",
                "description": "Left shoulder soreness",
                "severity": "mild",
                "status": "open",
            },
        )
        _attach_intake(
            store,
            {
                "guided_injuries": [
                    {
                        "area": "Left shoulder",
                        "zone": "l_shoulder",
                        "severity": "high",
                        "trend": "same",
                    }
                ]
            },
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        assert len(view.open_injuries) == 1
        assert view.open_injuries[0]["source"] == "manual"

    def test_intake_injury_dedupe_checks_beyond_display_limit(self):
        store = _store_with_plan()
        store.create_injury_flag(
            ATHLETE,
            {
                "source": "manual",
                "plan_id": PLAN,
                "body_area": "Left shoulder",
                "description": "Left shoulder soreness",
                "severity": "mild",
                "status": "open",
            },
        )
        for index in range(20):
            store.create_injury_flag(
                ATHLETE,
                {
                    "source": "checkin",
                    "plan_id": PLAN,
                    "body_area": f"Area {index}",
                    "description": f"Area {index}",
                    "severity": "mild",
                    "status": "open",
                },
            )
        _attach_intake(
            store,
            {
                "guided_injuries": [
                    {
                        "area": "Left shoulder",
                        "zone": "l_shoulder",
                        "severity": "high",
                        "trend": "same",
                    }
                ]
            },
        )

        build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")

        assert len(store.injury_flags[ATHLETE]) == 21
        assert not [flag for flag in store.injury_flags[ATHLETE] if flag["source"] == "intake"]

    def test_logged_session_pain_surfaces_without_a_checkin(self):
        # The badge must reflect training reality even with no check-in today:
        # a high logged post-session pain reading drives a risk-watch item.
        store = _store_with_plan()
        store.session_completions[ATHLETE] = [
            {
                "id": "c1",
                "athlete_id": ATHLETE,
                "plan_id": PLAN,
                "session_id": "s1",
                "training_day": "2026-06-18",
                "status": "done",
                "pain_after": 8,
            }
        ]
        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        )
        assert view.today.recommendation_state == "not_checked_in"
        assert "high_pain" in [risk.category for risk in view.risk_watch]

    def test_current_checkin_risks_carry_explicit_today_timing(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        submit_today_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=_checkin_payload(sleep="poor", pain="high"),
            now=now,
        )

        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=now,
        )
        by_category = {risk.category: risk for risk in view.risk_watch}
        assert by_category["high_pain"].timeframe == "today"
        assert by_category["high_pain"].text == "High pain reported during today's check-in."
        assert by_category["fatigue"].timeframe == "today"
        assert by_category["fatigue"].text == "Fatigue reported during today's check-in."

    def test_logged_session_pain_surfaces_when_checkin_history_fails(self):
        class FailingCheckinHistoryStore(FakeStore):
            def list_today_checkins(self, athlete_id: str, *, limit: int = 14):
                raise RuntimeError("check-in history unavailable")

        store = _store_with_plan(store_type=FailingCheckinHistoryStore)
        store.session_completions[ATHLETE] = [
            {
                "id": "c1",
                "athlete_id": ATHLETE,
                "plan_id": PLAN,
                "session_id": "s1",
                "training_day": "2026-06-18",
                "status": "done",
                "pain_after": 8,
            }
        ]
        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        )
        assert "high_pain" in [risk.category for risk in view.risk_watch]

    def test_old_elevated_pain_does_not_create_a_stale_reminder(self):
        store = _store_with_plan()
        store.session_completions[ATHLETE] = [
            {
                "id": "c1",
                "athlete_id": ATHLETE,
                "plan_id": PLAN,
                "session_id": "s1",
                "training_day": "2026-06-16",
                "status": "done",
                "pain_after": 5,
            }
        ]
        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        )
        assert "reminder" not in [risk.category for risk in view.risk_watch]

    def test_injury_checkin_opens_flag_and_surfaces_it(self):
        store = _store_with_plan()
        result = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={"injuries": [{"body_area": "left knee", "status": "ongoing"}]},
        )
        assert len(result["open_injuries"]) == 1
        flag = result["open_injuries"][0]
        assert flag["status"] == "open"
        assert flag["plan_id"] == PLAN  # attached to the active plan

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")
        assert len(view.open_injuries) == 1
        assert "reminder" in [risk.category for risk in view.risk_watch]

    def test_injury_checkin_rejects_stale_flag_id_without_identity(self):
        store = _store_with_plan()
        with pytest.raises(HTTPException) as exc_info:
            submit_today_injury_checkin(
                store,
                athlete_id=ATHLETE,
                payload={"injuries": [{"flag_id": "ghost", "status": "ongoing"}]},
            )

        assert exc_info.value.status_code == 422
        assert "body_area or description" in str(exc_info.value.detail)
        assert store.injury_flags.get(ATHLETE, []) == []

    def test_injury_checkin_resolves_an_open_flag(self):
        store = _store_with_plan()
        opened = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={"injuries": [{"body_area": "calf", "status": "ongoing"}]},
        )
        flag_id = opened["open_injuries"][0]["id"]

        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        resolved = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={"injuries": [{"flag_id": flag_id, "status": "resolved"}]},
            now=now,
        )
        assert resolved["open_injuries"] == []  # no longer open/monitoring

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")
        assert view.open_injuries == []
        injury_categories = {"active_injury_worse", "reminder"}
        assert not (injury_categories & {risk.category for risk in view.risk_watch})

    def test_resolved_injury_re_report_rotates_episode_once(self):
        store = _store_with_plan()
        opened = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={"injuries": [{"body_area": "left ankle", "status": "ongoing"}]},
        )
        flag = opened["open_injuries"][0]
        episode_a = next(row for row in store.injury_flags[ATHLETE] if row["id"] == flag["id"])["episode_id"]
        submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={"injuries": [{"flag_id": flag["id"], "status": "resolved"}]},
        )

        submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={"injuries": [{"flag_id": flag["id"], "status": "ongoing"}]},
        )
        episode_b = next(row for row in store.injury_flags[ATHLETE] if row["id"] == flag["id"])["episode_id"]
        assert episode_b != episode_a

        # A refresh/retry sees an open flag, so it does not rotate again.
        submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={"injuries": [{"flag_id": flag["id"], "status": "ongoing"}]},
        )
        assert next(row for row in store.injury_flags[ATHLETE] if row["id"] == flag["id"])["episode_id"] == episode_b

        from api.contracts.rehab_exposure import RehabExposureEvent

        old_event = RehabExposureEvent.model_validate(
            {
                "exposure_id": str(uuid4()),
                "injury_id": flag["id"],
                "injury_episode_id": episode_a,
                "drill_id": "ankle_control",
                "body_region": "ankle",
                "side": "left",
                "demand": {"target_regions": ["ankle"], "load": "low", "impact": "none", "velocity": "low"},
                "dose_completed": {"reps": 8},
                "occurred_at": "2026-06-18T12:00:00Z",
                "provenance": {"source": "athlete_logged_rehab", "recorded_at": "2026-06-18T12:01:00Z"},
            }
        )
        current = next(row for row in store.injury_flags[ATHLETE] if row["id"] == flag["id"])
        assert not old_event.is_attributable_to(current)

    def test_injury_checkin_status_update_preserves_existing_severity(self):
        store = _store_with_plan()
        opened = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={"injuries": [{"body_area": "shoulder", "severity": "severe", "status": "worse"}]},
        )
        flag_id = opened["open_injuries"][0]["id"]

        updated = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={"injuries": [{"flag_id": flag_id, "status": "ongoing"}]},
        )

        assert updated["open_injuries"][0]["severity"] == "severe"

    @pytest.mark.parametrize("reported_status", ["ongoing", "improving"])
    def test_clean_surface_recheck_never_lowers_manual_severe_severity(
        self, reported_status
    ):
        store = _store_with_plan()
        opened = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={
                "injuries": [
                    {
                        "body_area": "left hand",
                        "description": "left hand cut",
                        "severity": "severe",
                        "status": "ongoing",
                    }
                ]
            },
        )

        updated = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={
                "injuries": [
                    {
                        "flag_id": opened["open_injuries"][0]["id"],
                        "status": reported_status,
                        "skin_integrity": "intact",
                        "bleeding_status": "none",
                        "drainage": "none",
                        "infection_signs": [],
                        "coverable": "yes",
                        "friction_or_contact_problem": "no",
                    }
                ]
            },
        )

        assert updated["open_injuries"][0]["severity"] == "severe"

    def test_system_raised_wound_severity_is_released_by_a_clean_recheck(self):
        """The full lifecycle of a wound the SYSTEM escalated.

        A mild cut goes septic, the surface floor raises it to severe, and the
        athlete already has a readiness check-in for the day — so the wound has to
        beat the generic severe-injury pull-back to the recommendation. When the
        wound later comes back clean, the floor the system applied is released
        rather than pinning the injury at severe until it is resolved outright.
        """
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        checkin = submit_today_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=_checkin_payload(),
            now=now,
        )
        assert checkin["recommendation_state"] == "train_as_planned"

        opened = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={
                "injuries": [
                    {
                        "body_area": "left hand",
                        "description": "left hand cut",
                        "severity": "mild",
                        "status": "ongoing",
                    }
                ]
            },
            now=now,
        )
        flag_id = opened["open_injuries"][0]["id"]
        assert opened["open_injuries"][0]["severity"] == "mild"

        infected = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={
                "injuries": [
                    {
                        "flag_id": flag_id,
                        "status": "worse",
                        "skin_integrity": "open",
                        "bleeding_status": "controlled",
                        "drainage": "present",
                        "infection_signs": ["pus"],
                        "coverable": "no",
                        "friction_or_contact_problem": "yes",
                    }
                ]
            },
            now=now,
        )
        wound = infected["open_injuries"][0]
        assert wound["severity"] == "severe"
        assert wound["severity_source"] == "surface_system"
        # The athlete's own severity is kept underneath the floor, which is what
        # makes releasing it later a restore rather than a guess.
        assert wound["manual_severity"] == "mild"

        # A readiness check-in exists, so the generic severe-injury pull-back is
        # live and competing. The wound-specific guidance has to win it: "rehab
        # only" says nothing about keeping the wound clean, covered, and out of
        # contact. The readiness engine itself has to produce that — not just the
        # command view patching it back afterwards.
        stored = store.get_today_checkin(ATHLETE, PLAN, "2026-06-18")
        assert stored is not None
        assert stored["recommendation_state"] == "pull_back"
        assert "Get this checked." in stored["recommendation_reason"]
        assert "Rehab only today." not in stored["recommendation_reason"]
        assert "surface_injury_medical_review" in stored["recommendation_triggers"]

        view = build_today_command_view(
            store, athlete_id=ATHLETE, athlete_timezone="", now=now
        )
        assert view.today.recommendation_state == "pull_back"
        reason = view.today.recommendation_reason or ""
        assert "Get this checked." in reason
        assert "showing infection signs" in reason
        assert "Rehab only today." not in reason
        assert "Active severe injury" not in reason

        cleaned = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={
                "injuries": [
                    {
                        "flag_id": flag_id,
                        "status": "improving",
                        "skin_integrity": "intact",
                        "bleeding_status": "none",
                        "drainage": "none",
                        "infection_signs": [],
                        "coverable": "yes",
                        "friction_or_contact_problem": "no",
                    }
                ]
            },
            now=now,
        )
        healed = cleaned["open_injuries"][0]
        assert healed["severity"] == "mild"
        assert healed["severity_source"] == "manual"
        assert healed["manual_severity"] is None
        assert healed["surface_class"] == "stable_surface"

        view = build_today_command_view(
            store, athlete_id=ATHLETE, athlete_timezone="", now=now
        )
        assert "Get this checked." not in (view.today.recommendation_reason or "")

    def test_clean_recheck_never_releases_a_manually_chosen_severe_severity(self):
        """The release is scoped to floors the system applied.

        A severity the athlete chose is theirs; clean skin answers say the wound
        is clean, not that the athlete was wrong about how bad the injury is.
        """
        store = _store_with_plan()
        opened = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={
                "injuries": [
                    {
                        "body_area": "left hand",
                        "description": "left hand cut",
                        "severity": "severe",
                        "status": "ongoing",
                    }
                ]
            },
        )
        flag_id = opened["open_injuries"][0]["id"]

        cleaned = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={
                "injuries": [
                    {
                        "flag_id": flag_id,
                        "status": "improving",
                        "skin_integrity": "intact",
                        "bleeding_status": "none",
                        "drainage": "none",
                        "infection_signs": [],
                        "coverable": "yes",
                        "friction_or_contact_problem": "no",
                    }
                ]
            },
        )
        healed = cleaned["open_injuries"][0]
        assert healed["severity"] == "severe"
        assert healed["severity_source"] == "manual"
        assert healed["manual_severity"] is None

    def test_clean_recheck_releases_a_floor_to_the_severity_the_athlete_chose(self):
        """Releasing restores the athlete's severity, not a blanket "mild"."""
        store = _store_with_plan()
        opened = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={
                "injuries": [
                    {
                        "body_area": "left hand",
                        "description": "left hand cut",
                        "severity": "moderate",
                        "status": "ongoing",
                    }
                ]
            },
        )
        flag_id = opened["open_injuries"][0]["id"]

        infected = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={
                "injuries": [
                    {
                        "flag_id": flag_id,
                        "status": "worse",
                        "skin_integrity": "open",
                        "bleeding_status": "controlled",
                        "drainage": "present",
                        "infection_signs": ["pus"],
                        "coverable": "no",
                        "friction_or_contact_problem": "yes",
                    }
                ]
            },
        )
        assert infected["open_injuries"][0]["severity"] == "severe"
        assert infected["open_injuries"][0]["manual_severity"] == "moderate"

        cleaned = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={
                "injuries": [
                    {
                        "flag_id": flag_id,
                        "status": "improving",
                        "skin_integrity": "intact",
                        "bleeding_status": "none",
                        "drainage": "none",
                        "infection_signs": [],
                        "coverable": "yes",
                        "friction_or_contact_problem": "no",
                    }
                ]
            },
        )
        assert cleaned["open_injuries"][0]["severity"] == "moderate"
        assert cleaned["open_injuries"][0]["severity_source"] == "manual"

    def test_severe_non_surface_injury_still_outranks_a_wound_in_medical_review(self):
        """The wound pathway wins over the GENERIC injury pull-back, not over a
        real severe injury elsewhere."""
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        submit_today_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=_checkin_payload(),
            now=now,
        )
        store.create_injury_flag(
            ATHLETE,
            {
                "source": "checkin",
                "plan_id": PLAN,
                "body_area": "left shoulder",
                "description": "left shoulder dislocation",
                "severity": "severe",
                "status": "open",
            },
        )
        opened = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={
                "injuries": [
                    {
                        "body_area": "left hand",
                        "description": "left hand cut",
                        "severity": "mild",
                        "status": "ongoing",
                    }
                ]
            },
            now=now,
        )
        flag_id = next(
            injury["id"]
            for injury in opened["open_injuries"]
            if injury["description"] == "left hand cut"
        )
        submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={
                "injuries": [
                    {
                        "flag_id": flag_id,
                        "status": "worse",
                        "skin_integrity": "open",
                        "bleeding_status": "controlled",
                        "drainage": "present",
                        "infection_signs": ["pus"],
                        "coverable": "no",
                        "friction_or_contact_problem": "yes",
                    }
                ]
            },
            now=now,
        )

        view = build_today_command_view(
            store, athlete_id=ATHLETE, athlete_timezone="", now=now
        )

        assert view.today.recommendation_state == "pull_back"
        assert "Get this checked." not in (view.today.recommendation_reason or "")
        # The wound was still assessed — it just did not own the decision.
        assert view.today.recommendation_safety_checks == [
            {
                "code": "surface_injury",
                "label": "Skin injury",
                "result": "medical_review",
                "result_label": "Needs checking",
            }
        ]

    def test_worse_surface_answers_raise_severity_from_canonical_surface_class(self):
        store = _store_with_plan()
        opened = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={
                "injuries": [
                    {
                        "body_area": "left hand",
                        "description": "left hand cut",
                        "severity": "mild",
                        "status": "ongoing",
                    }
                ]
            },
        )
        flag_id = opened["open_injuries"][0]["id"]

        opened_wound = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={
                "injuries": [
                    {
                        "flag_id": flag_id,
                        "status": "worse",
                        "skin_integrity": "open",
                        "bleeding_status": "controlled",
                        "drainage": "none",
                        "infection_signs": [],
                        "coverable": "yes",
                        "friction_or_contact_problem": "yes",
                    }
                ]
            },
        )
        assert opened_wound["open_injuries"][0]["severity"] == "moderate"
        assert opened_wound["open_injuries"][0]["surface_class"] == "surface_no_contact"

        infected = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={
                "injuries": [
                    {
                        "flag_id": flag_id,
                        "status": "worse",
                        "skin_integrity": "open",
                        "bleeding_status": "controlled",
                        "drainage": "present",
                        "infection_signs": ["pus"],
                        "coverable": "no",
                        "friction_or_contact_problem": "yes",
                    }
                ]
            },
        )
        assert infected["open_injuries"][0]["severity"] == "severe"
        assert infected["open_injuries"][0]["surface_class"] == "surface_medical_review"

    def test_surface_medical_review_is_visible_without_session_or_daily_checkin(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        opened = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={
                "injuries": [
                    {
                        "body_area": "left hand",
                        "description": "left hand cut",
                        "severity": "mild",
                        "status": "ongoing",
                    }
                ]
            },
            now=now,
        )
        flag_id = opened["open_injuries"][0]["id"]

        result = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={
                "injuries": [
                    {
                        "flag_id": flag_id,
                        "status": "worse",
                        "skin_integrity": "open",
                        "bleeding_status": "controlled",
                        "drainage": "present",
                        "infection_signs": ["pus"],
                        "coverable": "no",
                        "friction_or_contact_problem": "yes",
                    }
                ]
            },
            now=now,
        )

        # No daily readiness check-in exists, so the write itself has no stored
        # recommendation to refresh. The command view must still surface the
        # medical-review guidance from the tracked wound.
        assert result["recommendation"] is None
        view = build_today_command_view(
            store, athlete_id=ATHLETE, athlete_timezone="", now=now
        )
        assert view.today.session_scope == "none"
        assert view.today.recommendation_state == "pull_back"
        assert "Get this checked." in (view.today.recommendation_reason or "")
        assert "showing infection signs" in (view.today.recommendation_reason or "")
        assert "Left hand cut — needs medical review" in view.today.recommendation_trigger_labels
        assert view.today.recommendation_safety_checks == [
            {
                "code": "surface_injury",
                "label": "Skin injury",
                "result": "medical_review",
                "result_label": "Needs checking",
            }
        ]

    def test_severe_structural_injury_owns_no_checkin_recommendation_over_surface_review(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        wound = store.create_injury_flag(
            ATHLETE,
            {
                "source": "checkin",
                "plan_id": PLAN,
                "body_area": "left hand",
                # The infection is carried by the structured answers, not the
                # description: "infected" in the text resolves the injury TYPE to
                # infection, which is not surface tissue, so the wound would never
                # reach the surface pathway this test is about.
                "description": "left hand cut",
                "severity": "moderate",
                "status": "open",
                "skin_integrity": "open",
                "drainage": "present",
                "infection_signs": ["pus"],
            },
        )
        shoulder = store.create_injury_flag(
            ATHLETE,
            {
                "source": "checkin",
                "plan_id": PLAN,
                "body_area": "left shoulder",
                "description": "left shoulder dislocation",
                "severity": "severe",
                "status": "monitoring",
                "latest_reported_status": "improving",
            },
        )

        view = build_today_command_view(
            store, athlete_id=ATHLETE, athlete_timezone="", now=now
        )

        assert "Active severe injury: Left shoulder dislocation" in (
            view.today.recommendation_reason or ""
        )
        assert view.today.recommendation_trigger_labels == [
            "Left shoulder dislocation — severe"
        ]
        assert view.today.recommendation_safety_checks == [
            {
                "code": "surface_injury",
                "label": "Skin injury",
                "result": "medical_review",
                "result_label": "Needs checking",
            }
        ]
        assert wound["id"] != shoulder["id"]

    def test_injury_hold_contributor_uses_responsible_severe_injury_id(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        store.create_injury_flag(
            ATHLETE,
            {
                "source": "checkin",
                "plan_id": PLAN,
                "body_area": "left shoulder",
                "description": "left shoulder dislocation",
                "severity": "severe",
                "status": "monitoring",
                "latest_reported_status": "improving",
            },
        )
        store.create_injury_flag(
            ATHLETE,
            {
                "source": "checkin",
                "plan_id": PLAN,
                "body_area": "right ankle",
                "description": "minor right ankle sprain",
                "severity": "mild",
                "status": "open",
                "latest_reported_status": "worse",
            },
        )

        view = build_today_command_view(
            store, athlete_id=ATHLETE, athlete_timezone="", now=now
        )

        assert view.today.recommendation_trigger_labels == [
            "Left shoulder dislocation — severe"
        ]

    def test_worse_injury_refreshes_existing_readiness_recommendation(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        initial = submit_today_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=_checkin_payload(),
            now=now,
        )
        assert initial["recommendation_state"] == "train_as_planned"

        submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={"injuries": [{"body_area": "belly", "severity": "severe", "status": "worse"}]},
            now=now,
        )

        updated = store.get_today_checkin(ATHLETE, PLAN, "2026-06-18")
        assert updated is not None
        assert updated["active_injury"] == "worse"  # re-derived from the tracked injury
        assert updated["recommendation_state"] == "pull_back"
        assert "active_injury_worse" in updated["recommendation_triggers"]
        assert "Rehab only today." in updated["recommendation_reason"]

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=now)
        assert view.today.recommendation_state == "pull_back"
        assert "Rehab only today." in (view.today.recommendation_reason or "")

    def test_worse_mild_injury_stop_survives_later_mild_injury(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        submit_today_checkin(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=_checkin_payload(), now=now
        )

        knee = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={"injuries": [{"body_area": "knee", "severity": "mild", "status": "worse"}]},
            now=now,
        )
        assert knee["recommendation"]["recommendation_state"] == "pull_back"
        assert knee["open_injuries"][0]["latest_reported_status"] == "worse"

        updated = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={
                "injuries": [
                    {"body_area": "ear", "description": "ear cut", "severity": "mild", "status": "ongoing"}
                ]
            },
            now=now,
        )

        assert updated["recommendation"]["recommendation_state"] == "pull_back"
        assert "active_injury_worse" in updated["recommendation"]["recommendation_triggers"]

    def test_multiple_mild_injuries_without_worsening_do_not_stop(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        submit_today_checkin(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=_checkin_payload(), now=now
        )

        updated = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={
                "injuries": [
                    {"body_area": "knee", "severity": "mild", "status": "ongoing"},
                    {"body_area": "ear", "description": "ear cut", "severity": "mild", "status": "ongoing"},
                ]
            },
            now=now,
        )

        assert updated["recommendation"]["recommendation_state"] in {"train_as_planned", "modify"}
        assert "active_injury_worse" not in updated["recommendation"]["recommendation_triggers"]

    def test_severe_injury_stop_survives_later_mild_injury(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        submit_today_checkin(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=_checkin_payload(), now=now
        )

        submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={"injuries": [{"body_area": "shoulder", "severity": "severe", "status": "ongoing"}]},
            now=now,
        )
        updated = submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={
                "injuries": [
                    {"body_area": "ear", "description": "ear cut", "severity": "mild", "status": "ongoing"}
                ]
            },
            now=now,
        )

        assert updated["recommendation"]["recommendation_state"] == "pull_back"
        assert "active_injury_worse" in updated["recommendation"]["recommendation_triggers"]

    def test_severe_ongoing_injury_escalates_recommendation_to_pull_back(self):
        # The reported bug: a severe injury added as "ongoing" (not "worse") used
        # to leave the recommendation at the daily "load reduced" copy. It must
        # now pull training back at the source, driven by severity.
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        initial = submit_today_checkin(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=_checkin_payload(), now=now
        )
        assert initial["recommendation_state"] == "train_as_planned"

        submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={"injuries": [{"body_area": "chest", "severity": "severe", "status": "ongoing"}]},
            now=now,
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=now)
        assert view.today.recommendation_state == "pull_back"

    def test_intake_severe_injury_escalates_command_view_without_a_checkin(self):
        # A severe injury carried in from intake (no injury check-in refresh) is
        # still escalated by the command-view catch-all.
        store = _store_with_plan()
        store.create_injury_flag(
            ATHLETE,
            {"source": "intake", "plan_id": PLAN, "body_area": "neck", "severity": "severe", "status": "open"},
        )
        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")
        assert view.today.recommendation_state == "pull_back"
        assert "severe injury" in (view.today.recommendation_reason or "").lower()

    def _cue_card_structured_plan(self):
        return {"weeks": [{"phase_label": "SPP", "days": [{
            "date": "2026-06-18", "day_type": "skill", "phase_label": "SPP",
            "countdown_label": "D-11",
            "sessions": [{
                "session_id": "2026-06-18-cue", "session_type": "support_insert",
                "category": "support_insert", "support_insert_category": "tactical",
                "title": "Tactical Cue Card", "objective": "distil one clean in-fight cue",
                "stress_class": "support", "governance": {"meaningful_stress": False},
                "blocks": [{"type": "mindset", "title": "Write one cue"}],
            }],
            "today_card": {"headline": "Tactical Cue Card"},
        }]}]}

    def test_severe_injury_does_not_block_a_safe_filler_session(self):
        # A muscle strain can preserve cognitive support work. Current neuro or
        # medical red flags instead stop every execution path.
        store = _store_with_plan()
        store.plans[PLAN]["structured_plan"] = self._cue_card_structured_plan()
        store.create_injury_flag(
            ATHLETE,
            {"source": "intake", "plan_id": PLAN, "body_area": "neck",
             "description": "neck muscle strain", "severity": "severe", "status": "open"},
        )
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=now)
        assert view.today.injury_hold_exempt is True
        assert view.today.decision_tier != "stop"
        assert "session blocked" not in (view.today.recommendation_reason or "").lower()

    def test_filler_session_is_completable_despite_severe_injury(self):
        store = _store_with_plan()
        store.plans[PLAN]["structured_plan"] = self._cue_card_structured_plan()
        store.create_injury_flag(
            ATHLETE,
            {"source": "intake", "plan_id": PLAN, "body_area": "neck",
             "description": "neck muscle strain", "severity": "severe", "status": "open"},
        )
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        row = _checked_in_completion(
            store, athlete_id=ATHLETE, athlete_timezone="",
            payload={"plan_id": PLAN, "session_id": "2026-06-18-cue", "status": "done"}, now=now,
        )
        assert row["status"] == "done"

    def test_new_injury_refreshes_existing_readiness_before_high_risk_session(self):
        store = _store_with_plan()
        structured_plan = _combined_contact_and_app_structured_plan()
        structured_plan["weeks"][0]["days"][0]["sessions"][1]["session_type"] = "sparring"
        structured_plan["weeks"][0]["days"][0]["sessions"][1]["title"] = "Hard sparring"
        store.plans[PLAN]["structured_plan"] = structured_plan
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        initial = submit_today_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=_checkin_payload(phase="SPP"),
            now=now,
        )
        assert initial["recommendation_state"] == "train_as_planned"

        submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={"injuries": [{"body_area": "left shoulder", "severity": "moderate", "status": "ongoing"}]},
            now=now,
        )

        updated = store.get_today_checkin(ATHLETE, PLAN, "2026-06-18")
        assert updated is not None
        assert updated["recommendation_state"] == "modify"
        assert "tracked_injury_high_risk_session" in updated["recommendation_triggers"]
        assert "Load controlled." in updated["recommendation_reason"]

    def test_tendon_injury_pulls_back_before_high_risk_session_with_tier(self):
        # Type-aware escalation end-to-end: a moderate tendon (load-sensitive) injury
        # before a high-exposure session is a pull-back, and the command view exposes
        # the authoritative decision_tier the whole UI renders from.
        store = _store_with_plan()
        structured_plan = _combined_contact_and_app_structured_plan()
        structured_plan["weeks"][0]["days"][0]["sessions"][1]["session_type"] = "sparring"
        structured_plan["weeks"][0]["days"][0]["sessions"][1]["title"] = "Hard sparring"
        store.plans[PLAN]["structured_plan"] = structured_plan
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        submit_today_checkin(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=_checkin_payload(phase="SPP"), now=now
        )

        submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={
                "injuries": [
                    {"body_area": "knee", "description": "knee tendonitis", "severity": "moderate", "status": "ongoing"}
                ]
            },
            now=now,
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=now)
        assert view.today.recommendation_state == "pull_back"
        assert view.today.decision_tier == "pull_back"

    def test_severe_open_injury_is_a_stop_level_risk(self):
        store = _store_with_plan()
        submit_today_injury_checkin(
            store,
            athlete_id=ATHLETE,
            payload={"injuries": [{"body_area": "shoulder", "severity": "severe", "status": "worse"}]},
        )
        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="")
        assert "active_injury_worse" in [risk.category for risk in view.risk_watch]

    def test_active_plan_phase_comes_from_resolved_current_week(self):
        store = _store_with_plan()
        # Simulate a legacy/minimal row with no top-level phase. Today must use
        # the current resolved week phase so the frontend cannot downgrade TAPER
        # decisions to GPP when it submits the check-in.
        store.plans[PLAN]["planning_brief"] = _taper_planning_brief()
        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        )
        assert view.active_plan.get("phase") == "TAPER"

    def test_open_plan_template_surfaces_next_session_and_checkin_phase(self):
        store = _store_with_plan()
        store.plans[PLAN]["planning_brief"] = {
            "open_plan_spec": {
                "plan_type": "open_ongoing_system",
                "weekly_template": {
                    "training_days": ["Monday", "Wednesday", "Friday", "Saturday", "Tuesday"],
                    "hard_sparring_days": ["Wednesday", "Friday"],
                },
                "development_block": {
                    "week_1": "Baseline",
                    "week_2": "Progress",
                    "week_3": "Highest controlled week",
                    "week_4": "Deload and reassess",
                },
            },
            "stage1_selection_summary": {"current_phase": "GPP"},
        }

        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc),
        )

        assert view.active_plan.get("phase") == "GPP"
        assert view.today.session_scope == "next"
        assert view.today.next_session["weekday"] == "Mon"
        assert view.today.next_session["title"] == "Mon training"
        assert view.today.next_session["session_id"] == "Mon"

    def test_today_session_uses_current_plan_day(self):
        store = _store_with_plan()
        store.plans[PLAN]["planning_brief"] = _calendar_training_brief(active_offsets=[0])
        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        )
        assert view.today.next_session["calendar_date"] == "2026-06-18"
        assert view.today.next_session["session_id"] == "2026-06-18"
        assert view.today.next_session["session_relation"] == "today"
        # The reconstructed structured calendar owns load as well as timing;
        # this headline-only session sits on its reduced/rest support day.
        assert view.today.next_session["effective_load"] == "reduced"

    def test_today_session_falls_forward_to_next_training_day(self):
        store = _store_with_plan()
        store.plans[PLAN]["planning_brief"] = _calendar_training_brief(active_offsets=[1])
        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        )
        assert view.today.next_session["calendar_date"] == "2026-06-19"
        assert view.today.next_session["session_id"] == "2026-06-19"
        assert view.today.next_session["session_relation"] == "next"
        assert view.today.completion_status == "not_started"
        assert view.today.session_scope == "next"

    def test_completed_today_session_falls_forward_to_next_training_day(self):
        store = _store_with_plan()
        store.plans[PLAN]["planning_brief"] = _calendar_training_brief(active_offsets=[0, 1])
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)

        active_view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=now,
        )
        assert active_view.today.session_scope == "today"
        assert active_view.today.session_label == "Today's session"
        assert active_view.today.next_session["calendar_date"] == "2026-06-18"

        _checked_in_completion(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={"plan_id": PLAN, "session_id": "2026-06-18", "status": "done"},
            now=now,
        )

        completed_view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=now,
        )
        assert completed_view.today.completion_status == "done"
        assert completed_view.today.session_scope == "next"
        assert completed_view.today.session_label == "Next session"
        assert completed_view.today.next_session["calendar_date"] == "2026-06-19"
        assert completed_view.today.next_session["session_relation"] == "next"

    def test_completed_today_structured_fallback_uses_earliest_future_date(self):
        store = _store_with_plan()
        store.plans[PLAN]["structured_plan"] = _out_of_order_countdown_structured_plan()
        now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)

        _checked_in_completion(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={"plan_id": PLAN, "session_id": "2026-06-23-app", "status": "done"},
            now=now,
        )

        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=now,
        )

        assert view.today.completion_status == "done"
        assert view.today.session_scope == "next"
        assert view.today.next_session["calendar_date"] == "2026-06-26"
        assert view.today.next_session["weekday"] == "Friday"
        assert view.today.next_session["title"] == "Friday app session"
        assert view.today.next_session["title"] != "Saturday technical sparring"

    def test_completed_today_prefers_earlier_structured_app_card_over_later_weekly_technical(self):
        store = _store_with_plan()
        store.plans[PLAN]["planning_brief"] = _tuesday_today_saturday_technical_brief()
        store.plans[PLAN]["structured_plan"] = _out_of_order_countdown_structured_plan()
        now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)

        _checked_in_completion(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload={"plan_id": PLAN, "session_id": "2026-06-23-app", "status": "done"},
            now=now,
        )

        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=now,
        )

        assert view.today.completion_status == "done"
        assert view.today.session_scope == "next"
        assert view.today.next_session["calendar_date"] == "2026-06-26"
        assert view.today.next_session["weekday"] == "Friday"
        assert view.today.next_session["title"] == "Friday app session"
        assert view.today.next_session["title"] != "Technical sparring"

    def test_calendar_rest_day_falls_forward_to_next_real_session(self):
        store = _store_with_plan()
        store.plans[PLAN]["planning_brief"] = _monday_rest_tuesday_training_brief()
        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
        )
        assert view.today.session_scope == "next"
        assert view.today.session_label == "Next session"
        assert view.today.next_session["calendar_date"] == "2026-06-23"
        assert view.today.next_session["session_relation"] == "next"
        assert view.today.next_session["effective_load"] == "hard"

    def test_structured_today_session_overrides_next_sparring_preview(self):
        store = _store_with_plan()
        store.plans[PLAN]["planning_brief"] = _monday_rest_tuesday_training_brief()
        store.plans[PLAN]["structured_plan"] = _monday_strength_structured_plan()
        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
        )
        assert view.today.session_scope == "today"
        assert view.today.session_label == "Today's session"
        assert view.today.next_session["session_relation"] == "today"
        assert view.today.next_session["session_id"] == "2026-06-22-strength"
        assert view.today.next_session["weekday"] == "Monday"
        assert view.today.next_session["title"] == "Posterior chain strength + control"
        assert view.today.next_session["title"] != "Hard sparring"

    def test_structured_today_combined_contact_prefers_app_session_summary(self):
        store = _store_with_plan()
        store.plans[PLAN]["structured_plan"] = _combined_contact_and_app_structured_plan()

        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        )

        assert view.today.session_scope == "today"
        assert view.today.next_session["session_relation"] == "today"
        assert view.today.next_session["session_id"] == "2026-06-18-app"
        assert view.today.next_session["title"] == "Power Transfer Touch"
        assert view.today.next_session["coach_led_contact"] == "Coach-led boxing - technical only"
        assert view.today.next_session["title"] != "Hard sparring"

    def test_auto_active_summary_row_rehydrates_structured_today_session_and_phase(self):
        store = SummaryActiveStore()
        store.plans[PLAN] = {
            "id": PLAN,
            "athlete_id": ATHLETE,
            "status": "ready",
            "plan_name": "Camp A",
            "created_at": "2026-06-01T00:00:00+00:00",
            "structured_plan": _monday_strength_structured_plan(),
        }
        store.set_active_plan_id(ATHLETE, PLAN)

        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc),
        )

        assert view.active_plan.get("phase") == "GPP"
        assert view.today.session_scope == "today"
        assert view.today.next_session["calendar_date"] == "2026-06-23"
        assert view.today.next_session["title"] == "Hard sparring"

    def test_malformed_structured_session_degrades_without_crashing(self):
        store = _store_with_plan()
        store.plans[PLAN]["structured_plan"] = {
            "weeks": [
                {
                    "phase_label": "GPP",
                    "days": [
                        {
                            "date": "2026-06-23",
                            "day_type": "high",
                            "sessions": ["bad-session"],
                        }
                    ],
                }
            ]
        }

        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc),
        )

        assert view.active_plan.get("phase") == "GPP"
        assert view.today.next_session == {}

    def test_malformed_structured_weeks_days_and_blocks_degrade_without_crashing(self):
        store = _store_with_plan()
        store.plans[PLAN]["structured_plan"] = {
            "weeks": [
                "bad-week",
                {
                    "phase_label": "GPP",
                    "days": [
                        "bad-day",
                        {
                            "date": "2026-06-23",
                            "day_type": "high",
                            "sessions": [{"title": "Strength", "blocks": ["bad-block"]}],
                        },
                    ],
                },
            ]
        }

        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc),
        )

        assert view.active_plan.get("phase") == "GPP"

    @pytest.mark.parametrize("level", ["weeks", "days", "sessions", "blocks"])
    @pytest.mark.parametrize("bad", [5, True, 1.5, "text", {"x": 1}])
    def test_scalar_or_mapping_where_rows_belong_degrades_without_crashing(self, level, bad):
        blocks = bad if level == "blocks" else []
        sessions = bad if level == "sessions" else [{"title": "Strength", "blocks": blocks}]
        days = bad if level == "days" else [{"date": "2026-06-23", "day_type": "high", "sessions": sessions}]
        weeks = bad if level == "weeks" else [{"phase_label": "GPP", "days": days}]
        store = _store_with_plan()
        store.plans[PLAN]["structured_plan"] = {"weeks": weeks}

        build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc),
        )

    def test_other_plan_checkin_is_reused_in_command_view(self):
        store = _store_with_plan()
        store.plans[OTHER_PLAN] = {
            "id": OTHER_PLAN,
            "athlete_id": ATHLETE,
            "status": "ready",
            "plan_name": "Camp B",
            "created_at": "2026-05-31T00:00:00+00:00",
        }
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        submit_today_checkin(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=_checkin_payload(plan_id=OTHER_PLAN),
            now=now,
        )

        view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="", now=now)

        assert view.today.recommendation_state != "not_checked_in"
        assert view.today.warnings == []

    def test_next_session_crosses_into_following_week(self):
        # Week 0 trains only on Mon; the rest of the week is rest. On a later
        # day with no remaining training this week the next session lives in
        # week 1 (Wed). It must surface rather than reporting "No session found".
        store = _store_with_plan()
        store.plans[PLAN]["planning_brief"] = _multi_week_taper_brief()
        for now in (
            datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),  # Thu, week 0
            datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),  # Sat, week 0
            datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc),  # Sun, week 0
        ):
            view = build_today_command_view(
                store, athlete_id=ATHLETE, athlete_timezone="", now=now
            )
            assert view.today.next_session["calendar_date"] == "2026-06-24"
            assert view.today.next_session["session_relation"] == "next"

    def test_next_session_prefers_todays_training_day(self):
        # When today itself is the training day, the card shows today's session
        # and never jumps ahead to a later week.
        store = _store_with_plan()
        store.plans[PLAN]["planning_brief"] = _multi_week_taper_brief()
        view = build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),  # Mon, week 0
        )
        assert view.today.next_session["calendar_date"] == "2026-06-15"
        assert view.today.next_session["session_relation"] == "today"


class TestSessionCompletion:
    def _payload(self, **overrides) -> dict:
        base = {"plan_id": PLAN, "session_id": "sess-1", "status": "started"}
        return {**base, **overrides}

    def test_start_sets_started_at(self):
        store = _store_with_plan()
        row = _checked_in_completion(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=self._payload(status="started")
        )
        assert row["status"] == "started"
        assert row["started_at"]
        assert completion_landing_state(completion_status_of(row)) == "resume"

    def test_done_sets_completed_at(self):
        store = _store_with_plan()
        row = _checked_in_completion(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=self._payload(status="done")
        )
        assert row["status"] == "done"
        assert row["completed_at"]
        assert completion_landing_state(completion_status_of(row)) == "completed"

    def test_modified_requires_modification_reason(self):
        store = _store_with_plan()
        with pytest.raises(HTTPException) as exc:
            _checked_in_completion(
                store, athlete_id=ATHLETE, athlete_timezone="", payload=self._payload(status="modified")
            )
        assert exc.value.status_code == 422
        row = _checked_in_completion(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=self._payload(status="modified", modification_reason="swapped to recovery"),
        )
        assert row["status"] == "modified"
        assert row["completed_at"]

    def test_skipped_is_allowed(self):
        store = _store_with_plan()
        row = upsert_session_completion(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=self._payload(status="skipped", modification_reason="travel day"),
        )
        assert row["status"] == "skipped"
        assert completion_landing_state(completion_status_of(row)) == "completed"

    def test_skipped_requires_a_reason(self):
        store = _store_with_plan()
        with pytest.raises(HTTPException) as exc:
            upsert_session_completion(
                store, athlete_id=ATHLETE, athlete_timezone="", payload=self._payload(status="skipped")
            )
        assert exc.value.status_code == 422

    def _add_severe_injury(self, store, *, status: str = "open"):
        store.create_injury_flag(
            ATHLETE,
            {
                "source": "checkin",
                "plan_id": PLAN,
                "body_area": "chest",
                "description": "chest bruise",
                "severity": "severe",
                "status": status,
            },
        )

    @pytest.mark.parametrize("training_status", ["started", "done", "modified"])
    def test_active_severe_injury_blocks_training_completion(self, training_status):
        # Server-side hold: a severe injury rejects actually training the session,
        # so the block is enforced at the API, not just hidden in the UI.
        store = _store_with_plan()
        self._add_severe_injury(store)
        payload = self._payload(status=training_status)
        if training_status == "modified":
            payload["modification_reason"] = "tried anyway"
        with pytest.raises(HTTPException) as exc:
            upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="", payload=payload)
        assert exc.value.status_code == 409
        assert "severe injury" in exc.value.detail.lower()

    def test_severe_injury_easing_still_blocks_completion(self):
        # Monitoring status must not open completion.
        store = _store_with_plan()
        self._add_severe_injury(store, status="monitoring")
        with pytest.raises(HTTPException) as exc:
            upsert_session_completion(
                store, athlete_id=ATHLETE, athlete_timezone="", payload=self._payload(status="done")
            )
        assert exc.value.status_code == 409

    def test_severe_injury_still_allows_skipping_the_session(self):
        # The athlete can still log that they backed off.
        store = _store_with_plan()
        self._add_severe_injury(store)
        row = upsert_session_completion(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=self._payload(status="skipped", modification_reason="injury hold"),
        )
        assert row["status"] == "skipped"

    def test_moderate_injury_does_not_block_completion(self):
        store = _store_with_plan()
        store.create_injury_flag(
            ATHLETE,
            {"source": "checkin", "plan_id": PLAN, "body_area": "calf", "severity": "moderate", "status": "open"},
        )
        row = _checked_in_completion(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=self._payload(status="done")
        )
        assert row["status"] == "done"

    def test_resolved_severe_injury_does_not_block_completion_when_supplied(self):
        store = _store_with_plan()
        self._add_severe_injury(store, status="resolved")
        row = _checked_in_completion(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=self._payload(status="done")
        )
        assert row["status"] == "done"

    def test_duplicate_completion_upserts_single_row(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        _checked_in_completion(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=self._payload(status="started"), now=now
        )
        _checked_in_completion(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=self._payload(status="done"),
            now=now,
        )
        assert len(store.session_completions[ATHLETE]) == 1
        assert store.session_completions[ATHLETE][0]["status"] == "done"

    def test_completed_at_preserved_on_idempotent_resave(self):
        store = _store_with_plan()
        first = _checked_in_completion(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=self._payload(status="done"),
            now=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
        )
        resave = _checked_in_completion(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=self._payload(status="done", session_rpe=7),
            now=datetime(2026, 6, 18, 18, 0, tzinfo=timezone.utc),
        )
        # completed_at is not overwritten by the later save.
        assert resave["completed_at"] == first["completed_at"]
        assert resave["session_rpe"] == 7

    def test_backward_transition_clears_completed_at(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
        _checked_in_completion(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=self._payload(status="done"), now=now
        )
        back = _checked_in_completion(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=self._payload(status="started"), now=now
        )
        # Moving back to started keeps started_at but clears completed_at.
        assert back["status"] == "started"
        assert back["started_at"]
        assert back["completed_at"] is None

    def test_reset_to_not_started_clears_both_timestamps(self):
        store = _store_with_plan()
        now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
        _checked_in_completion(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=self._payload(status="done"), now=now
        )
        reset = _checked_in_completion(
            store, athlete_id=ATHLETE, athlete_timezone="", payload=self._payload(status="not_started"), now=now
        )
        assert reset["started_at"] is None
        assert reset["completed_at"] is None


class TestScanForwardForNextTraining:
    """Direct coverage for the cross-week scan, including dict-shaped entries.

    In production the schedule resolver yields ``WeeklyDayEntry`` objects, but
    the rest of the service treats schedule entries as either objects or plain
    dicts. These assert the scan reads ``calendar_date`` correctly for dict
    entries so it never mistakes a past dated session for the next one.
    """

    @staticmethod
    def _week(days):
        return SimpleNamespace(week_count=2, days=days)

    def _scan(self, current_days, later_days, training_date):
        current = self._week(current_days)
        later = self._week(later_days)
        with mock.patch.object(
            today_service_module,
            "weekly_schedule_or_none",
            lambda _row, *, week_index: later if week_index == 1 else None,
        ):
            return _scan_forward_for_next_training(
                {"id": PLAN},
                week=current,
                week_index=0,
                training_date=training_date,
            )

    def test_dict_entry_skips_past_dated_session(self):
        # A later week whose only training day is dated on/before today must be
        # skipped rather than surfaced as the "next" session.
        past = {"weekday": "Mon", "calendar_date": "2026-06-15", "effective_load": "technical"}
        result = self._scan([], [past], training_date=date(2026, 6, 20))
        assert result is None

    def test_dict_entry_returns_future_dated_session(self):
        future = {"weekday": "Wed", "calendar_date": "2026-06-24", "effective_load": "technical"}
        result = self._scan([], [future], training_date=date(2026, 6, 20))
        assert result is future

    def test_dict_entry_returns_earliest_future_dated_session(self):
        saturday = {"weekday": "Sat", "calendar_date": "2026-06-27", "effective_load": "technical"}
        friday = {"weekday": "Fri", "calendar_date": "2026-06-26", "effective_load": "reduced"}
        result = self._scan([], [saturday, friday], training_date=date(2026, 6, 23))
        assert result is friday

    def test_dict_entry_without_date_is_returned(self):
        # Undated (weekday-only) plans can't be compared, so any later-week
        # training day qualifies as the next session.
        entry = {"weekday": "Wed", "calendar_date": None, "effective_load": "hard"}
        result = self._scan([], [entry], training_date=date(2026, 6, 20))
        assert result is entry


class TestRetroLogCompletion:
    """Explicit past training_day = 7-day back-fill (retro-log) contract."""

    NOW = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)  # training day 2026-06-19

    def _payload(self, **overrides) -> dict:
        base = {"plan_id": PLAN, "session_id": "sess-1", "status": "done"}
        return {**base, **overrides}

    def _upsert(self, store, **overrides):
        return upsert_session_completion(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=self._payload(**overrides),
            now=self.NOW,
        )

    def _structured_store(self):
        store = _store_with_plan()
        store.plans[PLAN]["structured_plan"] = {
            "weeks": [
                {
                    "days": [
                        {
                            "date": "2026-06-16",
                            "sessions": [{"session_id": "sess-1", "title": "Lower power", "blocks": []}],
                        },
                        {
                            "date": "2026-06-17",
                            "sessions": [{"title": "Untitled primary", "blocks": []}],
                        },
                    ]
                }
            ]
        }
        return store

    def test_retro_done_persists_requested_training_day(self):
        store = _store_with_plan()
        row = self._upsert(store, training_day="2026-06-16", session_rpe=7)
        assert row["training_day"] == "2026-06-16"
        assert row["status"] == "done"
        assert row["completed_at"]

    def test_todays_date_in_training_day_behaves_like_the_normal_flow(self):
        store = _store_with_plan()
        # The normal flow: today's session needs today's check-in.
        with pytest.raises(HTTPException) as exc:
            self._upsert(store, training_day="2026-06-19", status="started")
        assert exc.value.status_code == 409
        submit_today_checkin(store, athlete_id=ATHLETE, athlete_timezone="", payload=_checkin_payload(), now=self.NOW)
        row = self._upsert(store, training_day="2026-06-19", status="started")
        assert row["training_day"] == "2026-06-19"
        assert row["status"] == "started"

    def test_future_training_day_is_rejected(self):
        store = _store_with_plan()
        with pytest.raises(HTTPException) as exc:
            self._upsert(store, training_day="2026-06-20")
        assert exc.value.status_code == 422
        assert "future" in exc.value.detail

    def test_older_than_window_is_rejected(self):
        store = _store_with_plan()
        # 2026-06-12 is exactly 7 days back — allowed; 2026-06-11 is not.
        row = self._upsert(store, training_day="2026-06-12")
        assert row["training_day"] == "2026-06-12"
        with pytest.raises(HTTPException) as exc:
            self._upsert(store, training_day="2026-06-11")
        assert exc.value.status_code == 422

    def test_past_day_rejects_non_terminal_statuses(self):
        store = _store_with_plan()
        for bad_status in ("started", "not_started"):
            with pytest.raises(HTTPException) as exc:
                self._upsert(store, training_day="2026-06-16", status=bad_status)
            assert exc.value.status_code == 422

    def test_retro_log_bypasses_the_severe_injury_hold(self):
        # The injury hold protects today's training; a retro-log records what
        # already happened, so it must not be blocked.
        store = _store_with_plan()
        store.create_injury_flag(
            ATHLETE,
            {
                "source": "checkin",
                "plan_id": PLAN,
                "body_area": "chest",
                "description": "chest bruise",
                "severity": "severe",
                "status": "open",
            },
        )
        row = self._upsert(store, training_day="2026-06-16")
        assert row["status"] == "done"
        # The same status logged for today stays blocked.
        with pytest.raises(HTTPException) as exc:
            self._upsert(store)
        assert exc.value.status_code == 409

    def test_structured_plan_rejects_days_outside_the_plan(self):
        store = self._structured_store()
        with pytest.raises(HTTPException) as exc:
            self._upsert(store, training_day="2026-06-15")
        assert exc.value.status_code == 422
        assert "not part of this plan" in exc.value.detail

    def test_structured_plan_rejects_sessions_not_scheduled_that_day(self):
        store = self._structured_store()
        with pytest.raises(HTTPException) as exc:
            self._upsert(store, training_day="2026-06-16", session_id="sess-9")
        assert exc.value.status_code == 422
        assert "not scheduled" in exc.value.detail

    def test_structured_plan_accepts_the_scheduled_session(self):
        store = self._structured_store()
        row = self._upsert(store, training_day="2026-06-16")
        assert row["training_day"] == "2026-06-16"

    def test_structured_plan_rejects_a_date_as_identity_for_an_idless_session(self):
        store = self._structured_store()
        with pytest.raises(HTTPException) as exc:
            self._upsert(store, training_day="2026-06-17", session_id="2026-06-17")
        assert exc.value.status_code == 422
        assert "not scheduled" in exc.value.detail

    def test_skipped_retro_log_still_requires_a_reason(self):
        store = _store_with_plan()
        with pytest.raises(HTTPException) as exc:
            self._upsert(store, training_day="2026-06-16", status="skipped")
        assert exc.value.status_code == 422
        row = self._upsert(
            store, training_day="2026-06-16", status="skipped", modification_reason="was travelling"
        )
        assert row["status"] == "skipped"


def _session_timing_structured_plan() -> dict:
    """Mon rest, Tue two sessions, Wed one session — the shapes timing turns on."""
    return {
        "weeks": [
            {
                "phase_label": "GPP",
                "days": [
                    {
                        "date": "2026-06-22",
                        "countdown_label": "D-25",
                        "day_type": "rest",
                        "today_card": {"headline": "Rest or active recovery"},
                        "sessions": [],
                    },
                    {
                        "date": "2026-06-23",
                        "countdown_label": "D-24",
                        "day_type": "high",
                        "today_card": {"headline": "Strength then conditioning"},
                        "sessions": [
                            {
                                "session_id": "2026-06-23-strength",
                                "session_type": "strength",
                                "title": "Posterior chain strength",
                                "blocks": [{"block_id": "hinge", "display_name": "Trap bar deadlift"}],
                            },
                            {
                                "session_id": "2026-06-23-conditioning",
                                "session_type": "conditioning",
                                "title": "Aerobic support",
                                "blocks": [{"block_id": "bike", "display_name": "Easy Assault Bike"}],
                            },
                        ],
                    },
                    {
                        "date": "2026-06-24",
                        "countdown_label": "D-23",
                        "day_type": "high",
                        "today_card": {"headline": "Hard sparring"},
                        "sessions": [
                            {
                                "session_id": "2026-06-24-sparring",
                                "session_type": "sparring",
                                "title": "Hard sparring",
                                "blocks": [{"block_id": "rounds", "display_name": "Sparring rounds"}],
                            }
                        ],
                    },
                ],
            }
        ]
    }


class TestSessionTimingAuthority:
    """``build_today_command_view`` is the only derivation of session timing.

    Every surface (Overview, Today, XP, the notification services) reads
    ``session_scope`` instead of deciding for itself, so these cases fix what
    that one derivation answers. They previously lived in the app as
    ``reconcileTodayWithPlanCard``, which meant Today applied them and Overview
    and XP did not — the same payload described three different ways.
    """

    def _view(self, store, *, day: int):
        return build_today_command_view(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            now=datetime(2026, 6, day, 12, 0, tzinfo=timezone.utc),
        )

    def _store(self) -> FakeStore:
        store = _store_with_plan()
        store.plans[PLAN]["structured_plan"] = _session_timing_structured_plan()
        return store

    def _complete(self, store, session_id: str, training_day: str, status: str = "done"):
        payload = {"plan_id": PLAN, "session_id": session_id, "status": status}
        if status == "skipped":
            payload["modification_reason"] = "was travelling"
        return _checked_in_completion(
            store,
            athlete_id=ATHLETE,
            athlete_timezone="",
            payload=payload,
            now=datetime.fromisoformat(f"{training_day}T12:00:00+00:00"),
        )

    def test_rest_day_scopes_forward_to_the_next_training_day(self):
        view = self._view(self._store(), day=22)

        assert view.today.session_scope == "next"
        assert view.today.session_label == "Next session"
        assert view.today.next_session["calendar_date"] == "2026-06-23"
        assert view.today.next_session["session_relation"] == "next"

    def test_session_today_is_scoped_to_today(self):
        view = self._view(self._store(), day=23)

        assert view.today.session_scope == "today"
        assert view.today.session_label == "Today's session"
        assert view.today.next_session["session_id"] == "2026-06-23-strength"
        assert view.today.completion_status == "not_started"

    def test_one_log_finishes_a_two_session_day(self):
        store = self._store()
        self._complete(store, "2026-06-23-strength", "2026-06-23")

        view = self._view(store, day=23)

        # A day is one session to the athlete: one start, one RPE, one log. The
        # log is written to every session the card schedules that day, so Today
        # rolls forward instead of asking for the conditioning block separately.
        assert view.today.session_scope == "next"
        assert view.today.next_session["session_id"] == "2026-06-24-sparring"
        assert view.today.completion_status == "done"
        sibling = store.get_session_completion(ATHLETE, "2026-06-23-conditioning", "2026-06-23")
        assert sibling is not None and sibling["status"] == "done"

    def test_starting_the_day_starts_every_session_on_it(self):
        store = self._store()
        self._complete(store, "2026-06-23-strength", "2026-06-23", status="started")

        view = self._view(store, day=23)

        assert view.today.session_scope == "today"
        assert view.today.next_session["session_id"] == "2026-06-23-strength"
        assert view.today.completion_status == "started"
        sibling = store.get_session_completion(ATHLETE, "2026-06-23-conditioning", "2026-06-23")
        assert sibling is not None and sibling["status"] == "started"

    def test_a_day_logged_on_only_one_of_its_sessions_reads_as_done(self):
        store = self._store()
        # Logged before one completion covered the whole day: the primary is
        # done while the second session was left started.
        store.upsert_session_completion(
            ATHLETE,
            {
                "plan_id": PLAN,
                "session_id": "2026-06-23-strength",
                "training_day": "2026-06-23",
                "status": "done",
            },
        )
        store.upsert_session_completion(
            ATHLETE,
            {
                "plan_id": PLAN,
                "session_id": "2026-06-23-conditioning",
                "training_day": "2026-06-23",
                "status": "started",
            },
        )

        view = self._view(store, day=23)

        assert view.today.session_scope == "next"
        assert view.today.completion_status == "done"

    def test_completing_every_session_rolls_forward_and_reports_today_done(self):
        store = self._store()
        self._complete(store, "2026-06-23-strength", "2026-06-23")
        self._complete(store, "2026-06-23-conditioning", "2026-06-23")

        view = self._view(store, day=23)

        assert view.today.session_scope == "next"
        assert view.today.session_label == "Next session"
        assert view.today.next_session["calendar_date"] == "2026-06-24"
        assert view.today.next_session["session_id"] == "2026-06-24-sparring"
        # completion_status always describes today, never the card on screen.
        assert view.today.completion_status == "done"

    def test_next_day_preview_skips_a_session_already_logged_ahead(self):
        store = self._store()
        self._complete(store, "2026-06-23-strength", "2026-06-23")
        self._complete(store, "2026-06-23-conditioning", "2026-06-23")
        self._complete(store, "2026-06-24-sparring", "2026-06-24", status="done")

        view = self._view(store, day=23)

        # Tomorrow's only session is already terminal, so it is not the next
        # session; nothing further is scheduled in this plan.
        assert view.today.session_scope != "today"
        assert view.today.next_session.get("session_id") != "2026-06-24-sparring"

    def test_skipping_the_day_skips_every_session_on_it(self):
        store = self._store()
        self._complete(store, "2026-06-23-strength", "2026-06-23", status="skipped")

        view = self._view(store, day=23)

        assert view.today.session_scope == "next"
        assert view.today.completion_status == "skipped"
        sibling = store.get_session_completion(ATHLETE, "2026-06-23-conditioning", "2026-06-23")
        assert sibling is not None and sibling["status"] == "skipped"

    def test_every_surface_reads_one_answer(self):
        from api.contracts.command_view import session_is_today

        store = self._store()

        assert session_is_today(self._view(store, day=23)) is True
        assert session_is_today(self._view(store, day=22)) is False


class TestCheckInRequiredToLog:
    """One rule (session_hold): today's session needs today's check-in, for everyone."""

    NOW = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)

    def test_logging_today_without_a_check_in_is_refused(self):
        store = _store_with_plan()
        for status in ("started", "done"):
            with pytest.raises(HTTPException) as exc:
                upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="",
                                          payload={"plan_id": PLAN, "session_id": "sess-1", "status": status}, now=self.NOW)
            assert exc.value.status_code == 409
            assert exc.value.detail == "Complete today's check-in before starting."

    def test_check_in_unlocks_logging(self):
        store = _store_with_plan()
        row = _checked_in_completion(store, payload={"plan_id": PLAN, "session_id": "sess-1", "status": "done"}, now=self.NOW)
        assert row["status"] == "done"

    def test_skipping_and_past_days_never_need_a_check_in(self):
        store = _store_with_plan()
        skipped = upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="", now=self.NOW,
            payload={"plan_id": PLAN, "session_id": "sess-1", "status": "skipped", "modification_reason": "travel"})
        assert skipped["status"] == "skipped"
        retro = upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="", now=self.NOW,
            payload={"plan_id": PLAN, "session_id": "sess-2", "status": "done", "training_day": "2026-06-17"})
        assert retro["status"] == "done"


def test_athlete_without_health_consent_can_log_without_a_check_in():
    from tests.support import withdraw_health_consent, seed_default_profiles
    store = _store_with_plan()
    seed_default_profiles(store)
    withdraw_health_consent(store, ATHLETE)
    row = upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="",
        payload={"plan_id": PLAN, "session_id": "sess-1", "status": "done"},
        now=datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc))
    assert row["status"] == "done"
