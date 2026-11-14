#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
umask 077

[[ "${ROSTER_ACCOUNT}" =~ ^[0-9]{12}$ ]]
[[ "${GITHUB_SHA}" =~ ^[a-f0-9]{40}$ ]]
test "${GITHUB_REPOSITORY}" = pitekusu/shittim-chest
test "${GITHUB_REF}" = refs/heads/main

roster_path=data/student-gacha/blue_archive_characters_with_npcs.json
bucket="shittim-chest-production-records-media-${ROSTER_ACCOUNT}"
key=student-gacha/roster/blue_archive_characters_with_npcs.json
object=(--bucket "${bucket}" --key "${key}" --expected-bucket-owner "${ROSTER_ACCOUNT}")
scratch=$(mktemp -d "${RUNNER_TEMP}/student-gacha-roster.XXXXXXXX")
trap 'rm -r -- "${scratch}"' EXIT
git show "${GITHUB_SHA}:${roster_path}" > "${scratch}/roster.json"
cmp -- "${roster_path}" "${scratch}/roster.json"
sha256=$(sha256sum "${scratch}/roster.json" | cut -d' ' -f1)
checksum=$(openssl dgst -sha256 -binary "${scratch}/roster.json" | base64 -w0)
size=$(wc -c < "${scratch}/roster.json")

test "$(aws s3api get-bucket-versioning --bucket "${bucket}" \
  --expected-bucket-owner "${ROSTER_ACCOUNT}" --query Status --output text)" = Enabled

read_head() {
  aws s3api head-object "${object[@]}" --checksum-mode ENABLED
}

read_version() {
  jq -er '
    .VersionId | select(type == "string" and . != "null" and
      (utf8bytelength > 0 and utf8bytelength <= 1024) and (test("[[:cntrl:]]") | not))
  '
}

verify_metadata() {
  jq -e --arg checksum "${checksum}" --argjson bytes "${size}" --arg version "$1" '
    .VersionId == $version and .ChecksumSHA256 == $checksum and
    .ChecksumType == "FULL_OBJECT" and .ContentLength == $bytes and
    .ContentType == "application/json" and .CacheControl == "private, no-store" and
    .ServerSideEncryption == "AES256" and
    (.Metadata["source-commit"] | type == "string" and test("^[0-9a-f]{40}$"))
  ' >/dev/null
}

require_current_main() {
  local main_sha
  main_sha=$(gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/main" --jq '.object.sha')
  if [[ "${main_sha}" != "${GITHUB_SHA}" ]]; then
    echo "main advanced before roster publication" >&2
    return 1
  fi
}

version=
result=published
if read_head > "${scratch}/head.json" 2> "${scratch}/head.error"; then
  previous_version=$(read_version < "${scratch}/head.json")
  if verify_metadata "${previous_version}" < "${scratch}/head.json"; then
    version=${previous_version}
    result=already-current
  fi
  etag=$(jq -er '.ETag | select(type == "string" and startswith("\"") and endswith("\""))' \
    "${scratch}/head.json")
  precondition=(--if-match "${etag}")
elif grep -Eq '\((403|404|AccessDenied|NoSuchKey|NotFound)\) when calling the HeadObject operation' \
  "${scratch}/head.error"; then
  # A missing key can return 403 without permission to enumerate the bucket.
  precondition=(--if-none-match '*')
else
  echo "Unable to inspect the existing roster object" >&2
  exit 1
fi

require_current_main
if [[ -z "${version}" ]]; then
  if AWS_MAX_ATTEMPTS=1 aws s3api put-object "${object[@]}" "${precondition[@]}" \
    --body "${scratch}/roster.json" --checksum-algorithm SHA256 --checksum-sha256 "${checksum}" \
    --content-type application/json --cache-control 'private, no-store' --server-side-encryption AES256 \
    --metadata "source-commit=${GITHUB_SHA}" > "${scratch}/put.json" 2> "${scratch}/put.error"; then
    jq -e --arg checksum "${checksum}" '.ChecksumSHA256 == $checksum' \
      "${scratch}/put.json" >/dev/null
    version=$(read_version < "${scratch}/put.json")
  else
    # A network failure can occur after the write committed. Reconcile by readback without another PutObject.
    if ! read_head > "${scratch}/head.json" ||
       ! version=$(read_version < "${scratch}/head.json") ||
       ! verify_metadata "${version}" < "${scratch}/head.json" ||
       ! jq -e --arg sha "${GITHUB_SHA}" '.Metadata["source-commit"] == $sha' \
         "${scratch}/head.json" >/dev/null; then
      echo "Conditional roster publication failed and the intended object could not be confirmed" >&2
      exit 1
    fi
    result=reconciled
  fi
fi

aws s3api get-object "${object[@]}" --checksum-mode ENABLED "--version-id=${version}" \
  "${scratch}/published.json" > "${scratch}/version.json"
verify_metadata "${version}" < "${scratch}/version.json"
if [[ "${result}" != already-current ]]; then
  jq -e --arg sha "${GITHUB_SHA}" '.Metadata["source-commit"] == $sha' \
    "${scratch}/version.json" >/dev/null
fi
cmp -- "${scratch}/roster.json" "${scratch}/published.json"
read_head | verify_metadata "${version}"

{
  echo "Student Gacha roster: ${result}"
  echo
  echo "- Main commit: \`${GITHUB_SHA}\`"
  echo "- SHA-256: \`${sha256}\`"
  printf -- '- S3 version: <code>%s</code>\n' "$(jq -nr --arg version "${version}" '$version | @html')"
} >> "${GITHUB_STEP_SUMMARY}"
