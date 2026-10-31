from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from api.compliance import evaluate_profile_compliance
from api.compliance_guards import require_health_feature_access, require_onboarding_compliance, strip_health_intake_fields
from api.minor_safety import strip_minor_target_weight
from api.models import (
    ComplianceAcceptanceRequest,
    MeResponse,
    OnboardingDraftSaveRequest,
    ProfileRecord,
    ProfileUpdateRequest,
    UsernameChangeRequest,
)
from api.plan_mappers import _build_me_response, _map_profile_row
from api.services.xp_awards import (
    activation_xp_settled,
    plan_activation_ready,
    reconcile_activation_xp,
    settled_activation_milestones,
)
from api.store import AppStore

logger = logging.getLogger(__name__)


def build_profile_router(*, require_profile, get_store) -> APIRouter:
    router = APIRouter()

    def _activation_ready_plan(
        response: MeResponse,
        store: AppStore,
        athlete_id: str,
    ) -> object | None:
        """Find any persisted athlete-visible plan, not only the newest row."""

        if plan_activation_ready(response.latest_plan):
            return response.latest_plan
        try:
            for plan in store.list_user_plans(athlete_id):
                if plan_activation_ready(plan):
                    return plan
        except Exception:  # noqa: BLE001 - activation reconciliation fails closed
            logger.exception(
                "[xp] ready plan lookup failed athlete_id=%s",
                athlete_id,
            )
        return None

    def _build_me_with_activation_xp(
        profile: ProfileRecord,
        store: AppStore,
    ) -> MeResponse:
        response = _build_me_response(profile, store)
        if profile.role != "athlete":
            return response
        # Established athletes have every activation milestone already awarded;
        # once this process has seen that, profile reads stay reads.
        if activation_xp_settled(store, profile.athlete_id):
            return response
        try:
            plan_settled = "first_plan_ready" in settled_activation_milestones(
                store, profile.athlete_id
            )
            reconcile_activation_xp(
                store,
                athlete_id=profile.athlete_id,
                profile=response.profile,
                latest_intake=response.latest_intake,
                latest_plan=None
                if plan_settled
                else _activation_ready_plan(
                    response,
                    store,
                    profile.athlete_id,
                ),
            )
        except Exception:  # noqa: BLE001 - XP must never break profile reads
            logger.exception(
                "[xp] activation route reconciliation failed athlete_id=%s",
                profile.athlete_id,
            )
        return response

    @router.get("/api/me", response_model=MeResponse)
    def get_me(
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> MeResponse:
        return _build_me_with_activation_xp(profile, store)

    @router.put("/api/me", response_model=MeResponse)
    def update_me(
        update: ProfileUpdateRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> MeResponse:
        if update.starting_path is not None:
            require_onboarding_compliance(profile)
        if update.training_habits is not None and update.training_habits.enabled:
            require_health_feature_access(profile)
        updated = _map_profile_row(store.update_profile(profile.athlete_id, update))
        if update.starting_path is not None:
            store.record_onboarding_stage(profile.athlete_id, completed=True)
        return _build_me_with_activation_xp(updated, store)

    @router.post("/api/me/compliance", response_model=MeResponse)
    def record_compliance(
        acceptance: ComplianceAcceptanceRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> MeResponse:
        """Record date of birth, Terms acceptance and health-data consent.

        One endpoint covers first-time acceptance, later re-acceptance after a
        version bump, and withdrawal, because all three write the same evidence
        columns and all three must be server-stamped. The request carries intent
        only — see :class:`ComplianceAcceptanceRequest`.
        """
        updated = _map_profile_row(
            store.record_compliance_acceptance(
                profile.athlete_id,
                date_of_birth=acceptance.date_of_birth,
                accept_terms=acceptance.accept_terms,
                health_data_consent=acceptance.health_data_consent,
            )
        )
        return _build_me_with_activation_xp(updated, store)

    @router.post("/api/me/username", response_model=MeResponse)
    def change_username_endpoint(
        update: UsernameChangeRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> MeResponse:
        updated = _map_profile_row(store.change_username(profile.athlete_id, update.username))
        return _build_me_with_activation_xp(updated, store)

    @router.patch("/api/onboarding/draft")
    def save_onboarding_draft(
        update: OnboardingDraftSaveRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> dict[str, str | bool]:
        # Terms acceptance must precede intake. The draft is the first thing
        # onboarding writes, so gating it here is what makes "an athlete cannot
        # complete onboarding without accepting the Terms" true on the server
        # rather than only in the client's routing.
        require_onboarding_compliance(profile)
        update_data = update.model_dump(exclude_unset=True)
        if (profile.athlete_timezone or "").strip():
            # Drafts may only seed a first-time timezone. Once one is saved it
            # is owned by Settings, so a stale draft or a different browser zone
            # cannot overwrite it (and trip the database's timezone guard).
            update_data.pop("athlete_timezone", None)
        if (
            not evaluate_profile_compliance(profile).health_consent_granted
            and isinstance(update_data.get("onboarding_draft"), dict)
        ):
            update_data["onboarding_draft"] = strip_health_intake_fields(
                update_data["onboarding_draft"]
            )
        if profile.is_minor and isinstance(update_data.get("onboarding_draft"), dict):
            # The draft carries the same athlete block as the intake, so a
            # target weight would otherwise persist here for an under-18 even
            # though generation strips it later.
            update_data["onboarding_draft"] = strip_minor_target_weight(
                update_data["onboarding_draft"]
            )
        updated = store.update_profile(
            profile.athlete_id,
            ProfileUpdateRequest(**update_data),
        )
        store.record_onboarding_stage(profile.athlete_id)
        updated_at = (updated or {}).get("updated_at")
        return {"ok": True, "updated_at": str(updated_at or "")}

    return router
