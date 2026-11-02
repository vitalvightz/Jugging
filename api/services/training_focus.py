"""The fighter's last recorded sparring focus, for the next relevant session.

Read from existing sparring logs only. Which sessions it is shown before is the
client's call, from the session's own classification; this only picks the
focus and says which sport it was recorded for, when that is known.
"""
from datetime import date, timedelta

# Older focuses are stale: a reminder from a month ago is noise, not context.
FOCUS_MAX_AGE_DAYS = 28


def latest_training_focus(store, athlete_id: str, *, today: str) -> dict:
    """The newest non-empty focus logged within ``FOCUS_MAX_AGE_DAYS``.

    A focus on a manual session that has since been deleted goes with it.
    ``sport`` is the manual session's sport; timer and plan logs leave it None
    (they follow the athlete's own sport).
    """
    from_day = (date.fromisoformat(today) - timedelta(days=FOCUS_MAX_AGE_DAYS - 1)).isoformat()
    for row in store.list_sparring_logs(athlete_id, limit=60, from_day=from_day, with_focus=True):
        focus = str(row.get("focus") or "").strip()
        if not focus:
            continue
        sport = None
        external_id = row.get("external_session_id")
        if external_id:
            session = store.get_external_training_session(athlete_id, str(external_id))
            if not session:
                continue
            sport = session.get("sport")
        return {"focus": focus, "training_day": str(row.get("training_day") or "")[:10], "sport": sport}
    return {"focus": None, "training_day": None, "sport": None}
