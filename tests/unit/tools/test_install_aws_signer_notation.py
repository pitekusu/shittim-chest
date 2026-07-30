from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[3] / "tools" / "install_aws_signer_notation.sh"
BASH = Path("/usr/bin/bash")
FINGERPRINT = "E84AF8A2A9B52F1F4435AE71A3B52DA65461CF90"


def _write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _environment(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(
        bin_dir,
        "curl",
        'while (($#)); do\n  if [[ "$1" == "--output" ]]; then shift; : > "$1"; fi\n  shift\ndone',
    )
    _write_stub(bin_dir, "sha256sum", "exit 0")
    _write_stub(
        bin_dir,
        "gpg",
        (
            'if [[ " $* " == *" --with-colons "* ]]; then\n'
            f'  printf "fpr:::::::::{FINGERPRINT}:\\n"\n'
            "fi"
        ),
    )
    _write_stub(bin_dir, "sudo", "exit 0")
    _write_stub(bin_dir, "dpkg-query", 'printf "2.2.0-1"')
    _write_stub(
        bin_dir,
        "notation",
        'case "${1:-}" in\n'
        '  version) printf "Notation Version: 1.3.2\\n" ;;\n'
        '  plugin) printf "com.amazonaws.signer.notation.plugin  1.0.2292\\n" ;;\n'
        "  *) exit 2 ;;\n"
        "esac",
    )
    return {
        **os.environ,
        "AWS_SIGNER_NOTATION_ARCHIVE_SHA256": "0" * 64,
        "AWS_SIGNER_NOTATION_FINGERPRINT": FINGERPRINT,
        "AWS_SIGNER_NOTATION_INSTALLER_VERSION": "2.2.0-1",
        "AWS_SIGNER_NOTATION_KEY_SHA256": "1" * 64,
        "AWS_SIGNER_NOTATION_PLUGIN_VERSION": "1.0.2292",
        "AWS_SIGNER_NOTATION_SIGNATURE_SHA256": "2" * 64,
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "123",
        "NOTATION_CLI_VERSION": "1.3.2",
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
    }


def test_installer_verifies_each_component_version(tmp_path: Path) -> None:
    subprocess.run(  # noqa: S603 - both executable and script are trusted fixed paths.
        [str(BASH), str(SCRIPT)], check=True, env=_environment(tmp_path)
    )


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("AWS_SIGNER_NOTATION_INSTALLER_VERSION", "9.9.9-1"),
        ("NOTATION_CLI_VERSION", "9.9.9"),
        ("AWS_SIGNER_NOTATION_PLUGIN_VERSION", "9.9.9"),
    ],
)
def test_installer_rejects_component_version_mismatch(
    tmp_path: Path, variable: str, value: str
) -> None:
    environment = _environment(tmp_path)
    environment[variable] = value

    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(  # noqa: S603 - both executable and script are trusted fixed paths.
            [str(BASH), str(SCRIPT)], check=True, env=environment
        )
