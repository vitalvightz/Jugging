"""PDFs: rebuilt page by page into a new image-only file, switched on separately,
downloaded only (never shown inline), owner and consent checked on every link."""
from uuid import uuid4

import pytest

from api.services.training_attachments import pdf_display_name
from tests.test_coach_fighter_access import AS_COACH, BASE, COACH, FIGHTER
from tests.test_coach_fighter_access import camp  # noqa: F401 - shared pytest fixture

AS_FIGHTER = {"Authorization": "Bearer fighter-token"}
AS_STRANGER = {"Authorization": "Bearer stranger-token"}
UPLOADS = "/api/attachments/uploads"
URL = "/api/training/attachments"


@pytest.fixture
def docs(camp, monkeypatch):  # noqa: F811 - pytest fixture injection
    client, store, connection = camp
    connection["share_media"] = True
    monkeypatch.setenv("ATTACHMENT_PDFS_ENABLED", "true")
    signed: list[tuple[str, str | None]] = []
    calls: list[tuple[str, int | None, bool]] = []

    def sign(path, *, expires_in, download=None):
        signed.append((path, download))
        return f"https://signed.example/{path}?ttl={expires_in}&download={download}"

    def process(asset_id, page=None, assemble=False):
        # Stands in for the Edge Function: page 2 of a 666-byte upload is damaged.
        calls.append((asset_id, page, assemble))
        asset = store.attachment_assets[asset_id]
        if page:
            if asset["size_bytes"] == 666 and page == 2:
                asset.update(status="rejected", reject_reason="Page 2: Damaged JPEG")
                return {"status": "rejected", "reason": "Page 2: Damaged JPEG"}
            asset["status"] = "processing"
            return {"status": "page_ready", "page": page}
        asset.update(status="ready", ready_path=f"{asset['owner_id']}/{asset_id}/clean.pdf")
        return {"status": "ready", "kind": "pdf"}
    store.attachment_signed_url = sign
    store.process_attachment = process
    session = store.create_external_training_session(FIGHTER, {
        "training_day": "2026-11-15", "activity": "boxing", "duration_minutes": 40, "effort": "hard",
        "client_request_id": str(uuid4()), "request_fingerprint": "c" * 64})
    return client, store, connection, session["id"], signed, calls


def _pdf(client, headers, pages=2, size=5000, name="Week 1 plan.pdf"):
    reserved = client.post(UPLOADS, headers=headers, json={"kind": "pdf", "size_bytes": size, "page_count": pages,
                                                           "file_name": name})
    assert reserved.status_code == 201, reserved.text
    body = reserved.json()
    results = [client.post(f"{UPLOADS}/{body['asset_id']}/process", headers=headers, params={"page": page}).json()
               for page in range(1, pages + 1)]
    if all(result["status"] == "page_ready" for result in results):
        results.append(client.post(f"{UPLOADS}/{body['asset_id']}/process", headers=headers).json())
    return body, results


def test_a_pdf_is_rebuilt_page_by_page_and_only_ever_downloaded(docs):
    client, store, _, target, signed, calls = docs
    permission, results = _pdf(client, AS_FIGHTER)
    assert permission["path"] == f"{FIGHTER}/{permission['asset_id']}/pages"
    assert permission["content_type"] == "image/jpeg" and permission["page_count"] == 2
    assert [r["status"] for r in results] == ["page_ready", "page_ready", "ready"]
    assert [(page, assemble) for _, page, assemble in calls] == [(1, False), (2, False), (None, True)]
    created = client.post(URL, headers=AS_FIGHTER, json={"asset_id": permission["asset_id"], "source": "external",
                                                         "target_id": target})
    assert created.status_code == 201, created.text
    file = created.json()["file"]
    assert created.json()["kind"] == "pdf" and file["name"] == "Week 1 plan.pdf" and file["pages"] == 2
    assert file["url"] is None and signed == []
    played = client.get(f"{URL}/{created.json()['id']}/play", headers=AS_FIGHTER)
    assert played.status_code == 200 and "no-store" in played.headers["cache-control"]
    # Only the rebuilt file is signed, and always as a named download.
    assert signed == [(f"{FIGHTER}/{permission['asset_id']}/clean.pdf", "Week 1 plan.pdf")]


def test_one_bad_page_rejects_the_whole_pdf(docs):
    client, store, _, target, _, calls = docs
    permission, results = _pdf(client, AS_FIGHTER, pages=3, size=666)
    assert results[1] == {"asset_id": permission["asset_id"], "status": "rejected", "reason": "Page 2: Damaged JPEG"}
    assert results[2]["status"] == "rejected"
    assert all(not assemble for _, _, assemble in calls)
    assert client.post(URL, headers=AS_FIGHTER, json={"asset_id": permission["asset_id"], "source": "external",
                                                      "target_id": target}).status_code == 409
    assert store.training_attachments == []


