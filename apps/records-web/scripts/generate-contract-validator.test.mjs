// @vitest-environment node

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import { expect, test } from "vite-plus/test";

import {
  affectionRankingsResponse,
  authenticatedSession,
  costsResponse,
  listResponse,
  rankingsResponse,
  recordDetail,
  RECORD_ID,
} from "../src/test/recordsTestUtils";

const date = "2026-10-07T00:00:00Z";
const detail = recordDetail();
const prompts = {
  system: "test",
  moderator: "test",
  participantA: "test",
  participantB: "test",
  participantC: "test",
};
const revision = {
  revision: `r${"1".repeat(26)}`,
  createdAt: date,
  action: "publish",
  baseRevision: null,
  sourceRevision: null,
  checksum: "0".repeat(64),
};
const week = { weekId: "2026-10-04", periodStart: date, periodEnd: date, publishAt: date };
const room = { roomId: RECORD_ID, requester: detail.requester, questionCount: 1, state: "ready" };
const queuedRequest = {
  requestId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  question: "test",
  status: "queued",
  phase: null,
  createdAt: date,
  updatedAt: date,
  recordId: null,
  errorCode: null,
};
const publishedRequest = { ...queuedRequest, status: "published", recordId: RECORD_ID };
/** @type {[string, string, object][]} */
const samples = [
  ["ErrorResponse", "error", { error: { code: "TEST", message: "test", requestId: "test" } }],
  ["SessionResponse", "session", authenticatedSession()],
  ["RecordListResponse", "record-list", listResponse()],
  [
    "RecordSyncIndexResponse",
    "record-sync-index",
    {
      schemaVersion: 1,
      items: [{ recordId: RECORD_ID, revision: RECORD_ID, avatarRevision: RECORD_ID }],
      nextCursor: null,
    },
  ],
  ["RecordDetailResponse", "record-detail", detail],
  ["RankingsResponse", "rankings", rankingsResponse()],
  ["AffectionRankingsResponse", "affection-rankings", affectionRankingsResponse()],
  ["CostsResponse", "costs", costsResponse()],
  [
    "AdminStatusResponse",
    "admin-status",
    {
      schemaVersion: 1,
      generatedAt: date,
      expiresAt: date,
      stale: false,
      overall: {
        state: "healthy",
        criticalAlarms: 0,
        warningAlarms: 0,
        partial: false,
        activeAlarms: [],
      },
      sections: [],
    },
  ],
  [
    "AdminPromptsResponse",
    "admin-prompts",
    {
      schemaVersion: 1,
      mode: "managed",
      activeRevision: revision.revision,
      createdAt: date,
      action: "publish",
      prompts,
    },
  ],
  [
    "AdminPromptApplyResponse",
    "admin-apply",
    { schemaVersion: 1, revision: revision.revision, state: "saved" },
  ],
  [
    "AdminPromptRevisionsResponse",
    "admin-revisions",
    { schemaVersion: 1, items: [revision], nextCursor: null },
  ],
  ["AdminPromptRevisionResponse", "admin-revision", { schemaVersion: 1, ...revision, prompts }],
  [
    "MemorialStateResponse",
    "memorial-state",
    {
      schemaVersion: 1,
      state: "locked",
      cycle: 1,
      resetCount: 0,
      unlockedParticipant: null,
      unlockedAt: null,
      uploadReady: false,
      latestReadyCycle: null,
      memories: [],
    },
  ],
  [
    "MemorialUploadResponse",
    "memorial-upload",
    {
      schemaVersion: 1,
      cycle: 1,
      method: "POST",
      uploadUrl: "https://example.test/upload",
      expiresAt: date,
      fields: {
        key: "test",
        "Content-Type": "image/png",
        "x-amz-checksum-sha256": `${"A".repeat(43)}=`,
        "x-amz-algorithm": "AWS4-HMAC-SHA256",
        "x-amz-credential": "test",
        "x-amz-date": "20261007T000000Z",
        policy: "dGVzdA==",
        "x-amz-signature": "0".repeat(64),
      },
    },
  ],
  [
    "MemorialMemoryResponse",
    "memorial-memory",
    {
      schemaVersion: 1,
      cycle: 1,
      participant: "participant-a",
      unlockedAt: date,
      generatedAt: date,
      image: { url: "https://example.test/image", width: 1920, height: 1080, alt: "test" },
      narrative: "test",
    },
  ],
  [
    "MomotalkWeeksResponse",
    "momotalk-weeks",
    { schemaVersion: 1, weeks: [week], nextCursor: null },
  ],
  [
    "MomotalkRoomsResponse",
    "momotalk-rooms",
    { schemaVersion: 1, week, rooms: [room], nextCursor: null },
  ],
  [
    "MomotalkRoomResponse",
    "momotalk-room",
    {
      schemaVersion: 1,
      week,
      room,
      participants: detail.participants,
      messages: [{ id: 1, participant: "participant-a", text: "test" }],
      images: [],
    },
  ],
  ["DebateRequestResponse", "debate-request", queuedRequest],
  ["DebateRequestResponse", "debate-request", publishedRequest],
  [
    "DebateRequestsResponse",
    "debate-requests",
    { items: [queuedRequest, publishedRequest], nextCursor: null },
  ],
];

const previous = new Ajv2020({ allErrors: true, strict: true });
addFormats(previous);
previous.addSchema(
  JSON.parse(readFileSync(resolve("../../contracts/records/v1/records-api.schema.json"), "utf8")),
  "records-api",
);

function malformedVariants(sample) {
  const variants = [null, [], "test", { ...sample, privateUserId: "synthetic-test-only" }];
  function visit(value, path) {
    if (value === null || typeof value !== "object") return;
    for (const [key, item] of Object.entries(value)) {
      for (const replacement of [undefined, null, "invalid-value", -1, {}, []]) {
        const copy = structuredClone(sample);
        let parent = copy;
        for (const segment of path) parent = parent[segment];
        if (replacement === undefined) delete parent[key];
        else parent[key] = replacement;
        variants.push(copy);
      }
      visit(item, [...path, key]);
    }
  }
  visit(sample, []);
  return variants;
}

test.each(samples)(
  "fail-fast %s retains acceptance and malformed-input rejection",
  async (definition, filename, sample) => {
    const before = previous.compile({ $ref: `records-api#/$defs/${definition}` });
    const { default: after } = await import(`../src/generated/${filename}-response-validator.mjs`);
    expect(before(sample)).toBe(true);
    expect(after(sample)).toBe(true);
    for (const variant of malformedVariants(sample)) {
      expect(after(variant)).toBe(before(variant));
    }
  },
);

test("retains the request status and published-record consistency constraint", async () => {
  const beforeRequest = previous.compile({ $ref: "records-api#/$defs/DebateRequestResponse" });
  const beforeRequests = previous.compile({ $ref: "records-api#/$defs/DebateRequestsResponse" });
  const { default: afterRequest } =
    await import("../src/generated/debate-request-response-validator.mjs");
  const { default: afterRequests } =
    await import("../src/generated/debate-requests-response-validator.mjs");
  for (const inconsistent of [
    { ...queuedRequest, recordId: RECORD_ID },
    { ...publishedRequest, recordId: null },
  ]) {
    expect(beforeRequest(inconsistent)).toBe(false);
    expect(afterRequest(inconsistent)).toBe(false);
    const list = { items: [inconsistent], nextCursor: null };
    expect(beforeRequests(list)).toBe(false);
    expect(afterRequests(list)).toBe(false);
  }
});
