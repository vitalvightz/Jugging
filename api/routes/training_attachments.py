"""Files on an athlete's own logged work: videos, photos and voice notes."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from api.contracts.training_attachments import (
    AttachmentSettings, AttachmentSource, FileUploadInput, FileUploadPermission, FileUploadResult,
    TrainingAttachment, TrainingAttachmentInput,
)
from api.services.private_media import uploads_available
from api.services.training_attachments import (
    attach_file, attachment_settings, attachment_views, playable, process_file, reserve_file,
)

# A playable link must never be stored by a browser, proxy or service worker.
NO_STORE = {"Cache-Control": "no-store, private", "Pragma": "no-cache"}


def build_training_attachments_router(*, require_profile, get_store):
    router = APIRouter(prefix="/api/training/attachments", tags=["training attachments"])

    @router.get("", response_model=list[TrainingAttachment])
    def list_attachments(source: AttachmentSource, target_id: UUID,
                         exercise_ref: str = Query(default="", max_length=200),
                         profile=Depends(require_profile), store=Depends(get_store)):
        rows = store.list_training_attachments(profile.athlete_id, source, str(target_id), exercise_ref)
        return attachment_views(store, profile.athlete_id, rows)

    @router.get("/{attachment_id}/play", response_model=TrainingAttachment)
    def play(attachment_id: UUID, response: Response, profile=Depends(require_profile), store=Depends(get_store)):
        response.headers.update(NO_STORE)
        return playable(store, profile.athlete_id, attachment_id)

    @router.post("", response_model=TrainingAttachment, status_code=201)
    def attach(body: TrainingAttachmentInput, profile=Depends(require_profile), store=Depends(get_store)):
        if body.asset_id is not None:
            row = attach_file(store, profile.athlete_id, profile.athlete_id, body.asset_id, body.source,
                              body.target_id, body.exercise_ref)
        else:
            # Fails closed: with video uploads switched off (or the isolated
            # worker unhealthy) nothing new is attached, whatever the database holds.
            if not uploads_available(store):
                raise HTTPException(503, "Video attachments are switched off.")
            row = store.attach_training_media(profile.athlete_id, str(body.media_id), body.source,
                                              str(body.target_id), body.exercise_ref)
        views = attachment_views(store, profile.athlete_id, [row])
        if not views:
            raise HTTPException(409, "That file is still being checked. Try again in a moment.")
        return views[0]

    @router.delete("/{attachment_id}", status_code=204)
    def remove(attachment_id: UUID, profile=Depends(require_profile), store=Depends(get_store)):
        # Removing is idempotent: an already-removed file is a success. The
        # athlete may remove anything on their own records, coach notes included.
        store.delete_training_attachment(profile.athlete_id, str(attachment_id))
        return Response(status_code=204)

    return router


def build_attachment_uploads_router(*, require_profile, get_store):
    """Photo and voice-note uploads: reserve a quarantine path, then have it rebuilt."""
    router = APIRouter(prefix="/api/attachments", tags=["training attachments"])

    @router.get("/settings", response_model=AttachmentSettings)
    def settings(profile=Depends(require_profile)):
        return attachment_settings()

    @router.post("/uploads", response_model=FileUploadPermission, status_code=201)
    def reserve(body: FileUploadInput, profile=Depends(require_profile), store=Depends(get_store)):
        if profile.access_status != "approved":
            raise HTTPException(403, "Your account is not approved yet.")
        # Coaches only ever leave voice feedback.
        if profile.role in {"coach", "admin"} and body.kind != "voice":
            raise HTTPException(403, "Coaches can attach voice notes only.")
        return reserve_file(store, profile.athlete_id, body.kind, body.size_bytes)

    @router.post("/uploads/{asset_id}/process", response_model=FileUploadResult)
    def process(asset_id: UUID, profile=Depends(require_profile), store=Depends(get_store)):
        return process_file(store, profile.athlete_id, asset_id)

    return router
