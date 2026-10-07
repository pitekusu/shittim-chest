import { expect, it } from "vite-plus/test";

import validateRequest from "../generated/debate-request-response-validator.mjs";
import validateList from "../generated/debate-requests-response-validator.mjs";

const request = {
  requestId: "00000000-0000-4000-8000-00000000000a",
  question: "Fictional debate question",
  status: "publishing",
  phase: "completed",
  createdAt: "2026-10-04T00:00:00Z",
  updatedAt: "2026-10-04T00:01:00Z",
  recordId: null,
  errorCode: null,
};

it("allows result links only after publication and rejects private identity fields", () => {
  expect(validateRequest(request)).toBe(true);
  expect(validateRequest({ ...request, status: "published", recordId: "r".repeat(43) })).toBe(true);
  expect(validateRequest({ ...request, recordId: "r".repeat(43) })).toBe(false);
  expect(validateRequest({ ...request, status: "published" })).toBe(false);
  expect(validateRequest({ ...request, requesterKey: "owner" })).toBe(false);
  expect(validateRequest({ ...request, requestId: "m_internal_operation_key" })).toBe(false);
});

it("keeps owner-list responses to safe request DTOs and an opaque continuation", () => {
  expect(validateList({ items: [request], nextCursor: null })).toBe(true);
  expect(
    validateList({ items: [{ ...request, discordUserId: "private" }], nextCursor: null }),
  ).toBe(false);
});
