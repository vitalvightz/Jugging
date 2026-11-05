"""The fast PostgREST row parser returns exactly what supabase-py would."""

import json

import httpx
import pytest
from pydantic import ValidationError
from postgrest.base_request_builder import APIResponse

import api.store  # noqa: F401 - installs the parser, as the app does
from api.postgrest_parsing import install_fast_row_parsing, parse_rows


def _library_parse(response: httpx.Response) -> APIResponse:
    """supabase-py's own implementation, bypassing the installed fast path."""
    count = APIResponse._get_count_from_http_request_response(response)
    from postgrest.types import JSONAdapter

    try:
        data = JSONAdapter.validate_json(response.content)
    except ValidationError:
        data = response.text if len(response.text) > 0 else []
    return APIResponse(data=data, count=count)


def _response(body: bytes, *, prefer: str | None = None, content_range: str | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://example.supabase.co/rest/v1/plans", headers={"prefer": prefer} if prefer else {})
    headers = {"content-range": content_range} if content_range else {}
    return httpx.Response(200, content=body, request=request, headers=headers)


PLAN_LIKE = [
    {
        "id": "11111111-1111-1111-1111-111111111111",
        "fight_date": None,
        "stage2_attempt_count": 1,
        "score": 0.75,
        "big": 2**63 + 1,
        "flags": [True, False, None],
        "planning_brief": {"weeks": [{"days": [{"date": "2026-11-05", "label": "Ünïcødé — ✓", "load": 1.0}]}]},
        "plan_text": "line\nline \"quoted\" \\ back",
    },
    {"id": "2", "nested": [[[]], {}, [{"a": {"b": [1, 2.5e-7, -0.0]}}]]},
]


@pytest.mark.parametrize(
    "rows",
    [PLAN_LIKE, [], [1, "x", None, 1.5, True], [{"a": 1}] * 3],
)
def test_rows_match_library_values_and_types(rows):
    body = json.dumps(rows).encode()
    fast = APIResponse.from_http_request_response(_response(body))
    library = _library_parse(_response(body))
    assert fast.data == library.data
    # Same JSON, same Python types at every level (int stays int, float stays float).
    assert json.dumps(fast.data) == json.dumps(library.data)
    assert repr(fast.data) == repr(library.data)
    assert fast.count == library.count is None


def test_count_header_is_preserved():
    body = json.dumps([{"id": 1}]).encode()
    response = _response(body, prefer="count=exact", content_range="0-0/42")
    assert APIResponse.from_http_request_response(response).count == 42


@pytest.mark.parametrize("body", [b'{"a": 1}', b"5", b'"text"', b"null", b"true", b"not json"])
def test_non_array_bodies_keep_library_behaviour(body):
    # supabase-py rejects these; the request builder turns that into APIError.
    with pytest.raises(ValidationError):
        APIResponse.from_http_request_response(_response(body))
    assert parse_rows(_response(body)) is None


def test_empty_body_keeps_library_behaviour():
    assert APIResponse.from_http_request_response(_response(b"")).data == []
    assert parse_rows(_response(b"")) is None


def test_install_is_idempotent():
    installed = APIResponse.from_http_request_response
    install_fast_row_parsing()
    assert APIResponse.from_http_request_response is installed
