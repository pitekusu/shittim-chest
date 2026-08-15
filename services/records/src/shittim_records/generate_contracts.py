"""Generate deterministic Records API JSON Schema and OpenAPI documents."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from shittim_records.contracts import PUBLIC_RESPONSE_MODELS, RECORDS_API_SCHEMA_VERSION

SCHEMA_FILENAME = "records-api.schema.json"
OPENAPI_FILENAME = "openapi.json"
SCHEMA_ID = "https://shittim-chest.invalid/contracts/records/v1/records-api.schema.json"


def _rewrite_refs(value: Any, *, reference_prefix: str) -> Any:
    if isinstance(value, dict):
        rewritten: dict[str, Any] = {}
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str) and item.startswith("#/$defs/"):
                rewritten[key] = item.replace("#/$defs/", reference_prefix, 1)
            else:
                rewritten[key] = _rewrite_refs(item, reference_prefix=reference_prefix)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_refs(item, reference_prefix=reference_prefix) for item in value]
    return value


def _component_schemas(*, reference_prefix: str) -> dict[str, Any]:
    components: dict[str, Any] = {}
    for model in PUBLIC_RESPONSE_MODELS:
        schema = model.model_json_schema(by_alias=True, mode="serialization")
        definitions = schema.pop("$defs", {})
        rewritten_definitions = _rewrite_refs(
            definitions,
            reference_prefix=reference_prefix,
        )
        for name, definition in rewritten_definitions.items():
            existing = components.get(name)
            if existing is not None and existing != definition:
                raise RuntimeError(f"conflicting generated schema: {name}")
            components[name] = definition
        components[model.__name__] = _rewrite_refs(
            schema,
            reference_prefix=reference_prefix,
        )
    return dict(sorted(components.items()))


def build_json_schema() -> dict[str, Any]:
    definitions = _component_schemas(reference_prefix="#/$defs/")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Shittim Chest Records API v1",
        "oneOf": [{"$ref": f"#/$defs/{model.__name__}"} for model in PUBLIC_RESPONSE_MODELS],
        "$defs": definitions,
    }


def _response(schema_name: str, description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {"schema": {"$ref": f"#/components/schemas/{schema_name}"}}
        },
    }


def _parameter(
    name: str,
    location: str,
    schema: Mapping[str, Any],
    *,
    required: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "in": location,
        "required": required,
        "schema": dict(schema),
    }


def build_openapi() -> dict[str, Any]:
    error_responses = {
        code: _response("ErrorResponse", description)
        for code, description in (
            ("400", "Invalid request"),
            ("401", "Authentication required"),
            ("403", "Guild membership required"),
            ("404", "Record not found"),
            ("409", "Projection conflict"),
            ("429", "Request rate limited"),
            ("503", "Records service unavailable"),
        )
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Shittim Chest Records API",
            "version": str(RECORDS_API_SCHEMA_VERSION),
        },
        "paths": {
            "/api/v1/auth/discord/start": {
                "get": {
                    "operationId": "beginDiscordOAuth",
                    "security": [],
                    "parameters": [
                        _parameter(
                            "returnTo",
                            "query",
                            {"type": "string", "pattern": r"^/(?!/)", "maxLength": 256},
                        )
                    ],
                    "responses": {"302": {"description": "Discord authorization redirect"}},
                }
            },
            "/api/v1/auth/discord/callback": {
                "get": {
                    "operationId": "completeDiscordOAuth",
                    "security": [],
                    "parameters": [
                        _parameter(
                            "code", "query", {"type": "string", "minLength": 1}, required=True
                        ),
                        _parameter(
                            "state", "query", {"type": "string", "minLength": 1}, required=True
                        ),
                    ],
                    "responses": {"302": {"description": "Authenticated SPA redirect"}},
                }
            },
            "/api/v1/session": {
                "get": {
                    "operationId": "getSession",
                    "responses": {
                        "200": _response("SessionResponse", "Current browser session"),
                        **error_responses,
                    },
                }
            },
            "/api/v1/logout": {
                "post": {
                    "operationId": "logout",
                    "parameters": [
                        _parameter(
                            "X-CSRF-Token",
                            "header",
                            {"type": "string", "minLength": 1},
                            required=True,
                        )
                    ],
                    "responses": {"204": {"description": "Session removed"}, **error_responses},
                }
            },
            "/api/v1/records": {
                "get": {
                    "operationId": "listRecords",
                    "parameters": [
                        _parameter("cursor", "query", {"type": "string", "minLength": 1}),
                        _parameter(
                            "limit",
                            "query",
                            {"type": "integer", "minimum": 1, "maximum": 50, "default": 12},
                        ),
                        _parameter("from", "query", {"type": "string", "format": "date-time"}),
                        _parameter("to", "query", {"type": "string", "format": "date-time"}),
                        _parameter(
                            "winner",
                            "query",
                            {
                                "type": "string",
                                "enum": ["participant-a", "participant-b", "participant-c"],
                            },
                        ),
                    ],
                    "responses": {
                        "200": _response("RecordListResponse", "Completed debate records"),
                        **error_responses,
                    },
                }
            },
            "/api/v1/records/{recordId}": {
                "get": {
                    "operationId": "getRecord",
                    "parameters": [
                        {
                            "name": "recordId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "minLength": 1},
                        }
                    ],
                    "responses": {
                        "200": _response("RecordDetailResponse", "One completed debate"),
                        **error_responses,
                    },
                }
            },
            "/api/v1/insights/rankings": {
                "get": {
                    "operationId": "getRankings",
                    "responses": {
                        "200": _response("RankingsResponse", "Victory and request rankings"),
                        **error_responses,
                    },
                }
            },
            "/api/v1/insights/costs": {
                "get": {
                    "operationId": "getCosts",
                    "parameters": [
                        _parameter(
                            "period",
                            "query",
                            {
                                "type": "string",
                                "enum": ["today", "week", "month", "all"],
                                "default": "month",
                            },
                        )
                    ],
                    "responses": {
                        "200": _response("CostsResponse", "Approximate service costs"),
                        **error_responses,
                    },
                }
            },
        },
        "components": {
            "schemas": _component_schemas(reference_prefix="#/components/schemas/"),
            "securitySchemes": {
                "sessionCookie": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "__Host-shittim-records-session",
                }
            },
        },
        "security": [{"sessionCookie": []}],
    }


def _encoded(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def expected_documents() -> dict[str, bytes]:
    return {
        SCHEMA_FILENAME: _encoded(build_json_schema()),
        OPENAPI_FILENAME: _encoded(build_openapi()),
    }


def write_or_check(output: Path, *, check: bool) -> None:
    documents = expected_documents()
    if check:
        mismatches = [
            name
            for name, expected in documents.items()
            if not (output / name).is_file() or (output / name).read_bytes() != expected
        ]
        extras = sorted(path.name for path in output.glob("*.json") if path.name not in documents)
        if mismatches or extras:
            details = ", ".join((*mismatches, *extras))
            raise SystemExit(f"Records API contracts are not current: {details}")
        return
    output.mkdir(parents=True, exist_ok=True)
    for name, data in documents.items():
        (output / name).write_bytes(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    write_or_check(args.output, check=args.check)


if __name__ == "__main__":
    main()
