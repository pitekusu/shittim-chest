"""CI path classification regression tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import tools.classify_ci_paths as classifier
from tools.classify_ci_paths import changed_paths, classify_paths


@pytest.mark.parametrize(
    "path",
    (
        "Dockerfile",
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        "src/shittim_chest/application/service.py",
        "tools/check_container_policy.py",
        "tools/containers/Dockerfile",
        "tools/container_images.py",
        "security/container-risk-acceptance.json",
        "security/container-risk-acceptance.schema.json",
        ".github/tool-versions.json",
        ".github/workflows/ci.yml",
    ),
)
def test_runtime_image_or_validation_changes_require_container_gates(path: str) -> None:
    assert classify_paths((path,))["runtime_container"] is True


@pytest.mark.parametrize(
    "path",
    (
        "apps/records-web/src/styles/home.module.css",
        "services/records/src/shittim_records/contracts.py",
        "contracts/records/v1/openapi.json",
        ".github/workflows/records-ci.yml",
        ".github/workflows/records-release.yml",
        "tools/build_records_web_artifact.py",
        "tools/containers/Dockerfile",
        "tools/container_images.py",
        "tools/run_dynamodb_local.py",
        "tests/unit/tools/test_build_records_web_artifact.py",
        "infra/lib/release-identity-stack.ts",
        "infra/test/release-identity-stack.test.ts",
    ),
)
def test_records_changes_require_records_ci(path: str) -> None:
    assert classify_paths((path,))["records"] is True


@pytest.mark.parametrize(
    ("paths", "expected"),
    (
        (("apps/records-web/src/styles/home.module.css",), {"records_web"}),
        (
            ("services/records/src/shittim_records/rankings.py",),
            {"records_python", "records_contract"},
        ),
        (("services/records/uv.lock",), {"records_python", "records_contract"}),
        (
            ("contracts/records/v1/openapi.json",),
            {"records_python", "records_contract", "records_web"},
        ),
        (("infra/lib/records-application-stack.ts",), {"infra", "records_infra"}),
        (("package-lock.json",), {"infra", "records_infra"}),
        (("docs/11_Discord詳細設計.md",), set()),
        (("docs/24_シッテムの箱 議事録設計.md",), set()),
        (
            ("apps/records-android/app/build.gradle.kts", "docs/29_Androidアプリ設計.md"),
            {"android"},
        ),
        (
            ("apps/records-android/app/build.gradle.kts", ".github/dependabot.yml"),
            {"android", "core_tests"},
        ),
        (
            ("apps/records-android/app/build.gradle.kts", "services/records/main.py"),
            {"android", "records_python", "records_contract"},
        ),
        (("tests/unit/application/test_service.py",), {"core_tests"}),
        (
            ("apps/records-web/src/assets/fonts/font.woff2",),
            {"records_python", "records_contract", "records_web"},
        ),
    ),
)
def test_scopes_follow_changed_inputs(paths: tuple[str, ...], expected: set[str]) -> None:
    result = classify_paths(paths)
    assert {name for name in classifier.SCOPES if result[name]} == expected
    assert result["core"] is bool(expected & {"core_tests", "core_package", "infra"})
    assert result["records"] is any(name.startswith("records_") for name in expected)


@pytest.mark.parametrize(
    "path",
    ("src/shittim_chest/adapters/dynamodb/codec.py", "pyproject.toml", "uv.lock", "README.md"),
)
def test_core_package_inputs_also_verify_the_records_consumer(path: str) -> None:
    result = classify_paths((path,))
    for scope in (
        "core_tests",
        "core_package",
        "runtime_container",
        "records_python",
        "records_contract",
    ):
        assert result[scope] is True
    assert result["records_web"] is False


@pytest.mark.parametrize(
    "paths", ((), ("unknown.txt",), (".github/workflows/ci.yml",), ("tools/check_ci_scope.py",))
)
def test_empty_unknown_and_pipeline_changes_require_all_scopes(paths: tuple[str, ...]) -> None:
    assert all(classify_paths(paths).values())


def test_mixed_change_uses_the_union_without_losing_shared_dependencies() -> None:
    paths = (
        "services/records/uv.lock",
        "infra/lib/runtime-stack.ts",
        "apps/records-android/app/build.gradle.kts",
    )
    combined = classify_paths(paths)
    individual = [classify_paths((path,)) for path in paths]
    assert all(combined[scope] == any(result[scope] for result in individual) for scope in combined)


def test_repository_escape_is_rejected() -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        classify_paths(("../private",))


def test_changed_paths_uses_nul_delimiters_for_japanese_and_deleted_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_: object) -> CompletedProcess[str]:
        assert "-z" in command
        assert "--no-renames" in command
        assert "--diff-filter=ACMRD" in command
        return CompletedProcess(
            command,
            0,
            stdout="docs/24_シッテムの箱 議事録設計.md\0apps/records-web/removed.css\0",
            stderr="",
        )

    monkeypatch.setattr(classifier.shutil, "which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(classifier.subprocess, "run", fake_run)

    assert changed_paths("a" * 40, "b" * 40) == (
        "docs/24_シッテムの箱 議事録設計.md",
        "apps/records-web/removed.css",
    )


def test_manual_full_verification_ignores_a_narrow_diff(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(classifier, "changed_paths", lambda *_: ("docs/README.md",))
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "classifier",
            "--base",
            "a" * 40,
            "--head",
            "b" * 40,
            "--all",
            "--github-output",
            str(output),
        ],
    )
    classifier.main()
    assert all(json.loads(capsys.readouterr().out).values())
    assert all(line.endswith("=true") for line in output.read_text().splitlines())


def test_rename_between_scopes_requires_both_deleted_and_added_inputs() -> None:
    # git diff --no-renames reports a rename as a deletion and an addition.
    result = classify_paths(("services/records/old.py", "apps/records-web/new.ts"))
    assert {name for name in classifier.SCOPES if result[name]} == {
        "records_python",
        "records_contract",
        "records_web",
    }


def test_records_roots_are_not_reincluded_in_the_runtime_docker_context() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    negated_rules = {
        rule
        for rule in (repository_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if rule.startswith("!")
    }

    assert negated_rules == {
        "!LICENSE",
        "!README.md",
        "!pyproject.toml",
        "!uv.lock",
        "!tools/",
        "!tools/canonicalize_wheel_records.py",
        "!tools/transfer_tree_deterministically.py",
        "!src/",
        "!src/**",
        "!tests/",
        "!tests/__init__.py",
        "!tests/fixtures/",
        "!tests/fixtures/container_process.py",
    }
