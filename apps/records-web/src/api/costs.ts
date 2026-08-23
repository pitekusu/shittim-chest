import costsResponseValidator from "../generated/costs-response-validator.mjs";
import { requestJson } from "./http";
import type { CostPeriod, CostsResponse } from "./types";

function isCostsResponse(value: unknown): value is CostsResponse {
  return costsResponseValidator(value);
}

export async function getCosts(period: CostPeriod): Promise<CostsResponse> {
  const query = new URLSearchParams({ period });
  return requestJson(`/api/v1/insights/costs?${query.toString()}`, isCostsResponse);
}
