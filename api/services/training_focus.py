"""The fighter's recent training focuses, for the next relevant session.

Two kinds, newest training day first:

* ``athlete``: a focus the fighter wrote after sparring (always kept as is);
* ``inferred``: one AI cue drawn from a note they already wrote (see
  training_focus_inference.py), with the metadata Today matches on.

Which one Today shows, and before which session, is the client's call
(web/lib/training-focus.ts): the first item that fits the session the card
shows. This only reads, verifies and orders them.
"""
from datetime import date, timedelta

from api.services.training_focus_inference import note_revision, read_source

# Older focuses are stale: a reminder from a month ago is noise, not context.
FOCUS_MAX_AGE_DAYS = 28
_MAX_ITEMS = 5


def _explicit_focuses(store, athlete_id: str, from_day: str) -> list[dict]:
    """Non-empty sparring focuses, newest first. A focus on a manual session
    that has since been deleted goes with it. ``sport`` is the manual session's
    sport; timer and plan logs leave it None (they follow the athlete's sport)."""
    items = []
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
        items.append({"id": str(row.get("id") or ""), "origin": "athlete", "focus": focus,
                      "training_day": str(row.get("training_day") or "")[:10], "sport": sport,
                      "session_id": row.get("session_id"), "external_session_id": str(external_id or "") or None,
                      "created_at": str(row.get("created_at") or "")})
        if len(items) >= _MAX_ITEMS:
            break
    return items


def _inferred_focuses(store, athlete_id: str, from_day: str, explicit: list[dict]) -> list[dict]:
    """Ready inferences whose note is still exactly what they were generated
    from, on a live record, with no explicit focus from the same session."""
    explicit_logs = {item["id"] for item in explicit}
    explicit_manual = {item["external_session_id"] for item in explicit if item["external_session_id"]}
    explicit_plan = {(item["session_id"], item["training_day"]) for item in explicit if item["session_id"]}
    items = []
    for row in store.list_training_focus_inferences(athlete_id, from_day=from_day, limit=_MAX_ITEMS):
        kind, source_id = row.get("source_kind"), str(row.get("source_id") or "")
        if kind == "sparring_log" and source_id in explicit_logs:
            continue
        if kind == "manual_takeaway" and source_id in explicit_manual:
            continue
        source = read_source(store, athlete_id, str(kind), source_id)
        if not source or source["explicit_focus"] or note_revision(source["note"]) != row.get("source_revision"):
            continue
        if kind == "plan_completion" and (source["row"].get("session_id"), source["training_day"]) in explicit_plan:
            continue
        items.append({"id": str(row["id"]), "origin": "inferred", "focus": row.get("focus"),
                      "training_day": str(row.get("training_day") or "")[:10], "sport": row.get("sport"),
                      "scope": row.get("scope"), "category": row.get("category"),
                      "exercise_targets": list(row.get("exercise_targets") or []),
                      "movement_patterns": list(row.get("movement_patterns") or []),
                      "created_at": str(row.get("updated_at") or "")})
    return items


def latest_training_focus(store, athlete_id: str, *, today: str, include_inferred: bool = True) -> dict:
    """The newest explicit focus (``focus``/``training_day``/``sport``, read by
    older clients unchanged) and every candidate in ``items``."""
    from_day = (date.fromisoformat(today) - timedelta(days=FOCUS_MAX_AGE_DAYS - 1)).isoformat()
    explicit = _explicit_focuses(store, athlete_id, from_day)
    inferred = _inferred_focuses(store, athlete_id, from_day, explicit) if include_inferred else []
    # Newest day first; on the same day the athlete's own words come first.
    items = sorted(explicit + inferred, key=lambda item: (item["training_day"], item["origin"] == "athlete",
                                                          item["created_at"]), reverse=True)[:_MAX_ITEMS]
    for item in items:
        for key in ("created_at", "session_id", "external_session_id"):
            item.pop(key, None)
    newest = explicit[0] if explicit else None
    return {"focus": newest["focus"] if newest else None,
            "training_day": newest["training_day"] if newest else None,
            "sport": newest["sport"] if newest else None,
            "items": items}
