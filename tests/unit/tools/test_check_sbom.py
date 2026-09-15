# SPDX-License-Identifier: MIT

import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from tools.check_sbom import (
    CycloneDxInventory,
    SbomError,
    compare_inventories,
    github_spdx_python_purls,
    validate_cyclonedx_text,
    validate_project_inventory,
)


def _versioned(name: str, version: str) -> str:
    return f"{name}@{version}"


def _cyclonedx_document() -> dict[str, object]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:019f69e7-b31d-7693-b832-e299366b5e15",
        "version": 1,
        "metadata": {
            "timestamp": "2026-07-17T00:00:00Z",
            "tools": [{"vendor": "Astral Software Inc.", "name": "uv", "version": "0.11.29"}],
            "component": {
                "type": "library",
                "bom-ref": _versioned("shittim-chest", "1.0.0"),
                "name": "shittim_chest",
                "version": "1.0.0",
            },
        },
        "components": [
            {
                "type": "library",
                "bom-ref": _versioned("pytest", "9.1.1"),
                "name": "pytest",
                "version": "9.1.1",
                "purl": _versioned("pkg:pypi/pytest", "9.1.1"),
            }
        ],
        "dependencies": [
            {
                "ref": _versioned("shittim-chest", "1.0.0"),
                "dependsOn": [_versioned("pytest", "9.1.1")],
            },
            {"ref": _versioned("pytest", "9.1.1")},
        ],
    }


def _github_spdx(*purls: str) -> dict[str, object]:
    return {
        "sbom": {
            "spdxVersion": "SPDX-2.3",
            "creationInfo": {"creators": ["Tool: dependabot", "Tool: GitHub.com"]},
            "packages": [
                {
                    "name": purl,
                    "SPDXID": f"SPDXRef-{index}",
                    "downloadLocation": "NOASSERTION",
                    "filesAnalyzed": False,
                    "externalRefs": [
                        {
                            "referenceCategory": "PACKAGE-MANAGER",
                            "referenceType": "purl",
                            "referenceLocator": purl,
                        }
                    ],
                }
                for index, purl in enumerate(purls)
            ],
        }
    }


def _project_documents() -> tuple[dict[str, object], dict[str, object]]:
    lock: dict[str, object] = {
        "package": [
            {
                "name": "shittim-chest",
                "version": "1.0.0",
                "source": {"editable": "."},
            },
            {
                "name": "pytest",
                "version": "9.1.1",
                "source": {"registry": "https://pypi.org/simple"},
            },
        ]
    }
    project: dict[str, object] = {"project": {"name": "shittim-chest", "version": "1.0.0"}}
    return lock, project


def test_valid_cyclonedx_inventory_is_accepted() -> None:
    inventory = validate_cyclonedx_text(json.dumps(_cyclonedx_document()))

    assert inventory.component_count == 1
    assert inventory.package_purls == frozenset({_versioned("pkg:pypi/pytest", "9.1.1")})
    assert inventory.project_purl == _versioned("pkg:pypi/shittim-chest", "1.0.0")


@pytest.mark.parametrize("declared", [True, False])
def test_component_without_purl_requires_an_explicit_local_project(declared: bool) -> None:
    document = _cyclonedx_document()
    components = cast(list[dict[str, object]], document["components"])
    local_purl = cast(str, components[0].pop("purl"))
    if declared:
        inventory = validate_cyclonedx_text(
            json.dumps(document), local_project_purls=frozenset({local_purl})
        )
        assert inventory.package_purls == {local_purl}
    else:
        with pytest.raises(SbomError, match="not an included project"):
            validate_cyclonedx_text(json.dumps(document))


def test_cyclonedx_inventory_matches_project_and_lock() -> None:
    inventory = validate_cyclonedx_text(json.dumps(_cyclonedx_document()))
    lock, project = _project_documents()

    validate_project_inventory(inventory, lock, project)


def test_lock_inventory_difference_is_rejected() -> None:
    inventory = validate_cyclonedx_text(json.dumps(_cyclonedx_document()))
    lock, project = _project_documents()
    packages = lock["package"]
    assert isinstance(packages, list)
    packages = cast(list[object], packages)
    packages.append(
        {
            "name": "missing",
            "version": "1.0.0",
            "source": {"registry": "https://pypi.org/simple"},
        }
    )

    with pytest.raises(SbomError, match="missing from CycloneDX"):
        validate_project_inventory(inventory, lock, project)


def test_strict_schema_violation_is_rejected() -> None:
    document = _cyclonedx_document()
    document["unexpected"] = True

    with pytest.raises(SbomError, match="schema validation failed"):
        validate_cyclonedx_text(json.dumps(document))


def test_duplicate_json_key_is_rejected() -> None:
    text = json.dumps(_cyclonedx_document())
    duplicated = text.replace(
        '"bomFormat": "CycloneDX"', '"bomFormat": "CycloneDX", "bomFormat": "CycloneDX"'
    )

    with pytest.raises(SbomError, match="duplicate JSON key"):
        validate_cyclonedx_text(duplicated)


