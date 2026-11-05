"""Explicit email opt-in and passwordless unsubscribe for reactivation reminders."""

from __future__ import annotations

from datetime import date, datetime, timezone
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from api.models import ProfileRecord
from api.services.reactivation_email import verify_unsubscribe_token
from api.store import AppStore


class EmailPreference(BaseModel):
    enabled: bool


def build_reactivation_router(*, require_profile, get_store) -> APIRouter:
    router = APIRouter()

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
