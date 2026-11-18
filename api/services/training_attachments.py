"""Turn attachment rows into views for one viewer.

Lists never carry a playable link: a 60-second link to the sanitized file is
issued only by a play route, for one attachment, after ownership (and, for a
coach, the live connection and media-sharing consent) is checked on that
request. Photos and voice notes are only ever served from the approved
bucket, after the Edge Function has rebuilt them.
"""
import logging
import os

from fastapi import HTTPException

from api.services.private_media import media_preview

logger = logging.getLogger(__name__)
LINK_SECONDS = 60
CONTENT_TYPES = {"photo": "image/jpeg", "voice": "audio/wav"}


def file_kind_enabled(kind):
    """Photos and voice notes are switched on separately; anything else is off."""
    name = {"photo": "ATTACHMENT_PHOTOS_ENABLED", "voice": "ATTACHMENT_VOICE_ENABLED"}.get(kind)
    return bool(name) and os.getenv(name, "").strip().lower() == "true"


def attachment_settings():
    return {"photos_available": file_kind_enabled("photo"), "voice_available": file_kind_enabled("voice")}


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
            url = store.attachment_signed_url(asset["ready_path"], expires_in=LINK_SECONDS) if sign else None
            view.update(kind=asset["kind"], file={"url": url, "width": asset.get("width"),
                                                  "height": asset.get("height"), "duration_ms": asset.get("duration_ms")})
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


def reserve_file(store, owner_id, kind, size_bytes):
    if not file_kind_enabled(kind):
        raise HTTPException(503, "Photo uploads are switched off." if kind == "photo" else "Voice notes are switched off.")
    limit = 8 * 1024 * 1024 if kind == "photo" else 2 * 1024 * 1024
    if size_bytes > limit:
        raise HTTPException(422, "That photo is too large." if kind == "photo" else "That voice note is too long.")
    asset = store.reserve_attachment_upload(owner_id, kind, size_bytes, CONTENT_TYPES[kind])
    return {"asset_id": asset["id"], "path": asset["intake_path"], "content_type": CONTENT_TYPES[kind],
            "expires_at": asset["upload_expires_at"]}


def process_file(store, owner_id, asset_id):
    """Ask the Edge Function to rebuild the caller's own upload. Fails closed."""
    asset = store.get_attachment_asset(owner_id, str(asset_id))
    if not asset:
        raise HTTPException(404, "Upload not found.")
    if not file_kind_enabled(asset["kind"]):
        raise HTTPException(503, "This file type is switched off.")
    if asset["status"] == "ready":
        return {"asset_id": asset["id"], "status": "ready"}
    if asset["status"] in ("rejected", "deleted"):
        return {"asset_id": asset["id"], "status": "rejected", "reason": asset.get("reject_reason") or "That file could not be used."}
    result = store.process_attachment(str(asset["id"]))
    if result.get("status") == "ready":
        return {"asset_id": asset["id"], "status": "ready"}
    reason = str(result.get("reason") or "That file could not be used.")[:120]
    return {"asset_id": asset["id"], "status": "rejected", "reason": reason}


def attach_file(store, author_id, athlete_id, asset_id, source, target_id, exercise_ref):
    asset = store.get_attachment_asset(author_id, str(asset_id))
    if not asset:
        raise HTTPException(404, "That record or file is not available.")
    if not file_kind_enabled(asset["kind"]):
        raise HTTPException(503, "This file type is switched off.")
    return store.attach_training_asset(author_id, athlete_id, str(asset_id), source, str(target_id), exercise_ref)
