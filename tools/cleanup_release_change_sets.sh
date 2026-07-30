#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail

error() {
  echo "::error::Release change-set cleanup failed: $1" >&2
  exit 1
}

if [[ $# -ne 2 ]]; then
  error "expected exactly one cleanup selector"
fi

mode=""
change_set_name=""
current_change_set_name=""
manifest=""
active_manifest_state="false"
case "$1" in
  --change-set-name)
    mode="name"
    change_set_name="$2"
    if [[ ! "${change_set_name}" =~ ^release-[0-9a-f]{40}-[1-9][0-9]*-[1-9][0-9]*$ ]]; then
      error "invalid change-set name"
    fi
    ;;
  --attempt-name)
    mode="attempt"
    change_set_name="$2"
    if [[ ! "${change_set_name}" =~ ^release-[0-9a-f]{40}-[1-9][0-9]*-[1-9][0-9]*$ ]]; then
      error "invalid attempt change-set name"
    fi
    ;;
  --stale-before-plan)
    mode="stale"
    current_change_set_name="$2"
    if [[ ! "${current_change_set_name}" =~ ^release-[0-9a-f]{40}-[1-9][0-9]*-[1-9][0-9]*$ ]]; then
      error "invalid current change-set name"
    fi
    ;;
  --manifest)
    mode="manifest"
    manifest="$2"
    if [[ ! -f "${manifest}" || ! -r "${manifest}" ]]; then
      error "manifest is unavailable"
    fi
    ;;
  *)
    error "expected exactly one cleanup selector"
    ;;
esac

poll_seconds="${CLEANUP_POLL_SECONDS:-5}"
max_polls="${CLEANUP_MAX_POLLS:-12}"
if [[ ! "${poll_seconds}" =~ ^(0|[1-9][0-9]*)$ ]]; then
  error "invalid polling configuration"
fi
if [[ ! "${max_polls}" =~ ^[1-9][0-9]*$ ]]; then
  error "invalid polling configuration"
fi

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
work_directory=$(mktemp -d "${RUNNER_TEMP}/release-change-set-cleanup.XXXXXX")
trap 'rm -rf -- "${work_directory}"' EXIT
export AWS_PAGER=""

is_change_set_not_found() {
  local error_file="$1"
  grep --fixed-strings --quiet '(ChangeSetNotFound)' "${error_file}" &&
    grep --fixed-strings --quiet 'does not exist' "${error_file}"
}

is_stack_not_found() {
  local error_file="$1"
  grep --fixed-strings --quiet '(ValidationError)' "${error_file}" &&
    grep --fixed-strings --quiet 'does not exist' "${error_file}"
}

sleep_before_next_poll() {
  local poll="$1"
  if ((poll < max_polls && poll_seconds > 0)); then
    sleep "${poll_seconds}"
  fi
}

cleanup_change_set() {
  local stack="$1"
  local region="$2"
  local locator="$3"
  local error_file="${work_directory}/${stack}.error"
  local status_file="${work_directory}/${stack}.status"
  local seen="false"
  local delete_requested="false"
  local consecutive_missing=0
  local poll status execution_status extra

  for ((poll = 1; poll <= max_polls; poll++)); do
    local -a describe_arguments=(
      cloudformation describe-change-set
      --region "${region}"
      --stack-name "${stack}"
      --change-set-name "${locator}"
      --no-paginate
      --query '[Status,ExecutionStatus]'
      --output text
    )
    if aws "${describe_arguments[@]}" >"${status_file}" 2>"${error_file}"; then
      seen="true"
      consecutive_missing=0
      status=""
      execution_status=""
      extra=""
      read -r status execution_status extra <"${status_file}" || true
      if [[ -z "${status}" || -z "${execution_status}" || -n "${extra}" ]]; then
        error "invalid state response for ${stack}"
      fi

      case "${execution_status}" in
        EXECUTE_COMPLETE|EXECUTE_FAILED|OBSOLETE)
          if [[ "${mode}" == "manifest" || "${mode}" == "attempt" ]]; then
            return 0
          fi
          error "unexpected executed change set for ${stack}"
          ;;
        EXECUTE_IN_PROGRESS)
          if [[ "${mode}" == "manifest" ]]; then
            active_manifest_state="true"
            return 0
          fi
          error "unsafe executed change-set state for ${stack}"
          ;;
      esac

      case "${status}/${execution_status}" in
        CREATE_PENDING/UNAVAILABLE|CREATE_IN_PROGRESS/UNAVAILABLE|CREATE_COMPLETE/UNAVAILABLE)
          ;;
        CREATE_COMPLETE/AVAILABLE|FAILED/UNAVAILABLE)
          if [[ "${delete_requested}" == "false" ]]; then
            if aws cloudformation delete-change-set \
              --region "${region}" \
              --stack-name "${stack}" \
              --change-set-name "${locator}" \
              >/dev/null 2>"${error_file}"
            then
              delete_requested="true"
            elif is_change_set_not_found "${error_file}"; then
              return 0
            else
              error "delete request failed for ${stack}"
            fi
          fi
          ;;
        DELETE_PENDING/UNAVAILABLE|DELETE_IN_PROGRESS/UNAVAILABLE|DELETE_COMPLETE/UNAVAILABLE)
          delete_requested="true"
          ;;
        *)
          error "unexpected change-set state for ${stack}"
          ;;
      esac
    elif is_change_set_not_found "${error_file}"; then
      if [[ "${mode}" != "name" || "${seen}" == "true" || "${delete_requested}" == "true" ]]; then
        return 0
      fi
      consecutive_missing=$((consecutive_missing + 1))
      if ((consecutive_missing >= 3)); then
        return 0
      fi
    else
      error "describe request failed for ${stack}"
    fi
    sleep_before_next_poll "${poll}"
  done

  error "cleanup timed out for ${stack}"
}

