import adminApplyResponseValidator from "../generated/admin-apply-response-validator.mjs";
import adminPromptsResponseValidator from "../generated/admin-prompts-response-validator.mjs";
import adminRevisionResponseValidator from "../generated/admin-revision-response-validator.mjs";
import adminRevisionsResponseValidator from "../generated/admin-revisions-response-validator.mjs";
import { requestAdminGet } from "./adminGet";
import { requestJson } from "./http";
import type {
  AdminApplyRequest,
  AdminApplyResponse,
  AdminPromptsResponse,
  AdminRevisionResponse,
  AdminRevisionsResponse,
  AdminRollbackRequest,
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

function mutationHeaders(csrfToken: string, idempotencyKey: string): HeadersInit {
  return {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrfToken,
    "X-Idempotency-Key": idempotencyKey,
  };
}

export function getAdminPrompts(signal?: AbortSignal): Promise<AdminPromptsResponse> {
  return requestAdminGet("/api/v1/admin/prompts", isAdminPromptsResponse, signal);
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

export function getAdminRevisions(
  cursor?: string,
  signal?: AbortSignal,
): Promise<AdminRevisionsResponse> {
  const search = new URLSearchParams();
  if (cursor !== undefined) search.set("cursor", cursor);
  const query = search.size > 0 ? `?${search.toString()}` : "";
  return requestAdminGet(
    `/api/v1/admin/prompts/revisions${query}`,
    isAdminRevisionsResponse,
    signal,
  );
}

export function getAdminRevision(
  revision: string,
  signal?: AbortSignal,
): Promise<AdminRevisionResponse> {
  return requestAdminGet(
    `/api/v1/admin/prompts/revisions/${encodeURIComponent(revision)}`,
    isAdminRevisionResponse,
    signal,
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
