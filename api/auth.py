from __future__ import annotations

import hashlib
import logging
import os
import warnings
from dataclasses import dataclass
from threading import Lock, RLock
from time import monotonic, perf_counter, time as wall_time
from typing import Any, Callable, Protocol

import httpx
import jwt
from fastapi import HTTPException, status
from supabase import Client, ClientOptions, create_client

from . import server_timing
from .postgrest_parsing import install_fast_row_parsing
from .environment import is_production_environment
from .settings import env_flag, env_int

logger = logging.getLogger(__name__)
# Every Data API read in this process; see api/postgrest_parsing.py.
install_fast_row_parsing()


def _collect_auth_api_error_types() -> tuple[type[BaseException], ...]:
    classes: list[type[BaseException]] = []
    try:
        from supabase_auth.errors import AuthApiError as _SupabaseAuthApiError
    except ImportError:  # pragma: no cover - dependency may be absent
        pass
    else:
        classes.append(_SupabaseAuthApiError)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from gotrue.errors import AuthApiError as _GotrueAuthApiError
    except ImportError:  # pragma: no cover - dependency may be absent
        pass
    else:
        if _GotrueAuthApiError not in classes:
            classes.append(_GotrueAuthApiError)
    return tuple(classes)


AUTH_API_ERROR_TYPES: tuple[type[BaseException], ...] = _collect_auth_api_error_types()


def is_auth_api_error(exc: BaseException) -> bool:
    if AUTH_API_ERROR_TYPES and isinstance(exc, AUTH_API_ERROR_TYPES):
        return True
    # Duck-typing fallback covers vendored or renamed Supabase auth modules.
    return exc.__class__.__name__ == "AuthApiError" and (
        exc.__class__.__module__.startswith("gotrue")
        or exc.__class__.__module__.startswith("supabase_auth")
    )


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    email: str
    full_name: str
    metadata: dict[str, Any]


class AuthService(Protocol):
    def get_user_from_token(self, token: str) -> AuthenticatedUser: ...


def _token_cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Supabase signs access tokens with an asymmetric project key (ES256/RS256)
# published at the JWKS endpoint. Verifying those locally removes a network
# round trip to Supabase Auth from every request whose token is not already in
# the per-process cache. Legacy HS256 tokens, unknown key ids and any JWKS
# outage fall back to the upstream ``auth.get_user`` check.
_LOCAL_JWT_ALGORITHMS = frozenset({"ES256", "RS256"})
_JWT_AUDIENCE = "authenticated"


def _unverified_exp(token: str) -> float | None:
    """``exp`` read without verification, used only to bound the cache.

    Only consulted after the token passed local or upstream verification, so a
    forged value cannot extend anything: it can only shorten the cache entry.
    """
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


# A JWKS fetch times out after 3 s; waiters give up shortly after that.
_JWKS_WAIT_SECONDS = 4.0


class _LocalTokenRejected(Exception):
    """The token was checked against a known project key and is not valid."""


