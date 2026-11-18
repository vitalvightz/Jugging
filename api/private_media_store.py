"""Small store mixin; privileged queries always carry the authenticated owner."""
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException
from postgrest.exceptions import APIError as PostgrestAPIError

# attach_training_media raises these; each maps to one plain message.
_ATTACH_ERRORS = {
    "42501": (404, "That record or video is not available."),
    "55000": (409, "Video is still processing. Try again in a moment."),
    "54000": (409, "This already has six videos. Remove one to add another."),
    "22023": (422, "That exercise is not available."),
}


# attach_training_asset and reserve_attachment_upload raise these.
_FILE_ERRORS = {
    "42501": (404, "That record or file is not available."),
    "55000": (409, "That file is still being checked. Try again in a moment."),
    "54000": (409, "This already has six files. Remove one to add another."),
    "23514": (422, "That file type or size is not allowed."),
}
logger = logging.getLogger(__name__)


def _file_error(exc, fallback):
    status, detail = _FILE_ERRORS.get(getattr(exc, "code", None) or "", (503, fallback))
    return HTTPException(status, detail)


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
        if not rows:
            return None
        row = rows[0]
        if row["source"] == "progress":
            return row if row["target_id"] == athlete_id and not row.get("exercise_ref") else None
        valid = self.client.rpc("training_record_available", {
            "p_athlete_id": athlete_id, "p_source": row["source"],
            "p_target_id": row["target_id"], "p_ref": row.get("exercise_ref") or "",
        }).execute()
        return row if valid.data is True else None

    def recent_attachment_targets(self, athlete_id, *, limit=4):
        """The latest records that carry files, newest first, each still available."""
        response = (self.client.table("training_attachments").select("source,target_id,exercise_ref,created_at")
                    .eq("athlete_id", athlete_id).neq("source", "progress")
                    .order("created_at", desc=True).limit(60).execute())
        targets, seen = [], set()
        for row in getattr(response, "data", None) or []:
            key = (row["source"], row["target_id"], row.get("exercise_ref") or "")
            if key in seen:
                continue
            seen.add(key)
            valid = self.client.rpc("training_record_available", {
                "p_athlete_id": athlete_id, "p_source": key[0], "p_target_id": key[1], "p_ref": key[2],
            }).execute()
            if valid.data is True:
                targets.append({"source": key[0], "target_id": key[1], "exercise_ref": key[2],
                                "last_at": row["created_at"]})
            if len(targets) >= limit:
                break
        return targets

    def count_progress_photos(self, athlete_id):
        response = (self.client.table("training_attachments").select("id", count="exact")
                    .eq("athlete_id", athlete_id).eq("source", "progress").eq("target_id", athlete_id)
                    .limit(1).execute())
        return int(getattr(response, "count", None) or 0)

    def delete_training_attachment(self, athlete_id, attachment_id):
        response = (self.client.table("training_attachments").delete()
                    .eq("athlete_id", athlete_id).eq("id", attachment_id).execute())
        return bool(getattr(response, "data", None))

    # Photos and voice notes. The database functions are service-role only.
    def reserve_attachment_upload(self, owner_id, kind, size_bytes, content_type):
        try:
            response = self.client.rpc("reserve_attachment_upload", {
                "p_owner_id": owner_id, "p_kind": kind, "p_size_bytes": size_bytes, "p_content_type": content_type,
            }).execute()
        except PostgrestAPIError as exc:
            if getattr(exc, "code", None) == "54000":
                raise HTTPException(429, "Upload limit reached. Try again later.", headers={"Retry-After": "3600"}) from exc
            raise _file_error(exc, "Uploads are unavailable. Try again.") from exc
        if not response.data:
            raise HTTPException(503, "Uploads are unavailable. Try again.")
        return response.data[0]

    def get_attachment_asset(self, owner_id, asset_id):
        return self._select_first(self.client.table("attachment_assets").select("*")
                                 .eq("owner_id", owner_id).eq("id", asset_id))

    def get_attachment_assets(self, asset_ids):
        if not asset_ids:
            return {}
        response = (self.client.table("attachment_assets").select("*").in_("id", list(asset_ids))
                    .eq("status", "ready").execute())
        return {str(row["id"]): row for row in (getattr(response, "data", None) or [])}

    def reserve_attachment_document(self, owner_id, size_bytes, page_count, file_name):
        try:
            response = self.client.rpc("reserve_attachment_document", {
                "p_owner_id": owner_id, "p_size_bytes": size_bytes, "p_page_count": page_count, "p_file_name": file_name,
            }).execute()
        except PostgrestAPIError as exc:
            if getattr(exc, "code", None) == "54000":
                raise HTTPException(429, "Upload limit reached. Try again later.", headers={"Retry-After": "3600"}) from exc
            raise _file_error(exc, "Uploads are unavailable. Try again.") from exc
        if not response.data:
            raise HTTPException(503, "Uploads are unavailable. Try again.")
        return response.data[0]

    def attachment_signed_url(self, path, *, expires_in, download=None):
        # A download name makes Storage send Content-Disposition: attachment,
        # so a PDF is saved, never rendered inline from the storage origin.
        options = {"download": download} if download else None
        response = self.client.storage.from_("attachment-ready").create_signed_url(path, expires_in, options)
        url = response.get("signedURL") or response.get("signedUrl")
        if not url:
            raise HTTPException(503, "Unable to open this file. Try again.")
        return url

    def attachment_signed_urls(self, paths, *, expires_in):
        """Short-lived links for several rebuilt files at once (photo thumbnails
        in a list). Files that don't exist are simply left out."""
        if not paths:
            return {}
        try:
            items = self.client.storage.from_("attachment-ready").create_signed_urls(list(paths), expires_in)
        except Exception:  # noqa: BLE001 - previews are optional; the list still works without them
            logger.warning("[attachments] thumbnail signing failed")
            return {}
        return {item["path"]: item.get("signedURL") or item.get("signedUrl")
                for item in items or [] if not item.get("error") and (item.get("signedURL") or item.get("signedUrl"))}

    def process_attachment(self, asset_id, *, page=None, assemble=False):
        """Rebuild one upload (or one PDF page, or assemble a PDF) in the
        process-attachment Edge Function, never in this process."""
        url = os.getenv("SUPABASE_URL", "").rstrip("/")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        headers = {"apikey": key, "Content-Type": "application/json"}
        if key.count(".") == 2:
            headers["Authorization"] = f"Bearer {key}"
        body = {"asset_id": asset_id}
        if page:
            body["page"] = page
        if assemble:
            body["assemble"] = True
        try:
            response = httpx.post(f"{url}/functions/v1/process-attachment", json=body, headers=headers, timeout=60)
        except httpx.HTTPError as exc:
            logger.warning("[attachments] processor unreachable error=%s", type(exc).__name__)
            raise HTTPException(503, "Checking is unavailable. Try again in a minute.") from exc
        if response.status_code == 409:
            raise HTTPException(409, "Upload not found. Try again.")
        if response.status_code != 200:
            logger.warning("[attachments] processor status=%s", response.status_code)
            raise HTTPException(503, "Checking is unavailable. Try again in a minute.")
        return response.json()

    def attach_training_asset(self, author_id, athlete_id, asset_id, source, target_id, exercise_ref):
        try:
            response = self.client.rpc("attach_training_asset", {
                "p_author_id": author_id, "p_athlete_id": athlete_id, "p_asset_id": asset_id, "p_source": source,
                "p_target_id": target_id, "p_exercise_ref": exercise_ref,
            }).execute()
        except PostgrestAPIError as exc:
            if source == "progress" and getattr(exc, "code", None) == "54000":
                raise HTTPException(409, "You have 24 progress photos. Remove one to add another.") from exc
            raise _file_error(exc, "Unable to attach the file. Try again.") from exc
        if not response.data:
            raise HTTPException(409, "That file is already attached.")
        return response.data[0]

    def delete_authored_training_attachment(self, author_id, athlete_id, attachment_id):
        response = (self.client.table("training_attachments").delete().eq("athlete_id", athlete_id)
                    .eq("author_id", author_id).eq("id", attachment_id).execute())
        return bool(getattr(response, "data", None))
