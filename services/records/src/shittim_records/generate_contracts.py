"""Generate deterministic Records API JSON Schema and OpenAPI documents."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, get_args

from shittim_records.contracts import (
    PUBLIC_REQUEST_MODELS,
    PUBLIC_RESPONSE_MODELS,
    RECORDS_API_SCHEMA_VERSION,
    ParticipantSlot,
)

SCHEMA_FILENAME = "records-api.schema.json"
OPENAPI_FILENAME = "openapi.json"
INVARIANTS_FILENAME = "records-invariants.ts"
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
    for model in (*PUBLIC_RESPONSE_MODELS, *PUBLIC_REQUEST_MODELS):
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


def _request_body(schema_name: str) -> dict[str, Any]:
    return {
        "required": True,
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


def _admin_write_headers() -> list[dict[str, Any]]:
    return [
        _parameter(
            "X-CSRF-Token",
            "header",
            {"type": "string", "minLength": 1},
            required=True,
        ),
        _parameter(
            "X-Idempotency-Key",
            "header",
            {
                "type": "string",
                "pattern": r"^[A-Za-z0-9._~-]{16,128}$",
            },
            required=True,
        ),
    ]


def _memorial_write_headers() -> list[dict[str, Any]]:
    return [
        _parameter(
            "Origin",
            "header",
            {"type": "string", "format": "uri", "pattern": r"^https://"},
            required=True,
        ),
        *_admin_write_headers(),
    ]


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
                            {
                                "type": "string",
                                "pattern": r"^/(?![/\\])[^\t\r\n]*$",
                                "maxLength": 256,
                            },
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
                    "security": [],
                    "parameters": [
                        _parameter(
                            "contract",
                            "query",
                            {"type": "string", "enum": ["admin-v1"]},
                        )
                    ],
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
                        _parameter(
                            "cursor",
                            "query",
                            {"type": "string", "minLength": 1, "maxLength": 4096},
                        ),
                        _parameter(
                            "limit",
                            "query",
                            {"type": "integer", "minimum": 1, "maximum": 50, "default": 12},
                        ),
                        _parameter(
                            "sort",
                            "query",
                            {
                                "type": "string",
                                "enum": ["newest", "oldest"],
                                "default": "newest",
                            },
                        ),
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
                            "schema": {
                                "type": "string",
                                "pattern": "^[A-Za-z0-9_-]{43}$",
                            },
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
                        "200": _response("RankingsResponse", "Current Records rankings"),
                        **error_responses,
                    },
                }
            },
            "/api/v1/insights/affection-rankings": {
                "get": {
                    "operationId": "getAffectionRankings",
                    "parameters": [
                        _parameter(
                            "limit",
                            "query",
                            {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 50,
                                "default": 50,
                            },
                        ),
                        _parameter(
                            "cursor",
                            "query",
                            {"type": "string", "minLength": 1, "maxLength": 4096},
                        ),
                    ],
                    "responses": {
                        "200": _response(
                            "AffectionRankingsResponse",
                            "Requester affection rankings for all three participants",
                        ),
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
                                "default": "week",
                            },
                        )
                    ],
                    "responses": {
                        "200": _response("CostsResponse", "Estimated Records costs in JPY"),
                        **error_responses,
                    },
                }
            },
            "/api/v1/memorial": {
                "get": {
                    "operationId": "getMemorial",
                    "responses": {
                        "200": _response(
                            "MemorialStateResponse",
                            "Current owner's Memorial Lobby state",
                        ),
                        **error_responses,
                    },
                }
            },
            "/api/v1/memorial/upload": {
                "post": {
                    "operationId": "prepareMemorialUpload",
                    "parameters": _memorial_write_headers(),
                    "requestBody": _request_body("MemorialUploadRequest"),
                    "responses": {
                        "200": _response(
                            "MemorialUploadResponse",
                            "One owner-bound presigned image upload",
                        ),
                        **error_responses,
                    },
                }
            },
            "/api/v1/memorial/generate": {
                "post": {
                    "operationId": "generateMemorial",
                    "parameters": _memorial_write_headers(),
                    "requestBody": _request_body("MemorialGenerateRequest"),
                    "responses": {
                        "202": _response(
                            "MemorialStateResponse",
                            "Memorial generation queued",
                        ),
                        **error_responses,
                    },
                }
            },
            "/api/v1/memorial/memories/{cycle}": {
                "get": {
                    "operationId": "getMemorialMemory",
                    "parameters": [
                        _parameter(
                            "cycle",
                            "path",
                            {"type": "integer", "minimum": 1, "maximum": 1_000_000_000},
                            required=True,
                        )
                    ],
                    "responses": {
                        "200": _response(
                            "MemorialMemoryResponse",
                            "One generated Memorial Lobby memory owned by the session",
                        ),
                        **error_responses,
                    },
                }
            },
            "/api/v1/memorial/reset": {
                "post": {
                    "operationId": "resetMemorial",
                    "parameters": _memorial_write_headers(),
                    "requestBody": _request_body("MemorialResetRequest"),
                    "responses": {
                        "200": _response(
                            "MemorialStateResponse",
                            "Affection reset and next Memorial cycle started",
                        ),
                        **error_responses,
                    },
                }
            },
            "/api/v1/admin/prompts": {
                "get": {
                    "operationId": "getAdminPrompts",
                    "responses": {
                        "200": _response(
                            "AdminPromptsResponse",
                            "Current runtime prompt configuration",
                        ),
                        **error_responses,
                    },
                }
            },
            "/api/v1/admin/prompts/apply": {
                "post": {
                    "operationId": "applyAdminPrompts",
                    "parameters": _admin_write_headers(),
                    "requestBody": _request_body("AdminPromptApplyRequest"),
                    "responses": {
                        "200": _response(
                            "AdminPromptApplyResponse",
                            "Immutable prompt revision saved",
                        ),
                        **error_responses,
                    },
                }
            },
            "/api/v1/admin/prompts/revisions": {
                "get": {
                    "operationId": "listAdminPromptRevisions",
                    "parameters": [
                        _parameter(
                            "cursor",
                            "query",
                            {"type": "string", "pattern": r"^r[0-9a-hjkmnp-tv-z]{26}$"},
                        ),
                        _parameter(
                            "limit",
                            "query",
                            {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 50,
                                "default": 20,
                            },
                        ),
                    ],
                    "responses": {
                        "200": _response(
                            "AdminPromptRevisionsResponse",
                            "Content-free prompt revision history",
                        ),
                        **error_responses,
                    },
                }
            },
            "/api/v1/admin/prompts/revisions/{revision}": {
                "get": {
                    "operationId": "getAdminPromptRevision",
                    "parameters": [
                        _parameter(
                            "revision",
                            "path",
                            {"type": "string", "pattern": r"^r[0-9a-hjkmnp-tv-z]{26}$"},
                            required=True,
                        )
                    ],
                    "responses": {
                        "200": _response(
                            "AdminPromptRevisionResponse",
                            "One immutable prompt revision",
                        ),
                        **error_responses,
                    },
                }
            },
            "/api/v1/admin/prompts/rollback": {
                "post": {
                    "operationId": "rollbackAdminPrompts",
                    "parameters": _admin_write_headers(),
                    "requestBody": _request_body("AdminPromptRollbackRequest"),
                    "responses": {
                        "200": _response(
                            "AdminPromptApplyResponse",
                            "Rollback content saved as a new revision",
                        ),
                        **error_responses,
                    },
                }
            },
            "/api/v1/admin/status": {
                "get": {
                    "operationId": "getAdminStatus",
                    "responses": {
                        "200": _response(
                            "AdminStatusResponse",
                            "Sanitized allowlisted AWS status",
                        ),
                        **error_responses,
                    },
                }
            },
            "/api/v1/admin/status/refresh": {
                "post": {
                    "operationId": "refreshAdminStatus",
                    "parameters": _admin_write_headers(),
                    "responses": {
                        "200": _response(
                            "AdminStatusResponse",
                            "Sanitized allowlisted AWS status",
                        ),
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


def build_typescript_invariants() -> bytes:
    """Generate semantic checks that JSON Schema cannot express across collections."""

    slots = "\n".join(f'  "{slot}",' for slot in get_args(ParticipantSlot))
    source = f"""// Generated by shittim_records.generate_contracts. Do not edit.

