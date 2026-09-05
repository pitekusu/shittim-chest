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
    assert set(first) == {"openapi.json", "records-api.schema.json", "records-invariants.ts"}
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
