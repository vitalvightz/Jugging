"""Opt-in-only, at-most-once reactivation using the existing Resend account."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
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


def _send(row: dict) -> bool:
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
    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
                "Idempotency-Key": f"reactivation-{profile_id}",
            },
            json={
                "from": _sender(),
                "to": [str(row["email"])],
                "subject": "Your progress doesn't stop here",
                "html": html,
                "text": text,
                "headers": {
                    "List-Unsubscribe": f"<{unsubscribe}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                },
            },
            timeout=5,
        )
        response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - never affect generation
        logger.warning("[reactivation] resend_failed error_type=%s", type(exc).__name__)
        return False


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
            # Claim before the external call. If a worker crashes, never send twice.
            store.client.table("reactivation_email_deliveries").insert({
                "profile_id": profile_id, "status": "claimed",
            }).execute()
        except Exception:  # noqa: BLE001 - another worker claimed it
            continue
        status = "skipped"
        try:
            consent = store.client.table("reactivation_email_preferences").select("enabled").eq(
                "profile_id", profile_id
            ).single().execute().data
            # Recheck inactivity after claiming: the person might have just returned.
            recent = store.client.table("product_activity").select("id").eq(
                "athlete_id", profile_id
            ).gte("occurred_at", (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()).limit(1).execute().data
            if consent and consent.get("enabled") is True and not recent:
                status = "sent" if _send(row) else "failed"
        except Exception as exc:  # noqa: BLE001 - fail closed
            logger.warning("[reactivation] candidate_failed error_type=%s", type(exc).__name__)
            status = "failed"
        try:
            update = {"status": status}
            if status == "sent":
                update["sent_at"] = datetime.now(timezone.utc).isoformat()
                sent += 1
            store.client.table("reactivation_email_deliveries").update(update).eq(
                "profile_id", profile_id
            ).eq("status", "claimed").execute()
        except Exception as exc:  # noqa: BLE001 - permanent claim still prevents duplicates
            logger.warning("[reactivation] finalize_failed error_type=%s", type(exc).__name__)
    return sent
