#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Keep OCI 1.1 verification compatible with Docker's existing DHI signatures.
set -euo pipefail

test "$(uname -m)" = aarch64
test -n "${RUNNER_TEMP}"
install_binary() {
  local key="$1" binary="$2" repository version archive_name archive_sha archive
  repository=$(jq --exit-status --raw-output --arg key "${key}" '.tools[$key].repository' .github/tool-versions.json)
  version=$(jq --exit-status --raw-output --arg key "${key}" '.tools[$key].version' .github/tool-versions.json)
  archive_name=$(jq --exit-status --raw-output --arg key "${key}" '.tools[$key].archive_name' .github/tool-versions.json)
  archive_sha=$(jq --exit-status --raw-output --arg key "${key}" '.tools[$key].archive_sha256' .github/tool-versions.json)
  archive="${RUNNER_TEMP}/${archive_name}"
  curl --fail --location --proto '=https' --show-error --silent --tlsv1.2 \
    --output "${archive}" \
    "https://github.com/${repository}/releases/download/v${version}/${archive_name}"
  printf '%s  %s\n' "${archive_sha}" "${archive}" | sha256sum --check --strict
  install -m 0755 "${archive}" "${RUNNER_TEMP}/${binary}"
}
install_binary cosign-dhi dhi-cosign
install_binary regctl dhi-regctl
