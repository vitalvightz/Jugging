"""Coach chat pushes, chat file links and the coach check-in nudge sweep.

Messages are written by the database (``coach_chat_send``), never here. This
module only tells the other participant that a message arrived, signs short
links to chat files for a participant, and sends the check-in nudges the
database has claimed. Push text never carries message text or health detail:
a lock screen is not private.

Every sender is best-effort: a push failure must never fail a request.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from api.datetimes import parse_utc_datetime
from api.services.notification_foundation import NotificationCandidate
from api.services.push_notifications import dispatch_push_candidate
from api.services.training_attachments import thumb_path
from api.store import AppStore

logger = logging.getLogger(__name__)

# A notify call for an older message is a replay, not a new message.
NOTIFY_FRESH_FOR = timedelta(minutes=10)
FILE_LINK_SECONDS = 120
MAX_FILES_PER_REQUEST = 30
NUDGE_CLAIM_LIMIT = 200
MAX_NAME_CHARS = 24
COACH_ROLES = {"coach", "admin"}


def _first_name(value: Any, fallback: str) -> str:
    name = " ".join(str(value or "").split())
    return (name.split(" ")[0] if name else "")[:MAX_NAME_CHARS] or fallback


def _approved(row: Mapping[str, Any] | None, roles: set[str]) -> bool:
    return bool(row) and row.get("role") in roles and row.get("access_status") == "approved"


def _zone(name: Any) -> str:
    text = str(name or "").strip()
    try:
        ZoneInfo(text)
        return text
    except Exception:  # noqa: BLE001 - unknown or empty zone
        return "UTC"


def _local_day(reference: datetime, zone: str) -> str:
    return reference.astimezone(ZoneInfo(zone)).date().isoformat()


def build_chat_push(*, message: Mapping[str, Any], recipient: Mapping[str, Any], sender_name: str,
                    now_utc: datetime) -> NotificationCandidate:
    """The push for one new message. It says who wrote and what kind of
    thing arrived, never the words."""
    coach_id, athlete_id = str(message["coach_id"]), str(message["athlete_id"])
    to_coach = str(recipient["id"]) == coach_id
    kind = message.get("kind")
    if kind == "photo":
        body = "Sent you a photo."
    elif kind == "voice":
        body = "Sent you a voice note."
    elif message.get("quote") and not str(message.get("body") or "").strip():
        body = "Shared a training session with you."
    else:
        body = "New message. Open the chat to read it."
    zone = _zone(recipient.get("athlete_timezone"))
    return NotificationCandidate(
        profile_id=str(recipient["id"]),
        notification_type="coach_chat_message",
        category="coach_messages",
        priority=40,
        title=_first_name(sender_name, "Your coach" if not to_coach else "Your fighter"),
        body=body,
        url=f"/coach/chats?with={athlete_id}" if to_coach else f"/chat?with={coach_id}",
        # One notification per chat on the device: a newer message replaces it.
        tag=f"coach-chat-{message['chat_id']}"[:80],
        dedupe_key=f"coach-chat:{message['id']}",
        expires_at=now_utc + timedelta(hours=12),
        timezone_name=zone,
        respect_quiet_hours=True,
        training_day=_local_day(now_utc, zone),
        notification_class="conversation",
        source_event_metadata={"chat_id": str(message["chat_id"]), "message_id": str(message["id"])},
    )


def notify_chat_message(store: AppStore, *, message: Mapping[str, Any], sender_name: str,
                        now_utc: datetime | None = None) -> int:
    """Tell the other participant about a message the caller just sent.

    The route has already checked the caller wrote ``message``. This rechecks
    the live connection and the recipient's account at send time, so an ended
    connection sends nothing.
    """
    reference = now_utc or datetime.now(timezone.utc)
    try:
        created = parse_utc_datetime(message.get("created_at"))
        if created is None or reference - created > NOTIFY_FRESH_FOR:
            return 0
        coach_id, athlete_id = str(message["coach_id"]), str(message["athlete_id"])
        if not store.get_accepted_coach_connection(coach_id, athlete_id):
            return 0
        to_coach = str(message.get("sender_id")) == athlete_id
        recipient = store.get_profile(coach_id if to_coach else athlete_id)
        if not _approved(recipient, COACH_ROLES if to_coach else {"athlete"}):
            return 0
        candidate = build_chat_push(message=message, recipient=recipient, sender_name=sender_name, now_utc=reference)
        return dispatch_push_candidate(store, candidate, now_utc=reference)
    except Exception:  # noqa: BLE001 - a push must never fail the send
        logger.exception("[coach-chat] push failed message_id=%s", message.get("id"))
        return 0


def chat_file_links(store: AppStore, *, viewer_id: str, message_ids: Sequence[int]) -> list[dict[str, Any]]:
    """Short links to the files on chat messages the viewer may read.

    One read for the messages, one for their files and one signing call for
    every link, however many files. A coach reads only while still connected;
    the fighter keeps their history. Anything else is simply left out.
    """
    ids = list(dict.fromkeys(int(value) for value in message_ids))[:MAX_FILES_PER_REQUEST]
    if not ids:
        return []
    rows = store.get_coach_chat_messages(ids)
    live: dict[tuple[str, str], bool] = {}
    readable = []
    for row in rows:
        coach_id, athlete_id = str(row.get("coach_id")), str(row.get("athlete_id"))
        if not row.get("asset_id") or viewer_id not in (coach_id, athlete_id):
            continue
        if viewer_id == coach_id:
            pair = (coach_id, athlete_id)
            if pair not in live:
                live[pair] = bool(store.get_accepted_coach_connection(coach_id, athlete_id))
            if not live[pair]:
                continue
        readable.append(row)
    if not readable:
        return []
    assets = store.get_attachment_assets([str(row["asset_id"]) for row in readable])
    files = []
    for row in readable:
        asset = assets.get(str(row["asset_id"]))
        if not asset or asset.get("status") != "ready" or not asset.get("ready_path") \
                or asset.get("kind") not in ("photo", "voice") or asset.get("kind") != row.get("kind"):
            continue
        files.append((row, asset))
    paths = [asset["ready_path"] for _, asset in files]
    paths += [thumb_path(asset) for _, asset in files if asset["kind"] == "photo"]
    urls = store.attachment_signed_urls(paths, expires_in=FILE_LINK_SECONDS) if paths else {}
    links = []
    for row, asset in files:
        url = urls.get(asset["ready_path"])
        if not url:
            continue
        links.append({
            "message_id": int(row["id"]),
            "kind": asset["kind"],
            "url": url,
            "thumb_url": (urls.get(thumb_path(asset)) or url) if asset["kind"] == "photo" else None,
            "width": asset.get("width"),
            "height": asset.get("height"),
            "duration_ms": asset.get("duration_ms"),
        })
    return links


def build_checkin_nudge(row: Mapping[str, Any], *, now_utc: datetime) -> NotificationCandidate:
    coach = _first_name(row.get("coach_name"), "Your coach")
    day = str(row["training_day"])
    return NotificationCandidate(
        profile_id=str(row["athlete_id"]),
        notification_type="coach_checkin_nudge",
        category="checkin_reminders",
        priority=25,
        title=f"{coach} wants your check-in" if coach != "Your coach" else "Your coach wants your check-in",
        body="Sleep, body and pain. Twenty seconds, then you're set.",
        url="/today#today-checkin",
        # Shares the readiness reminder's tag: the device shows one, not two.
        tag="readiness-checkin",
        dedupe_key=f"coach-checkin:{row['nudge_id']}:{day}",
        expires_at=now_utc + timedelta(hours=2),
        timezone_name=_zone(row.get("timezone")),
        respect_quiet_hours=True,
        training_day=day,
        notification_class="routine",
        source_event_metadata={"nudge_id": str(row["nudge_id"]), "coach_id": str(row.get("coach_id") or "")},
    )


def run_coach_checkin_nudge_sweep(store: AppStore, *, now_utc: datetime | None = None,
                                  should_stop: Callable[[], bool] | None = None) -> int:
    """Send every check-in nudge due now.

    One database call finds and leases them all: fighters who already checked
    in, minors, and anyone without health-data consent or a live connection are
    excluded there. A lease lasts 15 minutes. Only a push that went out
    confirms the day; anything else (a failed delivery, a crash, quiet hours)
    leaves the nudge to be claimed again inside its two-hour window. The push
    ledger dedupes on the same key, so a retry never doubles a delivered push.
    """
    reference = now_utc or datetime.now(timezone.utc)
    rows = store.claim_coach_checkin_nudges(NUDGE_CLAIM_LIMIT)
    sent = 0
    for row in rows:
        if should_stop is not None and should_stop():
            break
        try:
            delivered = dispatch_push_candidate(store, build_checkin_nudge(row, now_utc=reference), now_utc=reference)
            if delivered:
                store.confirm_coach_checkin_nudge(str(row["nudge_id"]), str(row["training_day"]))
            sent += delivered
        except Exception:  # noqa: BLE001 - one fighter's push must not stop the sweep
            logger.exception("[coach-chat] check-in nudge failed nudge_id=%s", row.get("nudge_id"))
    if rows:
        logger.info("[coach-chat] check-in nudges claimed=%s sent=%s", len(rows), sent)
    return sent


__all__ = [
    "build_chat_push",
    "build_checkin_nudge",
    "chat_file_links",
    "notify_chat_message",
    "run_coach_checkin_nudge_sweep",
]
