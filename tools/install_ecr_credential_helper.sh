#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${ECR_CREDENTIAL_HELPER_VERSION:?ECR Credential Helper version is required}"
: "${ECR_CREDENTIAL_HELPER_SHA256:?ECR Credential Helper SHA-256 is required}"

registry="${1:?ECR registry hostname is required}"
if [[ ! "${registry}" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com$ ]]; then
  echo "invalid private ECR registry hostname" >&2
  exit 1
fi

binary="${RUNNER_TEMP}/docker-credential-ecr-login"
base_url="https://amazon-ecr-credential-helper-releases.s3.us-east-2.amazonaws.com"
curl --fail --location --proto '=https' --show-error --silent --tlsv1.2 \
  --output "${binary}" \
  "${base_url}/${ECR_CREDENTIAL_HELPER_VERSION}/linux-arm64/docker-credential-ecr-login"
printf '%s  %s\n' "${ECR_CREDENTIAL_HELPER_SHA256}" "${binary}" | \
  sha256sum --check --strict
sudo install --mode=0755 --owner=root --group=root \
  "${binary}" /usr/local/bin/docker-credential-ecr-login

version_output=$(docker-credential-ecr-login -v)
printf '%s\n' "${version_output}" | \
  grep --fixed-strings "Version:    ${ECR_CREDENTIAL_HELPER_VERSION}"

docker_config_directory="${DOCKER_CONFIG:-${HOME}/.docker}"
docker_config="${docker_config_directory}/config.json"
mkdir -p "${docker_config_directory}"
chmod 700 "${docker_config_directory}"
if [[ ! -e "${docker_config}" ]]; then
  printf '{}\n' > "${docker_config}"
fi
jq --exit-status 'type == "object"' "${docker_config}" >/dev/null
temporary_config=$(mktemp "${docker_config}.tmp.XXXXXX")
trap 'rm -f "${temporary_config}"' EXIT
jq --arg registry "${registry}" \
  '.credHelpers = ((.credHelpers // {}) + {($registry): "ecr-login"})' \
  "${docker_config}" > "${temporary_config}"
chmod 600 "${temporary_config}"
mv "${temporary_config}" "${docker_config}"
jq --exit-status --arg registry "${registry}" \
  '.credHelpers[$registry] == "ecr-login"' "${docker_config}" >/dev/null
