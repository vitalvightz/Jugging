"""Files on logged work: form-check videos, photos and voice notes.

Reads and writes always carry the owner. Lists never carry a playable link;
one short-lived link is issued per play, after ownership and consent checks.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.contracts.private_media import PrivateMediaPreview

AttachmentSource = Literal["unlxck", "external", "sparring"]
AttachmentKind = Literal["video", "photo", "voice"]
FileKind = Literal["photo", "voice"]


class TrainingAttachmentInput(BaseModel):
    """Attach a verified video (media_id) or a rebuilt photo / voice note (asset_id)."""
    model_config = ConfigDict(extra="forbid")
    media_id: UUID | None = None
    asset_id: UUID | None = None
    source: AttachmentSource
    target_id: UUID
    exercise_ref: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def one_file(self):
        if (self.media_id is None) == (self.asset_id is None):
            raise ValueError("Attach exactly one file.")
        return self


class CoachVoiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: UUID
    source: AttachmentSource
    target_id: UUID
    exercise_ref: str = Field(default="", max_length=200)


class AttachmentFile(BaseModel):
    """A rebuilt photo or voice note. url is set only on a play response."""
    url: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


class TrainingAttachment(BaseModel):
    id: UUID
    created_at: datetime
    kind: AttachmentKind = "video"
    # Who added it: the athlete, or their coach (voice feedback).
    author: Literal["athlete", "coach"] = "athlete"
    # True when the person asking may remove it.
    removable: bool = False
    # A short-lived link to the sanitized rendition, never the original upload.
    media: PrivateMediaPreview | None = None
    file: AttachmentFile | None = None


class FileUploadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: FileKind
    size_bytes: int = Field(gt=0, le=8 * 1024 * 1024)


class FileUploadPermission(BaseModel):
    asset_id: UUID
    bucket: Literal["attachment-intake"] = "attachment-intake"
    path: str
    content_type: Literal["image/jpeg", "audio/wav"]
    expires_at: datetime


class FileUploadResult(BaseModel):
    asset_id: UUID
    status: Literal["ready", "rejected"]
    reason: str | None = None


class AttachmentSettings(BaseModel):
    photos_available: bool
    voice_available: bool
