import memorialMemoryResponseValidator from "../generated/memorial-memory-response-validator.mjs";
import memorialStateResponseValidator from "../generated/memorial-state-response-validator.mjs";
import memorialUploadResponseValidator from "../generated/memorial-upload-response-validator.mjs";
import { RecordsApiError, requestJson } from "./http";
import type {
  MemorialGenerateRequest,
  MemoryResponse,
  MemorialMemorySummary,
  MemorialResetRequest,
  MemorialStateResponse,
  MemorialUploadContentType,
  MemorialUploadRequest,
  UploadResponse,
} from "./types";

export const MEMORIAL_QUERY_KEY = ["memorial"] as const;

const MAX_MEMORIAL_UPLOAD_BYTES = 10 * 1024 * 1024;
const MEMORIAL_UPLOAD_CONTENT_TYPES = new Set<MemorialUploadContentType>([
  "image/jpeg",
  "image/png",
  "image/webp",
]);
const MEMORIAL_GENERATION_RESPONSE_STATES = new Set<MemorialStateResponse["state"]>([
  "queued",
  "generating",
  "ready",
  "failed",
]);

function mutationHeaders(csrfToken: string, idempotencyKey: string): HeadersInit {
  return {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrfToken,
    "X-Idempotency-Key": idempotencyKey,
  };
}

function timestamp(value: string): number | null {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function hasValidMemorySummary(summary: MemorialMemorySummary): boolean {
  const unlockedAt = timestamp(summary.unlockedAt);
  const generatedAt = timestamp(summary.generatedAt);
  return unlockedAt !== null && generatedAt !== null && generatedAt >= unlockedAt;
}

function hasConsistentMemorialState(response: MemorialStateResponse): boolean {
  if (response.cycle !== response.resetCount + 1) return false;

  const hasParticipant = response.unlockedParticipant !== null;
  const hasUnlockedAt = response.unlockedAt !== null;
  if (hasParticipant !== hasUnlockedAt) return false;
  if ((response.state === "locked") === hasParticipant) return false;
  if (response.uploadReady && response.state !== "unlocked") return false;

  let previousCycle = 0;
  for (const memory of response.memories) {
    if (
      memory.cycle <= previousCycle ||
      memory.cycle > response.cycle ||
      !hasValidMemorySummary(memory)
    ) {
      return false;
    }
    previousCycle = memory.cycle;
  }

  const latest = response.memories.at(-1);
  if (response.latestReadyCycle !== (latest?.cycle ?? null)) return false;
  if (response.state !== "ready") {
    return response.latestReadyCycle === null || response.latestReadyCycle < response.cycle;
  }
  if (
    latest === undefined ||
    latest.cycle !== response.cycle ||
    latest.participant !== response.unlockedParticipant ||
    response.unlockedAt === null
  ) {
    return false;
  }
  return latest.unlockedAt === response.unlockedAt;
}

function isMemorialStateResponse(value: unknown): value is MemorialStateResponse {
  return (
    memorialStateResponseValidator(value) &&
    hasConsistentMemorialState(value as MemorialStateResponse)
  );
}

function isMemorialMemoryResponse(
  value: unknown,
  expected: MemorialMemorySummary,
): value is MemoryResponse {
  if (!memorialMemoryResponseValidator(value)) return false;
  const memory = value as MemoryResponse;
  const unlockedAt = timestamp(memory.unlockedAt);
  const generatedAt = timestamp(memory.generatedAt);
  return (
    memory.cycle === expected.cycle &&
    memory.participant === expected.participant &&
    memory.unlockedAt === expected.unlockedAt &&
    memory.generatedAt === expected.generatedAt &&
    unlockedAt !== null &&
    generatedAt !== null &&
    generatedAt >= unlockedAt
  );
}

function isMemorialUploadContentType(value: string): value is MemorialUploadContentType {
  return MEMORIAL_UPLOAD_CONTENT_TYPES.has(value as MemorialUploadContentType);
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const value of bytes) binary += String.fromCharCode(value);
  return btoa(binary);
}

function invalidUpload(message: string): RecordsApiError {
  return new RecordsApiError(400, "REQUEST_INVALID", message, "local-validation");
}

