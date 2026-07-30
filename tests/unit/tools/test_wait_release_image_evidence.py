from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[3] / "tools" / "wait_release_image_evidence.sh"
BASH = Path("/usr/bin/bash")
REPOSITORY = "shittim-chest"
DIGEST = f"sha256:{'a' * 64}"
PROFILE = "arn:aws:signer:ap-northeast-1:000000000000:/signing-profiles/shittim_chest_ecr"


def _write_stub(directory: Path) -> None:
    stub = directory / "aws"
    stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

operation="${1:-}/${2:-}"
shift 2
repository=""
image_id=""
while (($#)); do
  case "$1" in
    --repository-name) repository="$2"; shift 2 ;;
    --image-ids|--image-id) image_id="$2"; shift 2 ;;
    --filter-criteria|--output) shift 2 ;;
    *) exit 91 ;;
  esac
done
printf '%s\n' "${operation}" >> "${FAKE_AWS_LOG}"
counter_name="${operation//\\//-}"
counter_file="${FAKE_AWS_STATE}/${counter_name}.counter"
counter=0
if [[ -f "${counter_file}" ]]; then
  read -r counter < "${counter_file}"
fi
counter=$((counter + 1))
printf '%s\n' "${counter}" > "${counter_file}"

case "${operation}" in
  ecr/describe-images)
    test "${repository}" = "${EXPECTED_REPOSITORY}"
    test "${image_id}" = "imageDigest=${EXPECTED_DIGEST}"
    printf '{"imageDetails":[{"imageDigest":"%s","imageSizeInBytes":1}]}\n' \
      "${EXPECTED_DIGEST}"
    ;;
  ecr/describe-image-signing-status)
    case "${FAKE_AWS_SCENARIO}" in
      access-denied)
        echo "An error occurred (AccessDeniedException) with private diagnostic context" >&2
        exit 254
        ;;
      signer-failed)
        status="FAILED"
        ;;
      signer-mismatch)
        EXPECTED_PROFILE="arn:aws:signer:ap-northeast-1:000000000000:/signing-profiles/other_profile"
        status="COMPLETE"
        ;;
      timeout)
        status="IN_PROGRESS"
        ;;
      retryable)
        if [[ "${counter}" -eq 1 ]]; then
          echo "An error occurred (ThrottlingException) with private diagnostic context" >&2
          exit 254
        fi
        status="COMPLETE"
        ;;
      pending-ready)
        if [[ "${counter}" -eq 1 ]]; then status="IN_PROGRESS"; else status="COMPLETE"; fi
        ;;
      *)
        status="COMPLETE"
        ;;
    esac
    printf '{"signingStatuses":[{"signingProfileArn":"%s","status":"%s"}]}\n' \
      "${EXPECTED_PROFILE}" "${status}"
    ;;
  ecr/describe-image-scan-findings)
    case "${FAKE_AWS_SCENARIO}" in
      scan-terminal) status="UNSUPPORTED_IMAGE" ;;
      timeout) status="PENDING" ;;
      pending-ready)
        if [[ "${counter}" -eq 1 ]]; then
          echo "An error occurred (ScanNotFoundException): scan is not ready" >&2
          exit 254
        elif [[ "${counter}" -eq 2 ]]; then
          status="PENDING"
        else
          status="COMPLETE"
        fi
        ;;
      *) status="COMPLETE" ;;
    esac
    printf '{"imageScanStatus":{"status":"%s"},"imageScanFindings":{}}\n' "${status}"
    ;;
  inspector2/list-coverage)
    case "${FAKE_AWS_SCENARIO}" in
      inspector-terminal)
        printf '{"coveredResources":[{"resourceType":"AWS_ECR_CONTAINER_IMAGE",'
        printf '"resourceId":"repo@%s","scanType":"PACKAGE",' "${EXPECTED_DIGEST}"
        printf '"scanStatus":{"statusCode":"INACTIVE","reason":"ACCESS_DENIED"}}]}\n'
        ;;
      inspector-duplicate)
        printf '{"coveredResources":['
        for separator in '' ','; do
          printf '%s{"resourceType":"AWS_ECR_CONTAINER_IMAGE",' "${separator}"
          printf '"resourceId":"repo@%s","scanType":"PACKAGE",' "${EXPECTED_DIGEST}"
          printf '"scanStatus":{"statusCode":"ACTIVE","reason":"SUCCESSFUL"},'
          printf '"lastScannedAt":"2026-07-31T00:00:00Z"}'
        done
        printf ']}\n'
        ;;
      timeout)
        printf '{"coveredResources":[]}\n'
        ;;
      pending-ready)
        if [[ "${counter}" -eq 1 ]]; then
          printf '{"coveredResources":[]}\n'
        elif [[ "${counter}" -eq 2 ]]; then
          printf '{"coveredResources":[{"resourceType":"AWS_ECR_CONTAINER_IMAGE",'
          printf '"resourceId":"repo@%s","scanType":"PACKAGE",' "${EXPECTED_DIGEST}"
          printf '"scanStatus":{"statusCode":"PENDING","reason":"SCAN_IN_PROGRESS"}}]}\n'
        else
          printf '{"coveredResources":[{"resourceType":"AWS_ECR_CONTAINER_IMAGE",'
          printf '"resourceId":"repo@%s","scanType":"PACKAGE",' "${EXPECTED_DIGEST}"
          printf '"scanStatus":{"statusCode":"ACTIVE","reason":"SUCCESSFUL"},'
          printf '"lastScannedAt":"2026-07-31T00:00:00Z"}]}\n'
        fi
        ;;
      *)
        printf '{"coveredResources":[{"resourceType":"AWS_ECR_CONTAINER_IMAGE",'
        printf '"resourceId":"repo@%s","scanType":"PACKAGE",' "${EXPECTED_DIGEST}"
        printf '"scanStatus":{"statusCode":"ACTIVE","reason":"SUCCESSFUL"},'
        printf '"lastScannedAt":"2026-07-31T00:00:00Z"}]}\n'
        ;;
    esac
    ;;
  *) exit 92 ;;