class SupabaseJwksVerifier:
    def __init__(
        self,
        supabase_url: str,
        *,
        fetch_jwks: Callable[[str], dict[str, Any]] | None = None,
        cache_seconds: float = 600.0,
        min_refetch_seconds: float = 30.0,
    ) -> None:
        base = supabase_url.rstrip("/")
        self.issuer = f"{base}/auth/v1"
        self.jwks_url = f"{self.issuer}/.well-known/jwks.json"
        self._fetch_jwks = fetch_jwks or self._http_fetch_jwks
        self._cache_seconds = cache_seconds
        self._min_refetch_seconds = min_refetch_seconds
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = RLock()
        # One fetch at a time. Requests that arrive while it runs wait for its
        # keys instead of each falling back to /auth/v1/user (seen as concurrent
        # upstream calls on the first requests after every restart).
        self._fetch_lock = Lock()
        self._fetching = False

    @staticmethod
    def _http_fetch_jwks(url: str) -> dict[str, Any]:
        response = httpx.get(url, timeout=httpx.Timeout(3.0))
        response.raise_for_status()
        return response.json()

    def _due(self, *, force: bool) -> bool:
        with self._lock:
            if self._fetched_at is None:
                return True
            age = monotonic() - self._fetched_at
            return not (age < self._min_refetch_seconds or (not force and age < self._cache_seconds))

    def _wait_for_fetch(self) -> None:
        # Bounded by the fetch's own HTTP timeout; on expiry the caller defers upstream.
        if self._fetch_lock.acquire(timeout=_JWKS_WAIT_SECONDS):
            self._fetch_lock.release()

    def warm(self) -> None:
        """Load the keys ahead of the first request (startup)."""
        self._refresh(force=False)

    def _refresh(self, *, force: bool) -> None:
        if not self._due(force=force):
            with self._lock:
                fetching = self._fetching
            if fetching:
                self._wait_for_fetch()
            return
        with self._fetch_lock:
            with self._lock:
                if self._fetched_at is not None and not self._due(force=force):
                    return  # another request fetched while this one waited
                # Mark the attempt first so an outage cannot turn into a fetch per request.
                self._fetched_at = monotonic()
                self._fetching = True
            try:
                self._fetch_and_store()
            finally:
                with self._lock:
                    self._fetching = False

    def _fetch_and_store(self) -> None:
        try:
            payload = self._fetch_jwks(self.jwks_url)
        except Exception:
            logger.warning("[auth] jwks_fetch_failed; using upstream verification", exc_info=True)
            return
        keys: dict[str, jwt.PyJWK] = {}
        for raw in payload.get("keys", []) if isinstance(payload, dict) else []:
            kid = raw.get("kid") if isinstance(raw, dict) else None
            if not kid:
                continue
            try:
                key = jwt.PyJWK(raw)
            except Exception:
                continue
            if key.algorithm_name in _LOCAL_JWT_ALGORITHMS:
                keys[kid] = key
        with self._lock:
            self._keys = keys

    def _key_for(self, kid: str) -> jwt.PyJWK | None:
        self._refresh(force=False)
        with self._lock:
            key = self._keys.get(kid)
        if key is None:
            # Key rotation: an unseen kid triggers one rate-limited refetch.
            self._refresh(force=True)
            with self._lock:
                key = self._keys.get(kid)
        return key

    def verify(self, token: str) -> dict[str, Any] | None:
        """Return verified claims, ``None`` to defer upstream, or raise on a bad token."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            return None
        alg = header.get("alg")
        kid = header.get("kid")
        if alg not in _LOCAL_JWT_ALGORITHMS or not kid:
            return None
        key = self._key_for(str(kid))
        if key is None or key.algorithm_name != alg:
            return None
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[alg],
                audience=_JWT_AUDIENCE,
                issuer=self.issuer,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except jwt.InvalidTokenError as exc:
            raise _LocalTokenRejected(exc.__class__.__name__) from exc
        if not isinstance(claims, dict) or not claims.get("sub"):
            raise _LocalTokenRejected("missing_sub")
        return claims


class SupabaseAuthService:
    def __init__(self, client: Client, jwks_verifier: SupabaseJwksVerifier | None = None):
        self.client = client
        self.jwks_verifier = jwks_verifier
        self._token_cache: dict[str, tuple[AuthenticatedUser, float]] = {}
        self._cache_lock = RLock()
        self._cache_ttl_seconds = env_int("AUTH_TOKEN_CACHE_TTL", 60)
        self._max_cache_size = env_int("AUTH_TOKEN_CACHE_MAX_SIZE", 1000)

    def warm(self) -> None:
        """Fetch the project's signing keys before the first request needs them."""
        if self.jwks_verifier is not None:
            self.jwks_verifier.warm()

    @classmethod
    def from_env(cls) -> "SupabaseAuthService":
        url = os.getenv("SUPABASE_URL")
        service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        anon_key = os.getenv("SUPABASE_ANON_KEY")
        allow_anon_fallback = (
            os.getenv("ALLOW_SUPABASE_ANON_AUTH_FALLBACK", "").strip().lower()
            in {"1", "true", "yes"}
        )

        if not url:
            raise RuntimeError("SUPABASE_URL is required")

        # Disable HTTP/2 to avoid RemoteProtocolError (GOAWAY frames) when
        # Supabase terminates a multiplexed connection after several streams.
        # Explicit timeout avoids httpx's short default read timeout causing
        # false auth outages during transient Supabase latency spikes.
        _client_opts = ClientOptions(
            httpx_client=httpx.Client(
                http2=False,
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=30.0,
                    write=5.0,
                    pool=15.0,
                ),
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=20,
                    keepalive_expiry=30.0,
                ),
            )
        )

        # Kill switch: AUTH_LOCAL_JWT_VERIFICATION=0 restores upstream-only checks.
        jwks_verifier = (
            SupabaseJwksVerifier(url) if env_flag("AUTH_LOCAL_JWT_VERIFICATION", True) else None
        )

        if service_role_key:
            logger.info(
                "[auth] initializing with SUPABASE_SERVICE_ROLE_KEY has_url=%s local_jwt=%s",
                bool(url),
                jwks_verifier is not None,
            )
            return cls(
                create_client(url, service_role_key, options=_client_opts),
                jwks_verifier=jwks_verifier,
            )

        if allow_anon_fallback and is_production_environment():
            logger.error(
                "[auth] anon_fallback_blocked_in_production "
                "ALLOW_SUPABASE_ANON_AUTH_FALLBACK is ignored in production; "
                "SUPABASE_SERVICE_ROLE_KEY must be set"
            )
            raise RuntimeError(
                "SUPABASE_SERVICE_ROLE_KEY is required in production; "
                "ALLOW_SUPABASE_ANON_AUTH_FALLBACK is ignored."
            )

        if allow_anon_fallback and anon_key:
            logger.warning(
                "[auth] SUPABASE_SERVICE_ROLE_KEY not set; using SUPABASE_ANON_KEY because "
                "ALLOW_SUPABASE_ANON_AUTH_FALLBACK is enabled has_url=%s",
                bool(url),
            )
            return cls(create_client(url, anon_key, options=_client_opts), jwks_verifier=jwks_verifier)

        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is required. "
            "Set ALLOW_SUPABASE_ANON_AUTH_FALLBACK=true only for local/dev fallback."
        )

    def get_user_from_token(self, token: str) -> AuthenticatedUser:
        token = token.strip()

        if not token:
            raise self._unauthorized()

        if token.lower().startswith("bearer "):
            token = token.split(" ", 1)[1].strip()

        if not token:
            raise self._unauthorized()

        started = perf_counter()
        cache_key = _token_cache_key(token)

        cached_user = self._get_cached_user(cache_key)
        if cached_user is not None:
            server_timing.record("auth", (perf_counter() - started) * 1000, "cache")
            return cached_user

        # The cache entry never outlives the token: both paths cap it at exp.
        token_exp = _unverified_exp(token)

        local_user = self._verify_locally(token)
        if local_user is not None:
            self._cache_user(cache_key, local_user, token_exp=token_exp)
            server_timing.record("auth", (perf_counter() - started) * 1000, "local")
            return local_user

        authenticated_user = self._verify_upstream(token)
        self._cache_user(cache_key, authenticated_user, token_exp=token_exp)
        server_timing.record("auth", (perf_counter() - started) * 1000, "upstream")
        return authenticated_user

    def _verify_locally(self, token: str) -> AuthenticatedUser | None:
        if self.jwks_verifier is None:
            return None
        try:
            claims = self.jwks_verifier.verify(token)
        except _LocalTokenRejected as exc:
            logger.warning("[auth] invalid_token source=local reason=%s", exc)
            raise self._invalid_token() from exc
        if claims is None:
            return None
        raw_metadata = claims.get("user_metadata") or {}
        return self._build_user(
            user_id=claims["sub"],
            email=claims.get("email"),
            raw_metadata=raw_metadata,
        )

    def _verify_upstream(self, token: str) -> AuthenticatedUser:
        try:
            response = self.client.auth.get_user(token)
        except httpx.HTTPError as exc:  # pragma: no cover - network/runtime integration
            logger.exception("[auth] upstream token verification failed")
            raise self._auth_unavailable() from exc
        except Exception as exc:
            if not is_auth_api_error(exc):
                raise

            status_code = getattr(exc, "status", None) or getattr(exc, "status_code", None)

            if status_code in {400, 401, 403}:
                logger.warning(
                    "[auth] invalid_token status=%s error_class=%s",
                    status_code,
                    exc.__class__.__module__ + "." + exc.__class__.__name__,
                )
                raise self._invalid_token() from exc

            logger.exception("[auth] upstream token verification failed")
            raise self._auth_unavailable() from exc

        user = getattr(response, "user", None)

        if user is None:
            raise self._invalid_token()

        user_id = getattr(user, "id", None)
        if not user_id:
            logger.warning("[auth] verified user response missing user id")
            raise self._invalid_token()

        return self._build_user(
            user_id=user_id,
            email=getattr(user, "email", None),
            raw_metadata=getattr(user, "user_metadata", {}) or {},
        )

    @staticmethod
    def _build_user(*, user_id: Any, email: Any, raw_metadata: Any) -> AuthenticatedUser:
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        email_text = str(email or "")
        full_name = (
            metadata.get("full_name")
            or metadata.get("name")
            or email_text
            or "Athlete"
        )
        return AuthenticatedUser(
            user_id=str(user_id),
            email=email_text,
            full_name=str(full_name),
            metadata=metadata,
        )

    def _get_cached_user(self, cache_key: str) -> AuthenticatedUser | None:
        now = monotonic()

        with self._cache_lock:
            cached = self._token_cache.get(cache_key)

            if cached is None:
                return None

            user, expires_at = cached

            if expires_at <= now:
                self._token_cache.pop(cache_key, None)
                return None

            return user

    def _cache_user(
        self,
        cache_key: str,
        user: AuthenticatedUser,
        *,
        token_exp: float | None = None,
    ) -> None:
        now = monotonic()
        ttl = float(self._cache_ttl_seconds)
        if token_exp is not None:
            # exp is wall-clock; convert the remaining lifetime to monotonic.
            ttl = min(ttl, token_exp - wall_time())
        if ttl <= 0:
            return

        with self._cache_lock:
            self._token_cache[cache_key] = (user, now + ttl)
            self._prune_cache(now)

    def _prune_cache(self, now: float) -> None:
        expired_tokens = [
            cache_token
            for cache_token, (_, expires_at) in self._token_cache.items()
            if expires_at <= now
        ]

        for cache_token in expired_tokens:
            self._token_cache.pop(cache_token, None)

        while self._token_cache and len(self._token_cache) > self._max_cache_size:
            oldest_token = next(iter(self._token_cache))
            self._token_cache.pop(oldest_token, None)

    @staticmethod
    def _unauthorized() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        )

    @staticmethod
    def _invalid_token() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid authentication token",
        )

    @staticmethod
    def _auth_unavailable() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication service temporarily unavailable",
        )
