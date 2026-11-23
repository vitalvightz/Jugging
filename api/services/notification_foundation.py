"""Server-owned notification preferences, arbitration and delivery deduplication.

This module is deliberately independent from any one coaching trigger. Callers
submit typed candidates; the foundation applies account preferences, quiet
hours, expiry, priority and a profile-level delivery claim before Web Push is
attempted. The same delivery key therefore cannot fan out twice just because an
athlete has multiple devices or a worker restarts.
"""

from __future__ import annotations

import logging
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Mapping
from zoneinfo import ZoneInfo

from api.notification_models import NotificationCategory, NotificationPreferences
from api.datetimes import parse_utc_datetime as _parse_datetime

logger = logging.getLogger(__name__)

NOTIFICATION_TITLE_MAX_CHARS = 40
NOTIFICATION_BODY_MAX_CHARS = 90
NOTIFICATION_URL_MAX_CHARS = 500
NOTIFICATION_TAG_MAX_CHARS = 80
NOTIFICATION_DEDUPE_KEY_MAX_CHARS = 160
NOTIFICATION_TYPE_MAX_CHARS = 64
NOTIFICATION_MAX_ATTEMPTS = 3
NOTIFICATION_STALE_CLAIM_AFTER = timedelta(minutes=15)
ROUTINE_DAILY_CAP = 6
SAFETY_DAILY_CAP = 2
EVENT_DAILY_CAP = 3
CONVERSATION_DAILY_CAP = 40
CONVERSATION_MIN_SPACING_MINUTES = 1
ROUTINE_MIN_SPACING_MINUTES = 45

# Repeated worker sweeps must keep *evaluating* every intent, but persisting an
# unchanged diagnostic every ten minutes only churns the ledger. A changed
# decision, reason, dedupe key, source, timing, or candidate produces a
# different evaluation key and is therefore inserted immediately, so throttling
# never delays a state change - including a safety state change.
HIGH_FREQUENCY_DIAGNOSTIC_INTENTS = frozenset({
    "session_stop",
    "session_modified",
    "session_near",
    "session_ready",
    "session_preparation",
    "post_session_log",
    "injury_recheck",
    "high_pain_followup",
})
HIGH_FREQUENCY_PERSIST_INTERVAL = timedelta(minutes=30)
STABLE_PERSIST_INTERVAL = timedelta(hours=6)


def diagnostic_persist_interval(intent: str) -> timedelta:
    """How long an identical diagnostic fact may go without being rewritten."""

    if intent in HIGH_FREQUENCY_DIAGNOSTIC_INTENTS:
        return HIGH_FREQUENCY_PERSIST_INTERVAL
    return STABLE_PERSIST_INTERVAL

DeliveryStatus = Literal["sent", "partial", "failed"]
# "conversation" is coach chat: its own daily allowance, so a busy chat never
# uses up the event allowance that plan, feedback and safety alerts share.
NotificationClass = Literal["routine", "safety", "event", "conversation"]
TimingConfidence = Literal["high", "medium", "low"]


class NotificationStoreError(RuntimeError):
    """The notification preference/ledger store could not be used."""


