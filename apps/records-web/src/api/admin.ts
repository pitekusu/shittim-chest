import adminApplyResponseValidator from "../generated/admin-apply-response-validator.mjs";
import adminPromptsResponseValidator from "../generated/admin-prompts-response-validator.mjs";
import adminRevisionResponseValidator from "../generated/admin-revision-response-validator.mjs";
import adminRevisionsResponseValidator from "../generated/admin-revisions-response-validator.mjs";
import adminStatusResponseValidator from "../generated/admin-status-response-validator.mjs";
import { RecordsApiError, requestJson, type ResponseValidator } from "./http";
import type {
  AdminApplyRequest,
  AdminApplyResponse,
  AdminPromptsResponse,
  AdminRevisionResponse,
  AdminRevisionsResponse,
  AdminRollbackRequest,
  AdminStatusResponse,
} from "./types";

function isAdminPromptsResponse(value: unknown): value is AdminPromptsResponse {
  return adminPromptsResponseValidator(value);
}

function isAdminApplyResponse(value: unknown): value is AdminApplyResponse {
  return adminApplyResponseValidator(value);
}

function isAdminRevisionsResponse(value: unknown): value is AdminRevisionsResponse {
  return adminRevisionsResponseValidator(value);
}

function isAdminRevisionResponse(value: unknown): value is AdminRevisionResponse {
  return adminRevisionResponseValidator(value);
}

function isAdminStatusResponse(value: unknown): value is AdminStatusResponse {
  return adminStatusResponseValidator(value);
}

const ADMIN_GET_RETRYABLE_STATUSES = new Set([429, 503]);
const ADMIN_GET_RETRY_BASE_MS = 250;
const ADMIN_GET_RETRY_JITTER_MS = 250;

function waitForAdminGetRetry(): Promise<void> {
  const delay =
    ADMIN_GET_RETRY_BASE_MS + Math.floor(Math.random() * (ADMIN_GET_RETRY_JITTER_MS + 1));
  return new Promise((resolve) => setTimeout(resolve, delay));
}

async function requestAdminGet<T>(path: string, validate: ResponseValidator<T>): Promise<T> {
  try {
    return await requestJson(path, validate);
  } catch (error) {
    if (!(error instanceof RecordsApiError) || !ADMIN_GET_RETRYABLE_STATUSES.has(error.status)) {
      throw error;
    }
  }
  await waitForAdminGetRetry();
  return requestJson(path, validate);
}

function mutationHeaders(csrfToken: string, idempotencyKey: string): HeadersInit {
  return {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrfToken,
    "X-Idempotency-Key": idempotencyKey,
  };
}

export function getAdminPrompts(): Promise<AdminPromptsResponse> {
  return requestAdminGet("/api/v1/admin/prompts", isAdminPromptsResponse);
}

export function applyAdminPrompts(
  request: AdminApplyRequest,
  csrfToken: string,
  idempotencyKey: string,
): Promise<AdminApplyResponse> {
  return requestJson("/api/v1/admin/prompts/apply", isAdminApplyResponse, {
    method: "POST",
    headers: mutationHeaders(csrfToken, idempotencyKey),
    body: JSON.stringify(request),
  });
}

export function getAdminRevisions(cursor?: string): Promise<AdminRevisionsResponse> {
  const search = new URLSearchParams();
  if (cursor !== undefined) search.set("cursor", cursor);
  const query = search.size > 0 ? `?${search.toString()}` : "";
  return requestAdminGet(`/api/v1/admin/prompts/revisions${query}`, isAdminRevisionsResponse);
}

export function getAdminRevision(revision: string): Promise<AdminRevisionResponse> {
  return requestAdminGet(
    `/api/v1/admin/prompts/revisions/${encodeURIComponent(revision)}`,
    isAdminRevisionResponse,
  );
}

export function rollbackAdminPrompts(
  request: AdminRollbackRequest,
  csrfToken: string,
  idempotencyKey: string,
): Promise<AdminApplyResponse> {
  return requestJson("/api/v1/admin/prompts/rollback", isAdminApplyResponse, {
    method: "POST",
    headers: mutationHeaders(csrfToken, idempotencyKey),
    body: JSON.stringify(request),
  });
}

export function getAdminStatus(): Promise<AdminStatusResponse> {
  return requestAdminGet("/api/v1/admin/status", isAdminStatusResponse);
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
