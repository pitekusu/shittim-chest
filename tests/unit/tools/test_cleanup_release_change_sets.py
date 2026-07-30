from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[3] / "tools" / "cleanup_release_change_sets.sh"
BASH = Path("/usr/bin/bash")
CHANGE_SET_NAME = f"release-{'a' * 40}-123-1"
STALE_CHANGE_SET_NAME = f"release-{'b' * 40}-122-1"
SECOND_STALE_CHANGE_SET_NAME = f"release-{'c' * 40}-121-2"
STACKS = (
    ("ShittimChest-Prod-Stateful", "ap-northeast-1"),
    ("ShittimChest-Prod-Runtime", "ap-northeast-1"),
    ("ShittimChest-Prod-Operations", "ap-northeast-1"),
    ("ShittimChest-Prod-CostGovernance", "us-east-1"),
)


def _write_stub(directory: Path) -> None:
    stub = directory / "aws"
    stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

operation="${1:-}/${2:-}"
shift 2
stack=""
region=""
locator=""
while (($#)); do
  case "$1" in
    --stack-name) stack="$2"; shift 2 ;;
    --region) region="$2"; shift 2 ;;
    --change-set-name) locator="$2"; shift 2 ;;
    --no-paginate) shift ;;
    --query|--output) shift 2 ;;
    *) exit 91 ;;
  esac
done
test -n "${stack}"
test -n "${region}"
printf '%s\t%s\t%s\t%s\n' "${operation}" "${stack}" "${region}" "${locator}" \
  >> "${FAKE_AWS_LOG}"

not_found() {
  echo "An error occurred (ChangeSetNotFound) when calling DescribeChangeSet:" \
    "change set does not exist" >&2
  exit 254
}

print_summary() {
  local name="$1" execution_status="$2" status="$3"
  jq --null-input --arg region "${region}" --arg stack "${stack}" \
    --arg name "${name}" --arg execution "${execution_status}" --arg status "${status}" '
      {Summaries:[{
        StackId:("arn:aws:cloudformation:"+$region+":000000000000:stack/"+$stack+"/id"),
        StackName:$stack,
        ChangeSetId:("arn:aws:cloudformation:"+$region+
          ":000000000000:changeSet/"+$name+"/id"),
        ChangeSetName:$name,
        ExecutionStatus:$execution,
        Status:$status,
        IncludeNestedStacks:false
      }]}'
}

print_two_summaries() {
  print_summary "${STALE_CHANGE_SET_NAME}" AVAILABLE CREATE_COMPLETE | \
    jq --arg second "${SECOND_STALE_CHANGE_SET_NAME}" '
      .Summaries += [(.Summaries[0] |
        .ChangeSetName=$second |
        .ChangeSetId=(.ChangeSetId | sub("changeSet/[^/]+/"; "changeSet/"+$second+"/"))
      )]'
}

print_nested_summary() {
  print_summary "${STALE_CHANGE_SET_NAME}" AVAILABLE CREATE_COMPLETE | \
    jq --arg region "${region}" '
      .Summaries[0].IncludeNestedStacks=true |
      .Summaries[0].ParentChangeSetId=("arn:aws:cloudformation:"+$region+
        ":000000000000:changeSet/parent/id") |
      .Summaries[0].RootChangeSetId=("arn:aws:cloudformation:"+$region+
        ":000000000000:changeSet/root/id")'
}

