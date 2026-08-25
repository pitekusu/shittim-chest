import adminStatusResponseValidator from "../generated/admin-status-response-validator.mjs";
import { requestJson } from "./http";
import type { AdminStatusResponse } from "./types";

function isAdminStatusResponse(value: unknown): value is AdminStatusResponse {
  return adminStatusResponseValidator(value);
}

export function getAdminStatus(): Promise<AdminStatusResponse> {
  return requestJson("/api/v1/admin/status", isAdminStatusResponse);
}

export function refreshAdminStatus(
  csrfToken: string,
  idempotencyKey: string,
): Promise<AdminStatusResponse> {
  return requestJson("/api/v1/admin/status/refresh", isAdminStatusResponse, {
    method: "POST",
    headers: {
      "X-CSRF-Token": csrfToken,
      "X-Idempotency-Key": idempotencyKey,
    },
  });
}
