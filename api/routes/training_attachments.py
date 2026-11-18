"""An athlete's form-check videos on their own logged work."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from api.contracts.training_attachments import AttachmentSource, TrainingAttachment, TrainingAttachmentInput
from api.services.training_attachments import attachment_views


def build_training_attachments_router(*, require_profile, get_store):
    router = APIRouter(prefix="/api/training/attachments", tags=["training attachments"])

    @router.get("", response_model=list[TrainingAttachment])
    def list_attachments(source: AttachmentSource, target_id: UUID,
                         exercise_ref: str = Query(default="", max_length=200),
                         profile=Depends(require_profile), store=Depends(get_store)):
        rows = store.list_training_attachments(profile.athlete_id, source, str(target_id), exercise_ref)
        return attachment_views(store, profile.athlete_id, rows)

    @router.post("", response_model=TrainingAttachment, status_code=201)
    def attach(body: TrainingAttachmentInput, profile=Depends(require_profile), store=Depends(get_store)):
        row = store.attach_training_media(profile.athlete_id, str(body.media_id), body.source,
                                          str(body.target_id), body.exercise_ref)
        views = attachment_views(store, profile.athlete_id, [row])
        if not views:
            raise HTTPException(409, "Video is still processing. Try again in a moment.")
        return views[0]

    @router.delete("/{attachment_id}", status_code=204)
    def remove(attachment_id: UUID, profile=Depends(require_profile), store=Depends(get_store)):
        # Removing is idempotent: an already-removed video is a success.
        store.delete_training_attachment(profile.athlete_id, str(attachment_id))
        return Response(status_code=204)

    return router
