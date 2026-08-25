import { readFileSync, readdirSync, unlinkSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import Ajv2020 from "ajv/dist/2020.js";
import standaloneCode from "ajv/dist/standalone/index.js";
import addFormats from "ajv-formats";

const schemaUrl = new URL("../../../contracts/records/v1/records-api.schema.json", import.meta.url);
const generatedDirectoryUrl = new URL("../src/generated/", import.meta.url);

const validators = [
  ["ErrorResponse", "error-response-validator.mjs"],
  ["SessionResponse", "session-response-validator.mjs"],
  ["RecordListResponse", "record-list-response-validator.mjs"],
  ["RecordDetailResponse", "record-detail-response-validator.mjs"],
  ["RankingsResponse", "rankings-response-validator.mjs"],
  ["CostsResponse", "costs-response-validator.mjs"],
  ["AdminStatusResponse", "admin-status-response-validator.mjs"],
];
const expectedOutputFilenames = new Set(
  validators.flatMap(([, filename]) => [filename, filename.replace(/\.mjs$/, ".d.mts")]),
);

const declarationSource = `interface ValidationError {
  readonly instancePath: string;
  readonly schemaPath: string;
  readonly keyword: string;
  readonly message?: string;
  readonly params: Readonly<Record<string, unknown>>;
}

declare function validate(value: unknown): boolean;

declare namespace validate {
  let errors: readonly ValidationError[] | null;
}

export default validate;
`;

export function generateValidatorSources() {
  const schema = JSON.parse(readFileSync(schemaUrl, "utf8"));
  const ajv = new Ajv2020({
    allErrors: true,
    code: { esm: true, source: true },
    strict: true,
  });
  addFormats(ajv);
  ajv.addSchema(schema, "records-api");
  return new Map(
    validators.map(([definition, filename]) => {
      const validator = ajv.compile({ $ref: `records-api#/$defs/${definition}` });
      return [filename, `${standaloneCode(ajv, validator).trimEnd()}\n`];
    }),
  );
}

export function assertGeneratedValidatorCurrent() {
  const actualFilenames = new Set(readdirSync(generatedDirectoryUrl));
  const missingFilenames = [...expectedOutputFilenames].filter(
    (filename) => !actualFilenames.has(filename),
  );
  const unexpectedFilenames = [...actualFilenames].filter(
    (filename) => !expectedOutputFilenames.has(filename),
  );
  if (missingFilenames.length > 0 || unexpectedFilenames.length > 0) {
    throw new Error(
      `Generated Records API validator file set differs: missing=${missingFilenames.join(",") || "none"} unexpected=${unexpectedFilenames.join(",") || "none"}; run pnpm run contracts:generate`,
    );
  }

  for (const [filename, expected] of generateValidatorSources()) {
    const outputs = [
      [filename, expected],
      [filename.replace(/\.mjs$/, ".d.mts"), declarationSource],
    ];
    for (const [outputFilename, outputExpected] of outputs) {
      const generatedUrl = new URL(outputFilename, generatedDirectoryUrl);
      let actual;
      try {
        actual = readFileSync(generatedUrl, "utf8");
      } catch {
        throw new Error(
          `Generated Records API validator is missing: ${outputFilename}; run pnpm run contracts:generate`,
        );
      }
      if (actual !== outputExpected) {
        throw new Error(
          `Generated Records API validator is stale: ${outputFilename}; run pnpm run contracts:generate`,
        );
      }
    }
  }
}

const mode = process.argv[2];
if (mode === "--write") {
  for (const filename of readdirSync(generatedDirectoryUrl)) {
    if (!expectedOutputFilenames.has(filename)) {
      unlinkSync(new URL(filename, generatedDirectoryUrl));
    }
  }
  for (const [filename, source] of generateValidatorSources()) {
    writeFileSync(new URL(filename, generatedDirectoryUrl), source, "utf8");
    writeFileSync(
      new URL(filename.replace(/\.mjs$/, ".d.mts"), generatedDirectoryUrl),
      declarationSource,
      "utf8",
    );
  }
} else if (mode === "--check") {
  assertGeneratedValidatorCurrent();
} else if (process.argv[1] === fileURLToPath(import.meta.url)) {
  throw new Error("Expected --write or --check");
}
