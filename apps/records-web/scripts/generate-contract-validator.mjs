import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import Ajv2020 from "ajv/dist/2020.js";
import standaloneCode from "ajv/dist/standalone/index.js";
import addFormats from "ajv-formats";

const schemaUrl = new URL("../../../contracts/records/v1/records-api.schema.json", import.meta.url);
const generatedUrl = new URL("../src/generated/records-api-validator.mjs", import.meta.url);

export function generateValidatorSource() {
  const schema = JSON.parse(readFileSync(schemaUrl, "utf8"));
  const ajv = new Ajv2020({
    allErrors: true,
    code: { esm: true, source: true },
    strict: true,
  });
  addFormats(ajv);
  const validator = ajv.compile(schema);
  return `${standaloneCode(ajv, validator).trimEnd()}\n`;
}

export function assertGeneratedValidatorCurrent() {
  const expected = generateValidatorSource();
  let actual;
  try {
    actual = readFileSync(generatedUrl, "utf8");
  } catch {
    throw new Error("Generated Records API validator is missing; run pnpm run contracts:generate");
  }
  if (actual !== expected) {
    throw new Error("Generated Records API validator is stale; run pnpm run contracts:generate");
  }
}

const mode = process.argv[2];
if (mode === "--write") {
  writeFileSync(generatedUrl, generateValidatorSource(), "utf8");
} else if (mode === "--check") {
  assertGeneratedValidatorCurrent();
} else if (process.argv[1] === fileURLToPath(import.meta.url)) {
  throw new Error("Expected --write or --check");
}
