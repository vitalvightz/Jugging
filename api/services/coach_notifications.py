"""Pushes between a coach and a connected fighter.

Each one is a ``coach_message`` candidate, so it obeys the recipient's own
"Coach messages" switch, quiet hours, spacing and the delivery ledger. Every
sender re-checks the accepted connection at send time: an ended connection
sends nothing. Push text never carries a coach's note or a health detail; it
only says where to look, because a lock screen is not private.

All senders are best-effort. A push failure must never fail the request that
triggered it, so callers run these as background tasks.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from api.services.progress_notifications import send_coach_message_notification
from api.store import AppStore

logger = logging.getLogger(__name__)

MAX_NAME_CHARS = 40


def _first_name(value: Any, fallback: str) -> str:
    name = " ".join(str(value or "").split())
    first = name.split(" ")[0] if name else ""
    return first[:MAX_NAME_CHARS] or fallback


def _profile(store: AppStore, profile_id: str) -> Mapping[str, Any] | None:
    try:
        return store.get_profile(profile_id)
    except Exception:  # noqa: BLE001 - lookups must not break the trigger
        logger.exception("[coach-push] profile lookup failed profile_id=%s", profile_id)
        return None


def _approved(row: Mapping[str, Any] | None, roles: set[str]) -> bool:
    return bool(row) and row.get("role") in roles and row.get("access_status") == "approved"


def _timezone(row: Mapping[str, Any] | None) -> str:
    return str((row or {}).get("athlete_timezone") or "").strip() or "UTC"


def _send(store: AppStore, *, recipient: Mapping[str, Any], message_id: str, title: str, body: str,
          url: str, urgent: bool = False) -> int:
    try:
        return send_coach_message_notification(
            store,
            athlete_id=str(recipient["id"]),
            message_id=message_id,
            title=title,
            body=body,
            url=url,
            timezone_name=_timezone(recipient),
            urgent=urgent,
        )
    except Exception:  # noqa: BLE001 - one push must never fail the trigger
        logger.exception("[coach-push] send failed message_id=%s", message_id)
        return 0


def notify_fighter_of_feedback(store: AppStore, *, coach_id: str, athlete_id: str,
                               feedback: Mapping[str, Any]) -> int:
    """A coach saved a note on one of the fighter's records."""
    if not store.get_accepted_coach_connection(coach_id, athlete_id):
        return 0
    fighter = _profile(store, athlete_id)
    if not _approved(fighter, {"athlete"}):
        return 0
    coach = _first_name(feedback.get("coach_name"), "Your coach")
    # updated_at in the key: an edited note is a new message, a retry is not.
    stamp = str(feedback.get("updated_at") or feedback.get("created_at") or "")
    return _send(
        store,
        recipient=fighter,
        message_id=f"feedback:{feedback.get('id')}:{stamp}",
        title=f"Feedback from {coach}" if coach != "Your coach" else "New coach feedback",
        body="Your coach left a note on your training. Open History to read it.",
        url="/history",
    )


def notify_fighter_of_plan(store: AppStore, *, coach_id: str, athlete_id: str,
                           plan: Mapping[str, Any]) -> int:
    """A coach sent the fighter a plan to accept or decline."""
    if not store.get_accepted_coach_connection(coach_id, athlete_id):
        return 0
    fighter = _profile(store, athlete_id)
    if not _approved(fighter, {"athlete"}):
        return 0
    coach = _first_name(plan.get("coach_name"), "Your coach")
    return _send(
        store,
        recipient=fighter,
        message_id=f"plan-sent:{plan.get('id')}:{plan.get('assigned_at') or ''}",
        title=f"{coach} sent you a plan" if coach != "Your coach" else "Your coach sent you a plan",
        body="Review it in Plans, then accept or decline.",
        url="/plans",
    )


def notify_coach_of_plan_response(store: AppStore, *, plan: Mapping[str, Any], fighter_name: str,
                                  accepted: bool) -> int:
    """The fighter accepted or declined a coach's plan."""
    coach_id = str(plan.get("athlete_id") or "")
    fighter_id = str(plan.get("fighter_id") or "")
    if not coach_id or not fighter_id or not store.get_accepted_coach_connection(coach_id, fighter_id):
        return 0
    coach = _profile(store, coach_id)
    if not _approved(coach, {"coach", "admin"}):
        return 0
    name = _first_name(fighter_name, "Your fighter")
    verb = "accepted" if accepted else "declined"
    return _send(
        store,
        recipient=coach,
        message_id=f"plan-{verb}:{plan.get('id')}",
        title=f"{name} {verb} your plan",
        body=("It is now their active plan." if accepted
              else "Open their plans to revise it or send another."),
        url=f"/coach/fighters/{fighter_id}",
    )


def notify_coaches_of_contact_concern(store: AppStore, *, athlete_id: str, fighter_name: str,
                                      log: Mapping[str, Any]) -> int:
    """The fighter logged being rocked, or heavy head contact, in sparring.

    Only coaches the fighter shares injury status with are told: that is the
    same consent that shows this report on the coach's overview.
    """
    if not (log.get("rocked") or log.get("head_contact") == "heavy"):
        return 0
    fighter = _profile(store, athlete_id)
    if not _approved(fighter, {"athlete"}):
        return 0
    try:
        connections = store.list_accepted_coach_connections_for_athlete(athlete_id)
    except Exception:  # noqa: BLE001 - lookups must not break the trigger
        logger.exception("[coach-push] connection lookup failed athlete_id=%s", athlete_id)
        return 0
    name = _first_name(fighter_name, "A fighter")
    sent = 0
    for connection in connections:
        if connection.get("status") != "accepted" or connection.get("share_injuries") is not True:
            continue
        coach = _profile(store, str(connection.get("coach_id") or ""))
        if not _approved(coach, {"coach", "admin"}):
            continue
        sent += _send(
            store,
            recipient=coach,
            message_id=f"contact:{log.get('id')}",
            title=f"Check in with {name}",
            body="A sparring head-contact concern was logged. Open their overview.",
            url=f"/coach/fighters/{athlete_id}",
            urgent=True,
        )
    return sent


__all__ = [
    "notify_coach_of_plan_response",
    "notify_coaches_of_contact_concern",
    "notify_fighter_of_feedback",
    "notify_fighter_of_plan",
]
