import { hasConsistentRecordInvariants } from "../../../../contracts/records/v1/records-invariants";
import recordDetailResponseValidator from "../generated/record-detail-response-validator.mjs";
import recordFollowUpsResponseValidator from "../generated/record-follow-ups-response-validator.mjs";
import { requestJson } from "./http";
import type { RecordDetailResponse, RecordFollowUpsResponse } from "./types";

function isRecordDetailResponse(value: unknown): value is RecordDetailResponse {
  return recordDetailResponseValidator(value) && hasConsistentRecordInvariants(value);
}

export async function getRecord(
  recordId: string,
  signal?: AbortSignal,
): Promise<RecordDetailResponse> {
  return requestJson(
    `/api/v1/records/${encodeURIComponent(recordId)}?contract=record-followups-v1`,
    isRecordDetailResponse,
    { signal },
  );
}

export function getRecordFollowUps(
  recordId: string,
  cursor: string | null,
  signal?: AbortSignal,
): Promise<RecordFollowUpsResponse> {
  const query = new URLSearchParams();
  if (cursor !== null) query.set("cursor", cursor);
  return requestJson(
    `/api/v1/records/${encodeURIComponent(recordId)}/follow-ups${query.size ? `?${query.toString()}` : ""}`,
    (value): value is RecordFollowUpsResponse => recordFollowUpsResponseValidator(value),
    { signal },
  );
}
