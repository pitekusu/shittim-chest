"""Tests for release-tool pin validation and latest-release comparison."""

import json
from pathlib import Path

import pytest
from tools.check_tool_versions import ToolPin, find_outdated_pins, load_tool_pins


def _valid_config() -> dict[str, object]:
    version = "1.6.1"
    return {
        "schema_version": 1,
        "tools": {
            "betterleaks": {
                "archive_name": f"betterleaks_{version}_linux_x64.tar.gz",
                "archive_sha256": "a" * 64,
                "certificate_identity": (
                    "https://github.com/betterleaks/betterleaks/.github/workflows/"
                    f"release.yml@refs/tags/v{version}"
                ),
                "certificate_oidc_issuer": "https://token.actions.githubusercontent.com",
                "checksums_name": "checksums.txt",
                "checksums_sha256": "b" * 64,
                "repository": "betterleaks/betterleaks",
                "signature_bundle_name": "checksums.txt.sigstore.json",
                "signature_bundle_sha256": "c" * 64,
                "tag_prefix": "v",
                "version": version,
            }
        },
    }


def _write_config(path: Path, config: object) -> None:
    path.write_text(json.dumps(config) + "\n", encoding="utf-8")


def test_load_tool_pins_validates_betterleaks_identity(tmp_path: Path) -> None:
    config = tmp_path / "tools.json"
    _write_config(config, _valid_config())

    pins = load_tool_pins(config)

    assert pins == (
        ToolPin(
            name="betterleaks",
            repository="betterleaks/betterleaks",
            version="1.6.1",
            tag_prefix="v",
            archive_name="betterleaks_1.6.1_linux_x64.tar.gz",
            archive_sha256="a" * 64,
        ),
    )


def test_load_tool_pins_rejects_identity_for_another_tag(tmp_path: Path) -> None:
    config = _valid_config()
    tools = config["tools"]
    assert isinstance(tools, dict)
    betterleaks = tools["betterleaks"]
    assert isinstance(betterleaks, dict)
    betterleaks["certificate_identity"] = str(betterleaks["certificate_identity"]).replace(
        "v1.6.1", "v1.6.0"
    )
    path = tmp_path / "tools.json"
    _write_config(path, config)

    with pytest.raises(ValueError, match="certificate identity"):
        load_tool_pins(path)


def test_load_tool_pins_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "tools.json"
    path.write_text('{"schema_version":1,"schema_version":1,"tools":{}}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_tool_pins(path)


def test_find_outdated_pins_reports_only_mismatches() -> None:
    pins = (
        ToolPin("current", "owner/current", "1.2.3", "v", "current_1.2.3.tar.gz", "a" * 64),
        ToolPin("old", "owner/old", "2.0.0", "v", "old_2.0.0.tar.gz", "b" * 64),
    )
    tags = {"owner/current": "v1.2.3", "owner/old": "v2.1.0"}

    assert find_outdated_pins(pins, tags.__getitem__) == (
        "old: pinned v2.0.0, latest v2.1.0 (owner/old)",
    )
