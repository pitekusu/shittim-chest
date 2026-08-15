import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";

import recordsApiSchema from "../../../contracts/records/v1/records-api.schema.json";

const ajv = new Ajv2020({ allErrors: true, strict: true });
addFormats(ajv);
const validator = ajv.compile(recordsApiSchema);

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasConsistentRecordDetailWinner(value: unknown): boolean {
  if (!isObject(value) || !("finalDecision" in value)) {
    return true;
  }
  if (!isObject(value.result) || !isObject(value.finalDecision)) {
    return false;
  }
  return value.result.winner === value.finalDecision.winner;
}

export function isRecordsApiResponse(value: unknown): boolean {
  return validator(value) && hasConsistentRecordDetailWinner(value);
}
