#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate the source SBOM and compare it with GitHub's managed SPDX export."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator

MAXIMUM_SBOM_BYTES = 10 * 1024 * 1024
PYPI_PURL_PREFIX = "pkg:pypi/"


class SbomError(RuntimeError):
    """Raised when an SBOM is malformed or inventories do not match."""


@dataclass(frozen=True, slots=True)
class CycloneDxInventory:
    """Python package inventory extracted from a validated CycloneDX document."""

    package_purls: frozenset[str]
    project_purl: str
    component_count: int


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SbomError(f"{label} must be a JSON object with string keys")
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise SbomError(f"{label} must be a JSON array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SbomError(f"{label} must be a non-empty string")
    return value


def _read_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise SbomError(f"SBOM must be a regular non-symlink file: {path}")
    if path.stat().st_size > MAXIMUM_SBOM_BYTES:
        raise SbomError(f"SBOM exceeds {MAXIMUM_SBOM_BYTES} bytes: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SbomError(f"cannot read UTF-8 SBOM {path}: {error}") from error


def _parse_json(text: str, label: str) -> dict[str, object]:
    def reject_constant(value: str) -> object:
        raise SbomError(f"{label} contains non-JSON constant: {value}")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SbomError(f"{label} contains duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value: object = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        raise SbomError(f"{label} is not valid JSON: {error}") from error
    return _object(value, label)


def _package_purl(name: str, version: str, label: str) -> str:
    normalized_name = re.sub(r"[-_.]+", "-", name).lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", normalized_name):
        raise SbomError(f"{label} name cannot be represented as a PyPI purl")
    if any(character in version for character in "/?#@"):
        raise SbomError(f"{label} version contains a purl delimiter")
    return f"{PYPI_PURL_PREFIX}{normalized_name}@{version}"


def _project_purl(component: dict[str, object]) -> str:
    name = _text(component.get("name"), "metadata.component.name")
    version = _text(component.get("version"), "metadata.component.version")
    return _package_purl(name, version, "metadata.component")


def validate_cyclonedx_text(text: str) -> CycloneDxInventory:
    """Validate strict CycloneDX 1.5 JSON plus the uv export inventory contract."""

    document = _parse_json(text, "CycloneDX document")
    schema_error = JsonStrictValidator(SchemaVersion.V1_5).validate_str(text)
    if schema_error is not None:
        raise SbomError(f"CycloneDX 1.5 schema validation failed: {schema_error}")

    if document.get("bomFormat") != "CycloneDX" or document.get("specVersion") != "1.5":
        raise SbomError("expected a CycloneDX 1.5 document")

    metadata = _object(document.get("metadata"), "metadata")
    root_component = _object(metadata.get("component"), "metadata.component")
    root_ref = _text(root_component.get("bom-ref"), "metadata.component.bom-ref")
    project_purl = _project_purl(root_component)

    components = _array(document.get("components"), "components")
    if not components:
        raise SbomError("components must contain the resolved dependency inventory")

    refs = {root_ref}
    package_purls: set[str] = set()
    for index, raw_component in enumerate(components):
        component = _object(raw_component, f"components[{index}]")
        component_ref = _text(component.get("bom-ref"), f"components[{index}].bom-ref")
        if component_ref in refs:
            raise SbomError(f"duplicate bom-ref: {component_ref}")
        refs.add(component_ref)

        purl = _text(component.get("purl"), f"components[{index}].purl")
        if not purl.startswith(PYPI_PURL_PREFIX) or "@" not in purl:
            raise SbomError(f"resolved component must use a versioned PyPI purl: {purl}")
        if purl in package_purls:
            raise SbomError(f"duplicate package purl: {purl}")
        package_purls.add(purl)

    dependency_entries = _array(document.get("dependencies"), "dependencies")
    dependency_refs: set[str] = set()
    for index, raw_dependency in enumerate(dependency_entries):
        dependency = _object(raw_dependency, f"dependencies[{index}]")
        dependency_ref = _text(dependency.get("ref"), f"dependencies[{index}].ref")
        if dependency_ref in dependency_refs:
            raise SbomError(f"duplicate dependency ref: {dependency_ref}")
        if dependency_ref not in refs:
            raise SbomError(f"unknown dependency ref: {dependency_ref}")
        dependency_refs.add(dependency_ref)
        for child in _array(dependency.get("dependsOn", []), f"dependencies[{index}].dependsOn"):
            child_ref = _text(child, f"dependencies[{index}].dependsOn[]")
            if child_ref not in refs:
                raise SbomError(f"unknown dependency child ref: {child_ref}")

    missing_dependency_entries = refs - dependency_refs
    if missing_dependency_entries:
        missing = ", ".join(sorted(missing_dependency_entries))
        raise SbomError(f"components without dependency entries: {missing}")

    return CycloneDxInventory(
        package_purls=frozenset(package_purls),
        project_purl=project_purl,
        component_count=len(components),
    )


def validate_project_inventory(
    inventory: CycloneDxInventory,
    lock_document: dict[str, object],
    project_document: dict[str, object],
) -> None:
    """Require the source SBOM to describe this project and every locked registry package."""

    project = _object(project_document.get("project"), "pyproject.project")
    project_purl = _package_purl(
        _text(project.get("name"), "pyproject.project.name"),
        _text(project.get("version"), "pyproject.project.version"),
        "pyproject.project",
    )
    if inventory.project_purl != project_purl:
        raise SbomError(
            f"CycloneDX project {inventory.project_purl} does not match pyproject {project_purl}"
        )

    locked_purls: set[str] = set()
    root_package_seen = False
    packages = _array(lock_document.get("package"), "uv.lock package")
    for index, raw_package in enumerate(packages):
        package = _object(raw_package, f"uv.lock package[{index}]")
        name = _text(package.get("name"), f"uv.lock package[{index}].name")
        version = _text(package.get("version"), f"uv.lock package[{index}].version")
        source = _object(package.get("source"), f"uv.lock package[{index}].source")
        if source.get("editable") == ".":
            package_purl = _package_purl(name, version, f"uv.lock package[{index}]")
            if package_purl != project_purl or root_package_seen:
                raise SbomError("uv.lock must contain exactly one editable project root")
            root_package_seen = True
            continue
        if source.get("registry") != "https://pypi.org/simple":
            raise SbomError(f"unsupported non-PyPI lock source for {name}=={version}: {source}")
        purl = _package_purl(name, version, f"uv.lock package[{index}]")
        if purl in locked_purls:
            raise SbomError(f"duplicate locked package purl: {purl}")
        locked_purls.add(purl)

    if not root_package_seen:
        raise SbomError("uv.lock is missing the editable project root")
    missing = locked_purls - inventory.package_purls
    unexpected = set(inventory.package_purls) - locked_purls
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing from CycloneDX: " + ", ".join(sorted(missing)))
        if unexpected:
            details.append("unexpected in CycloneDX: " + ", ".join(sorted(unexpected)))
        raise SbomError("uv.lock and CycloneDX inventories differ; " + "; ".join(details))


def github_spdx_python_purls(document: dict[str, object]) -> frozenset[str]:
    """Extract versioned PyPI purls from a GitHub Dependency Graph SPDX export."""

    sbom = _object(document.get("sbom"), "sbom")
    if sbom.get("spdxVersion") != "SPDX-2.3":
        raise SbomError("GitHub Dependency Graph export must use SPDX-2.3")
    creation_info = _object(sbom.get("creationInfo"), "sbom.creationInfo")
    _array(creation_info.get("creators"), "sbom.creationInfo.creators")

    package_purls: set[str] = set()
    for package_index, raw_package in enumerate(_array(sbom.get("packages"), "sbom.packages")):
        package = _object(raw_package, f"sbom.packages[{package_index}]")
        references = _array(
            package.get("externalRefs", []),
            f"sbom.packages[{package_index}].externalRefs",
        )
        for reference_index, raw_reference in enumerate(references):
            reference = _object(
                raw_reference,
                f"sbom.packages[{package_index}].externalRefs[{reference_index}]",
            )
            if reference.get("referenceType") != "purl":
                continue
            purl = _text(
                reference.get("referenceLocator"),
                f"sbom.packages[{package_index}].externalRefs[{reference_index}].referenceLocator",
            )
            if purl.startswith(PYPI_PURL_PREFIX):
                if "@" not in purl:
                    raise SbomError(f"GitHub SPDX contains an unversioned PyPI purl: {purl}")
                package_purls.add(purl)
    if not package_purls:
        raise SbomError("GitHub SPDX contains no PyPI package inventory")
    return frozenset(package_purls)


def compare_inventories(
    cyclonedx_inventory: CycloneDxInventory,
    github_python_purls: frozenset[str],
) -> None:
    """Require GitHub's managed Python inventory to match the tested uv inventory."""

    expected = set(cyclonedx_inventory.package_purls)
    missing = expected - github_python_purls
    allowed = expected | {cyclonedx_inventory.project_purl}
    unexpected = github_python_purls - allowed
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing from GitHub: " + ", ".join(sorted(missing)))
        if unexpected:
            details.append("unexpected in GitHub: " + ", ".join(sorted(unexpected)))
        raise SbomError("dependency inventories differ; " + "; ".join(details))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate a CycloneDX source SBOM")
    validate.add_argument("cyclonedx", type=Path)
    validate.add_argument("--lock", type=Path, default=Path("uv.lock"))
    validate.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    compare = subparsers.add_parser("compare-github", help="compare with GitHub SPDX export")
    compare.add_argument("cyclonedx", type=Path)
    compare.add_argument("github_spdx", type=Path)
    compare.add_argument("--lock", type=Path, default=Path("uv.lock"))
    compare.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        cyclonedx = validate_cyclonedx_text(_read_text(args.cyclonedx))
        try:
            lock_document = _object(tomllib.loads(_read_text(args.lock)), "uv.lock")
            project_document = _object(tomllib.loads(_read_text(args.project)), "pyproject.toml")
        except tomllib.TOMLDecodeError as error:
            raise SbomError(f"invalid project TOML: {error}") from error
        validate_project_inventory(cyclonedx, lock_document, project_document)
        if args.command == "validate":
            print(
                "CycloneDX 1.5 source SBOM is valid: "
                f"{cyclonedx.component_count} dependency components"
            )
            return 0

        github_document = _parse_json(_read_text(args.github_spdx), "GitHub SPDX document")
        github_purls = github_spdx_python_purls(github_document)
        compare_inventories(cyclonedx, github_purls)
        print(
            "GitHub SPDX inventory matches CycloneDX: "
            f"{len(github_purls)} Python packages including the project"
        )
    except SbomError as error:
        print(f"SBOM check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
