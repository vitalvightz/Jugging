"""Turn attachment rows into views for one viewer.

Lists never carry a playable link: a 60-second link to the sanitized file is
issued only by a play route, for one attachment, after ownership (and, for a
coach, the live connection and media-sharing consent) is checked on that
request. Photos, voice notes and PDFs are only ever served from the approved
bucket, after the Edge Function has rebuilt them. A PDF is only ever a new
file written from rebuilt page images, and its link forces a download.
"""
import logging
import os
import re

from fastapi import HTTPException

from api.services.private_media import media_preview

logger = logging.getLogger(__name__)
LINK_SECONDS = 60
CONTENT_TYPES = {"photo": "image/jpeg", "voice": "audio/wav"}
FLAGS = {"photo": "ATTACHMENT_PHOTOS_ENABLED", "voice": "ATTACHMENT_VOICE_ENABLED", "pdf": "ATTACHMENT_PDFS_ENABLED"}
SIZE_LIMITS = {"photo": 8 * 1024 * 1024, "voice": 2 * 1024 * 1024, "pdf": 8 * 1024 * 1024}
OFF_MESSAGES = {"photo": "Photo uploads are switched off.", "voice": "Voice notes are switched off.",
                "pdf": "PDF uploads are switched off."}
TOO_LARGE = {"photo": "That photo is too large.", "voice": "That voice note is too long.",
             "pdf": "That PDF is too large. Try one under 5 MB."}


def file_kind_enabled(kind):
    """Photos, voice notes and PDFs are switched on separately; anything else is off."""
    name = FLAGS.get(kind)
    return bool(name) and os.getenv(name, "").strip().lower() == "true"


def attachment_settings():
    return {"photos_available": file_kind_enabled("photo"), "voice_available": file_kind_enabled("voice"),
            "pdfs_available": file_kind_enabled("pdf")}


def pdf_display_name(name):
    """A plain display name for a PDF: letters, digits, spaces and . _ ( ) -
    only, starting with a letter or digit, at most 100 characters, ending .pdf.
    It is shown and offered as the download name, never used in a path."""
    stem = re.sub(r"\.pdf$", "", str(name or "").strip(), flags=re.IGNORECASE)
    stem = re.sub(r"[^A-Za-z0-9 ._()-]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" ._()-")[:96].rstrip(" ._()-")
    return f"{stem or 'Document'}.pdf"


def attachment_views(store, athlete_id, rows, *, viewer_id=None, sign=False):
    """Views for rows on one athlete's record. A file that is no longer
    available (removed, reprocessing, cleaned up) is left out rather than
    failing the list. ``viewer_id`` decides what the viewer may remove: the
    athlete removes anything on their record, a coach only what they added."""
    viewer_id = viewer_id or athlete_id
    assets = store.get_attachment_assets([str(r["asset_id"]) for r in rows if r.get("asset_id")])
    views = []
    for row in rows:
        author_id = str(row.get("author_id") or athlete_id)
        view = {"id": row["id"], "created_at": row["created_at"],
                "author": "athlete" if author_id == str(athlete_id) else "coach",
                "removable": str(viewer_id) == str(athlete_id) or author_id == str(viewer_id)}
        if row.get("asset_id"):
            asset = assets.get(str(row["asset_id"]))
            if not asset or asset.get("status") != "ready" or not asset.get("ready_path"):
                continue
            if asset["kind"] == "pdf" and not file_kind_enabled("pdf"):
                continue
            download = asset.get("file_name") if asset["kind"] == "pdf" else None
            url = store.attachment_signed_url(asset["ready_path"], expires_in=LINK_SECONDS, download=download) if sign else None
            view.update(kind=asset["kind"], file={"url": url, "width": asset.get("width"),
                                                  "height": asset.get("height"), "duration_ms": asset.get("duration_ms"),
                                                  "name": asset.get("file_name"), "pages": asset.get("page_count"),
                                                  "size_bytes": asset.get("size_bytes") if asset["kind"] == "pdf" else None})
        else:
            try:
                view.update(kind="video", media=media_preview(store, athlete_id, row["media_id"], sign=sign))
            except HTTPException as exc:
                logger.info("[attachments] skipped attachment status=%s", exc.status_code)
                continue
        views.append(view)
    return views


def playable(store, athlete_id, attachment_id, *, viewer_id=None):
    """One attachment on this athlete's record with a 60-second link, or 404."""
    row = store.get_training_attachment(athlete_id, str(attachment_id))
    views = attachment_views(store, athlete_id, [row], viewer_id=viewer_id, sign=True) if row else []
    if not views:
        raise HTTPException(404, "File not available.")
    return views[0]


def reserve_file(store, owner_id, kind, size_bytes, *, page_count=None, file_name=None):
    if not file_kind_enabled(kind):
        raise HTTPException(503, OFF_MESSAGES[kind])
    if size_bytes > SIZE_LIMITS[kind]:
        raise HTTPException(422, TOO_LARGE[kind])
    if kind == "pdf":
        if not page_count or not 1 <= page_count <= 30:
            raise HTTPException(422, "PDFs can have up to 30 pages.")
        asset = store.reserve_attachment_document(owner_id, size_bytes, page_count, pdf_display_name(file_name))
        # Pages upload as JPEGs to {path}/001.jpg onwards.
        return {"asset_id": asset["id"], "path": asset["intake_path"], "content_type": "image/jpeg",
                "expires_at": asset["upload_expires_at"], "page_count": asset["page_count"]}
    asset = store.reserve_attachment_upload(owner_id, kind, size_bytes, CONTENT_TYPES[kind])
    return {"asset_id": asset["id"], "path": asset["intake_path"], "content_type": CONTENT_TYPES[kind],
            "expires_at": asset["upload_expires_at"]}


def process_file(store, owner_id, asset_id, page=None):
    """Ask the Edge Function to rebuild the caller's own upload. Fails closed.
    A PDF is rebuilt one page per call (``page``), then assembled (no page)."""
    asset = store.get_attachment_asset(owner_id, str(asset_id))
    if not asset:
        raise HTTPException(404, "Upload not found.")
    if not file_kind_enabled(asset["kind"]):
        raise HTTPException(503, "This file type is switched off.")
    if asset["status"] == "ready":
        return {"asset_id": asset["id"], "status": "ready"}
    if asset["status"] in ("rejected", "deleted"):
        return {"asset_id": asset["id"], "status": "rejected", "reason": asset.get("reject_reason") or "That file could not be used."}
    if page is not None and asset["kind"] != "pdf":
        raise HTTPException(422, "Only PDFs have pages.")
    if page is not None and not 1 <= page <= (asset.get("page_count") or 0):
        raise HTTPException(422, "That page is not part of this PDF.")
    if asset["kind"] == "pdf":
        result = store.process_attachment(str(asset["id"]), page=page) if page else store.process_attachment(str(asset["id"]), assemble=True)
    else:
        result = store.process_attachment(str(asset["id"]))
    if result.get("status") in ("ready", "page_ready"):
        return {"asset_id": asset["id"], "status": result["status"]}
    reason = str(result.get("reason") or "That file could not be used.")[:120]
    return {"asset_id": asset["id"], "status": "rejected", "reason": reason}


def attach_file(store, author_id, athlete_id, asset_id, source, target_id, exercise_ref):
    asset = store.get_attachment_asset(author_id, str(asset_id))
    if not asset:
        raise HTTPException(404, "That record or file is not available.")
    if not file_kind_enabled(asset["kind"]):
        raise HTTPException(503, "This file type is switched off.")
    return store.attach_training_asset(author_id, athlete_id, str(asset_id), source, str(target_id), exercise_ref)