if [[ "${operation}" == "cloudformation/list-change-sets" ]]; then
  list_counter_file="${FAKE_AWS_STATE}/${stack}.list-counter"
  list_counter=0
  if [[ -f "${list_counter_file}" ]]; then
    read -r list_counter < "${list_counter_file}"
  fi
  list_counter=$((list_counter + 1))
  printf '%s\n' "${list_counter}" > "${list_counter_file}"
  case "${FAKE_AWS_SCENARIO}" in
    stale)
      if [[ -f "${FAKE_AWS_STATE}/${stack}.${STALE_CHANGE_SET_NAME}.deleted" ]]; then
        printf '{"Summaries":[]}\n'
      else
        print_summary "${STALE_CHANGE_SET_NAME}" AVAILABLE CREATE_COMPLETE
      fi
      ;;
    stale-two-pages)
      if [[ -f "${FAKE_AWS_STATE}/${stack}.${STALE_CHANGE_SET_NAME}.deleted" &&
        -f "${FAKE_AWS_STATE}/${stack}.${SECOND_STALE_CHANGE_SET_NAME}.deleted" ]]
      then
        printf '{"Summaries":[]}\n'
      else
        print_two_summaries
      fi
      ;;
    stale-current)
      print_summary "${CURRENT_CHANGE_SET_NAME}" AVAILABLE CREATE_COMPLETE
      ;;
    stale-invalid)
      print_summary release-invalid AVAILABLE CREATE_COMPLETE
      ;;
    stale-leading-zero)
      print_summary "${STALE_CHANGE_SET_NAME/-122-1/-0122-01}" AVAILABLE CREATE_COMPLETE
      ;;
    stale-executing)
      print_summary "${STALE_CHANGE_SET_NAME}" EXECUTE_IN_PROGRESS CREATE_COMPLETE
      ;;
    stale-executed)
      print_summary "${STALE_CHANGE_SET_NAME}" EXECUTE_COMPLETE CREATE_COMPLETE
      ;;
    stale-execute-failed)
      print_summary "${STALE_CHANGE_SET_NAME}" EXECUTE_FAILED CREATE_COMPLETE
      ;;
    stale-delete-failed)
      print_summary "${STALE_CHANGE_SET_NAME}" UNAVAILABLE DELETE_FAILED
      ;;
    stale-race-execute)
      print_summary "${STALE_CHANGE_SET_NAME}" AVAILABLE CREATE_COMPLETE
      ;;
    stale-disappeared)
      if [[ "${list_counter}" -eq 1 ]]; then
        print_summary "${STALE_CHANGE_SET_NAME}" AVAILABLE CREATE_COMPLETE
      else
        printf '{"Summaries":[]}\n'
      fi
      ;;
    stale-remains)
      print_summary "${STALE_CHANGE_SET_NAME}" AVAILABLE CREATE_COMPLETE
      ;;
    stale-truncated)
      print_summary "${STALE_CHANGE_SET_NAME}" AVAILABLE CREATE_COMPLETE | \
        jq '.NextToken="still-more"'
      ;;
    stale-nested)
      print_nested_summary
      ;;
    stale-nonrelease)
      print_summary operator-plan AVAILABLE CREATE_COMPLETE
      ;;
    stale-missing-stack)
      echo "An error occurred (ValidationError): stack does not exist" >&2
      exit 254
      ;;
    stale-list-access-denied)
      echo "An error occurred (AccessDenied) with private diagnostic context" >&2
      exit 254
      ;;
    *) exit 93 ;;
  esac
  exit 0
fi

test -n "${locator}"

if [[ "${operation}" == "cloudformation/delete-change-set" ]]; then
  if [[ "${FAKE_AWS_SCENARIO}" == "delete-access-denied" ]]; then
    echo "An error occurred (AccessDenied) with private diagnostic context" >&2
    exit 254
  fi
  locator_name="${locator#*changeSet/}"
  locator_name="${locator_name%%/*}"
  : > "${FAKE_AWS_STATE}/${stack}.${locator_name}.deleted"
  exit 0
fi
test "${operation}" = "cloudformation/describe-change-set"

counter_file="${FAKE_AWS_STATE}/${stack}.counter"
counter=0
if [[ -f "${counter_file}" ]]; then
  read -r counter < "${counter_file}"
fi
counter=$((counter + 1))
printf '%s\n' "${counter}" > "${counter_file}"

