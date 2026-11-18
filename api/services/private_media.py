"""Private attachments only: server validation and ownership before every read/write."""
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import HTTPException

from api.contracts.private_media import PrivateMediaPreview, YouTubeClipInput
from api.services.exercise_media import check_youtube_video, parse_youtube_video_id, youtube_api_key


def uploads_available(store):
    if os.getenv("PRIVATE_VIDEO_UPLOADS_ENABLED", "").lower() != "true":
        return False
    return store.private_media_worker_healthy()


def validate_clip(body: YouTubeClipInput):
    # Reuse the catalogue parser, but private input must be an actual HTTPS URL.
    parsed = urlparse(body.url)
    try:
        port = parsed.port
    except ValueError:
        raise HTTPException(422, "Paste a valid HTTPS YouTube link.") from None
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        raise HTTPException(422, "Paste a valid HTTPS YouTube link.")
    video_id = parse_youtube_video_id(body.url)
    if not video_id:
        raise HTTPException(422, "Paste a valid YouTube link.")
    key = youtube_api_key()
    if not key:
        raise HTTPException(503, "YouTube preview is temporarily unavailable.")
    check = check_youtube_video(video_id, api_key=key)
    if check.status == "unavailable":
        raise HTTPException(422, "Video is unavailable for embedding.")
    if check.status == "unknown" or check.duration_seconds is None:
        raise HTTPException(503, "Unable to verify this video. Try again later.")
    if check.status != "ok" or body.end_s > check.duration_seconds:
        raise HTTPException(422, "Video unavailable or clip ends after the video.")
    return {
        "provider": "youtube", "video_id": video_id,
        "start_s": body.start_s, "end_s": body.end_s,
        "channel_title": check.channel_title, "orientation": check.orientation,
        "made_for_kids": check.made_for_kids,
    }


def owned_media(store, athlete_id, media_id):
    row = store.get_private_media(athlete_id, str(media_id))
    if not row:
        raise HTTPException(404, "Video not found.")
    if row["provider"] == "uploaded":
        asset = store.get_private_video_asset(athlete_id, row["asset_id"])
        if not asset or asset["status"] != "verified":
            raise HTTPException(409, "Video is not ready. Choose another video.")
    return row


def media_preview(store, athlete_id, media_id, *, sign=True):
    """A preview of the owner's media. ``sign=False`` leaves the link out, so a
    list never hands out playable URLs before someone asks to watch."""
    row = owned_media(store, athlete_id, media_id)
    fields = {key: row.get(key) for key in (
        "id", "provider", "video_id", "asset_id", "start_s", "end_s", "channel_title", "orientation"
    )}
    if row["provider"] == "uploaded" and sign:
        # Only sanitized rendition, only after ownership and verification checks.
        asset = store.get_private_video_asset(athlete_id, row["asset_id"])
        fields["url"] = store.private_video_signed_url(asset["rendition_path"], expires_in=60)
    return PrivateMediaPreview(**fields)


def upload_state(store, athlete_id, asset_id):
    asset = store.get_private_video_asset(athlete_id, str(asset_id))
    if not asset:
        raise HTTPException(404, "Video not found.")
    if asset["status"] == "reserved" and datetime.fromisoformat(asset["upload_expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        return {"status": "rejected", "media": None}
    media = None
    if asset["status"] == "verified":
        row = store.get_private_media_for_asset(athlete_id, str(asset_id))
        if row:
            media = media_preview(store, athlete_id, row["id"])
    return {"status": asset["status"], "media": media}