@dataclass(frozen=True)
class NotificationCandidate:
    profile_id: str
    notification_type: str
    category: NotificationCategory
    priority: int
    title: str
    body: str
    url: str
    tag: str
    dedupe_key: str
    expires_at: datetime
    timezone_name: str = "UTC"
    respect_quiet_hours: bool = True
    intent: str = ""
    training_day: str | None = None
    scheduled_for: datetime | None = None
    timing_source: str | None = None
    timing_confidence: TimingConfidence | None = None
    variant_id: str | None = None
    source_event_metadata: Mapping[str, Any] = field(default_factory=dict)
    action_key: str | None = None
    notification_class: NotificationClass = "routine"
    merged_intents: tuple[str, ...] = ()
    daily_cap: int | None = None
    min_spacing_minutes: int | None = None

    def __post_init__(self) -> None:
        normalized = {
            "profile_id": str(self.profile_id or "").strip(),
            "notification_type": str(self.notification_type or "").strip(),
            "title": str(self.title or "").strip(),
            "body": str(self.body or "").strip(),
            "url": str(self.url or "").strip(),
            "tag": str(self.tag or "").strip(),
            "dedupe_key": str(self.dedupe_key or "").strip(),
            "timezone_name": str(self.timezone_name or "").strip() or "UTC",
            "intent": str(self.intent or self.notification_type or "").strip(),
        }
        for key, value in normalized.items():
            object.__setattr__(self, key, value)

        if not self.profile_id:
            raise ValueError("notification profile_id is required")
        if not self.notification_type or len(self.notification_type) > NOTIFICATION_TYPE_MAX_CHARS:
            raise ValueError("notification_type is invalid")
        if not 1 <= int(self.priority) <= 100:
            raise ValueError("notification priority must be between 1 and 100")
        if not self.title or len(self.title) > NOTIFICATION_TITLE_MAX_CHARS:
            raise ValueError(f"notification title must be 1-{NOTIFICATION_TITLE_MAX_CHARS} characters")
        if not self.body or len(self.body) > NOTIFICATION_BODY_MAX_CHARS:
            raise ValueError(f"notification body must be 1-{NOTIFICATION_BODY_MAX_CHARS} characters")
        if not self.url.startswith("/") or len(self.url) > NOTIFICATION_URL_MAX_CHARS:
            raise ValueError("notification URL must be a bounded app-relative path")
        if not self.tag or len(self.tag) > NOTIFICATION_TAG_MAX_CHARS:
            raise ValueError("notification tag is invalid")
        if not self.dedupe_key or len(self.dedupe_key) > NOTIFICATION_DEDUPE_KEY_MAX_CHARS:
            raise ValueError("notification dedupe_key is invalid")
        if self.expires_at.tzinfo is None:
            object.__setattr__(self, "expires_at", self.expires_at.replace(tzinfo=timezone.utc))
        if self.scheduled_for is not None and self.scheduled_for.tzinfo is None:
            object.__setattr__(
                self,
                "scheduled_for",
                self.scheduled_for.replace(tzinfo=timezone.utc),
            )
        if self.timing_confidence not in {None, "high", "medium", "low"}:
            raise ValueError("notification timing_confidence is invalid")
        if self.notification_class not in {"routine", "safety", "event", "conversation"}:
            raise ValueError("notification_class is invalid")
        if self.daily_cap is not None and int(self.daily_cap) < 1:
            raise ValueError("notification daily_cap must be positive")
        if self.min_spacing_minutes is not None and int(self.min_spacing_minutes) < 0:
            raise ValueError("notification min spacing cannot be negative")
        object.__setattr__(
            self,
            "source_event_metadata",
            dict(self.source_event_metadata or {}),
        )
        object.__setattr__(
            self,
            "merged_intents",
            tuple(str(value).strip() for value in self.merged_intents if str(value).strip()),
        )


@dataclass(frozen=True)
class NotificationDeliveryClaim:
    delivery_id: str
    claim_token: str
    attempt_count: int


@dataclass(frozen=True)
class NotificationClaimAttempt:
    claim: NotificationDeliveryClaim | None
    decision: str


def default_notification_preferences() -> NotificationPreferences:
    return NotificationPreferences()


def _candidate_training_day(candidate: NotificationCandidate, now_utc: datetime) -> str:
    if candidate.training_day:
        return str(candidate.training_day)
    return _local_now(now_utc, candidate.timezone_name).date().isoformat()


def _candidate_daily_cap(candidate: NotificationCandidate) -> int:
    if candidate.daily_cap is not None:
        return int(candidate.daily_cap)
    return {
        "routine": ROUTINE_DAILY_CAP,
        "safety": SAFETY_DAILY_CAP,
        "event": EVENT_DAILY_CAP,
        "conversation": CONVERSATION_DAILY_CAP,
    }[candidate.notification_class]


def _candidate_min_spacing(candidate: NotificationCandidate) -> int:
    if candidate.min_spacing_minutes is not None:
        return int(candidate.min_spacing_minutes)
    if candidate.notification_class == "routine":
        return ROUTINE_MIN_SPACING_MINUTES
    if candidate.notification_class == "safety":
        return 30
    if candidate.notification_class == "conversation":
        return CONVERSATION_MIN_SPACING_MINUTES
    return 30