case "${FAKE_AWS_SCENARIO}" in
  stale|stale-two-pages)
    locator_name="${locator#*changeSet/}"
    locator_name="${locator_name%%/*}"
    if [[ -f "${FAKE_AWS_STATE}/${stack}.${locator_name}.deleted" ]]; then
      not_found
    fi
    printf 'CREATE_COMPLETE\tAVAILABLE\n'
    ;;
  stale-delete-failed)
    printf 'DELETE_FAILED\tUNAVAILABLE\n'
    ;;
  stale-race-execute)
    printf 'CREATE_COMPLETE\tEXECUTE_IN_PROGRESS\n'
    ;;
  stale-disappeared|stale-remains)
    not_found
    ;;
  transition)
    case "${counter}" in
      1) printf 'CREATE_PENDING\tUNAVAILABLE\n' ;;
      2) printf 'CREATE_IN_PROGRESS\tUNAVAILABLE\n' ;;
      3) printf 'CREATE_COMPLETE\tAVAILABLE\n' ;;
      4) printf 'DELETE_IN_PROGRESS\tUNAVAILABLE\n' ;;
      *) not_found ;;
    esac
    ;;
  failed)
    case "${counter}" in
      1) printf 'FAILED\tUNAVAILABLE\n' ;;
      2) printf 'DELETE_PENDING\tUNAVAILABLE\n' ;;
      *) not_found ;;
    esac
    ;;
  missing)
    not_found
    ;;
  eventually-visible)
    case "${counter}" in
      1|2) not_found ;;
      3) printf 'CREATE_COMPLETE\tAVAILABLE\n' ;;
      *) not_found ;;
    esac
    ;;
  executed)
    printf 'CREATE_COMPLETE\tEXECUTE_COMPLETE\n'
    ;;
  partial-executed)
    if [[ "${stack}" == "ShittimChest-Prod-Stateful" ]]; then
      printf 'CREATE_COMPLETE\tEXECUTE_COMPLETE\n'
    else
      locator_name="${locator#*changeSet/}"
      locator_name="${locator_name%%/*}"
      if [[ -f "${FAKE_AWS_STATE}/${stack}.${locator_name}.deleted" ]]; then
        not_found
      fi
      printf 'CREATE_COMPLETE\tAVAILABLE\n'
    fi
    ;;
  partial-unsafe-manifest-*)
    if [[ "${stack}" == "ShittimChest-Prod-Stateful" ]]; then
      printf 'CREATE_COMPLETE\tEXECUTE_COMPLETE\n'
    elif [[ "${stack}" == "ShittimChest-Prod-Runtime" ]]; then
      if [[ "${FAKE_AWS_SCENARIO}" == "partial-unsafe-manifest-failed" ]]; then
        printf 'CREATE_COMPLETE\tEXECUTE_FAILED\n'
      else
        printf 'CREATE_COMPLETE\tEXECUTE_IN_PROGRESS\n'
      fi
    else
      locator_name="${locator#*changeSet/}"
      locator_name="${locator_name%%/*}"
      if [[ -f "${FAKE_AWS_STATE}/${stack}.${locator_name}.deleted" ]]; then
        not_found
      fi
      printf 'CREATE_COMPLETE\tAVAILABLE\n'
    fi
    ;;
  execute-failed)
    printf 'CREATE_COMPLETE\tEXECUTE_FAILED\n'
    ;;
  execute-in-progress)
    printf 'CREATE_COMPLETE\tEXECUTE_IN_PROGRESS\n'
    ;;
  obsolete)
    printf 'CREATE_COMPLETE\tOBSOLETE\n'
    ;;
  access-denied)
    echo "An error occurred (AccessDenied) with private diagnostic context" >&2
    exit 254
    ;;
  delete-access-denied)
    printf 'CREATE_COMPLETE\tAVAILABLE\n'
    ;;
  in-progress)
    printf 'CREATE_IN_PROGRESS\tUNAVAILABLE\n'
    ;;
  misleading-not-found)
    echo "An error occurred (ValidationError): change set does not exist" >&2
    exit 254
    ;;
  *)
    exit 92
    ;;
