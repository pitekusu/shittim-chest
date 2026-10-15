"""Contract generation checks; validation behavior is exercised by API tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

from shittim_records.generate_contracts import expected_documents, write_or_check


def test_generated_contracts_are_deterministic_and_checkable(tmp_path: Path) -> None:
    write_or_check(tmp_path, check=False)
    first = {path.name: path.read_bytes() for path in sorted(tmp_path.iterdir())}

    write_or_check(tmp_path, check=True)

    assert first == expected_documents()
    assert set(first) == {
        "openapi.json",
        "records-api.schema.json",
        "records-invariants.ts",
        "mobile-auth.schema.json",
    }
    schema = json.loads(first["records-api.schema.json"])
    openapi = json.loads(first["openapi.json"])
    for filename, prefix, definitions in (
        ("records-api.schema.json", "#/$defs/", schema["$defs"]),
        ("openapi.json", "#/components/schemas/", openapi["components"]["schemas"]),
    ):
        references = re.findall(r'"\$ref": "([^"]+)"', first[filename].decode())
        assert references
        assert all(reference.startswith(prefix) for reference in references)
        assert all(reference.removeprefix(prefix) in definitions for reference in references)

    assert openapi["security"] == [{"sessionCookie": []}]
    bearer_routes = {
        (method, path)
        for path, operations in openapi["paths"].items()
        for method, operation in operations.items()
        if {"mobileBearer": []} in operation.get("security", [])
    }
    assert bearer_routes == {
        ("post", "/api/v1/debate-attachments/uploads"),
        ("get", "/api/v1/debate-attachments/uploads/{attachmentId}"),
        ("post", "/api/v1/debate-attachments/uploads/{attachmentId}/complete"),
        ("post", "/api/v1/debate-requests"),
        ("get", "/api/v1/debate-requests"),
        ("get", "/api/v1/debate-requests/{requestId}"),
        ("get", "/api/v1/records"),
        ("get", "/api/v1/records/sync-index"),
        ("get", "/api/v1/records/{recordId}"),
        ("get", "/api/v1/auth/mobile/session"),
        ("post", "/api/v1/auth/mobile/logout"),
        ("put", "/api/v1/auth/mobile/notifications/device"),
        ("delete", "/api/v1/auth/mobile/notifications/device"),
    }
    mobile_paths = {path for path in openapi["paths"] if "/auth/mobile/" in path}
    assert mobile_paths == {
        f"/api/v1/auth/mobile/{name}"
        for name in ("start", "authorize", "exchange", "session", "logout", "notifications/device")
    }
    assert "MobileStartRequest" not in schema["$defs"]  # Web validators stay independent.
    for name in ("start", "exchange"):
        operation = openapi["paths"][f"/api/v1/auth/mobile/{name}"]["post"]
        assert operation["security"] == [] and operation["requestBody"]["required"]
    assert openapi["components"]["securitySchemes"]["mobileBearer"]["scheme"] == "bearer"
    for route in ("/api/v1/auth/discord/start", "/api/v1/auth/discord/callback", "/api/v1/session"):
        assert openapi["paths"][route]["get"]["security"] == []
    for route in (
        "/api/v1/admin/prompts/apply",
        "/api/v1/admin/prompts/rollback",
        "/api/v1/admin/status/refresh",
        "/api/v1/memorial/upload",
        "/api/v1/memorial/generate",
        "/api/v1/memorial/reset",
    ):
        operation = openapi["paths"][route]["post"]
        required_headers = {
            parameter["name"]
            for parameter in operation["parameters"]
            if parameter.get("required") and parameter["in"] == "header"
        }
        assert {"X-CSRF-Token", "X-Idempotency-Key"} <= required_headers
        assert {"401", "403", "409"} <= operation["responses"].keys()
    for private_name in ("requesterKey", "ownerKey", "discordUserId"):
        assert private_name not in first["openapi.json"].decode()


def test_documented_login_return_path_rejects_external_redirects() -> None:
    openapi = json.loads(expected_documents()["openapi.json"])
    parameter = openapi["paths"]["/api/v1/auth/discord/start"]["get"]["parameters"][0]
    pattern = parameter["schema"]["pattern"]
    for safe in ("/", f"/records/{'r' * 43}"):
        assert re.search(pattern, safe) is not None
    for unsafe in (
        "//evil.example",
        r"/\evil.example",
        r"/\\evil.example",
        "/\t/evil.example",
        "/\r/evil.example",
        "/\n/evil.example",
    ):
        assert re.search(pattern, unsafe) is None


def test_archive_search_keeps_private_criteria_in_body_and_uses_browser_auth() -> None:
    openapi = json.loads(expected_documents()["openapi.json"])
    search = openapi["paths"]["/api/v1/records/search"]["post"]
    assert search["security"] == [{"sessionCookie": []}]
    assert search["requestBody"]["required"]
    assert search["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RecordSearchRequest"
    }
    assert {parameter["name"] for parameter in search["parameters"]} == {
        "Origin",
        "X-CSRF-Token",
        "contract",
    }
    assert all(
        parameter["in"] == "header" and parameter["required"]
        for parameter in search["parameters"]
        if parameter["name"] != "contract"
    )
    for operation in (openapi["paths"]["/api/v1/records"]["get"], search):
        contract = next(item for item in operation["parameters"] if item["name"] == "contract")
        assert contract["in"] == "query" and not contract["required"]
        assert contract["schema"]["enum"] == ["record-attachments-v1"]
    requesters = openapi["paths"]["/api/v1/records/requesters"]["get"]
    assert requesters["security"] == [{"sessionCookie": []}]
    assert [parameter["name"] for parameter in requesters["parameters"]] == ["cursor"]


def test_momotalk_detail_documents_opt_in_multi_participant_images() -> None:
    openapi = json.loads(expected_documents()["openapi.json"])
    detail = openapi["paths"]["/api/v1/momotalk/weeks/{weekId}/rooms/{roomId}"]["get"]
    contract = next(item for item in detail["parameters"] if item["name"] == "contract")
    assert contract == {
        "name": "contract",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["momotalk-images-v2"]},
    }
    assert "最大3画像" in detail["description"] and "最大2画像" in detail["description"]
    for path in ("/api/v1/momotalk/weeks", "/api/v1/momotalk/weeks/{weekId}/rooms"):
        parameters = openapi["paths"][path]["get"]["parameters"]
        assert "contract" not in {item["name"] for item in parameters}
