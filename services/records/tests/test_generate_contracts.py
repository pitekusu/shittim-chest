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
    detail_properties = json_schema["$defs"]["RecordDetailResponse"]["properties"]
    for collection_name, identity_field in (
        ("participants", "slot"),
        ("initialOpinions", "participant"),
        ("finalProposals", "participant"),
        ("votes", "voter"),
    ):
        assert {
            constraint["contains"]["properties"][identity_field]["const"]
            for constraint in detail_properties[collection_name]["allOf"]
        } == {"participant-a", "participant-b", "participant-c"}
    vote_count_constraints = json_schema["$defs"]["RecordResultSummary"]["properties"][
        "voteCounts"
    ]["allOf"]
    assert {
        constraint["contains"]["properties"]["participant"]["const"]
        for constraint in vote_count_constraints
    } == {"participant-a", "participant-b", "participant-c"}
    assert {
        constraint["if"]["properties"]["voter"]["const"]: constraint["then"]["properties"][
            "candidate"
        ]["not"]["const"]
        for constraint in json_schema["$defs"]["VoteView"]["allOf"]
    } == {
        "participant-a": "participant-a",
        "participant-b": "participant-b",
        "participant-c": "participant-c",
    }
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
