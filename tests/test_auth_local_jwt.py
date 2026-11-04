from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException, status

from api import server_timing
from api.auth import SupabaseAuthService, SupabaseJwksVerifier

SUPABASE_URL = "https://project.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"


def _key_pair(kid: str):
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": kid, "alg": "ES256", "use": "sig"})
    return private_key, public_jwk


def _token(private_key, kid: str, **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": "athlete-1",
        "aud": "authenticated",
        "iss": ISSUER,
        "exp": now + 3600,
        "iat": now,
        "email": "a@example.com",
        "user_metadata": {"full_name": "Ada Fighter"},
        "role": "authenticated",
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


class _Jwks:
    def __init__(self, *keys):
        self.keys = list(keys)
        self.calls = 0
        self.fail = False

    def __call__(self, url: str):
        assert url == f"{ISSUER}/.well-known/jwks.json"
        self.calls += 1
        if self.fail:
            raise RuntimeError("jwks down")
        return {"keys": self.keys}


def _service(fetch, client=None):
    verifier = SupabaseJwksVerifier(SUPABASE_URL, fetch_jwks=fetch, min_refetch_seconds=0)
    return SupabaseAuthService(client or MagicMock(), jwks_verifier=verifier)


def test_valid_es256_token_verifies_without_upstream_call():
    private_key, public_jwk = _key_pair("k1")
    client = MagicMock()
    service = _service(_Jwks(public_jwk), client)

    user = service.get_user_from_token(_token(private_key, "k1"))

    assert user.user_id == "athlete-1"
    assert user.email == "a@example.com"
    assert user.full_name == "Ada Fighter"
    client.auth.get_user.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 10},
        {"aud": "anon"},
        {"iss": "https://other.supabase.co/auth/v1"},
    ],
)
def test_bad_claims_are_rejected_locally(overrides):
    private_key, public_jwk = _key_pair("k1")
    client = MagicMock()
    service = _service(_Jwks(public_jwk), client)

    with pytest.raises(HTTPException) as exc_info:
        service.get_user_from_token(_token(private_key, "k1", **overrides))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    client.auth.get_user.assert_not_called()


def test_token_signed_by_wrong_key_with_known_kid_is_rejected():
    _, public_jwk = _key_pair("k1")
    attacker_key, _ = _key_pair("k1")
    client = MagicMock()
    service = _service(_Jwks(public_jwk), client)

    with pytest.raises(HTTPException) as exc_info:
        service.get_user_from_token(_token(attacker_key, "k1"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    client.auth.get_user.assert_not_called()


def _upstream_client():
    client = MagicMock()
    client.auth.get_user.return_value = MagicMock(
        user=MagicMock(id="athlete-2", email="b@example.com", user_metadata={})
    )
    return client


def test_hs256_legacy_token_falls_back_to_upstream():
    client = _upstream_client()
    service = _service(_Jwks(), client)
    token = jwt.encode(
        {"sub": "athlete-2", "aud": "authenticated", "iss": ISSUER, "exp": int(time.time()) + 60},
        "legacy-secret-legacy-secret-legacy-secret",
        algorithm="HS256",
    )

    user = service.get_user_from_token(token)

    assert user.user_id == "athlete-2"
    client.auth.get_user.assert_called_once_with(token)


def test_unknown_kid_refetches_once_then_falls_back_upstream():
    private_key, _ = _key_pair("rotated")
    jwks = _Jwks()
    client = _upstream_client()
    service = _service(jwks, client)

    service.get_user_from_token(_token(private_key, "rotated"))

    assert jwks.calls == 2  # initial load + one forced refetch for the unseen kid
    client.auth.get_user.assert_called_once()


def test_rotated_key_is_picked_up_on_refetch():
    old_key, old_jwk = _key_pair("old")
    new_key, new_jwk = _key_pair("new")
    jwks = _Jwks(old_jwk)
    client = MagicMock()
    service = _service(jwks, client)
    service.get_user_from_token(_token(old_key, "old"))

    jwks.keys.append(new_jwk)
    user = service.get_user_from_token(_token(new_key, "new"))

    assert user.user_id == "athlete-1"
    client.auth.get_user.assert_not_called()


def test_jwks_outage_falls_back_to_upstream():
    private_key, _ = _key_pair("k1")
    jwks = _Jwks()
    jwks.fail = True
    client = _upstream_client()
    service = _service(jwks, client)

    user = service.get_user_from_token(_token(private_key, "k1"))

    assert user.user_id == "athlete-2"
    client.auth.get_user.assert_called_once()


def test_jwks_outage_is_rate_limited():
    private_key, _ = _key_pair("k1")
    jwks = _Jwks()
    jwks.fail = True
    verifier = SupabaseJwksVerifier(SUPABASE_URL, fetch_jwks=jwks, min_refetch_seconds=60)
    service = SupabaseAuthService(_upstream_client(), jwks_verifier=verifier)

    for index in range(5):
        service.get_user_from_token(_token(private_key, "k1", sub=f"athlete-{index}"))

    assert jwks.calls == 1


def test_no_verifier_keeps_upstream_only_behaviour():
    private_key, _ = _key_pair("k1")
    client = _upstream_client()
    service = SupabaseAuthService(client)

    service.get_user_from_token(_token(private_key, "k1"))

    client.auth.get_user.assert_called_once()


def test_auth_timing_records_resolution_source():
    private_key, public_jwk = _key_pair("k1")
    service = _service(_Jwks(public_jwk))
    token = _token(private_key, "k1")

    timings, handle = server_timing.begin()
    try:
        service.get_user_from_token(token)
        service.get_user_from_token(token)
    finally:
        server_timing.end(handle)

    assert [(name, desc) for name, _, desc in timings.entries] == [
        ("auth", "local"),
        ("auth", "cache"),
    ]


def test_authenticated_response_carries_server_timing():
    from support import _build_client

    client, _, _ = _build_client()

    response = client.get(
        "/api/me",
        headers={"Authorization": "Bearer athlete-token", "Origin": "http://localhost:3000"},
    )

    assert response.status_code == 200
    header = response.headers["server-timing"]
    names = [part.strip().split(";")[0] for part in header.split(",")]
    assert "profile" in names
    assert names[-1] == "app"
    assert "athlete" not in header and "@" not in header


def test_unauthenticated_response_still_carries_app_timing():
    from support import _build_client

    client, _, _ = _build_client()

    response = client.get("/health")

    assert response.headers["server-timing"].startswith("app;dur=")
