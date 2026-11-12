import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CADDYFILE_SOURCE = (REPO_ROOT / "caddy" / "Caddyfile").read_text(encoding="utf-8")
COMPOSE_SOURCE = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")


def _documentation_csp_directives() -> dict[str, set[str]]:
    csp_line = next(
        line.strip()
        for line in CADDYFILE_SOURCE.splitlines()
        if line.strip().startswith("header @documentation Content-Security-Policy")
    )
    policy = csp_line.split('"', 1)[1].rsplit('"', 1)[0]
    return {
        parts[0]: set(parts[1:])
        for directive in policy.split(";")
        if (parts := directive.split())
    }


def _origins(urls: list[str]) -> set[str]:
    return {f"{parsed.scheme}://{parsed.netloc}" for url in urls if (parsed := urlsplit(url)).netloc}


def test_production_caddyfile_preserves_api_proxy_and_security_headers():
    shared_headers = (
        'Cache-Control "no-store"',
        'Strict-Transport-Security "max-age=31536000; includeSubDomains"',
        'X-Content-Type-Options "nosniff"',
        'X-Frame-Options "DENY"',
        'X-Robots-Tag "noindex, nofollow, noarchive"',
        'Referrer-Policy "no-referrer"',
        'Permissions-Policy "camera=(), microphone=(), geolocation=()"',
    )

    assert "header {" in CADDYFILE_SOURCE
    assert "-Server" in CADDYFILE_SOURCE
    for header in shared_headers:
        assert header in CADDYFILE_SOURCE

    strict_csp = 'Content-Security-Policy "default-src \'none\'; frame-ancestors \'none\'"'
    assert f"header @api {strict_csp}" in CADDYFILE_SOURCE
    assert "@api {\n        not path /docs /docs/* /redoc /redoc/*\n    }" in CADDYFILE_SOURCE

    documentation_csp = next(
        line.strip()
        for line in CADDYFILE_SOURCE.splitlines()
        if line.strip().startswith("header @documentation Content-Security-Policy")
    )
    assert "@documentation path /docs /docs/* /redoc /redoc/*" in CADDYFILE_SOURCE
    for required_source in (
        "script-src 'unsafe-inline' https://cdn.jsdelivr.net",
        "style-src 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
        "img-src https://fastapi.tiangolo.com data:",
        "font-src https://fonts.gstatic.com",
        "connect-src 'self'",
        "frame-ancestors 'none'",
    ):
        assert required_source in documentation_csp

    for preserved_directive in (
        "encode zstd gzip",
        "reverse_proxy api:8000",
        "dial_timeout 10s",
        "response_header_timeout 180s",
        "output stdout",
        "format json",
    ):
        assert preserved_directive in CADDYFILE_SOURCE


def test_cloudflare_facing_origin_rejects_early_data_and_unnecessary_http3_port():
    for directive in (
        "0rtt off",
        "max_header_size 32KB",
        "read_header 10s",
        "idle 2m",
    ):
        assert directive in CADDYFILE_SOURCE

    assert '"443:443"' in COMPOSE_SOURCE
    assert '"443:443/udp"' not in COMPOSE_SOURCE


def test_documentation_csp_allows_fastapi_default_assets():
    pages = "\n".join(
        (
            get_swagger_ui_html(openapi_url="/openapi.json", title="docs").body.decode(),
            get_redoc_html(openapi_url="/openapi.json", title="redoc").body.decode(),
            get_swagger_ui_oauth2_redirect_html().body.decode(),
        )
    )
    directives = _documentation_csp_directives()

    script_urls = re.findall(r'<script[^>]+src="([^"]+)"', pages)
    stylesheet_urls = re.findall(r'<link rel="stylesheet"[^>]+href="([^"]+)"', pages)
    image_urls = re.findall(r'<link rel="shortcut icon"[^>]+href="([^"]+)"', pages)

    assert _origins(script_urls) <= directives["script-src"]
    assert _origins(stylesheet_urls) <= directives["style-src"]
    assert _origins(image_urls) <= directives["img-src"]
    assert "'unsafe-inline'" in directives["script-src"]
    assert "'unsafe-inline'" in directives["style-src"]
    assert "https://fonts.gstatic.com" in directives["font-src"]
    assert directives["connect-src"] == {"'self'"}


def test_caddy_config_is_mounted_as_a_directory_so_reloads_see_deployed_changes():
    # The deploy replaces files with `git reset --hard` (new inodes) and then
    # runs `caddy reload`. A single-file bind mount keeps the container on the
    # old inode, so the reload silently re-read stale config.
    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "- ./caddy:/etc/caddy:ro" in compose
    assert "./Caddyfile:" not in compose
    assert not (REPO_ROOT / "Caddyfile").exists()
