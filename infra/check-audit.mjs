// Fail-closed npm audit gate with a dated exception allowlist.
// Reads `npm audit --json` from stdin and exits 1 on any advisory that is not
// covered by an unexpired entry in npm-audit-exceptions.json.
import { readFileSync } from "node:fs";

const exceptionsPath = new URL("./npm-audit-exceptions.json", import.meta.url);
const exceptions = JSON.parse(readFileSync(exceptionsPath, "utf8"));
const allowed = new Map(exceptions.map((entry) => [entry.id, entry]));

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
      } else if (exception.expires <= today) {
        failures.push(`${name}: ${id} exception expired on ${exception.expires}`);
      }
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
