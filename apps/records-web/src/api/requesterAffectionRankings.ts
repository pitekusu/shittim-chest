import requesterAffectionRankingsResponseValidator from "../generated/requester-affection-rankings-response-validator.mjs";
import { RecordsApiError, requestJson } from "./http";
import type { RequesterAffectionRankingsResponse } from "./types";

export const REQUESTER_AFFECTION_PAGE_LIMIT = 10;

function isRequesterAffectionRankingsResponse(
  value: unknown,
): value is RequesterAffectionRankingsResponse {
  return (
    requesterAffectionRankingsResponseValidator(value) &&
    (value as RequesterAffectionRankingsResponse).requesters.length <=
      REQUESTER_AFFECTION_PAGE_LIMIT
  );
}

export async function getRequesterAffectionRankings(
  cursor?: string,
  signal?: AbortSignal,
): Promise<RequesterAffectionRankingsResponse> {
  const query = new URLSearchParams({
    view: "requesters",
    limit: String(REQUESTER_AFFECTION_PAGE_LIMIT),
  });
  if (cursor !== undefined) query.set("cursor", cursor);
  const response = await requestJson(
    `/api/v1/insights/affection-rankings?${query.toString()}`,
    isRequesterAffectionRankingsResponse,
    { signal },
  );
  if (cursor !== undefined && response.nextCursor === cursor) {
    throw new RecordsApiError(
      200,
      "INVALID_API_RESPONSE",
      "サーバーから不正な応答を受信しました。",
      "local-validation",
    );
  }
  return response;
}
