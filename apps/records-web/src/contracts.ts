import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";

import { hasConsistentRecordInvariants } from "../../../contracts/records/v1/records-invariants";
import recordsApiSchema from "../../../contracts/records/v1/records-api.schema.json";

const ajv = new Ajv2020({ allErrors: true, strict: true });
addFormats(ajv);
const validator = ajv.compile(recordsApiSchema);

export function isRecordsApiResponse(value: unknown): boolean {
  return validator(value) && hasConsistentRecordInvariants(value);
}
