from support import _build_client


def test_existing_profile_admission_is_read_only():
    client, store, _ = _build_client()
    headers = {"Authorization": "Bearer athlete-token"}

    assert client.get("/api/me/admission", headers=headers).json() == {"approved": True}

    store.profiles["athlete-1"]["access_status"] = "pending"
    assert client.get("/api/me/admission", headers=headers).json() == {"approved": False}

    del store.profiles["athlete-1"]
    assert client.get("/api/me/admission", headers=headers).json() == {"approved": False}
    assert "athlete-1" not in store.profiles


def test_existing_profile_admission_requires_authentication():
    client, _, _ = _build_client()
    assert client.get("/api/me/admission").status_code == 401
