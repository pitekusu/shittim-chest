import { hasConsistentRecordInvariants } from "../../../contracts/records/v1/records-invariants";
import validator from "./generated/records-api-validator.mjs";

export function isRecordsApiResponse(value: unknown): boolean {
  return validator(value) && hasConsistentRecordInvariants(value);
}
