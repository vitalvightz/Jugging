"""Files on logged work: form-check videos, photos, voice notes and PDFs.

Reads and writes always carry the owner. Lists never carry a playable link;
one short-lived link is issued per play, after ownership and consent checks.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.contracts.private_media import PrivateMediaPreview

# A logged record (session, sparring report), or 'progress': the athlete's own
# progress photos, whose target is the athlete themself.
RecordSource = Literal["unlxck", "external", "sparring"]
AttachmentSource = Literal["unlxck", "external", "sparring", "progress"]
AttachmentKind = Literal["video", "photo", "voice", "pdf"]
FileKind = Literal["photo", "voice", "pdf"]


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
    """A coach replies on a logged record only; progress photos are the athlete's own."""
    model_config = ConfigDict(extra="forbid")
    asset_id: UUID
    source: RecordSource
    target_id: UUID
    exercise_ref: str = Field(default="", max_length=200)


class AttachmentFile(BaseModel):
    """A rebuilt photo, voice note or PDF. url is set only on a play response;
    for a PDF it is a download-only link that carries the display name."""
    url: str | None = None
    # A photo's small preview, rebuilt from the same clean pixels: a 2-minute
    # link, issued with the list after the same ownership and consent checks.
    thumb_url: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    name: str | None = None
    pages: int | None = None
    size_bytes: int | None = None


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
    """A PDF is reserved as its page images: size_bytes is their total, and
    file_name is a display name only (it is cleaned and never used in a path)."""
    model_config = ConfigDict(extra="forbid")
    kind: FileKind
    size_bytes: int = Field(gt=0, le=8 * 1024 * 1024)
    page_count: int | None = Field(default=None, ge=1, le=30)
    file_name: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def document_shape(self):
        if (self.kind == "pdf") != (self.page_count is not None):
            raise ValueError("A PDF needs its page count, and only a PDF has one.")
        return self


class FileUploadPermission(BaseModel):
    asset_id: UUID
    bucket: Literal["attachment-intake"] = "attachment-intake"
    path: str
    content_type: Literal["image/jpeg", "audio/wav"]
    expires_at: datetime
    # PDFs only: pages upload to {path}/001.jpg ... {path}/NNN.jpg.
    page_count: int | None = None


class FileUploadResult(BaseModel):
    asset_id: UUID
    # page_ready: one PDF page was rebuilt; the next page (or assembly) follows.
    status: Literal["ready", "page_ready", "rejected"]
    reason: str | None = None


class AttachmentSettings(BaseModel):
    photos_available: bool
    voice_available: bool
    pdfs_available: bool = False


class AttachmentTarget(BaseModel):
    source: RecordSource
    target_id: UUID
    exercise_ref: str = ""
    last_at: datetime


class FighterMedia(BaseModel):
    """What a coach may open from a fighter's media: nothing at all unless the
    fighter shares media with this coach, and only records that still exist."""
    share_media: bool
    progress_count: int = 0
    recent: list[AttachmentTarget] = []
