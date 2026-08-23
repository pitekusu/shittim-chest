import { hasConsistentRecordInvariants } from "../../../../contracts/records/v1/records-invariants";
import recordDetailResponseValidator from "../generated/record-detail-response-validator.mjs";
import { requestJson } from "./http";
import type { RecordDetailResponse } from "./types";

function isRecordDetailResponse(value: unknown): value is RecordDetailResponse {
  return recordDetailResponseValidator(value) && hasConsistentRecordInvariants(value);
}

export async function getRecord(recordId: string): Promise<RecordDetailResponse> {
  return requestJson(`/api/v1/records/${encodeURIComponent(recordId)}`, isRecordDetailResponse);
}
