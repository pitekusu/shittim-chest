import { hasConsistentRecordInvariants } from "../../../../contracts/records/v1/records-invariants";
import recordListResponseValidator from "../generated/record-list-response-validator.mjs";
import recordRequestersResponseValidator from "../generated/record-requesters-response-validator.mjs";
import { requestJson } from "./http";
import type {
  RecordListFilters,
  RecordListResponse,
  RecordRequestersResponse,
  RecordSearchFilters,
} from "./types";

function isRecordListResponse(value: unknown): value is RecordListResponse {
  return recordListResponseValidator(value) && hasConsistentRecordInvariants(value);
}

export function searchRecords(
  filters: RecordSearchFilters,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<RecordListResponse> {
  return requestJson("/api/v1/records/search", isRecordListResponse, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify({ ...filters, limit: 12 }),
    signal,
  });
}

export function getRecordRequesters(
  cursor?: string,
  signal?: AbortSignal,
): Promise<RecordRequestersResponse> {
  const query = new URLSearchParams();
  if (cursor !== undefined) query.set("cursor", cursor);
  return requestJson(
    `/api/v1/records/requesters${query.size ? `?${query.toString()}` : ""}`,
    (value): value is RecordRequestersResponse => recordRequestersResponseValidator(value),
    { signal },
  );
}

export async function getRecords(
  filters: RecordListFilters,
  signal?: AbortSignal,
): Promise<RecordListResponse> {
  const query = new URLSearchParams({ limit: "12" });
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") {
      query.set(key, value);
    }
  }
  return requestJson(`/api/v1/records?${query.toString()}`, isRecordListResponse, { signal });
}
