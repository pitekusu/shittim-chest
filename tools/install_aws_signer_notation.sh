#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${AWS_SIGNER_NOTATION_VERSION:?AWS_SIGNER_NOTATION_VERSION is required}"
: "${AWS_SIGNER_NOTATION_ARCHIVE_SHA256:?archive SHA-256 is required}"
: "${AWS_SIGNER_NOTATION_SIGNATURE_SHA256:?signature SHA-256 is required}"
: "${AWS_SIGNER_NOTATION_KEY_SHA256:?public-key SHA-256 is required}"
: "${AWS_SIGNER_NOTATION_FINGERPRINT:?public-key fingerprint is required}"

package="${RUNNER_TEMP}/aws-signer-notation-cli_arm64.deb"
signature="${package}.sig"
public_key="${RUNNER_TEMP}/aws-signer-notation-public.key"
base_url="https://d2hvyiie56hcat.cloudfront.net"

curl --fail --location --proto '=https' --show-error --silent --tlsv1.2 \
  --output "${package}" \
  "${base_url}/linux/arm64/installer/deb/latest/aws-signer-notation-cli_arm64.deb"
curl --fail --location --proto '=https' --show-error --silent --tlsv1.2 \
  --output "${signature}" \
  "${base_url}/linux/arm64/installer/deb/latest/aws-signer-notation-cli_arm64.deb.sig"
curl --fail --location --proto '=https' --show-error --silent --tlsv1.2 \
  --output "${public_key}" "${base_url}/linux/public.key"

printf '%s  %s\n' "${AWS_SIGNER_NOTATION_ARCHIVE_SHA256}" "${package}" | \
  sha256sum --check --strict
printf '%s  %s\n' "${AWS_SIGNER_NOTATION_SIGNATURE_SHA256}" "${signature}" | \
  sha256sum --check --strict
printf '%s  %s\n' "${AWS_SIGNER_NOTATION_KEY_SHA256}" "${public_key}" | \
  sha256sum --check --strict

export GNUPGHOME="${RUNNER_TEMP}/aws-signer-gnupg-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}"
mkdir -m 700 "${GNUPGHOME}"
gpg --batch --import "${public_key}"
fingerprint=$(gpg --batch --with-colons --fingerprint | \
  awk -F: '$1 == "fpr" {print $10; exit}')
test "${fingerprint}" = "${AWS_SIGNER_NOTATION_FINGERPRINT}"
gpg --batch --verify "${signature}" "${package}"
sudo dpkg -i -E "${package}"
notation version | grep --fixed-strings "${AWS_SIGNER_NOTATION_VERSION%-1}"
notation plugin ls | grep --fixed-strings com.amazonaws.signer.notation.plugin