def test_non_json_constant_is_rejected() -> None:
    text = json.dumps(_cyclonedx_document())
    non_json = text.replace('"version": 1', '"version": NaN')

    with pytest.raises(SbomError, match="non-JSON constant"):
        validate_cyclonedx_text(non_json)


def test_unknown_dependency_child_is_rejected() -> None:
    document = _cyclonedx_document()
    dependencies = document["dependencies"]
    assert isinstance(dependencies, list)
    dependencies = cast(list[object], dependencies)
    root_dependency = dependencies[0]
    assert isinstance(root_dependency, dict)
    root_dependency = cast(dict[str, object], root_dependency)
    root_dependency["dependsOn"] = [_versioned("missing", "1.0.0")]

    with pytest.raises(SbomError, match="unknown dependency child ref"):
        validate_cyclonedx_text(json.dumps(document))


def test_github_spdx_match_is_accepted() -> None:
    inventory = validate_cyclonedx_text(json.dumps(_cyclonedx_document()))
    github_purls = github_spdx_python_purls(
        _github_spdx(
            _versioned("pkg:pypi/pytest", "9.1.1"),
            _versioned("pkg:pypi/shittim-chest", "1.0.0"),
        )
    )

    compare_inventories(inventory, github_purls)


@pytest.mark.parametrize("difference", [None, "missing-core", "missing-records", "unexpected"])
def test_repository_comparison_requires_both_project_inventories(difference: str | None) -> None:
    core = validate_cyclonedx_text(json.dumps(_cyclonedx_document()))
    records_only = _versioned("pkg:pypi/pillow", "12.3.0")
    records = CycloneDxInventory(
        package_purls=frozenset({records_only, core.project_purl}),
        project_purl=_versioned("pkg:pypi/shittim-records", "0.1.0"),
        component_count=2,
    )
    github = set(core.package_purls | records.package_purls | {records.project_purl})
    if difference == "missing-core":
        github.difference_update(core.package_purls)
    elif difference == "missing-records":
        github.remove(records_only)
    elif difference == "unexpected":
        github.add(_versioned("pkg:pypi/unexpected", "1.0.0"))

    if difference is None:
        compare_inventories(core, frozenset(github), (records,))
    else:
        with pytest.raises(SbomError, match="inventories differ"):
            compare_inventories(core, frozenset(github), (records,))


@pytest.mark.parametrize("declared", [True, False])
def test_local_dependency_must_match_an_included_project(tmp_path: Path, declared: bool) -> None:
    inventory = validate_cyclonedx_text(json.dumps(_cyclonedx_document()))
    local_purl = _versioned("pkg:pypi/local-project", "1.0.0")
    inventory = CycloneDxInventory(
        package_purls=inventory.package_purls | {local_purl},
        project_purl=inventory.project_purl,
        component_count=2,
    )
    lock, project = _project_documents()
    packages = cast(list[dict[str, object]], lock["package"])
    packages.append(
        {"name": "local-project", "version": "1.0.0", "source": {"directory": "../local"}}
    )
    local_projects = {tmp_path / "local": local_purl} if declared else {}
    if declared:
        validate_project_inventory(
            inventory,
            lock,
            project,
            local_projects=local_projects,
            lock_directory=tmp_path / "app",
        )
    else:
        with pytest.raises(SbomError, match="unsupported non-PyPI lock source"):
            validate_project_inventory(
                inventory,
                lock,
                project,
                local_projects=local_projects,
                lock_directory=tmp_path / "app",
            )


@pytest.mark.parametrize(
    "purls, expected_message",
    [
        ((), "no PyPI package inventory"),
        (
            (
                _versioned("pkg:pypi/pytest", "9.1.1"),
                _versioned("pkg:pypi/shittim-chest", "1.0.0"),
                _versioned("pkg:pypi/unexpected", "1.0.0"),
            ),
            "unexpected in GitHub",
        ),
    ],
)
def test_github_spdx_difference_is_rejected(
    purls: tuple[str, ...],
    expected_message: str,
) -> None:
    inventory = validate_cyclonedx_text(json.dumps(_cyclonedx_document()))
    with pytest.raises(SbomError, match=expected_message):
        github_purls = github_spdx_python_purls(_github_spdx(*purls))
        compare_inventories(inventory, github_purls)


def test_wrong_spdx_version_is_rejected() -> None:
    document = deepcopy(_github_spdx(_versioned("pkg:pypi/pytest", "9.1.1")))
    sbom = document["sbom"]
    assert isinstance(sbom, dict)
    sbom = cast(dict[str, object], sbom)
    sbom["spdxVersion"] = "SPDX-2.2"

    with pytest.raises(SbomError, match=r"SPDX-2\.3"):
        github_spdx_python_purls(document)
