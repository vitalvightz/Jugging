"""The two coach chat calls that need the server.

Reading and sending go straight from the browser to the database
(``coach_chat_inbox``, ``coach_chat_send`` and a participant-only table read),
so a message costs one round trip. The server is only needed to push the other
participant and to sign links to chat files, which the browser cannot do.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from pydantic import BaseModel

from api.models import ProfileRecord
from api.routes.training_attachments import NO_STORE
from api.services.coach_chat import MAX_FILES_PER_REQUEST, chat_file_links, notify_chat_message
from api.store import AppStore


class ChatFile(BaseModel):
    message_id: int
    kind: Literal["photo", "voice"]
    url: str
    thumb_url: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


def _message_ids(raw: str) -> list[int]:
    try:
        ids = [int(part) for part in raw.split(",") if part.strip()]
    except ValueError:
        raise HTTPException(422, "Message ids must be numbers.") from None
    if not ids or len(ids) > MAX_FILES_PER_REQUEST or any(value < 1 for value in ids):
        raise HTTPException(422, f"Ask for 1 to {MAX_FILES_PER_REQUEST} messages.")
    return ids


def build_coach_chat_router(*, require_profile, get_store) -> APIRouter:
    router = APIRouter(prefix="/api/chats", tags=["coach-chat"])

    @router.post("/messages/{message_id}/notify", status_code=202)
    def notify_message(background_tasks: BackgroundTasks, message_id: int = Path(ge=1),
                       profile: ProfileRecord = Depends(require_profile),
                       store: AppStore = Depends(get_store)):
        """Push the other participant about a message the caller just sent.

        Only the sender can trigger it, and only once per message: the push
        ledger dedupes on the message id. It answers before the push is sent.
        """
        message = store.get_coach_chat_message(message_id)
        if not message or str(message.get("sender_id")) != profile.athlete_id:
            raise HTTPException(404, "Message not found.")
        background_tasks.add_task(notify_chat_message, store, message=dict(message), sender_name=profile.full_name)
        return Response(status_code=202)

    @router.get("/files", response_model=list[ChatFile])
    def chat_files(response: Response, ids: str = Query(min_length=1, max_length=400),
                   profile: ProfileRecord = Depends(require_profile),
                   store: AppStore = Depends(get_store)):
        """Two-minute links to the photos and voice notes on up to 30 messages,
        in one call. Messages the caller may not read are left out."""
        response.headers.update(NO_STORE)
        return chat_file_links(store, viewer_id=profile.athlete_id, message_ids=_message_ids(ids))

    return router
