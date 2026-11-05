"""Parse PostgREST row lists with ``json.loads`` instead of recursive pydantic validation.

supabase-py's ``APIResponse.from_http_request_response`` validates every
response body against the recursive ``JSON`` union twice (``validate_json``,
then the ``List[JSON]`` field). For JSON that cost grows with every nested
value, and it ran on every Data API read this process makes. One active plan
row (``plans?select=*``, ~1.3 MB) took 115–175 ms of CPU to parse that way
against ~6 ms for ``json.loads``; the training projection (~550 KB) is read
several times per app open. Under ``uvicorn --workers 1`` that CPU holds the
GIL every other request in the process needs. See
docs/performance/app-open-critical-path-2026-11-05.md.

A JSON array decodes to exactly the values the validator returns (lists,
dicts, str, int, float, bool, None), so for a successful array body the
response is built without re-validating it. Anything else (an object or
scalar body, invalid JSON, an empty body) goes through the library's own
method unchanged, including the errors it raises.
"""

from __future__ import annotations

import json
from typing import Any

from postgrest.base_request_builder import APIResponse

_INSTALLED_ATTR = "_unlxck_fast_rows"


def parse_rows(request_response: Any) -> APIResponse | None:
    """The response for a JSON-array body, else ``None`` (caller falls back)."""
    content = request_response.content
    if not content:
        return None
    try:
        data = json.loads(content)
    except ValueError:
        return None
    if type(data) is not list:
        return None
    count = APIResponse._get_count_from_http_request_response(request_response)
    return APIResponse.model_construct(data=data, count=count)


def install_fast_row_parsing() -> None:
    """Idempotently route ``APIResponse.from_http_request_response`` through ``parse_rows``."""
    if getattr(APIResponse, _INSTALLED_ATTR, False):
        return
    original = APIResponse.from_http_request_response

    def from_http_request_response(request_response: Any) -> APIResponse:
        parsed = parse_rows(request_response)
        return parsed if parsed is not None else original(request_response)

    APIResponse.from_http_request_response = staticmethod(from_http_request_response)  # type: ignore[method-assign]
    setattr(APIResponse, _INSTALLED_ATTR, True)


__all__ = ["install_fast_row_parsing", "parse_rows"]