def _evaluation_key(
    *,
    training_day: str,
    intent: str,
    dedupe_key: str,
    decision: str,
    rejection_reasons: Iterable[str],
    scheduled_for: datetime | None,
    diagnostic_context: Mapping[str, Any] | None = None,
) -> str:
    payload = {
        "training_day": training_day,
        "intent": intent,
        "dedupe_key": dedupe_key,
        "decision": decision,
        "rejection_reasons": sorted(set(rejection_reasons)),
        # Coalesce an unchanged decision for the same scheduled moment while
        # preserving separate diagnostics for genuinely separate reminders.
        "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
        # Different timing evidence, template variants, action sources, or
        # orchestration classes must remain separate diagnostic facts even when
        # their top-level decision text is identical.
        "diagnostic_context": dict(diagnostic_context or {}),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_notification_evaluation(
    store: Any,
    *,
    profile_id: str,
    training_day: str,
    intent: str,
    now_utc: datetime,
    decision: str,
    rejection_reasons: Iterable[str] = (),
    eligible: bool = False,
    candidate: NotificationCandidate | None = None,
    resulting_delivery_id: str | None = None,
    source_event_metadata: Mapping[str, Any] | None = None,
    min_persist_interval: timedelta | None = None,
) -> dict[str, Any]:
    """Persist one auditable decision, coalescing only an identical state.

    ``evaluation_count`` plus first/last timestamps retains how often the state
    was observed. A new reason, scheduled moment, dedupe key, or decision gets a
    separate row, so day/intent diagnostics remain exact without a row explosion.
    """

    reference = now_utc if now_utc.tzinfo is not None else now_utc.replace(tzinfo=timezone.utc)
    reasons = tuple(sorted({str(reason).strip() for reason in rejection_reasons if str(reason).strip()}))
    scheduled_for = candidate.scheduled_for if candidate else None
    dedupe_key = candidate.dedupe_key if candidate else ""
    metadata = dict(source_event_metadata or {})
    if candidate is not None:
        metadata = {**dict(candidate.source_event_metadata), **metadata}
    evaluation_key = _evaluation_key(
        training_day=training_day,
        intent=intent,
        dedupe_key=dedupe_key,
        decision=decision,
        rejection_reasons=reasons,
        scheduled_for=scheduled_for,
        diagnostic_context={
            "notification_type": candidate.notification_type if candidate else None,
            "category": candidate.category if candidate else None,
            "priority": candidate.priority if candidate else None,
            "timing_source": candidate.timing_source if candidate else None,
            "timing_confidence": candidate.timing_confidence if candidate else None,
            "variant_id": candidate.variant_id if candidate else None,
            "notification_class": candidate.notification_class if candidate else None,
            "action_key": candidate.action_key if candidate else None,
            "source_event_metadata": metadata,
        },
    )
    if candidate is not None:
        metadata.setdefault(
            "_candidate_snapshot",
            {
                "title": candidate.title,
                "body": candidate.body,
                "url": candidate.url,
                "tag": candidate.tag,
                "expires_at": candidate.expires_at.isoformat(),
                "notification_class": candidate.notification_class,
                "action_key": candidate.action_key,
                "merged_intents": list(candidate.merged_intents),
                "daily_cap": _candidate_daily_cap(candidate),
                "min_spacing_minutes": _candidate_min_spacing(candidate),
                "respect_quiet_hours": candidate.respect_quiet_hours,
            },
        )
    row = {
        "profile_id": profile_id,
        "training_day": training_day,
        "intent": intent,
        "notification_type": candidate.notification_type if candidate else None,
        "category": candidate.category if candidate else None,
        "evaluated_at": reference.isoformat(),
        "first_evaluated_at": reference.isoformat(),
        "last_evaluated_at": reference.isoformat(),
        "evaluation_count": 1,
        "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
        "timing_source": candidate.timing_source if candidate else None,
        "timing_confidence": candidate.timing_confidence if candidate else None,
        "eligible": bool(eligible),
        "decision": decision,
        "rejection_reasons": list(reasons),
        "priority": candidate.priority if candidate else None,
        "dedupe_key": dedupe_key or None,
        "variant_id": candidate.variant_id if candidate else None,
        "source_event_metadata": metadata,
        "resulting_delivery_id": resulting_delivery_id,
        "evaluation_key": evaluation_key,
    }

    min_interval_seconds = (
        max(0, int(min_persist_interval.total_seconds()))
        if min_persist_interval is not None
        else 0
    )
    try:
        result = store.record_notification_evaluation(
            dict(row), min_interval_seconds=min_interval_seconds
        )
    except Exception as exc:  # noqa: BLE001 - observability must not enable a send
        logger.warning(
            "[notification] evaluation write failed profile_id=%s intent=%s error_class=%s",
            profile_id,
            intent,
            type(exc).__name__,
        )
        raise NotificationStoreError("notification evaluation ledger unavailable") from exc
    return dict(result) if isinstance(result, Mapping) else row


def list_notification_evaluations(
    store: Any,
    *,
    profile_id: str,
    training_day: str,
    intent: str | None = None,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store.list_notification_evaluations(profile_id, training_day, intent=intent) or []
    ]


def has_notification_evaluation_decision(
    store: Any,
    *,
    profile_id: str,
    dedupe_key: str,
    decision: str,
) -> bool:
    """Has this exact profile/dedupe key ever recorded this exact decision?

    Deliberately a targeted, index-backed existence check rather than a
    historical scan: the deferred-event sweep needs to know whether one source
    event was already resolved, not to reload notification history.
    """

    profile_id = str(profile_id or "").strip()
    dedupe_key = str(dedupe_key or "").strip()
    decision = str(decision or "").strip()
    if not profile_id or not dedupe_key or not decision:
        return False
    try:
        return bool(
            store.has_notification_evaluation_decision(
                profile_id, dedupe_key=dedupe_key, decision=decision
            )
        )
    except Exception as exc:  # noqa: BLE001 - adapter normalizes backend clients
        logger.warning(
            "[notification] evaluation decision lookup failed profile_id=%s error_class=%s",
            profile_id,
            type(exc).__name__,
        )
        raise NotificationStoreError("notification evaluation ledger unavailable") from exc


def get_notification_preferences(store: Any, profile_id: str) -> NotificationPreferences:
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        raise NotificationStoreError("profile_id is required")

    try:
        return NotificationPreferences.model_validate(
            store.get_notification_preferences(profile_id) or {}
        )
    except Exception as exc:  # noqa: BLE001 - adapter normalizes backend clients
        logger.warning(
            "[notification] preference read failed profile_id=%s error_class=%s",
            profile_id,
            type(exc).__name__,
        )
        raise NotificationStoreError("notification preferences unavailable") from exc


def update_notification_preferences(
    store: Any,
    profile_id: str,
    changes: dict[str, Any],
) -> NotificationPreferences:
    """Persist a preference patch.

    ``push_enabled`` is a gate, not a bulk write: pausing the account suppresses
    every category at delivery time (see ``candidate_is_allowed``) while leaving
    the per-category choices stored, so resuming restores exactly what the
    athlete had before rather than switching everything back on.
    """

    current = get_notification_preferences(store, profile_id)
    merged = current.model_dump()
    merged.update(changes)
    validated = NotificationPreferences.model_validate(merged)

    try:
        row = store.upsert_notification_preferences(profile_id, validated.model_dump())
        return NotificationPreferences.model_validate(row or validated.model_dump())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[notification] preference write failed profile_id=%s error_class=%s",
            profile_id,
            type(exc).__name__,
        )
        raise NotificationStoreError("notification preferences unavailable") from exc


