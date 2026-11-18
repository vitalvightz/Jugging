"""The web app's API types stay generated from, and checked against, the API.

web/lib/api-schema.generated.ts is written by tools/generate_api_types.py from
the OpenAPI schema; web/lib/api-contract.ts makes `npm run typecheck` fail when
the hand-written web types (web/lib/types.ts) drift from it. These tests keep
the generated file current and the contract complete.
"""

import re
from copy import deepcopy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from generate_api_types import (  # noqa: E402
    OUTPUT,
    build_openapi,
    render,
    request_schema_names,
    response_schema_names,
)

SPEC = build_openapi()


def test_generated_types_match_the_api_schema():
    assert OUTPUT.read_text(encoding="utf-8") == render(SPEC), (
        "web/lib/api-schema.generated.ts is out of date: run python tools/generate_api_types.py"
    )


def test_equivalent_enum_orders_generate_identical_types():
    reordered = deepcopy(SPEC)
    def reverse_enums(node):
        if isinstance(node, dict):
            if "enum" in node:
                node["enum"] = list(reversed(node["enum"]))
            for value in node.values():
                reverse_enums(value)
        elif isinstance(node, list):
            for value in node:
                reverse_enums(value)
    reverse_enums(reordered)
    assert render(reordered) == render(SPEC)


def test_every_shared_type_is_checked_in_the_right_direction():
    web_types = set(re.findall(r"^export (?:type|interface) (\w+)", (ROOT / "web/lib/types.ts").read_text(), re.M))
    contract = (ROOT / "web/lib/api-contract.ts").read_text(encoding="utf-8")
    requests, responses = request_schema_names(SPEC), response_schema_names(SPEC)
    missing = []
    for name in sorted(web_types & set(SPEC["components"]["schemas"])):
        if name in responses and f"export type Read{name} = " not in contract:
            missing.append(f"Read{name}")
        if name in responses and f"export type Declared{name} = " not in contract:
            missing.append(f"Declared{name}")
        if name in requests and f"export type Send{name} = " not in contract:
            missing.append(f"Send{name}")

    assert missing == [], f"add these checks to web/lib/api-contract.ts: {missing}"


def test_response_types_mark_every_field_present():
    # Served models are serialized with every field, even defaulted ones.
    generated = OUTPUT.read_text(encoding="utf-8")
    plan_detail = generated.split("export type PlanDetail = {", 1)[1].split("\n};", 1)[0]
    assert "plan_name: string | null;" in plan_detail
    assert "?:" not in plan_detail


def test_two_way_schemas_get_a_request_variant():
    generated = OUTPUT.read_text(encoding="utf-8")
    both = request_schema_names(SPEC) & response_schema_names(SPEC)

    assert both
    for name in both:
        assert f"export type {name} = " in generated
        assert f"export type {name}Request = " in generated


def test_fastapi_input_output_names_are_valid_typescript_and_keep_request_variants():
    spec = {"components": {"schemas": {
        "Example-Input": {"type": "object", "properties": {"value": {"type": "string"}}},
        "Example-Output": {"type": "object", "properties": {"value": {"type": "string"}}},
        "Wrapper": {"type": "object", "properties": {"request": {"$ref": "#/components/schemas/Example-Input"},
                                                            "response": {"$ref": "#/components/schemas/Example-Output"}}},
    }}, "paths": {"/example": {"post": {"requestBody": {"$ref": "#/components/schemas/Example-Input"},
                                               "responses": {"200": {"$ref": "#/components/schemas/Example-Output"}}}}}}
    generated = render(spec)
    assert "export type ExampleRequest =" in generated
    assert "export type Example =" in generated
    assert "value?: string" in generated
    assert "request: ExampleRequest" in generated and "response: Example" in generated
    assert "Example-Input" not in generated and "Example-Output" not in generated
