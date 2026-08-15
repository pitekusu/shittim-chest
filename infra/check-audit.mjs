// Fail-closed npm audit gate with a dated exception allowlist.
// Reads `npm audit --json` from stdin and exits 1 on any advisory that is not
// covered by an unexpired entry in npm-audit-exceptions.json.
import { readFileSync } from "node:fs";

if (process.argv.length > 3) {
  console.error("usage: node infra/check-audit.mjs [exceptions-path]");
  process.exit(1);
}

const exceptionsPath = process.argv[2] ?? new URL("./npm-audit-exceptions.json", import.meta.url);
let exceptions;
try {
  exceptions = JSON.parse(readFileSync(exceptionsPath, "utf8"));
} catch {
  console.error("npm audit exceptions were not valid JSON; refusing to pass");
  process.exit(1);
}

const exceptionFields = new Set(["id", "package", "severity", "reason", "expires"]);
const severities = new Set(["info", "low", "moderate", "high", "critical"]);
if (!Array.isArray(exceptions)) {
  console.error("npm audit exceptions must be an array; refusing to pass");
  process.exit(1);
}

const allowed = new Map();
for (const [index, entry] of exceptions.entries()) {
  if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
    console.error(`npm audit exception ${index} was not an object; refusing to pass`);
    process.exit(1);
  }
  const fields = Object.keys(entry);
  const unknown = fields.filter((field) => !exceptionFields.has(field));
  const missing = [...exceptionFields].filter((field) => !Object.hasOwn(entry, field));
  if (unknown.length > 0 || missing.length > 0) {
    console.error(
      `npm audit exception ${index} had invalid fields` +
        ` (unknown: ${unknown.join(", ") || "none"}; missing: ${missing.join(", ") || "none"})`,
    );
    process.exit(1);
  }
  for (const field of ["id", "package", "reason", "expires"]) {
    if (typeof entry[field] !== "string" || entry[field].trim() === "") {
      console.error(`npm audit exception ${index} had invalid ${field}; refusing to pass`);
      process.exit(1);
    }
  }
  if (typeof entry.severity !== "string" || !severities.has(entry.severity)) {
    console.error(`npm audit exception ${index} had invalid severity; refusing to pass`);
    process.exit(1);
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(entry.expires)) {
    console.error(`npm audit exception ${index} had invalid expires; refusing to pass`);
    process.exit(1);
  }
  if (allowed.has(entry.id)) {
    console.error(`npm audit exception ${entry.id} was duplicated; refusing to pass`);
    process.exit(1);
  }
  allowed.set(entry.id, entry);
}

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  input += chunk;
});
process.stdin.on("end", () => {
  let report;
  try {
    report = JSON.parse(input);
  } catch {
    console.error("npm audit output was not valid JSON; refusing to pass");
    process.exit(1);
  }
  if (
    report === null ||
    typeof report !== "object" ||
    "error" in report ||
    typeof report.auditReportVersion !== "number" ||
    report.metadata === null ||
    typeof report.metadata !== "object" ||
    report.metadata.vulnerabilities === null ||
    typeof report.metadata.vulnerabilities !== "object" ||
    report.vulnerabilities === null ||
    typeof report.vulnerabilities !== "object"
  ) {
    console.error("npm audit report was incomplete or reported an error; refusing to pass");
    process.exit(1);
  }
  const today = new Date().toISOString().slice(0, 10);
  const failures = [];
  const referencedExceptions = new Set();
  for (const [name, vulnerability] of Object.entries(report.vulnerabilities ?? {})) {
    const advisories = (vulnerability.via ?? []).filter(
      (via) => typeof via === "object" && via !== null && typeof via.url === "string",
    );
    if (advisories.length === 0) {
      failures.push(`${name}: vulnerable with no advisory metadata; refusing to pass`);
      continue;
    }
    for (const advisory of advisories) {
      const id = advisory.url.split("/").pop();
      const exception = allowed.get(id);
      if (exception === undefined) {
        failures.push(`${name}: ${id} (${advisory.severity}) ${advisory.title}`);
      } else if (exception.package !== name) {
        referencedExceptions.add(id);
        failures.push(
          `${name}: ${id} exception package ${exception.package} did not match ${name}`,
        );
      } else if (exception.severity !== advisory.severity) {
        referencedExceptions.add(id);
        failures.push(
          `${name}: ${id} exception severity ${exception.severity} did not match ${advisory.severity}`,
        );
      } else if (exception.expires <= today) {
        referencedExceptions.add(id);
        failures.push(`${name}: ${id} exception expired on ${exception.expires}`);
      } else {
        referencedExceptions.add(id);
      }
    }
  }
  for (const id of allowed.keys()) {
    if (!referencedExceptions.has(id)) {
      failures.push(`${id}: unused npm audit exception; refusing to pass`);
    }
  }
  if (failures.length > 0) {
    console.error(`npm audit: ${failures.length} unexcepted finding(s)`);
    for (const failure of failures) {
      console.error(`- ${failure}`);
    }
    process.exit(1);
  }
  console.log(`npm audit: clean (${allowed.size} dated exception(s) active)`);
});
