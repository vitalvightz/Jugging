"""Small store mixin; privileged queries always carry the authenticated owner."""
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from postgrest.exceptions import APIError as PostgrestAPIError

# attach_training_media raises these; each maps to one plain message.
_ATTACH_ERRORS = {
    "42501": (404, "That record or video is not available."),
    "55000": (409, "Video is still processing. Try again in a moment."),
    "54000": (409, "This already has six videos. Remove one to add another."),
    "22023": (422, "That exercise is not available."),
}


class PrivateMediaStoreMixin:
    def private_media_worker_healthy(self):
        try:
            since = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
            row = self._select_first(self.client.table("private_media_worker_health").select("id")
                                     .eq("id", "isolated-v1").gt("healthy_at", since))
            return bool(row)
        except Exception:
            # A missing migration or worker must never accidentally open uploads.
            return False

    def claim_private_media_budget(self, athlete_id):
        response = self.client.rpc("claim_private_media_budget", {"p_athlete_id": athlete_id}).execute()
        if not response.data:
            raise HTTPException(429, "Video limit reached. Try again later.", headers={"Retry-After": "3600"})

    def reserve_private_video(self, athlete_id, size_bytes, content_type):
        response = self.client.rpc("reserve_private_video", {
            "p_athlete_id": athlete_id, "p_size_bytes": size_bytes, "p_content_type": content_type,
        }).execute()
        if not response.data:
            raise HTTPException(429, "Video limit reached. Try again later.", headers={"Retry-After": "3600"})
        return response.data[0]

    def create_private_media(self, athlete_id, fields):
        allowed = ("provider", "video_id", "start_s", "end_s", "channel_title", "orientation", "made_for_kids")
        payload = {key: fields[key] for key in allowed if key in fields}
        response = self.client.table("private_exercise_media").insert({**payload, "athlete_id": athlete_id}).execute()
        return response.data[0]

    def get_private_media(self, athlete_id, media_id):
        return self._select_first(self.client.table("private_exercise_media").select("*")
                                 .eq("athlete_id", athlete_id).eq("id", media_id))

    def get_private_media_for_asset(self, athlete_id, asset_id):
        return self._select_first(self.client.table("private_exercise_media").select("*")
                                 .eq("athlete_id", athlete_id).eq("asset_id", asset_id))

    def get_private_video_asset(self, athlete_id, asset_id):
        return self._select_first(self.client.table("private_video_assets").select("*")
                                 .eq("athlete_id", athlete_id).eq("id", asset_id))

    def private_video_signed_url(self, path, *, expires_in):
        response = self.client.storage.from_("private-demo-ready").create_signed_url(path, expires_in)
        url = response.get("signedURL") or response.get("signedUrl")
        if not url:
            raise HTTPException(503, "Unable to open video. Try again.")
        return url

    def attach_training_media(self, athlete_id, media_id, source, target_id, exercise_ref):
        try:
            response = self.client.rpc("attach_training_media", {
                "p_athlete_id": athlete_id, "p_media_id": media_id, "p_source": source,
                "p_target_id": target_id, "p_exercise_ref": exercise_ref,
            }).execute()
        except PostgrestAPIError as exc:
            status, detail = _ATTACH_ERRORS.get(getattr(exc, "code", None) or "", (503, "Unable to attach the video. Try again."))
            raise HTTPException(status, detail) from exc
        if not response.data:
            raise HTTPException(409, "That video is already attached.")
        return response.data[0]

    def list_training_attachments(self, athlete_id, source, target_id, exercise_ref):
        response = (self.client.table("training_attachments").select("*").eq("athlete_id", athlete_id)
                    .eq("source", source).eq("target_id", target_id).eq("exercise_ref", exercise_ref)
                    .order("created_at").execute())
        return list(getattr(response, "data", None) or [])

    def get_training_attachment(self, athlete_id, attachment_id):
        response = (self.client.table("training_attachments").select("*").eq("athlete_id", athlete_id)
                    .eq("id", attachment_id).limit(1).execute())
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    def delete_training_attachment(self, athlete_id, attachment_id):
        response = (self.client.table("training_attachments").delete()
                    .eq("athlete_id", athlete_id).eq("id", attachment_id).execute())
        return bool(getattr(response, "data", None))
