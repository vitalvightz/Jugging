"""Explicit email opt-in and passwordless unsubscribe for reactivation reminders."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from datetime import date, datetime, timezone
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from api.models import ProfileRecord
from api.services.reactivation_email import verify_unsubscribe_token
from api.store import AppStore


class EmailPreference(BaseModel):
    enabled: bool


def build_reactivation_router(*, require_profile, get_store) -> APIRouter:
    router = APIRouter()

    @router.post("/api/reactivation/webhook")
    async def delivery_webhook(request: Request, store: AppStore = Depends(get_store)):
        # Resend signs webhooks with Svix. Verify raw bytes before parsing anything.
        secret = os.getenv("RESEND_WEBHOOK_SECRET", "")
        if not secret:
            raise HTTPException(503, "Webhook unavailable")
        body = await request.body()
        if len(body) > 65536:
            raise HTTPException(413, "Payload too large")
        event_id = request.headers.get("svix-id", "")
        timestamp = request.headers.get("svix-timestamp", "")
        try:
            if abs(time.time() - int(timestamp)) > 300 or not event_id:
                raise ValueError()
            key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
            signed = event_id.encode() + b"." + timestamp.encode() + b"." + body
            expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
            signatures = request.headers.get("svix-signature", "").split()
            if not any(hmac.compare_digest(sig, "v1," + expected) for sig in signatures):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(401, "Invalid signature") from None
        try:
            event = json.loads(body)
            column = {"email.delivered": "delivered_at", "email.clicked": "clicked_at"}.get(event.get("type"))
            if column:
                provider_id = event["data"]["email_id"]
                rows = store.client.table("reactivation_email_deliveries").select("profile_id").eq(
                    "provider_id", provider_id
                ).limit(1).execute().data
                if not rows:
                    # Shared Resend account: ignore unrelated messages, but retry
                    # while a send may still be awaiting local finalization.
                    pending = store.client.table("reactivation_email_deliveries").select("profile_id").eq(
                        "status", "claimed"
                    ).limit(1).execute().data
                    if not pending:
                        return {"received": True}
                    raise HTTPException(503, "Delivery not yet available")
                store.client.table("reactivation_email_deliveries").update({
                    column: datetime.now(timezone.utc).isoformat(),
                }).eq("provider_id", provider_id).is_(column, "null").execute()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, "Invalid event") from None
        return {"received": True}

    @router.get("/api/reactivation/preferences", response_model=EmailPreference)
    def read_preference(
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> EmailPreference:
        try:
            rows = store.client.table("reactivation_email_preferences").select("enabled").eq(
                "profile_id", profile.athlete_id
            ).limit(1).execute().data or []
            return EmailPreference(enabled=bool(rows and rows[0]["enabled"]))
        except Exception as exc:
            raise HTTPException(503, "Email preferences unavailable") from exc

    @router.put("/api/reactivation/preferences", response_model=EmailPreference)
    def save_preference(
        preference: EmailPreference,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> EmailPreference:
        try:
            person = store.get_profile(profile.athlete_id) or {}
            if preference.enabled:
                birthday = date.fromisoformat(str(person.get("date_of_birth")))
                today = datetime.now(timezone.utc).date()
                years = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
                if years < 18 or person.get("role") != "athlete":
                    raise HTTPException(403, "Reminders are available to adult athletes only")
            store.client.table("reactivation_email_preferences").upsert({
                "profile_id": profile.athlete_id,
                "enabled": preference.enabled,
                "opted_in_at": datetime.now(timezone.utc).isoformat() if preference.enabled else None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="profile_id").execute()
            return preference
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(403, "Date of birth required for email reminders") from exc
        except Exception as exc:
            raise HTTPException(503, "Email preferences unavailable") from exc

    @router.get("/api/reactivation/unsubscribe", response_class=HTMLResponse)
    def show_unsubscribe(token: str = Query(max_length=256)) -> HTMLResponse:
        if not verify_unsubscribe_token(token):
            return HTMLResponse("<h1>Invalid unsubscribe link</h1>", status_code=400)
        safe = escape(token, quote=True)
        return HTMLResponse(
            "<h1>UNLXCK reminders</h1><p>Stop receiving reactivation emails?</p>"
            f'<form method="post" action="/api/reactivation/unsubscribe?token={safe}">'
            '<button type="submit">Unsubscribe</button></form>'
        )

    @router.post("/api/reactivation/unsubscribe", response_class=HTMLResponse)
    def unsubscribe(
        token: str = Query(max_length=256),
        store: AppStore = Depends(get_store),
    ) -> HTMLResponse:
        profile_id = verify_unsubscribe_token(token)
        if not profile_id:
            return HTMLResponse("<h1>Invalid unsubscribe link</h1>", status_code=400)
        try:
            store.client.table("reactivation_email_preferences").upsert({
                "profile_id": profile_id,
                "enabled": False,
                "opted_in_at": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="profile_id").execute()
        except Exception as exc:
            raise HTTPException(503, "Unsubscribe unavailable") from exc
        return HTMLResponse("<h1>Unsubscribed</h1><p>You will no longer receive UNLXCK reactivation emails.</p>")

    return router