list_change_sets() {
  local stack="$1"
  local region="$2"
  local output_file="$3"
  local error_file="${work_directory}/${stack}.list-error"

  if aws cloudformation list-change-sets \
    --region "${region}" \
    --stack-name "${stack}" \
    --output json >"${output_file}" 2>"${error_file}"
  then
    return 0
  fi
  if is_stack_not_found "${error_file}"; then
    printf '{"Summaries":[]}\n' >"${output_file}"
    return 0
  fi
  error "list request failed for ${stack}"
}

cleanup_stale_change_sets() {
  local stack="$1"
  local region="$2"
  local list_file="${work_directory}/${stack}.list.json"
  local stale_file="${work_directory}/${stack}.stale.tsv"
  local release_name_pattern='^release-[0-9a-f]{40}-[1-9][0-9]*-[1-9][0-9]*$'
  local stale_name stale_arn listed_stack listed_stack_arn status execution_status nested
  local parent_arn root_arn extra account

  list_change_sets "${stack}" "${region}" "${list_file}"
  jq --exit-status --arg pattern "${release_name_pattern}" \
    --arg current "${current_change_set_name}" '
      (.Summaries | type) == "array" and
      ((.NextToken // "") == "") and
      ([.Summaries[] | select(
        (.ExecutionStatus == "AVAILABLE" or .ExecutionStatus == "UNAVAILABLE") and
        (.ChangeSetName | startswith("release-"))
      )] | all(
        (.ChangeSetName | type) == "string" and
        (.ChangeSetName | test($pattern)) and
        .ChangeSetName != $current and
        (.ChangeSetId | type) == "string" and
        (.StackName | type) == "string" and
        (.StackId | type) == "string" and
        (.Status | type) == "string" and
        ((.IncludeNestedStacks // false) == false) and
        ((.ParentChangeSetId // "") == "") and
        ((.RootChangeSetId // "") == "")
      )) and
      ([.Summaries[] | select(
        (.ChangeSetName | type) == "string" and
        (.ChangeSetName | test($pattern)) and
        (.ExecutionStatus == "EXECUTE_IN_PROGRESS" or
         .ExecutionStatus == "EXECUTE_COMPLETE" or
         .ExecutionStatus == "EXECUTE_FAILED")
      )] | length == 0)
    ' "${list_file}" >/dev/null || error "invalid stale change-set inventory for ${stack}"
  jq --raw-output --arg pattern "${release_name_pattern}" '
      .Summaries[] | select(
        (.ExecutionStatus == "AVAILABLE" or .ExecutionStatus == "UNAVAILABLE") and
        (.ChangeSetName | type) == "string" and
        (.ChangeSetName | test($pattern))
      ) | [
        .ChangeSetName,
        .ChangeSetId,
        .StackName,
        .StackId,
        .Status,
        .ExecutionStatus,
        ((.IncludeNestedStacks // false) | tostring),
        (.ParentChangeSetId // "-"),
        (.RootChangeSetId // "-")
      ] | @tsv
    ' "${list_file}" >"${stale_file}" || error "unable to select stale change sets"

  if (( $(wc -l <"${stale_file}") > 100 )); then
    error "too many stale change sets for ${stack}"
  fi

  while IFS=$'\t' read -r stale_name stale_arn listed_stack listed_stack_arn status \
    execution_status nested parent_arn root_arn extra
  do
    if [[ -z "${stale_name}" && -z "${stale_arn}" && -z "${listed_stack}" &&
      -z "${listed_stack_arn}" && -z "${status}" && -z "${execution_status}" &&
      -z "${nested}" && -z "${parent_arn}" && -z "${root_arn}" && -z "${extra}" ]]
    then
      continue
    fi
    if [[ -z "${stale_name}" || -z "${stale_arn}" || -z "${listed_stack}" ||
      -z "${listed_stack_arn}" || -z "${status}" || -z "${execution_status}" ||
      -z "${nested}" || "${parent_arn}" != "-" || "${root_arn}" != "-" ||
      -n "${extra}" ]]
    then
      error "invalid stale change-set record for ${stack}"
    fi
    if [[ "${listed_stack}" != "${stack}" || "${nested}" != "false" ]]; then
      error "stale change-set scope mismatch for ${stack}"
    fi
    if [[ ! "${listed_stack_arn}" =~ ^arn:aws:cloudformation:${region}:([0-9]{12}):stack/${stack}/[A-Za-z0-9-]+$ ]]; then
      error "invalid stale stack ARN for ${stack}"
    fi
    account="${BASH_REMATCH[1]}"
    if [[ ! "${stale_arn}" =~ ^arn:aws:cloudformation:${region}:${account}:changeSet/${stale_name}/[A-Za-z0-9-]+$ ]]; then
      error "invalid stale change-set ARN for ${stack}"
    fi
    cleanup_change_set "${stack}" "${region}" "${stale_arn}"
  done <"${stale_file}"

  list_change_sets "${stack}" "${region}" "${list_file}"
  jq --exit-status --arg pattern "${release_name_pattern}" '
      (.Summaries | type) == "array" and
      ((.NextToken // "") == "") and
      ([.Summaries[] | select(
        (.ExecutionStatus == "AVAILABLE" or .ExecutionStatus == "UNAVAILABLE") and
        (.ChangeSetName | type) == "string" and
        (.ChangeSetName | startswith("release-"))
      )] | length == 0) and
      ([.Summaries[] | select(
        (.ChangeSetName | type) == "string" and
        (.ChangeSetName | test($pattern)) and
        (.ExecutionStatus == "EXECUTE_IN_PROGRESS" or
         .ExecutionStatus == "EXECUTE_COMPLETE" or
         .ExecutionStatus == "EXECUTE_FAILED")
      )] | length == 0)
    ' "${list_file}" >/dev/null || error "stale change sets remain for ${stack}"
}

for mapping in \
  'ShittimChest-Prod-Stateful=ap-northeast-1' \
  'ShittimChest-Prod-Runtime=ap-northeast-1' \
  'ShittimChest-Prod-Operations=ap-northeast-1' \
  'ShittimChest-Prod-CostGovernance=us-east-1'
do
  stack="${mapping%%=*}"
  region="${mapping#*=}"
  if [[ "${mode}" == "stale" ]]; then
    cleanup_stale_change_sets "${stack}" "${region}"
    continue
  elif [[ "${mode}" == "name" || "${mode}" == "attempt" ]]; then
    locator="${change_set_name}"
  else
    if ! locator=$(jq --exit-status --raw-output --arg stack "${stack}" \
      '.change_sets[$stack] | select(type == "string")' \
      "${manifest}" 2>/dev/null)
    then
      error "manifest does not contain every release change set"
    fi
    if [[ ! "${locator}" =~ ^arn:aws:cloudformation:${region}:[0-9]{12}:changeSet/release-[0-9a-f]{40}-[1-9][0-9]*-[1-9][0-9]*/[A-Za-z0-9-]+$ ]]; then
      error "manifest contains an invalid release change set"
    fi
  fi
  cleanup_change_set "${stack}" "${region}" "${locator}"
done

if [[ "${active_manifest_state}" == "true" ]]; then
  error "one or more manifest change sets are still executing"
fi