def _local_now(now_utc: datetime, timezone_name: str) -> datetime:
    reference = now_utc if now_utc.tzinfo is not None else now_utc.replace(tzinfo=timezone.utc)
    try:
        return reference.astimezone(ZoneInfo(timezone_name))
    except Exception:  # noqa: BLE001 - invalid client timezone falls back safely
        return reference.astimezone(timezone.utc)


def is_within_quiet_hours(local_now: datetime, preferences: NotificationPreferences) -> bool:
    if not preferences.quiet_hours_enabled:
        return False
    start_hour, start_minute = (int(part) for part in preferences.quiet_hours_start.split(":"))
    end_hour, end_minute = (int(part) for part in preferences.quiet_hours_end.split(":"))
    current = local_now.hour * 60 + local_now.minute
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def candidate_is_allowed(
    candidate: NotificationCandidate,
    preferences: NotificationPreferences,
    *,
    now_utc: datetime,
) -> bool:
    return not candidate_rejection_reasons(candidate, preferences, now_utc=now_utc)


def candidate_rejection_reasons(
    candidate: NotificationCandidate,
    preferences: NotificationPreferences,
    *,
    now_utc: datetime,
) -> tuple[str, ...]:
    reference = now_utc if now_utc.tzinfo is not None else now_utc.replace(tzinfo=timezone.utc)
    reasons: list[str] = []
    if candidate.expires_at.astimezone(timezone.utc) <= reference.astimezone(timezone.utc):
        reasons.append("outside_due_window")
    if not preferences.push_enabled:
        reasons.append("push_disabled")
    if not bool(getattr(preferences, candidate.category)):
        reasons.append("category_disabled")
    if candidate.respect_quiet_hours and is_within_quiet_hours(
        _local_now(reference, candidate.timezone_name), preferences
    ):
        reasons.append("quiet_hours")
    return tuple(reasons)


