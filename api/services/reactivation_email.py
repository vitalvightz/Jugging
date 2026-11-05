"""One-time reactivation using Resend, with email unsubscribe suppression."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from html import escape
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import httpx

from api.settings import env_flag
from api.store import AppStore

logger = logging.getLogger(__name__)
_TEMPLATE = Path(__file__).with_suffix(".html")


def _secret() -> str:
    return os.getenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_SECRET", "").strip()


def unsubscribe_token(profile_id: str) -> str:
    secret = _secret()
    if not secret:
        raise ValueError("reactivation unsubscribe secret not configured")
    profile_id = str(UUID(profile_id))
    signature = hmac.new(secret.encode(), f"unsubscribe:{profile_id}".encode(), hashlib.sha256).hexdigest()
    return f"{profile_id}.{signature}"


def verify_unsubscribe_token(token: str) -> str | None:
    try:
        profile_id, signature = token.split(".", 1)
        profile_id = str(UUID(profile_id))
        expected = unsubscribe_token(profile_id).split(".", 1)[1]
        return profile_id if hmac.compare_digest(signature, expected) else None
    except (ValueError, TypeError):
        return None


def _sender() -> str:
    configured = os.getenv("PLAN_READY_FROM_EMAIL", "").strip()
    address = parseaddr(os.getenv("FEEDBACK_FROM_EMAIL", ""))[1]
    return configured or (f"UNLXCK <{address}>" if address else "")


def _configured() -> bool:
    return bool(
        os.getenv("RESEND_API_KEY", "").strip()
        and _sender()
        and _secret()
        and os.getenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_URL", "").startswith("https://")
    )


def _payload(row: dict) -> dict:
    profile_id = str(row["profile_id"])
    name = str(row.get("first_name") or "").strip()[:60]
    greeting = f"Hi {name}," if name else "Hi,"
    link = "https://app.unlxck.com/dashboard"
    unsubscribe = os.getenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_URL", "").strip()
    unsubscribe += ("&" if "?" in unsubscribe else "?") + "token=" + quote(unsubscribe_token(profile_id))
    text = (
        f"{greeting}\n\nIt's been a while since you've checked in.\n\n"
        "Whether you're training hard or finding your rhythm again, UNLXCK is here to help you stay consistent and keep progressing.\n\n"
        f"Get back to training: {link}\n\nSee you inside,\nUNLXCK\n\nUnsubscribe from these reminders: {unsubscribe}"
    )
    html = (_TEMPLATE.read_text(encoding="utf-8")
        .replace("{{GREETING}}", escape(greeting))
        .replace("{{DASHBOARD_URL}}", escape(link, quote=True))
        .replace("{{UNSUBSCRIBE_URL}}", escape(unsubscribe, quote=True)))
    return {
        "from": _sender(), "to": [str(row["email"])],
        "subject": "Your progress doesn't stop here", "html": html, "text": text,
        "headers": {"List-Unsubscribe": f"<{unsubscribe}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    }


def _send(profile_id: str, payload: dict) -> tuple[str, str | None, int]:
    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
                     "Idempotency-Key": f"reactivation-{profile_id}"},
            json=payload, timeout=5,
        )
        if response.status_code == 429:
            try:
                delay = max(60, int(response.headers.get("Retry-After", "60")))
            except ValueError:
                delay = 60
            return "failed", None, delay
        if response.status_code >= 500:
            return "failed", None, 60
        response.raise_for_status()
        provider_id = response.json().get("id")
        # A malformed success is uncertain: retry only with the same payload/key.
        return ("sent", provider_id, 0) if provider_id else ("failed", None, 60)
    except httpx.HTTPStatusError:
        return "skipped", None, 0  # Permanent validation/auth error; no retry.
    except (httpx.RequestError, ValueError):
        return "failed", None, 60


def run_reactivation_email_sweep(store: AppStore) -> int:
    """One gentle reminder, never a campaign. Disabled until explicitly enabled."""
    if not env_flag("UNLXCK_REACTIVATION_EMAIL_ENABLED", False) or not _configured():
        return 0
    try:
        rows = store.client.rpc("reactivation_email_candidates").execute().data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[reactivation] candidates_unavailable error_type=%s", type(exc).__name__)
        return 0
    sent = 0
    for row in rows:
        profile_id = str(row["profile_id"])
        try:
            claims = store.client.rpc("claim_reactivation_email", {
                "p_profile_id": profile_id,
            }).execute().data or []
            if not claims:
                continue
            claim = claims[0]
            # Freeze the entire request before the first network call, so retries
            # after a restart have exactly the same body and idempotency key.
            payload = claim.get("payload") or _payload(row)
            store.client.table("reactivation_email_deliveries").update({
                "payload": payload,
            }).eq("profile_id", profile_id).eq("attempts", claim["attempts"]).execute()
            preferences = store.client.table("reactivation_email_preferences").select("enabled").eq(
                "profile_id", profile_id
            ).limit(1).execute().data or []
            # Recheck visits after the claim, including analytics-independent opens.
            since = datetime.now(timezone.utc) - timedelta(days=7)
            recent = store.client.table("product_activity").select("id").eq(
                "athlete_id", profile_id
            ).gte("occurred_at", since.isoformat()).limit(1).execute().data
            visits = store.client.table("athlete_daily_activity").select("activity_date").eq(
                "athlete_id", profile_id
            ).gte("activity_date", (since - timedelta(days=2)).date().isoformat()).limit(1).execute().data
            status, provider_id, delay = "skipped", None, 0
            if (not preferences or preferences[0].get("enabled") is True) and not recent and not visits:
                status, provider_id, delay = _send(profile_id, payload)
            update = {"status": status, "next_attempt_at": (
                datetime.now(timezone.utc) + timedelta(seconds=max(delay, min(3600, 60 * 2 ** claim["attempts"])))
            ).isoformat()}
            if status == "sent":
                update.update(sent_at=datetime.now(timezone.utc).isoformat(), provider_id=provider_id)
                sent += 1
            store.client.table("reactivation_email_deliveries").update(update).eq(
                "profile_id", profile_id
            ).eq("attempts", claim["attempts"]).execute()
            # Also bound request throughput in a single sweep. Database claims
            # enforce this across workers; 429 Retry-After is persisted above.
            time.sleep(1)
        except Exception as exc:  # Fail closed; lease permits bounded recovery.
            logger.warning("[reactivation] candidate_failed error_type=%s", type(exc).__name__)
    return sent
