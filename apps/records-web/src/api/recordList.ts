import { hasConsistentRecordInvariants } from "../../../../contracts/records/v1/records-invariants";
import recordListResponseValidator from "../generated/record-list-response-validator.mjs";
import { requestJson } from "./http";
import type { RecordListFilters, RecordListResponse } from "./types";

function isRecordListResponse(value: unknown): value is RecordListResponse {
  return recordListResponseValidator(value) && hasConsistentRecordInvariants(value);
}

export async function getRecords(filters: RecordListFilters): Promise<RecordListResponse> {
  const query = new URLSearchParams({ limit: "12" });
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") {
      query.set(key, value);
    }
  }
  return requestJson(`/api/v1/records?${query.toString()}`, isRecordListResponse);
}