def select_notification_candidate(
    candidates: Iterable[NotificationCandidate],
    preferences: NotificationPreferences,
    *,
    now_utc: datetime,
) -> NotificationCandidate | None:
    eligible = [
        candidate
        for candidate in candidates
        if candidate_is_allowed(candidate, preferences, now_utc=now_utc)
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda candidate: (
            candidate.priority,
            candidate.notification_type,
            candidate.dedupe_key,
        ),
    )


def _simulation_state(
    store: Any,
    candidates: list[NotificationCandidate],
    *,
    now_utc: datetime,
) -> tuple[list[dict[str, Any]], set[tuple[str, str, str]]]:
    """Read the claim ledger and action state without changing either."""

    profile_id = candidates[0].profile_id
    try:
        state = store.get_notification_simulation_state(
            profile_id,
            dedupe_keys=sorted({candidate.dedupe_key for candidate in candidates}),
            training_days=sorted(
                {_candidate_training_day(candidate, now_utc) for candidate in candidates}
            ),
            notification_classes=sorted({candidate.notification_class for candidate in candidates}),
            action_keys=sorted(
                {candidate.action_key for candidate in candidates if candidate.action_key}
            ),
        )
        deliveries = list(
            {
                str(row.get("id") or (row.get("profile_id"), row.get("dedupe_key"))): dict(row)
                for row in state["deliveries"]
            }.values()
        )
        evaluations = list(
            {
                str(row.get("id") or row.get("evaluation_key")): dict(row)
                for row in state["evaluations"]
            }.values()
        )
        actions = {
            (str(row["profile_id"]), str(row["action_key"]), str(row["training_day"]))
            for row in state["action_rows"]
        }
    except Exception as exc:  # noqa: BLE001 - adapter normalizes backend clients
        logger.warning(
            "[notification] simulation state read failed profile_id=%s error_class=%s",
            profile_id,
            type(exc).__name__,
        )
        raise NotificationStoreError("notification simulation state unavailable") from exc

    # Prior observe selections form an isolated shadow claim ledger. A selection
    # says a claim would have happened; it does not predict successful delivery.
    real_keys = {(str(row.get("profile_id")), str(row.get("dedupe_key"))) for row in deliveries}
    for row in evaluations:
        key = (str(row.get("profile_id")), str(row.get("dedupe_key")))
        if not row.get("dedupe_key") or key in real_keys:
            continue
        snapshot = row.get("source_event_metadata") or {}
        snapshot = snapshot.get("_candidate_snapshot") if isinstance(snapshot, Mapping) else {}
        deliveries.append(
            {
                "profile_id": row.get("profile_id"),
                "dedupe_key": row.get("dedupe_key"),
                "training_day": row.get("training_day"),
                "notification_class": (snapshot or {}).get("notification_class", "routine"),
                "status": "pending",
                "attempt_count": int(row.get("evaluation_count") or 1),
                "claimed_at": row.get("last_evaluated_at") or row.get("evaluated_at"),
                "sent_at": None,
            }
        )
    return deliveries, actions


