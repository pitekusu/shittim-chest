import { spawnSync } from "node:child_process";
import { cpSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { describe, expect, test } from "vitest";

const asset = resolve("infra/assets/cdk-helper-tls/shittim-tls");
const preload = join(asset, "trust.cjs");
const oldRoot = resolve("tests/fixtures/tls/roots/emsign-g3.pem");

function execute(source: string, options: Record<string, string> = {}) {
  return spawnSync(process.execPath, ["-e", source], {
    encoding: "utf8",
    timeout: 10_000,
    env: { ...process.env, NODE_OPTIONS: "", ...options },
  });
}

describe("CDK helper TLS preload", () => {
  test("replaces ambient OS, extra and bundled trust for standard Node clients", () => {
    const result = execute(`
      const { readFileSync } = require('node:fs');
      const { X509Certificate } = require('node:crypto');
      const tls = require('node:tls');
      const fingerprints = values => [...new Set(values.map(value => new X509Certificate(value).fingerprint256))].sort();
      const expected = fingerprints(readFileSync(${JSON.stringify(join(asset, "cacert.pem"))}, 'utf8').match(/-----BEGIN CERTIFICATE-----[\\s\\S]*?-----END CERTIFICATE-----/g));
      const actual = fingerprints(tls.getCACertificates('default'));
      console.log(JSON.stringify({ matches: JSON.stringify(actual) === JSON.stringify(expected), count: actual.length,
        oldExtraStillObservable: tls.getCACertificates('extra').some(value => new X509Certificate(value).fingerprint256 === new X509Certificate(readFileSync(${JSON.stringify(oldRoot)})).fingerprint256) }));
    `, {
      NODE_OPTIONS: `--require=${preload}`,
      SSL_CERT_FILE: oldRoot,
      SSL_CERT_DIR: resolve("tests/fixtures/tls/no-extra-ca"),
      NODE_EXTRA_CA_CERTS: oldRoot,
    });
    expect(result.status).toBe(0);
    expect(JSON.parse(result.stdout)).toEqual({ matches: true, count: 120, oldExtraStillObservable: true });
  });

  test.each(["disabled", "missing-api"])("fails closed with %s TLS", (mode) => {
    const result = execute(`
      ${mode === "missing-api" ? "require('node:tls').setDefaultCACertificates = undefined;" : ""}
      require(${JSON.stringify(preload)});
    `, mode === "disabled" ? { NODE_TLS_REJECT_UNAUTHORIZED: "0" } : {});
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("cdk_helper_tls_initialization_failed");
  });

  test.each(["missing", "malformed"])("fails closed on a %s public bundle", (mode) => {
    const directory = mkdtempSync(join(tmpdir(), "shittim-cdk-tls-"));
    try {
      cpSync(join(asset, "trust.cjs"), join(directory, "trust.cjs"));
      cpSync(join(asset, "metadata.json"), join(directory, "metadata.json"));
      if (mode === "malformed") writeFileSync(join(directory, "cacert.pem"), "malformed public certificate");
      const result = execute(`require(${JSON.stringify(join(directory, "trust.cjs"))});`);
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain("cdk_helper_tls_initialization_failed");
    } finally {
      rmSync(directory, { recursive: true });
    }
  });
});