const participantSlots = [
{slots}
] as const;
type ParticipantSlot = (typeof participantSlots)[number];

function isObject(value: unknown): value is Record<string, unknown> {{
  return typeof value === "object" && value !== null && !Array.isArray(value);
}}

function isParticipantSlot(value: unknown): value is ParticipantSlot {{
  return participantSlots.some((slot) => slot === value);
}}

function validatedVoteCounts(
  record: Record<string, unknown>,
): Map<ParticipantSlot, number> | null {{
  if (!isObject(record.result) || !Array.isArray(record.result.voteCounts)) {{
    return null;
  }}
  const counts = new Map<ParticipantSlot, number>();
  for (const item of record.result.voteCounts) {{
    if (
      !isObject(item) ||
      !isParticipantSlot(item.participant) ||
      typeof item.count !== "number" ||
      !Number.isInteger(item.count) ||
      counts.has(item.participant)
    ) {{
      return null;
    }}
    counts.set(item.participant, item.count);
  }}
  if (
    counts.size !== participantSlots.length ||
    [...counts.values()].reduce((total, count) => total + count, 0) !== participantSlots.length
  ) {{
    return null;
  }}
  const highestCount = Math.max(...counts.values());
  const leaders = participantSlots.filter((slot) => counts.get(slot) === highestCount);
  if (
    !isParticipantSlot(record.result.winner) ||
    !leaders.includes(record.result.winner) ||
    record.result.tieBreakApplied !== leaders.length > 1
  ) {{
    return null;
  }}
  return counts;
}}

