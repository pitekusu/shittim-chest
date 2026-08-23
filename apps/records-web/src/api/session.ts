import sessionResponseValidator from "../generated/session-response-validator.mjs";
import { parseErrorResponse, RecordsApiError, requestJson } from "./http";
import type { SessionResponse } from "./types";

function isSessionResponse(value: unknown): value is SessionResponse {
  return sessionResponseValidator(value);
}

export async function getSession(): Promise<SessionResponse> {
  return requestJson("/api/v1/session", isSessionResponse);
}

export async function logout(csrfToken: string): Promise<void> {
  const response = await fetch("/api/v1/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
  });
  if (response.status === 204) {
    return;
  }
  const error = parseErrorResponse(await response.json());
  if (error !== null) {
    throw new RecordsApiError(
      response.status,
      error.error.code,
      error.error.message,
      error.error.requestId,
    );
  }
  throw new RecordsApiError(
    response.status,
    "LOGOUT_FAILED",
    "ログアウトできませんでした。",
    "local-validation",
  );
}
