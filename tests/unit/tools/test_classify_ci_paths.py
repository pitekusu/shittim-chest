"""CI path classification regression tests."""

from __future__ import annotations

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
        "security/container-risk-acceptance.json",
        ".github/tool-versions.json",
        ".github/workflows/ci.yml",
    ),
)
def test_runtime_image_or_validation_changes_require_container_gates(path: str) -> None:
    assert classify_paths((path,))["runtime_container"] is True


@pytest.mark.parametrize(
    "path",
    (
        "apps/records-web/src/App.module.css",
        "services/records/src/shittim_records/contracts.py",
        "contracts/records/v1/openapi.json",
        ".github/workflows/records-ci.yml",
        ".github/workflows/records-release.yml",
        "tools/build_records_web_artifact.py",
        "tests/unit/tools/test_build_records_web_artifact.py",
        "infra/lib/release-identity-stack.ts",
        "infra/test/release-identity-stack.test.ts",
        "docs/24_シッテムの箱 議事録設計.md",
    ),
)
def test_records_changes_require_records_ci(path: str) -> None:
    assert classify_paths((path,))["records"] is True


def test_records_css_does_not_rebuild_the_fargate_images() -> None:
    classification = classify_paths(("apps/records-web/src/App.module.css",))

    assert classification == {"runtime_container": False, "records": True}


def test_unrelated_documentation_does_not_run_either_specialized_gate() -> None:
    assert classify_paths(("docs/11_Discord詳細設計.md",)) == {
        "runtime_container": False,
        "records": False,
    }


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
