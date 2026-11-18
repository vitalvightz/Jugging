"""Form-check videos on logged work. Reads and writes always carry the owner."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from api.contracts.private_media import PrivateMediaPreview

AttachmentSource = Literal["unlxck", "external", "sparring"]


class TrainingAttachmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_id: UUID
    source: AttachmentSource
    target_id: UUID
    exercise_ref: str = Field(default="", max_length=200)


class TrainingAttachment(BaseModel):
    id: UUID
    created_at: datetime
    # A short-lived link to the sanitized rendition, never the original upload.
    media: PrivateMediaPreview
