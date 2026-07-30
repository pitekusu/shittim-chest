from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[3] / "tools" / "install_ecr_credential_helper.sh"
BASH = Path("/usr/bin/bash")
REGISTRY = "000000000000.dkr.ecr.ap-northeast-1.amazonaws.com"


def _write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _environment(tmp_path: Path) -> dict[str, str]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    _write_stub(
        bin_directory,
        "curl",
        'while (($#)); do\n  if [[ "$1" == "--output" ]]; then shift; : > "$1"; fi\n  shift\ndone',
    )
    _write_stub(bin_directory, "sha256sum", "exit 0")
    _write_stub(bin_directory, "sudo", "exit 0")
    _write_stub(
        bin_directory,
        "docker-credential-ecr-login",
        'printf "amazon-ecr-credential-helper\\nVersion:    0.12.0\\nGit commit: test\\n"',
    )
    docker_config = tmp_path / "docker"
    docker_config.mkdir()
    (docker_config / "config.json").write_text(
        '{"auths":{"example.invalid":{}}}\n', encoding="utf-8"
    )
    return {
        **os.environ,
        "DOCKER_CONFIG": str(docker_config),
        "ECR_CREDENTIAL_HELPER_SHA256": "0" * 64,
        "ECR_CREDENTIAL_HELPER_VERSION": "0.12.0",
        "PATH": f"{bin_directory}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
    }


def test_installer_verifies_version_and_preserves_other_registry_config(tmp_path: Path) -> None:
    environment = _environment(tmp_path)

    subprocess.run(  # noqa: S603 - both executable and script are trusted fixed paths.
        [str(BASH), str(SCRIPT), REGISTRY], check=True, env=environment
    )

    config = json.loads((Path(environment["DOCKER_CONFIG"]) / "config.json").read_text())
    assert config["auths"] == {"example.invalid": {}}
    assert config["credHelpers"] == {REGISTRY: "ecr-login"}


def test_installer_rejects_version_mismatch(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment["ECR_CREDENTIAL_HELPER_VERSION"] = "9.9.9"

    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(  # noqa: S603 - both executable and script are trusted fixed paths.
            [str(BASH), str(SCRIPT), REGISTRY], check=True, env=environment
        )


@pytest.mark.parametrize(
    "registry",
    [
        "public.ecr.aws",
        "example.invalid",
        "000000000000.dkr.ecr.ap-northeast-1.amazonaws.com/extra",
    ],
)
def test_installer_rejects_non_private_ecr_registry(tmp_path: Path, registry: str) -> None:
    environment = _environment(tmp_path)

    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(  # noqa: S603 - both executable and script are trusted fixed paths.
            [str(BASH), str(SCRIPT), registry], check=True, env=environment
        )