def _simulated_claim_decision(
    candidate: NotificationCandidate,
    *,
    now_utc: datetime,
    deliveries: list[dict[str, Any]],
    actions: set[tuple[str, str, str]],
) -> str:
    """Mirror the non-mutating decisions made by claim_notification_delivery_v2."""

    training_day = _candidate_training_day(candidate, now_utc)
    if candidate.action_key and (
        candidate.profile_id,
        candidate.action_key,
        training_day,
    ) in actions:
        return "user_action_already_done"
    existing = next(
        (
            row
            for row in deliveries
            if row.get("profile_id") == candidate.profile_id
            and row.get("dedupe_key") == candidate.dedupe_key
        ),
        None,
    )
    if existing is not None:
        status = str(existing.get("status") or "")
        attempts = int(existing.get("attempt_count") or 0)
        claimed_at = _parse_datetime(existing.get("claimed_at"))
        stale = claimed_at is None or now_utc - claimed_at >= NOTIFICATION_STALE_CLAIM_AFTER
        if not (
            attempts < NOTIFICATION_MAX_ATTEMPTS
            and (status == "failed" or (status == "pending" and stale))
        ):
            return "duplicate_dedupe_key"
        return "would_claim"
    active = [
        row
        for row in deliveries
        if row.get("profile_id") == candidate.profile_id
        and str(row.get("training_day")) == training_day
        and row.get("notification_class") == candidate.notification_class
        and row.get("status") in {"pending", "sent", "partial"}
    ]
    if len(active) >= _candidate_daily_cap(candidate):
        return "daily_cap"
    spacing = _candidate_min_spacing(candidate)
    active_times = [
        parsed
        for row in active
        if (parsed := _parse_datetime(row.get("sent_at") or row.get("claimed_at"))) is not None
    ]
    if spacing and active_times and now_utc - max(active_times) < timedelta(minutes=spacing):
        return "cooldown_active"
    return "would_claim"


def attempt_notification_delivery_claim(
    store: Any,
    candidate: NotificationCandidate,
    *,
    now_utc: datetime,
) -> NotificationClaimAttempt:
    params = {
        "profile_id": candidate.profile_id,
        "notification_type": candidate.notification_type,
        "intent": candidate.intent,
        "category": candidate.category,
        "priority": candidate.priority,
        "title": candidate.title,
        "body": candidate.body,
        "url": candidate.url,
        "tag": candidate.tag,
        "dedupe_key": candidate.dedupe_key,
        "expires_at": candidate.expires_at.isoformat(),
        "training_day": _candidate_training_day(candidate, now_utc),
        "scheduled_for": (
            candidate.scheduled_for.isoformat() if candidate.scheduled_for else None
        ),
        "timing_source": candidate.timing_source or "",
        "timing_confidence": candidate.timing_confidence or "",
        "variant_id": candidate.variant_id or "",
        "source_event_metadata": dict(candidate.source_event_metadata),
        "action_key": candidate.action_key or "",
        "notification_class": candidate.notification_class,
        "respect_quiet_hours": candidate.respect_quiet_hours,
        "merged_intents": list(candidate.merged_intents),
        "daily_cap": _candidate_daily_cap(candidate),
        "min_spacing_minutes": _candidate_min_spacing(candidate),
    }
    try:
        payload = store.claim_notification_delivery(params, now_utc=now_utc)
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        if not isinstance(payload, Mapping):
            return NotificationClaimAttempt(None, "duplicate_dedupe_key")
        decision = str(payload.get("decision") or "duplicate_dedupe_key")
        row = payload.get("delivery")
        if decision != "claimed" or not isinstance(row, Mapping):
            return NotificationClaimAttempt(None, decision)
        return NotificationClaimAttempt(
            NotificationDeliveryClaim(
                delivery_id=str(row.get("id") or ""),
                claim_token=str(row.get("claim_token") or ""),
                attempt_count=int(row.get("attempt_count") or 1),
            ),
            decision,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[notification] delivery claim failed profile_id=%s type=%s error_class=%s",
            candidate.profile_id,
            candidate.notification_type,
            type(exc).__name__,
        )
        raise NotificationStoreError("notification delivery ledger unavailable") from exc


