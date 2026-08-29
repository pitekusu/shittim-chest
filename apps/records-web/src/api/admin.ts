import adminApplyResponseValidator from "../generated/admin-apply-response-validator.mjs";
import adminPromptsResponseValidator from "../generated/admin-prompts-response-validator.mjs";
import adminRevisionResponseValidator from "../generated/admin-revision-response-validator.mjs";
import adminRevisionsResponseValidator from "../generated/admin-revisions-response-validator.mjs";
import adminStatusResponseValidator from "../generated/admin-status-response-validator.mjs";
import { requestJson } from "./http";
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

function mutationHeaders(csrfToken: string, idempotencyKey: string): HeadersInit {
  return {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrfToken,
    "X-Idempotency-Key": idempotencyKey,
  };
}

export function getAdminPrompts(): Promise<AdminPromptsResponse> {
  return requestJson("/api/v1/admin/prompts", isAdminPromptsResponse);
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
  return requestJson(`/api/v1/admin/prompts/revisions${query}`, isAdminRevisionsResponse);
}

export function getAdminRevision(revision: string): Promise<AdminRevisionResponse> {
  return requestJson(
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
