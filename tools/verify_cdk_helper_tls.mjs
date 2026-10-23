#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// Local validation only. Online mode sends credential-free HEAD requests to
// public certificate fixtures, never an AWS SDK command or CloudFormation URL.
import { createHash, X509Certificate } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import https from "node:https";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import tls from "node:tls";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const asset = join(root, "infra/assets/cdk-helper-tls/shittim-tls");
const bundlePath = join(asset, "cacert.pem");
const preloadPath = join(asset, "trust.cjs");
const fixtures = join(root, "tests/fixtures/tls");
const digest = (value) => createHash("sha256").update(value).digest("hex");
const fingerprints = (values) =>
  [...new Set(values.map((value) => digest(new X509Certificate(value).raw)))].sort();
const incidentHosts = ["dv", "ov", "ev"].flatMap((kind) =>
  ["g3b", "g3c"].flatMap((issuer) =>
    ["", "-aaa"].map((suffix) => `${kind}-${issuer}${suffix}-emudhra.we-have-a-problem.com`),
  ),
);
const normalHosts = ["api.openai.com", "discord.com", "www.python.org"];
const caErrors = new Set([
  "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
  "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
  "CERT_REVOKED",
]);
const transportErrors = new Set([
  "EAI_AGAIN",
  "ENOTFOUND",
  "ECONNRESET",
  "ECONNREFUSED",
  "ETIMEDOUT",
  "ERR_SOCKET_CONNECTION_TIMEOUT",
  "ABORT_ERR",
]);

function argumentsFrom(values) {
  const options = { online: false };
  for (let index = 0; index < values.length; index += 1) {
    const flag = values[index];
    if (flag === "--online") options.online = true;
    else if ((flag === "--sdk-root" || flag === "--output") && values[index + 1]) {
      options[flag === "--sdk-root" ? "sdkRoot" : "output"] = resolve(values[++index]);
    } else throw new Error("cdk_helper_tls_arguments_invalid");
  }
  if (!options.output || (options.online && !options.sdkRoot)) {
    throw new Error("cdk_helper_tls_arguments_missing");
  }
  return options;
}

function inspectStore() {
  const require = createRequire(import.meta.url);
  if (!require.cache[preloadPath]) throw new Error("cdk_helper_tls_preload_not_loaded");
  const bundle = readFileSync(bundlePath);
  const pems = bundle.toString("ascii").match(/-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g);
  if (!pems) throw new Error("cdk_helper_tls_bundle_invalid");
  const expected = fingerprints(pems);
  const actual = fingerprints(tls.getCACertificates("default"));
  const policy = JSON.parse(readFileSync(join(root, "tools/emudhra_ca_distrust.json"), "utf8"));
  const deniedCertificates = new Set(policy.certificates.map((item) => item.sha256));
  const deniedKeys = new Set(policy.certificates.map((item) => item.spki_sha256));
  const blocked = pems.some((pem) => {
    const cert = new X509Certificate(pem);
    return deniedCertificates.has(digest(cert.raw)) || deniedKeys.has(
      digest(cert.publicKey.export({ type: "spki", format: "der" })),
    );
  });
  const matches = expected.length > 0 && JSON.stringify(expected) === JSON.stringify(actual);
  return {
    status: matches && !blocked ? "passed" : "failed",
    bundle_sha256: digest(bundle),
    default_ca_count: actual.length,
    default_certificate_sha256: actual,
    default_matches_packaged_bundle: matches,
    incident_keys_absent: !blocked,
    // These remain observable after the supported API replaces the default.
    // Their presence must not be mistaken for inclusion in the selected store.
    ambient_system_ca_count: fingerprints(tls.getCACertificates("system")).length,
    ambient_extra_ca_count: fingerprints(tls.getCACertificates("extra")).length,
    bundled_ca_count: fingerprints(tls.rootCertificates).length,
  };
}

function verifyOfflineFixtures() {
  const results = [];
  for (const [directory, accept] of [["emudhra", false], ["valid", true]]) {
    const files = readdirSync(join(fixtures, directory)).filter((name) => name.endsWith(".pem")).sort();
    if (files.length !== (accept ? 3 : 12)) throw new Error("cdk_helper_tls_fixture_count_invalid");
    for (const file of files) {
      const path = join(fixtures, directory, file);
      const host = file.slice(0, -4);
      const result = spawnSync("openssl", [
        "verify", "-CAfile", bundlePath, "-no-CApath", "-no-CAstore",
        "-attime", "1792584000", "-purpose", "sslserver", "-verify_hostname", host,
        "-untrusted", path, path,
      ], { encoding: "utf8", timeout: 10_000, maxBuffer: 1_048_576 });
      const codes = [...(result.stderr ?? "").matchAll(/error (\d+) at /g)].map((match) => Number(match[1]));
      const accepted = result.status === 0;
      results.push({
        host,
        expected: accept ? "accept" : "reject",
        accepted,
        verify_codes: codes,
        passed: !result.error && (accept ? accepted : !accepted && codes.length > 0 && codes.every((code) => [20, 23].includes(code))),
        fixture_sha256: digest(readFileSync(path)),
      });
    }
  }
  return results;
}

