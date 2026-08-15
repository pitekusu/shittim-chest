"""Deterministic contract generation tests."""

from __future__ import annotations

import json
from pathlib import Path

from shittim_records.generate_contracts import expected_documents, write_or_check


def test_generated_contracts_are_deterministic_and_checkable(tmp_path: Path) -> None:
    write_or_check(tmp_path, check=False)
    first = {path.name: path.read_bytes() for path in sorted(tmp_path.iterdir())}

    write_or_check(tmp_path, check=True)

    assert first == expected_documents()
    json_schema = json.loads(first["records-api.schema.json"])
    assert len(json_schema["oneOf"]) == 6
    assert "#/components/schemas/" not in first["records-api.schema.json"].decode()
    assert '"$ref": "#/$defs/AvatarRef"' in first["records-api.schema.json"].decode()
    session_branches = json_schema["$defs"]["SessionResponse"]["anyOf"]
    assert session_branches == [
        {"$ref": "#/$defs/AuthenticatedSession"},
        {"$ref": "#/$defs/AnonymousSession"},
    ]
    openapi = json.loads(first["openapi.json"])
    assert openapi["info"]["title"] == "Shittim Chest Records API"
    assert set(openapi["paths"]) == {
        "/api/v1/auth/discord/start",
        "/api/v1/auth/discord/callback",
        "/api/v1/session",
        "/api/v1/logout",
        "/api/v1/records",
        "/api/v1/records/{recordId}",
        "/api/v1/insights/rankings",
        "/api/v1/insights/costs",
    }
    assert openapi["paths"]["/api/v1/auth/discord/start"]["get"]["security"] == []
    assert openapi["paths"]["/api/v1/session"]["get"]["security"] == []
    assert [
        parameter["name"] for parameter in openapi["paths"]["/api/v1/records"]["get"]["parameters"]
    ] == ["cursor", "limit", "from", "to", "winner"]
    assert openapi["paths"]["/api/v1/logout"]["post"]["parameters"][0]["name"] == ("X-CSRF-Token")