def claim_notification_delivery(
    store: Any,
    candidate: NotificationCandidate,
    *,
    now_utc: datetime,
) -> NotificationDeliveryClaim | None:
    """Compatibility wrapper returning only a successful claim."""

    return attempt_notification_delivery_claim(
        store,
        candidate,
        now_utc=now_utc,
    ).claim


def finalize_notification_delivery(
    store: Any,
    claim: NotificationDeliveryClaim,
    *,
    status: DeliveryStatus,
    delivered_count: int,
    error_code: str | None = None,
) -> None:
    try:
        store.finalize_notification_delivery(
            claim.delivery_id,
            claim.claim_token,
            status=status,
            delivered_count=max(0, int(delivered_count)),
            error_code=str(error_code or "")[:120] or None,
        )
    except Exception as exc:  # noqa: BLE001 - sending already happened; do not re-raise
        logger.warning(
            "[notification] delivery finalize failed delivery_id=%s error_class=%s",
            claim.delivery_id,
            type(exc).__name__,
        )


def prepare_notification_delivery(
    store: Any,
    candidates: Iterable[NotificationCandidate],
    *,
    now_utc: datetime | None = None,
) -> tuple[NotificationCandidate, NotificationDeliveryClaim] | None:
    candidate_list = list(candidates)
    if not candidate_list:
        return None
    profile_ids = {candidate.profile_id for candidate in candidate_list}
    if len(profile_ids) != 1:
        raise ValueError("notification arbitration must be scoped to one profile")
    reference = now_utc or datetime.now(timezone.utc)
    try:
        preferences = get_notification_preferences(store, candidate_list[0].profile_id)
        ranked = sorted(
            candidate_list,
            key=lambda candidate: (
                candidate.priority,
                candidate.notification_type,
                candidate.dedupe_key,
            ),
        )
        for candidate in ranked:
            training_day = _candidate_training_day(candidate, reference)
            rejection_reasons = candidate_rejection_reasons(
                candidate,
                preferences,
                now_utc=reference,
            )
            if rejection_reasons:
                decision = (
                    "deferred_until_quiet_end"
                    if rejection_reasons == ("quiet_hours",)
                    else "suppressed"
                )
                record_notification_evaluation(
                    store,
                    profile_id=candidate.profile_id,
                    training_day=training_day,
                    intent=candidate.intent,
                    now_utc=reference,
                    decision=decision,
                    rejection_reasons=rejection_reasons,
                    eligible=False,
                    candidate=candidate,
                )
                continue
            attempt = attempt_notification_delivery_claim(
                store,
                candidate,
                now_utc=reference,
            )
            if attempt.claim is None:
                record_notification_evaluation(
                    store,
                    profile_id=candidate.profile_id,
                    training_day=training_day,
                    intent=candidate.intent,
                    now_utc=reference,
                    decision="rejected",
                    rejection_reasons=(attempt.decision,),
                    eligible=True,
                    candidate=candidate,
                )
                # A duplicate/capped/cooling candidate must not starve a lower
                # ranked action that is still useful in this sweep.
                continue
            record_notification_evaluation(
                store,
                profile_id=candidate.profile_id,
                training_day=training_day,
                intent=candidate.intent,
                now_utc=reference,
                decision="selected",
                eligible=True,
                candidate=candidate,
                resulting_delivery_id=attempt.claim.delivery_id,
            )
            for lower in ranked[ranked.index(candidate) + 1 :]:
                lower_reasons = candidate_rejection_reasons(
                    lower,
                    preferences,
                    now_utc=reference,
                )
                if lower_reasons:
                    lower_decision = (
                        "deferred_until_quiet_end"
                        if lower_reasons == ("quiet_hours",)
                        else "suppressed"
                    )
                    record_notification_evaluation(
                        store,
                        profile_id=lower.profile_id,
                        training_day=_candidate_training_day(lower, reference),
                        intent=lower.intent,
                        now_utc=reference,
                        decision=lower_decision,
                        rejection_reasons=lower_reasons,
                        eligible=False,
                        candidate=lower,
                    )
                    continue
                record_notification_evaluation(
                    store,
                    profile_id=lower.profile_id,
                    training_day=_candidate_training_day(lower, reference),
                    intent=lower.intent,
                    now_utc=reference,
                    decision="not_selected",
                    rejection_reasons=("higher_priority_selected",),
                    eligible=True,
                    candidate=lower,
                )
            return candidate, attempt.claim
        return None
    except NotificationStoreError:
        # Fail closed: a preference or ledger failure must never bypass an opt-out
        # or send a duplicate notification.
        return None


