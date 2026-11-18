"""Turn attachment rows into playable previews for one owner."""
import logging

from fastapi import HTTPException

from api.services.private_media import media_preview

logger = logging.getLogger(__name__)


def attachment_views(store, athlete_id, rows):
    """Each row with a fresh, short-lived link. A video that is no longer
    playable (removed or reprocessing) is left out rather than failing the list."""
    views = []
    for row in rows:
        try:
            media = media_preview(store, athlete_id, row["media_id"])
        except HTTPException as exc:
            logger.info("[attachments] skipped media_id=%s status=%s", row.get("media_id"), exc.status_code)
            continue
        views.append({"id": row["id"], "created_at": row["created_at"], "media": media})
    return views
