#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail

error() {
  echo "::error::Release image evidence collection failed: $1" >&2
  exit 1
}

repository=""
digest=""
signing_profile_arn=""
output_directory=""
mode=""
repository_set="false"
digest_set="false"
profile_set="false"
output_set="false"
mode_set="false"

while (($#)); do
  if (($# < 2)); then
    error "invalid arguments"
  fi
  case "$1" in
    --repository)
      [[ "${repository_set}" == "false" ]] || error "invalid arguments"
      repository="$2"
      repository_set="true"
      ;;
    --digest)
      [[ "${digest_set}" == "false" ]] || error "invalid arguments"
      digest="$2"
      digest_set="true"
      ;;
    --signing-profile-arn)
      [[ "${profile_set}" == "false" ]] || error "invalid arguments"
      signing_profile_arn="$2"
      profile_set="true"
      ;;
    --output-dir)
      [[ "${output_set}" == "false" ]] || error "invalid arguments"
      output_directory="$2"
      output_set="true"
      ;;
    --mode)
      [[ "${mode_set}" == "false" ]] || error "invalid arguments"
      mode="$2"
      mode_set="true"
      ;;
    *)
      error "invalid arguments"
      ;;
  esac
  shift 2
done

if [[ "${repository_set}/${digest_set}/${profile_set}/${output_set}/${mode_set}" != \
  "true/true/true/true/true" ]]
then
  error "invalid arguments"
