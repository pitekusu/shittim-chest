import rankingsResponseValidator from "../generated/rankings-response-validator.mjs";
import { requestJson } from "./http";
import type { RankingsResponse } from "./types";

function isRankingsResponse(value: unknown): value is RankingsResponse {
  return rankingsResponseValidator(value);
}

export async function getRankings(signal?: AbortSignal): Promise<RankingsResponse> {
  return requestJson("/api/v1/insights/rankings", isRankingsResponse, { signal });
}