function requestWithHttps(host) {
  return new Promise((resolveRequest, reject) => {
    const request = https.request({
      hostname: host, method: "HEAD", path: "/", signal: AbortSignal.timeout(10_000),
    }, (response) => {
      response.resume();
      resolveRequest();
    });
    request.on("error", reject);
    request.end();
  });
}

async function classifyAttempt(request, host, accept) {
  try {
    await request(host);
    return { result: "accepted", passed: accept, retryable: false };
  } catch (error) {
    const code = typeof error?.code === "string" ? error.code : "";
    if (caErrors.has(code)) {
      return { result: "certificate_rejected", code, passed: !accept, retryable: false };
    }
    const transport = transportErrors.has(code) || ["AbortError", "TimeoutError"].includes(error?.name);
    return {
      result: transport ? "unverified_connection" : "unverified_tls_error",
      // Unknown exception text may contain requests or URLs. Only fixed codes survive.
      code: transportErrors.has(code) ? code : transport ? "TIMEOUT" : "TLS_ERROR",
      passed: false,
      retryable: transport,
    };
  }
}

async function verifyOnline(sdkRoot) {
  const sdkRequire = createRequire(join(sdkRoot, "package.json"));
  const { NodeHttpHandler } = sdkRequire("@smithy/node-http-handler");
  const version = sdkRequire("@smithy/node-http-handler/package.json").version;
  const handler = new NodeHttpHandler();
  const requestWithSdk = async (hostname) => {
    const { response } = await handler.handle({
      protocol: "https:", hostname, port: 443, method: "HEAD", path: "/", headers: {},
    }, { abortSignal: AbortSignal.timeout(10_000) });
    response.body.destroy();
  };
  const tasks = [
    ["https.request", requestWithHttps], ["NodeHttpHandler", requestWithSdk],
  ].flatMap(([transport, request]) => [
    ...incidentHosts.map((host) => ({ transport, request, host, accept: false })),
    ...normalHosts.map((host) => ({ transport, request, host, accept: true })),
  ]);
  const results = new Array(tasks.length);
  let next = 0;
  try {
    await Promise.all(Array.from({ length: 4 }, async () => {
      while (next < tasks.length) {
        const index = next++;
        const { transport, request, host, accept } = tasks[index];
        const attempts = [];
        for (let number = 1; number <= 3; number += 1) {
          const result = await classifyAttempt(request, host, accept);
          attempts.push({ attempt: number, ...result });
          if (!result.retryable || number === 3) break;
          await new Promise((done) => setTimeout(done, 250 * number));
        }
        results[index] = { transport, host, expected: accept ? "accept" : "reject", ...attempts.at(-1), attempts };
      }
    }));
  } finally {
    handler.destroy();
    https.globalAgent.destroy();
  }
  return { sdk_package: "@smithy/node-http-handler", sdk_version: version, checks: results };
}

async function main() {
  let options;
  try {
    options = argumentsFrom(process.argv.slice(2));
    const report = {
      node: process.version,
      openssl: process.versions.openssl,
      credentials_supplied: false,
      aws_commands_sent: false,
      managed_lambda_runtime_verified: false,
      store: inspectStore(),
      offline_verification_epoch: 1792584000,
      offline: verifyOfflineFixtures(),
      online: options.online ? await verifyOnline(options.sdkRoot) : { status: "not_requested" },
    };
    report.status = report.store.status === "passed" && report.offline.every((row) => row.passed)
      && (!options.online || report.online.checks.every((row) => row.passed)) ? "passed" : "failed";
    mkdirSync(dirname(options.output), { recursive: true });
    writeFileSync(options.output, `${JSON.stringify(report, null, 2)}\n`);
    console.log(`CDK helper TLS verification: ${report.status}`);
    process.exitCode = report.status === "passed" ? 0 : 1;
  } catch {
    if (options?.output) {
      mkdirSync(dirname(options.output), { recursive: true });
      writeFileSync(options.output, `${JSON.stringify({ status: "unverified", reason: "cdk_helper_tls_validation_failed" })}\n`);
    }
    console.error("CDK helper TLS verification unavailable");
    process.exitCode = 1;
  }
}

await main();