function unavailableUpload(status: number): RecordsApiError {
  return new RecordsApiError(
    status,
    "MEMORIAL_UPLOAD_UNAVAILABLE",
    "画像アップロードを一時的に利用できません。",
    "local-validation",
  );
}

export function getMemorialState(): Promise<MemorialStateResponse> {
  return requestJson("/api/v1/memorial", isMemorialStateResponse);
}

export async function prepareMemorialUpload(
  source: File,
  expectedCycle: number,
  csrfToken: string,
  idempotencyKey: string,
): Promise<UploadResponse> {
  if (
    !isMemorialUploadContentType(source.type) ||
    source.size < 1 ||
    source.size > MAX_MEMORIAL_UPLOAD_BYTES
  ) {
    throw invalidUpload("JPEG、PNG、WebPの10 MiB以下の画像を選択してください。");
  }

  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", await source.arrayBuffer()));
  const request: MemorialUploadRequest = {
    schemaVersion: 1,
    expectedCycle,
    contentType: source.type,
    sizeBytes: source.size,
    sha256: bytesToHex(digest),
  };
  const checksum = bytesToBase64(digest);
  const validate = (value: unknown): value is UploadResponse => {
    if (!memorialUploadResponseValidator(value)) return false;
    const response = value as UploadResponse;
    return (
      response.cycle === expectedCycle &&
      response.fields["Content-Type"] === source.type &&
      response.fields["x-amz-checksum-sha256"] === checksum
    );
  };
  return requestJson("/api/v1/memorial/upload", validate, {
    method: "POST",
    headers: mutationHeaders(csrfToken, idempotencyKey),
    body: JSON.stringify(request),
  });
}

export async function uploadMemorialSource(upload: UploadResponse, source: File): Promise<void> {
  if (!memorialUploadResponseValidator(upload) || upload.fields["Content-Type"] !== source.type) {
    throw invalidUpload("アップロードする画像が予約内容と一致しません。");
  }

  const body = new FormData();
  for (const [name, value] of Object.entries(upload.fields)) {
    if (value !== null && value !== undefined) body.append(name, value);
  }
  body.append("file", source, "memorial-source");

  let response: Response;
  try {
    response = await fetch(upload.uploadUrl, {
      method: upload.method,
      credentials: "omit",
      referrerPolicy: "no-referrer",
      body,
    });
  } catch {
    throw unavailableUpload(0);
  }
  if (response.status !== 204) throw unavailableUpload(response.status);
}

export function queueMemorialGeneration(
  expectedCycle: number,
  confirmation: MemorialGenerateRequest["confirmation"],
  csrfToken: string,
  idempotencyKey: string,
): Promise<MemorialStateResponse> {
  const request: MemorialGenerateRequest = { schemaVersion: 1, expectedCycle, confirmation };
  const validate = (value: unknown): value is MemorialStateResponse =>
    isMemorialStateResponse(value) &&
    value.cycle === expectedCycle &&
    MEMORIAL_GENERATION_RESPONSE_STATES.has(value.state);
  return requestJson("/api/v1/memorial/generate", validate, {
    method: "POST",
    headers: mutationHeaders(csrfToken, idempotencyKey),
    body: JSON.stringify(request),
  });
}

export function getMemorialMemory(summary: MemorialMemorySummary): Promise<MemoryResponse> {
  return requestJson(`/api/v1/memorial/memories/${summary.cycle}`, (value) =>
    isMemorialMemoryResponse(value, summary),
  );
}

export function resetMemorial(
  expectedCycle: number,
  confirmation: MemorialResetRequest["confirmation"],
  csrfToken: string,
  idempotencyKey: string,
): Promise<MemorialStateResponse> {
  const request: MemorialResetRequest = { schemaVersion: 1, expectedCycle, confirmation };
  const validate = (value: unknown): value is MemorialStateResponse =>
    isMemorialStateResponse(value) && value.cycle === expectedCycle + 1 && value.state === "locked";
  return requestJson("/api/v1/memorial/reset", validate, {
    method: "POST",
    headers: mutationHeaders(csrfToken, idempotencyKey),
    body: JSON.stringify(request),
  });
}