def test_pdf_limits_names_and_switch(docs, monkeypatch):
    client, store, _, _, _, calls = docs
    too_many = {"kind": "pdf", "size_bytes": 10, "page_count": 31, "file_name": "a.pdf"}
    assert client.post(UPLOADS, headers=AS_FIGHTER, json=too_many).status_code == 422
    assert client.post(UPLOADS, headers=AS_FIGHTER, json={"kind": "pdf", "size_bytes": 10}).status_code == 422
    assert client.post(UPLOADS, headers=AS_FIGHTER, json={"kind": "pdf", "size_bytes": 9 * 1024 * 1024,
                                                          "page_count": 1}).status_code == 422
    assert client.post(UPLOADS, headers=AS_FIGHTER, json={"kind": "photo", "size_bytes": 10,
                                                          "page_count": 1}).status_code == 422
    hostile = client.post(UPLOADS, headers=AS_FIGHTER, json={"kind": "pdf", "size_bytes": 10, "page_count": 1,
                                                             "file_name": "../../<script>x</script>.pdf"}).json()
    assert store.attachment_assets[hostile["asset_id"]]["file_name"] == "script x script.pdf"
    # Pages outside the document, or pages for a photo, are refused before the processor.
    assert client.post(f"{UPLOADS}/{hostile['asset_id']}/process", headers=AS_FIGHTER, params={"page": 2}).status_code == 422
    assert client.post(f"{UPLOADS}/{hostile['asset_id']}/process", headers=AS_FIGHTER, params={"page": 31}).status_code == 422
    assert calls == []
    monkeypatch.delenv("ATTACHMENT_PDFS_ENABLED")
    assert client.get("/api/attachments/settings", headers=AS_FIGHTER).json()["pdfs_available"] is False
    assert client.post(f"{UPLOADS}/{hostile['asset_id']}/process", headers=AS_FIGHTER, params={"page": 1}).status_code == 503


def test_switching_pdfs_off_hides_them_and_stops_links(docs, monkeypatch):
    client, _, _, target, signed, _ = docs
    permission, _ = _pdf(client, AS_FIGHTER, pages=1)
    created = client.post(URL, headers=AS_FIGHTER, json={"asset_id": permission["asset_id"], "source": "external",
                                                         "target_id": target}).json()
    monkeypatch.delenv("ATTACHMENT_PDFS_ENABLED")
    assert client.get(URL, headers=AS_FIGHTER, params={"source": "external", "target_id": target}).json() == []
    assert client.get(f"{URL}/{created['id']}/play", headers=AS_FIGHTER).status_code == 404
    assert signed == []


def test_nobody_else_can_process_attach_or_download_a_pdf(docs):
    client, _, _, target, signed, _ = docs
    mine, _ = _pdf(client, AS_FIGHTER, pages=1)
    assert client.post(f"{UPLOADS}/{mine['asset_id']}/process", headers=AS_STRANGER, params={"page": 1}).status_code == 404
    assert client.post(URL, headers=AS_STRANGER, json={"asset_id": mine["asset_id"], "source": "external",
                                                       "target_id": target}).status_code == 404
    created = client.post(URL, headers=AS_FIGHTER, json={"asset_id": mine["asset_id"], "source": "external",
                                                         "target_id": target}).json()
    assert client.get(f"{URL}/{created['id']}/play", headers=AS_STRANGER).status_code == 404
    assert signed == []


def test_a_coach_shares_a_pdf_only_with_media_sharing_on(docs):
    client, _, connection, target, signed, _ = docs
    path = f"{BASE}/training/attachments"
    params = {"source": "external", "target_id": target}
    guide, _ = _pdf(client, AS_COACH, pages=1, name="Nutrition guide.pdf")
    assert guide["path"].startswith(f"{COACH}/")
    connection["share_media"] = False
    assert client.post(path, headers=AS_COACH, json={"asset_id": guide["asset_id"], **params}).status_code == 404
    connection["share_media"] = True
    created = client.post(path, headers=AS_COACH, json={"asset_id": guide["asset_id"], **params})
    assert created.status_code == 201, created.text
    shared = created.json()
    assert shared["kind"] == "pdf" and shared["author"] == "coach"
    # The athlete downloads it; the coach loses access the moment sharing stops.
    assert client.get(f"{URL}/{shared['id']}/play", headers=AS_FIGHTER).status_code == 200
    connection["share_media"] = False
    assert client.get(f"{path}/{shared['id']}/play", headers=AS_COACH).status_code == 404
    assert len(signed) == 1


@pytest.mark.parametrize("raw,clean", [
    ("Week 1 plan.pdf", "Week 1 plan.pdf"), ("../../etc/passwd", "etc passwd.pdf"), (None, "Document.pdf"),
    ("é€.PDF", "Document.pdf"), ("a" * 300 + ".pdf", "a" * 96 + ".pdf"), ("  .hidden.pdf", "hidden.pdf"),
])
def test_display_names_are_plain(raw, clean):
    assert pdf_display_name(raw) == clean