esac
""",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _environment(
    tmp_path: Path,
    scenario: str,
    *,
    max_polls: str = "8",
) -> dict[str, str]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    _write_stub(bin_directory)
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    log = tmp_path / "aws.log"
    log.touch()
    return {
        **os.environ,
        "CLEANUP_MAX_POLLS": max_polls,
        "CLEANUP_POLL_SECONDS": "0",
        "FAKE_AWS_LOG": str(log),
        "FAKE_AWS_SCENARIO": scenario,
        "FAKE_AWS_STATE": str(state_directory),
        "CURRENT_CHANGE_SET_NAME": CHANGE_SET_NAME,
        "PATH": f"{bin_directory}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "SECOND_STALE_CHANGE_SET_NAME": SECOND_STALE_CHANGE_SET_NAME,
        "STALE_CHANGE_SET_NAME": STALE_CHANGE_SET_NAME,
    }


def _manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "change_sets": {
                    stack: (
                        f"arn:aws:cloudformation:{region}:000000000000:"
                        f"changeSet/{CHANGE_SET_NAME}/00000000-0000-7000-8000-000000000000"
                    )
                    for stack, region in STACKS
                }
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _run(
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - executable and script are fixed trusted paths.
        [str(BASH), str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def _operations(environment: dict[str, str]) -> list[str]:
    log = Path(environment["FAKE_AWS_LOG"])
    return [line.split("\t", 1)[0] for line in log.read_text(encoding="utf-8").splitlines()]


def test_waits_for_creation_deletes_available_sets_and_confirms_deletion(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "transition")

    result = _run(environment, "--change-set-name", CHANGE_SET_NAME)

    assert result.returncode == 0, result.stderr
    assert _operations(environment).count("cloudformation/delete-change-set") == len(STACKS)
    assert _operations(environment).count("cloudformation/describe-change-set") == 5 * len(STACKS)


def test_deletes_failed_unexecuted_sets_and_waits_for_not_found(tmp_path: Path) -> None:
    environment = _environment(tmp_path, "failed")

    result = _run(environment, "--change-set-name", CHANGE_SET_NAME)

    assert result.returncode == 0, result.stderr
    assert _operations(environment).count("cloudformation/delete-change-set") == len(STACKS)
    assert _operations(environment).count("cloudformation/describe-change-set") == 3 * len(STACKS)


def test_partial_name_requires_three_consecutive_missing_observations(tmp_path: Path) -> None:
    environment = _environment(tmp_path, "missing")

    result = _run(environment, "--change-set-name", CHANGE_SET_NAME)

    assert result.returncode == 0, result.stderr
    assert _operations(environment) == ["cloudformation/describe-change-set"] * (3 * len(STACKS))


def test_partial_name_does_not_accept_two_eventual_consistency_misses(tmp_path: Path) -> None:
    environment = _environment(tmp_path, "eventually-visible")

    result = _run(environment, "--change-set-name", CHANGE_SET_NAME)

    assert result.returncode == 0, result.stderr
    assert _operations(environment).count("cloudformation/delete-change-set") == len(STACKS)
    assert _operations(environment).count("cloudformation/describe-change-set") == 4 * len(STACKS)


@pytest.mark.parametrize("scenario", ["executed", "execute-failed", "obsolete"])
def test_manifest_safely_skips_consumed_or_superseded_sets(
    tmp_path: Path,
    scenario: str,
) -> None:
    environment = _environment(tmp_path, scenario)

    result = _run(environment, "--manifest", str(_manifest(tmp_path)))

    assert result.returncode == 0, result.stderr
    assert _operations(environment) == ["cloudformation/describe-change-set"] * len(STACKS)


def test_attempt_fallback_skips_completed_stack_and_cleans_later_stacks(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "partial-executed")

    result = _run(environment, "--attempt-name", CHANGE_SET_NAME)

    assert result.returncode == 0, result.stderr
    assert _operations(environment).count("cloudformation/delete-change-set") == len(STACKS) - 1
    assert _operations(environment).count("cloudformation/describe-change-set") == (
        1 + 2 * (len(STACKS) - 1)
    )


def test_manifest_skips_failed_execution_and_cleans_later_stacks(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "partial-unsafe-manifest-failed")

    result = _run(environment, "--manifest", str(_manifest(tmp_path)))

    assert result.returncode == 0, result.stderr
    assert _operations(environment).count("cloudformation/delete-change-set") == len(STACKS) - 2
    assert _operations(environment).count("cloudformation/describe-change-set") == (
        2 + 2 * (len(STACKS) - 2)
    )


def test_manifest_reports_active_execution_after_cleaning_later_stacks(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "partial-unsafe-manifest-in-progress")

    result = _run(environment, "--manifest", str(_manifest(tmp_path)))

    assert result.returncode != 0
    assert "one or more manifest change sets are still executing" in result.stderr
    assert _operations(environment).count("cloudformation/delete-change-set") == len(STACKS) - 2
    assert _operations(environment).count("cloudformation/describe-change-set") == (
        2 + 2 * (len(STACKS) - 2)
    )


def test_attempt_fallback_refuses_active_execution(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "execute-in-progress")

    result = _run(environment, "--attempt-name", CHANGE_SET_NAME)

    assert result.returncode != 0
    assert _operations(environment) == ["cloudformation/describe-change-set"]


def test_attempt_fallback_safely_skips_failed_execution(tmp_path: Path) -> None:
    environment = _environment(tmp_path, "execute-failed")

    result = _run(environment, "--attempt-name", CHANGE_SET_NAME)

    assert result.returncode == 0, result.stderr
    assert _operations(environment) == ["cloudformation/describe-change-set"] * len(STACKS)


def test_partial_plan_name_mode_does_not_skip_an_executed_target(tmp_path: Path) -> None:
    environment = _environment(tmp_path, "executed")

    result = _run(environment, "--change-set-name", CHANGE_SET_NAME)

    assert result.returncode != 0
    assert _operations(environment) == ["cloudformation/describe-change-set"]


@pytest.mark.parametrize("scenario", ["access-denied", "delete-access-denied"])
def test_access_denied_fails_without_echoing_provider_diagnostics(
    tmp_path: Path,
    scenario: str,
) -> None:
    environment = _environment(tmp_path, scenario)

    result = _run(environment, "--change-set-name", CHANGE_SET_NAME)

    assert result.returncode != 0
    assert "AccessDenied" not in result.stderr
    assert "private diagnostic context" not in result.stderr
    assert "Release change-set cleanup failed" in result.stderr


def test_only_exact_change_set_not_found_is_accepted(tmp_path: Path) -> None:
    environment = _environment(tmp_path, "misleading-not-found")

    result = _run(environment, "--change-set-name", CHANGE_SET_NAME)

    assert result.returncode != 0
    assert "ValidationError" not in result.stderr
    assert "describe request failed" in result.stderr


def test_polling_is_bounded(tmp_path: Path) -> None:
    environment = _environment(tmp_path, "in-progress", max_polls="2")

    result = _run(environment, "--change-set-name", CHANGE_SET_NAME)

    assert result.returncode != 0
    assert "cleanup timed out" in result.stderr
    assert _operations(environment) == ["cloudformation/describe-change-set"] * 2


def test_stale_preflight_deletes_only_prior_unexecuted_release_sets(tmp_path: Path) -> None:
    environment = _environment(tmp_path, "stale")

    result = _run(environment, "--stale-before-plan", CHANGE_SET_NAME)

    assert result.returncode == 0, result.stderr
    assert _operations(environment).count("cloudformation/list-change-sets") == 2 * len(STACKS)
    assert _operations(environment).count("cloudformation/describe-change-set") == 2 * len(STACKS)
    assert _operations(environment).count("cloudformation/delete-change-set") == len(STACKS)
    log = Path(environment["FAKE_AWS_LOG"]).read_text(encoding="utf-8")
    assert STALE_CHANGE_SET_NAME in log
    assert f"changeSet/{CHANGE_SET_NAME}/" not in log


def test_stale_preflight_consumes_the_cli_auto_paginated_inventory(tmp_path: Path) -> None:
    environment = _environment(tmp_path, "stale-two-pages")

    result = _run(environment, "--stale-before-plan", CHANGE_SET_NAME)

    assert result.returncode == 0, result.stderr
    assert _operations(environment).count("cloudformation/delete-change-set") == 2 * len(STACKS)
    assert _operations(environment).count("cloudformation/describe-change-set") == 4 * len(STACKS)


def test_stale_preflight_rejects_a_truncated_inventory(tmp_path: Path) -> None:
    environment = _environment(tmp_path, "stale-truncated")

    result = _run(environment, "--stale-before-plan", CHANGE_SET_NAME)

    assert result.returncode != 0
    assert _operations(environment) == ["cloudformation/list-change-sets"]


@pytest.mark.parametrize("scenario", ["stale-nonrelease", "stale-missing-stack"])
def test_stale_preflight_preserves_unrelated_plans_and_accepts_absent_stacks(
    tmp_path: Path,
    scenario: str,
) -> None:
    environment = _environment(tmp_path, scenario)

    result = _run(environment, "--stale-before-plan", CHANGE_SET_NAME)

    assert result.returncode == 0, result.stderr
    assert _operations(environment) == ["cloudformation/list-change-sets"] * (2 * len(STACKS))


@pytest.mark.parametrize(
    "scenario",
    [
        "stale-current",
        "stale-invalid",
        "stale-leading-zero",
        "stale-nested",
        "stale-executing",
        "stale-executed",
        "stale-execute-failed",
    ],
)
def test_stale_preflight_fails_closed_on_collision_or_ambiguous_inventory(
    tmp_path: Path,
    scenario: str,
) -> None:
    environment = _environment(tmp_path, scenario)

    result = _run(environment, "--stale-before-plan", CHANGE_SET_NAME)

    assert result.returncode != 0
    assert _operations(environment) == ["cloudformation/list-change-sets"]


@pytest.mark.parametrize("scenario", ["stale-delete-failed", "stale-race-execute"])
def test_stale_preflight_fails_closed_on_describe_state_races(
    tmp_path: Path,
    scenario: str,
) -> None:
    environment = _environment(tmp_path, scenario)

    result = _run(environment, "--stale-before-plan", CHANGE_SET_NAME)

    assert result.returncode != 0
    assert _operations(environment) == [
        "cloudformation/list-change-sets",
        "cloudformation/describe-change-set",
    ]


def test_stale_preflight_accepts_describe_not_found_only_after_clean_relist(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "stale-disappeared")

    result = _run(environment, "--stale-before-plan", CHANGE_SET_NAME)

    assert result.returncode == 0, result.stderr
    assert _operations(environment) == [
        "cloudformation/list-change-sets",
        "cloudformation/describe-change-set",
        "cloudformation/list-change-sets",
    ] * len(STACKS)


def test_stale_preflight_rejects_a_candidate_that_remains_after_not_found(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "stale-remains")

    result = _run(environment, "--stale-before-plan", CHANGE_SET_NAME)

    assert result.returncode != 0
    assert _operations(environment) == [
        "cloudformation/list-change-sets",
        "cloudformation/describe-change-set",
        "cloudformation/list-change-sets",
    ]


def test_stale_preflight_sanitizes_list_provider_failures(tmp_path: Path) -> None:
    environment = _environment(tmp_path, "stale-list-access-denied")

    result = _run(environment, "--stale-before-plan", CHANGE_SET_NAME)

    assert result.returncode != 0
    assert "AccessDenied" not in result.stderr
    assert "private diagnostic context" not in result.stderr
    assert "list request failed" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("--manifest",),
        ("--unknown", "value"),
        ("--change-set-name", "bad/name"),
        ("--attempt-name", "bad/name"),
        ("--stale-before-plan", "release-invalid"),
        ("--stale-before-plan", f"release-{'a' * 40}-01-1"),
    ],
)
def test_rejects_invalid_selectors(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    environment = _environment(tmp_path, "missing")

    result = _run(environment, *arguments)

    assert result.returncode != 0
    assert _operations(environment) == []


@pytest.mark.parametrize(
    ("variable", "value"),
    [("CLEANUP_POLL_SECONDS", "-1"), ("CLEANUP_POLL_SECONDS", "01"), ("CLEANUP_MAX_POLLS", "0")],
)
def test_rejects_invalid_polling_configuration(
    tmp_path: Path,
    variable: str,
    value: str,
) -> None:
    environment = _environment(tmp_path, "missing")
    environment[variable] = value

    result = _run(environment, "--change-set-name", CHANGE_SET_NAME)

    assert result.returncode != 0
    assert _operations(environment) == []
