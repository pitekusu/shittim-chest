// Fail-closed npm audit gate with a dated exception allowlist.
// Reads npm or pnpm `audit --json` and exits 1 on any advisory that is not
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
const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
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
  let expiry;
  try {
    expiry = /^\d{4}-\d{2}-\d{2}$/.test(entry.expires)
      ? Temporal.PlainDate.from(entry.expires)
      : null;
  } catch {
    expiry = null;
  }
  if (expiry === null) {
    console.error(`npm audit exception ${index} had invalid expires; refusing to pass`);
    process.exit(1);
  }
  if (allowed.has(entry.id)) {
    console.error(`npm audit exception ${entry.id} was duplicated; refusing to pass`);
    process.exit(1);
  }
  allowed.set(entry.id, { ...entry, expires: expiry });
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
    !isObject(report) ||
    "error" in report ||
    !isObject(report.metadata) ||
    !isObject(report.metadata.vulnerabilities)
  ) {
    console.error("npm audit report was incomplete or reported an error; refusing to pass");
    process.exit(1);
  }
  const today = Temporal.Now.plainDateISO("UTC");
  const failures = [];
  const referencedExceptions = new Set();
  const advisories = [];
  const visited = new Set();
  const visiting = new Set();
  function collectAdvisories(name) {
    if (visiting.has(name)) {
      failures.push(`${name}: cyclic npm audit via reference; refusing to pass`);
      return;
    }
    if (visited.has(name)) return;
    if (!Object.hasOwn(report.vulnerabilities, name)) {
      failures.push(`${name}: missing npm audit via reference; refusing to pass`);
      return;
    }
    const vulnerability = report.vulnerabilities[name];
    if (
      vulnerability === null ||
      typeof vulnerability !== "object" ||
      Array.isArray(vulnerability) ||
      !Array.isArray(vulnerability.via) ||
      vulnerability.via.length === 0
    ) {
      failures.push(`${name}: vulnerable with no advisory metadata; refusing to pass`);
      return;
    }
    visiting.add(name);
    for (const via of vulnerability.via) {
      if (typeof via === "string") {
        collectAdvisories(via);
      } else if (
        via !== null &&
        typeof via === "object" &&
        !Array.isArray(via) &&
        typeof via.url === "string" &&
        via.url.trim() !== "" &&
        typeof via.title === "string" &&
        via.title.trim() !== "" &&
        severities.has(via.severity)
      ) {
        // Bind exceptions to the package carrying the advisory, not its dependants.
        advisories.push({ name, advisory: via });
      } else {
        failures.push(`${name}: invalid npm audit advisory metadata; refusing to pass`);
      }
    }
    visiting.delete(name);
    visited.add(name);
  }
  if (typeof report.auditReportVersion === "number" && isObject(report.vulnerabilities)) {
    for (const name of Object.keys(report.vulnerabilities)) {
      collectAdvisories(name);
    }
  } else if (
    // pnpm 12 uses the legacy advisory map. Keep the unfiltered report so
    // pnpm's persistent ignore settings cannot hide findings from this gate.
    Object.keys(report).every((key) => key === "advisories" || key === "metadata") &&
    isObject(report.advisories)
  ) {
    const counts = Object.fromEntries([...severities].map((severity) => [severity, 0]));
    for (const [key, advisory] of Object.entries(report.advisories)) {
      if (
        !isObject(advisory) ||
        !Number.isSafeInteger(advisory.id) ||
        String(advisory.id) !== key ||
        typeof advisory.module_name !== "string" ||
        advisory.module_name.trim() === "" ||
        typeof advisory.title !== "string" ||
        advisory.title.trim() === "" ||
        !severities.has(advisory.severity) ||
        typeof advisory.github_advisory_id !== "string" ||
        !/^GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$/.test(advisory.github_advisory_id) ||
        advisory.url !== `https://github.com/advisories/${advisory.github_advisory_id}`
      ) {
        failures.push(`${key}: invalid pnpm audit advisory metadata; refusing to pass`);
        continue;
      }
      counts[advisory.severity] += 1;
      advisories.push({ name: advisory.module_name, advisory });
    }
    for (const severity of severities) {
      if (
        !Number.isSafeInteger(report.metadata.vulnerabilities[severity]) ||
        report.metadata.vulnerabilities[severity] !== counts[severity]
      ) {
        failures.push(`pnpm audit ${severity} count did not match advisories; refusing to pass`);
      }
    }
  } else {
    failures.push("npm audit report was incomplete or reported an error; refusing to pass");
  }
  for (const { name, advisory } of advisories) {
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
    } else if (Temporal.PlainDate.compare(exception.expires, today) <= 0) {
      referencedExceptions.add(id);
      failures.push(`${name}: ${id} exception expired on ${exception.expires}`);
    } else {
      referencedExceptions.add(id);
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
