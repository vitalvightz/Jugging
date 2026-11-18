"""Turn attachment rows into previews for one owner.

Lists never carry a playable link: a short-lived link to the sanitized
rendition is issued only by the play route, for one video, after ownership
(and, for a coach, current consent) is checked on that request.
"""
import logging

from fastapi import HTTPException

from api.services.private_media import media_preview

logger = logging.getLogger(__name__)


def attachment_views(store, athlete_id, rows, *, sign=False):
    """A video that is no longer playable (removed or reprocessing) is left out
    rather than failing the list."""
    views = []
    for row in rows:
        try:
            media = media_preview(store, athlete_id, row["media_id"], sign=sign)
        except HTTPException as exc:
            logger.info("[attachments] skipped attachment status=%s", exc.status_code)
            continue
        views.append({"id": row["id"], "created_at": row["created_at"], "media": media})
    return views


def playable(store, athlete_id, attachment_id):
    """One owned attachment with a 60-second link, or 404."""
    row = store.get_training_attachment(athlete_id, str(attachment_id))
    views = attachment_views(store, athlete_id, [row], sign=True) if row else []
    if not views:
        raise HTTPException(404, "Video not available.")
    return views[0]