def simulate_notification_delivery_decision(
    store: Any,
    candidates: Iterable[NotificationCandidate],
    *,
    now_utc: datetime | None = None,
) -> NotificationCandidate | None:
    """Record send-equivalent arbitration without claiming or changing delivery state."""

    candidate_list = list(candidates)
    if not candidate_list:
        return None
    profile_ids = {candidate.profile_id for candidate in candidate_list}
    if len(profile_ids) != 1:
        raise ValueError("notification arbitration must be scoped to one profile")
    reference = now_utc or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    try:
        preferences = get_notification_preferences(store, candidate_list[0].profile_id)
        deliveries, actions = _simulation_state(store, candidate_list, now_utc=reference)
        ranked = sorted(
            candidate_list,
            key=lambda candidate: (
                candidate.priority,
                candidate.notification_type,
                candidate.dedupe_key,
            ),
        )
        selected: NotificationCandidate | None = None
        for candidate in ranked:
            reasons = candidate_rejection_reasons(candidate, preferences, now_utc=reference)
            if selected is not None and not reasons:
                reasons = ("higher_priority_selected",)
                decision = "would_not_select"
                eligible = True
            elif reasons:
                decision = (
                    "deferred_until_quiet_end" if reasons == ("quiet_hours",) else "would_reject"
                )
                eligible = False
            else:
                claim_decision = _simulated_claim_decision(
                    candidate,
                    now_utc=reference,
                    deliveries=deliveries,
                    actions=actions,
                )
                if claim_decision == "would_claim":
                    selected = candidate
                    decision = "would_select"
                    reasons = ()
                    eligible = True
                else:
                    decision = "would_reject"
                    reasons = (claim_decision,)
                    eligible = True
            record_notification_evaluation(
                store,
                profile_id=candidate.profile_id,
                training_day=_candidate_training_day(candidate, reference),
                intent=candidate.intent,
                now_utc=reference,
                decision=decision,
                rejection_reasons=reasons,
                eligible=eligible,
                candidate=candidate,
                # A selection is the shadow claim itself: its freshness drives
                # stale-claim and attempt accounting, so it is never throttled.
                # Only repeated unchanged arbitration diagnostics are.
                min_persist_interval=(
                    None
                    if decision == "would_select"
                    else diagnostic_persist_interval(candidate.intent)
                ),
            )
        return selected
    except NotificationStoreError:
        # Match send mode's fail-closed behaviour when durable state is unavailable.
        return None


def invalidate_notification_action(
    store: Any,
    *,
    profile_id: str,
    action_key: str,
    training_day: str,
    completed_at: datetime | None = None,
    source_metadata: Mapping[str, Any] | None = None,
) -> int:
    """Immediately invalidate future reminders for a completed athlete action."""

    reference = completed_at or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return int(
        store.invalidate_notification_action(
            profile_id,
            action_key=action_key,
            training_day=training_day,
            completed_at=reference,
            source_metadata=dict(source_metadata or {}),
        )
        or 0
    )


def list_recent_notification_deliveries(
    store: Any,
    *,
    profile_id: str,
    intent: str | None = None,
    training_day: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store.list_notification_deliveries(
            profile_id,
            intent=intent,
            training_day=training_day,
            limit=limit,
        )
        or []
    ]