fi
if [[ ! "${repository}" =~ ^[a-z0-9]+([._/-][a-z0-9]+)*$ || ${#repository} -gt 256 ]]; then
  error "invalid repository"
fi
if [[ ! "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  error "invalid digest"
fi
if [[ ! "${signing_profile_arn}" =~ ^arn:aws:signer:[a-z0-9-]+:[0-9]{12}:/signing-profiles/[A-Za-z0-9_]{2,64}$ ]]; then
  error "invalid signing profile"
fi
case "${mode}" in
  normal|break-glass) ;;
  *) error "invalid mode" ;;
esac

max_polls="${EVIDENCE_MAX_POLLS:-30}"
poll_seconds="${EVIDENCE_POLL_SECONDS:-10}"
if [[ ! "${max_polls}" =~ ^[1-9][0-9]*$ ]]; then
  error "invalid polling configuration"
fi
if [[ ! "${poll_seconds}" =~ ^(0|[1-9][0-9]*)$ ]]; then
  error "invalid polling configuration"
fi

mkdir -p -- "${output_directory}"
if [[ ! -d "${output_directory}" || -L "${output_directory}" ]]; then
  error "invalid output directory"
fi
work_directory=$(mktemp -d "${output_directory}/.release-image-evidence.XXXXXX")
trap 'rm -rf -- "${work_directory}"' EXIT
export AWS_PAGER=""

image_file="${work_directory}/${mode}.image.json"
signing_file="${work_directory}/${mode}.signing.json"
scan_file="${work_directory}/${mode}.scan.json"
coverage_file="${work_directory}/${mode}.coverage.json"
error_file="${work_directory}/aws.error"
image_ready="false"
signing_ready="false"
scan_ready="false"
coverage_ready="false"

is_retryable_aws_error() {
  grep --extended-regexp --quiet \
    '\((Throttling|TooManyRequests|InternalFailure|InternalServer|ServiceUnavailable|RequestTimeout)(Exception|Error)?\)' \
    "$1"
}

is_scan_not_found() {
  grep --extended-regexp --quiet '\(ScanNotFound(Exception)?\)' "$1"
}

coverage_filter=$(jq --compact-output --null-input --arg repository "${repository}" \
  '{ecrRepositoryName:[{comparison:"EQUALS",value:$repository}]}' 2>/dev/null) || \
  error "unable to prepare Inspector query"

for ((attempt = 1; attempt <= max_polls; attempt++)); do
  if [[ "${image_ready}" == "false" ]]; then
    if aws ecr describe-images \
      --repository-name "${repository}" \
      --image-ids "imageDigest=${digest}" \
      --output json >"${image_file}" 2>"${error_file}"
    then
      if jq --exit-status --arg digest "${digest}" \
        '.imageDetails as $details |
         ($details | type) == "array" and
         ($details | length) == 1 and
         $details[0].imageDigest == $digest' "${image_file}" >/dev/null 2>&1
      then
        image_ready="true"
      else
        error "invalid image evidence"
      fi
    elif ! is_retryable_aws_error "${error_file}"; then
      error "image query failed"
    fi
  fi

  if [[ "${image_ready}" == "true" && "${signing_ready}" == "false" ]]; then
    if aws ecr describe-image-signing-status \
      --repository-name "${repository}" \
      --image-id "imageDigest=${digest}" \
      --output json >"${signing_file}" 2>"${error_file}"
    then
      signing_state=$(jq --raw-output --arg profile "${signing_profile_arn}" '
        if (.signingStatuses | type) != "array" or (.signingStatuses | length) != 1 then
          "mismatch"
        elif .signingStatuses[0].signingProfileArn != $profile then
          "mismatch"
        elif .signingStatuses[0].status == "COMPLETE" then
          "ready"
        elif .signingStatuses[0].status == "IN_PROGRESS" then
          "pending"
        elif .signingStatuses[0].status == "FAILED" then
          "failed"
        else
          "unknown"
        end
      ' "${signing_file}" 2>/dev/null) || error "invalid signing evidence"
      case "${signing_state}" in
        ready) signing_ready="true" ;;
        pending) ;;
        failed) error "image signing failed" ;;
        mismatch|unknown) error "invalid signing evidence" ;;
        *) error "invalid signing evidence" ;;
      esac
    elif ! is_retryable_aws_error "${error_file}"; then
      error "signing query failed"
    fi
  fi

  if [[ "${image_ready}" == "true" && "${scan_ready}" == "false" ]]; then
    if aws ecr describe-image-scan-findings \
      --repository-name "${repository}" \
      --image-id "imageDigest=${digest}" \
      --output json >"${scan_file}" 2>"${error_file}"
    then
      scan_state=$(jq --raw-output '
        if (.imageScanStatus | type) != "object" or
           (.imageScanStatus.status | type) != "string" then
          "unknown"
        else
          .imageScanStatus.status
        end
      ' "${scan_file}" 2>/dev/null) || error "invalid scan evidence"
      case "${scan_state}" in
        ACTIVE|COMPLETE) scan_ready="true" ;;
        IN_PROGRESS|PENDING) ;;
        FAILED|UNSUPPORTED_IMAGE|SCAN_ELIGIBILITY_EXPIRED|FINDINGS_UNAVAILABLE|LIMIT_EXCEEDED|IMAGE_ARCHIVED)
          error "image scan reached a terminal state"
          ;;
        *) error "invalid scan evidence" ;;
      esac
    elif is_scan_not_found "${error_file}" || is_retryable_aws_error "${error_file}"; then
      :
    else
      error "scan query failed"
    fi
  fi

  if [[ "${image_ready}" == "true" && "${coverage_ready}" == "false" ]]; then
    if aws inspector2 list-coverage \
      --filter-criteria "${coverage_filter}" \
      --output json >"${coverage_file}" 2>"${error_file}"
    then
      coverage_state=$(jq --raw-output --arg digest "${digest}" '
        if (.coveredResources | type) != "array" then
          "invalid"
        else
          [.coveredResources[] | select(
            .resourceType == "AWS_ECR_CONTAINER_IMAGE" and
            .scanType == "PACKAGE" and
            (.resourceId | type) == "string" and
            (.resourceId | endswith($digest))
          )] as $matches |
          if ($matches | length) == 0 then
            "pending"
          elif ($matches | length) != 1 then
            "invalid"
          elif $matches[0].scanStatus.statusCode == "ACTIVE" and
               $matches[0].scanStatus.reason == "SUCCESSFUL" and
               ($matches[0].lastScannedAt | type) == "string" and
               ($matches[0].lastScannedAt | length) > 0 then
            "ready"
          elif ([
              "PENDING_INITIAL_SCAN",
              "SCAN_IN_PROGRESS",
              "INTERNAL_ERROR",
              "PENDING_REVIVAL_SCAN"
            ] | index($matches[0].scanStatus.reason)) != null then
            "pending"
          else
            "invalid"
          end
        end
      ' "${coverage_file}" 2>/dev/null) || error "invalid Inspector evidence"
      case "${coverage_state}" in
        ready) coverage_ready="true" ;;
        pending) ;;
        invalid) error "invalid Inspector evidence" ;;
        *) error "invalid Inspector evidence" ;;
      esac
    elif ! is_retryable_aws_error "${error_file}"; then
      error "Inspector query failed"
    fi
  fi

  if [[ "${image_ready}/${signing_ready}/${scan_ready}/${coverage_ready}" == \
    "true/true/true/true" ]]
  then
    mv -- "${image_file}" "${signing_file}" "${scan_file}" "${coverage_file}" \
      "${output_directory}/"
    exit 0
  fi
  if ((attempt < max_polls && poll_seconds > 0)); then
    sleep "${poll_seconds}"
  fi
done

error "evidence collection timed out"