export function hasConsistentRecordInvariants(value: unknown): boolean {{
  if (!isObject(value)) {{
    return false;
  }}
  if ("items" in value) {{
    return (
      Array.isArray(value.items) &&
      value.items.every((item) => isObject(item) && validatedVoteCounts(item) !== null)
    );
  }}
  if (!("finalDecision" in value)) {{
    return true;
  }}
  const summaryCounts = validatedVoteCounts(value);
  if (
    summaryCounts === null ||
    !isObject(value.result) ||
    !isObject(value.finalDecision) ||
    value.result.winner !== value.finalDecision.winner ||
    !Array.isArray(value.votes)
  ) {{
    return false;
  }}
  const ballotCounts = new Map<ParticipantSlot, number>(
    participantSlots.map((slot) => [slot, 0]),
  );
  for (const vote of value.votes) {{
    if (
      !isObject(vote) ||
      !isParticipantSlot(vote.voter) ||
      !isParticipantSlot(vote.candidate) ||
      vote.voter === vote.candidate
    ) {{
      return false;
    }}
    ballotCounts.set(vote.candidate, (ballotCounts.get(vote.candidate) ?? 0) + 1);
  }}
  return participantSlots.every((slot) => ballotCounts.get(slot) === summaryCounts.get(slot));
}}
"""
    return source.encode()


def expected_documents() -> dict[str, bytes]:
    return {
        SCHEMA_FILENAME: _encoded(build_json_schema()),
        OPENAPI_FILENAME: _encoded(build_openapi()),
        INVARIANTS_FILENAME: build_typescript_invariants(),
    }


def write_or_check(output: Path, *, check: bool) -> None:
    documents = expected_documents()
    if check:
        mismatches = [
            name
            for name, expected in documents.items()
            if not (output / name).is_file() or (output / name).read_bytes() != expected
        ]
        extras = sorted(
            path.name for path in output.iterdir() if path.is_file() and path.name not in documents
        )
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