esac
""",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _environment(
    tmp_path: Path,
    scenario: str,
    *,
    max_polls: str = "5",
) -> tuple[dict[str, str], Path]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    _write_stub(bin_directory)
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    log = tmp_path / "aws.log"
    log.touch()
    output_directory = tmp_path / "evidence"
    return (
        {
            **os.environ,
            "EVIDENCE_MAX_POLLS": max_polls,
            "EVIDENCE_POLL_SECONDS": "0",
            "EXPECTED_DIGEST": DIGEST,
            "EXPECTED_PROFILE": PROFILE,
            "EXPECTED_REPOSITORY": REPOSITORY,
            "FAKE_AWS_LOG": str(log),
            "FAKE_AWS_SCENARIO": scenario,
            "FAKE_AWS_STATE": str(state_directory),
            "PATH": f"{bin_directory}:{os.environ['PATH']}",
        },
        output_directory,
    )


def _run(
    environment: dict[str, str],
    output_directory: Path,
    *,
    mode: str = "normal",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - executable and script are fixed trusted paths.
        [
            str(BASH),
            str(SCRIPT),
            "--repository",
            REPOSITORY,
            "--digest",
            DIGEST,
            "--signing-profile-arn",
            PROFILE,
            "--output-dir",
            str(output_directory),
            "--mode",
            mode,
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def _operations(environment: dict[str, str]) -> list[str]:
    return Path(environment["FAKE_AWS_LOG"]).read_text(encoding="utf-8").splitlines()


def test_pending_evidence_becomes_ready_without_logging_content(tmp_path: Path) -> None:
    environment, output_directory = _environment(tmp_path, "pending-ready")

    result = _run(environment, output_directory, mode="break-glass")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert _operations(environment).count("ecr/describe-images") == 1
    assert _operations(environment).count("ecr/describe-image-signing-status") == 2
    assert _operations(environment).count("ecr/describe-image-scan-findings") == 3
    assert _operations(environment).count("inspector2/list-coverage") == 3
    for kind in ("image", "signing", "scan", "coverage"):
        evidence = output_directory / f"break-glass.{kind}.json"
        assert isinstance(json.loads(evidence.read_text(encoding="utf-8")), dict)


def test_access_denied_fails_immediately_without_provider_diagnostics(tmp_path: Path) -> None:
    environment, output_directory = _environment(tmp_path, "access-denied")

    result = _run(environment, output_directory)

    assert result.returncode != 0
    assert _operations(environment) == [
        "ecr/describe-images",
        "ecr/describe-image-signing-status",
    ]
    assert "AccessDenied" not in result.stderr
    assert "private diagnostic context" not in result.stderr
    assert "signing query failed" in result.stderr


def test_retryable_aws_error_is_retried(tmp_path: Path) -> None:
    environment, output_directory = _environment(tmp_path, "retryable")

    result = _run(environment, output_directory)

    assert result.returncode == 0, result.stderr
    assert _operations(environment).count("ecr/describe-image-signing-status") == 2


def test_signer_failed_status_fails_immediately(tmp_path: Path) -> None:
    environment, output_directory = _environment(tmp_path, "signer-failed")

    result = _run(environment, output_directory)

    assert result.returncode != 0
    assert "image signing failed" in result.stderr
    assert _operations(environment) == [
        "ecr/describe-images",
        "ecr/describe-image-signing-status",
    ]


def test_signer_profile_mismatch_fails_immediately(tmp_path: Path) -> None:
    environment, output_directory = _environment(tmp_path, "signer-mismatch")

    result = _run(environment, output_directory)

    assert result.returncode != 0
    assert "invalid signing evidence" in result.stderr
    assert _operations(environment) == [
        "ecr/describe-images",
        "ecr/describe-image-signing-status",
    ]


def test_terminal_scan_status_fails_immediately(tmp_path: Path) -> None:
    environment, output_directory = _environment(tmp_path, "scan-terminal")

    result = _run(environment, output_directory)

    assert result.returncode != 0
    assert "terminal state" in result.stderr
    assert _operations(environment) == [
        "ecr/describe-images",
        "ecr/describe-image-signing-status",
        "ecr/describe-image-scan-findings",
    ]


@pytest.mark.parametrize("scenario", ["inspector-terminal", "inspector-duplicate"])
def test_terminal_or_ambiguous_inspector_evidence_fails_immediately(
    tmp_path: Path,
    scenario: str,
) -> None:
    environment, output_directory = _environment(tmp_path, scenario)

    result = _run(environment, output_directory)

    assert result.returncode != 0
    assert "invalid Inspector evidence" in result.stderr
    assert _operations(environment) == [
        "ecr/describe-images",
        "ecr/describe-image-signing-status",
        "ecr/describe-image-scan-findings",
        "inspector2/list-coverage",
    ]


def test_pending_evidence_times_out_at_the_configured_bound(tmp_path: Path) -> None:
    environment, output_directory = _environment(tmp_path, "timeout", max_polls="2")

    result = _run(environment, output_directory)

    assert result.returncode != 0
    assert "timed out" in result.stderr
    assert _operations(environment).count("ecr/describe-images") == 1
    assert _operations(environment).count("ecr/describe-image-signing-status") == 2
    assert _operations(environment).count("ecr/describe-image-scan-findings") == 2
    assert _operations(environment).count("inspector2/list-coverage") == 2


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("EVIDENCE_MAX_POLLS", "0"),
        ("EVIDENCE_MAX_POLLS", "01"),
        ("EVIDENCE_POLL_SECONDS", "-1"),
    ],
)
def test_rejects_invalid_polling_configuration(
    tmp_path: Path,
    variable: str,
    value: str,
) -> None:
    environment, output_directory = _environment(tmp_path, "pending-ready")
    environment[variable] = value

    result = _run(environment, output_directory)

    assert result.returncode != 0
    assert _operations(environment) == []
