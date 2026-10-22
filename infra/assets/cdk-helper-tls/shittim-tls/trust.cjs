// SPDX-License-Identifier: MIT
"use strict";

// Node's supported API replaces the default CA store, including build-time,
// OS and NODE_EXTRA_CA_CERTS sources. Run before the CDK handler or AWS SDK loads.
const { createHash, X509Certificate } = require("node:crypto");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const tls = require("node:tls");

try {
  if (
    typeof tls.setDefaultCACertificates !== "function" ||
    process.env.NODE_TLS_REJECT_UNAUTHORIZED === "0"
  ) {
    throw new Error();
  }
  const bundle = readFileSync(join(__dirname, "cacert.pem"));
  const metadata = JSON.parse(readFileSync(join(__dirname, "metadata.json"), "utf8"));
  const certificates = bundle
    .toString("ascii")
    .match(/-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g);
  if (
    !certificates ||
    certificates.length !== metadata.ca_count ||
    createHash("sha256").update(bundle).digest("hex") !== metadata.sha256
  ) {
    throw new Error();
  }
  const fingerprints = (values) =>
    [...new Set(values.map((value) => new X509Certificate(value).fingerprint256))]
      .sort()
      .join("\n");
  const expected = fingerprints(certificates);
  tls.setDefaultCACertificates(certificates);
  if (!expected || fingerprints(tls.getCACertificates("default")) !== expected) {
    throw new Error();
  }
} catch {
  // No certificate, SDK response, environment value or request data in logs.
  throw new Error("cdk_helper_tls_initialization_failed");
}
