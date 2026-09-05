import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import {
  RECORD_ID,
  listResponse,
  placeholder,
  recordDetail,
  response,
} from "../test/recordsTestUtils";

import affectionRankingsResponseValidator from "../generated/affection-rankings-response-validator.mjs";
import { getAffectionRankings, mergeAffectionRankingPages } from "./affectionRankings";
import { getCosts } from "./costs";
import {
  applyAdminPrompts,
  getAdminPrompts,
  getAdminRevision,
  getAdminRevisions,
  getAdminStatus,
  refreshAdminStatus,
  rollbackAdminPrompts,
} from "./admin";
import { getRecord } from "./recordDetail";
import { getRecords } from "./recordList";
import { getRankings } from "./rankings";
import { getSession } from "./session";
import type { AffectionRankingsResponse } from "./types";

function rankingsResponse() {
  return {
    schemaVersion: 1,
    wins: [],
    requests: [],
    generatedAt: "2026-08-22T00:00:00Z",
  };
}

function affectionRankingsResponse() {
  return {
    schemaVersion: 1,
    generatedAt: "2026-08-22T00:05:00Z",
    defaultScore: 500,
    maxScore: 1000,
    rankings: [
      { participant: "participant-a", displayName: "アロナ", entries: [] },
      { participant: "participant-b", displayName: "プラナ", entries: [] },
      { participant: "participant-c", displayName: "安倍晋三AI", entries: [] },
    ],
    nextCursor: null,
  };
}

function affectionRankingsPage(
  rank: number,
  score: number,
  nextCursor: string | null,
): AffectionRankingsResponse {
  const base = affectionRankingsResponse();
  return {
    ...base,
    rankings: base.rankings.map((ranking) => ({
      ...ranking,
      entries: [
        {
          rank,
          displayName: `依頼者${rank}`,
          avatar: placeholder(`依頼者${rank}`, "cyan"),
          score,
          resetCount: rank,
        },
      ],
    })),
    nextCursor,
  } as AffectionRankingsResponse;
}

it("accepts affection ranking entries with or without optional resetCount", () => {
  const withResetCount = affectionRankingsPage(1, 600, null);
  const withoutResetCount = {
    ...withResetCount,
    rankings: withResetCount.rankings.map((ranking) => ({
      ...ranking,
      entries: ranking.entries.map(({ resetCount: _resetCount, ...entry }) => entry),
    })),
  };

  expect(affectionRankingsResponseValidator(withResetCount)).toBe(true);
  expect(affectionRankingsResponseValidator(withoutResetCount)).toBe(true);
});

function costsResponse() {
  return {
    schemaVersion: 1,
    period: "week",
    timeZone: "Asia/Tokyo",
    startDate: "2026-08-17",
    endDate: "2026-08-23",
    currency: "JPY",
    total: "123.456789",
    breakdown: {
      fargate: "10.000000",
      lambda: "2.000000",
      openai: "100.000000",
      otherAws: "11.456789",
    },
    conversion: {
      source: "frankfurter-v2",
      method: "daily-reference-rate",
      baseCurrency: "USD",
      updatedAt: "2026-08-23T12:17:00+09:00",
    },
    updatedAt: "2026-08-23T12:17:00+09:00",
    status: "partial",
  };
}

function adminStatusResponse() {
  return {
    schemaVersion: 1,
    generatedAt: "2026-08-24T03:00:00Z",
    expiresAt: "2026-08-24T03:01:00Z",
    stale: false,
    overall: { state: "healthy", criticalAlarms: 0, warningAlarms: 0, partial: false },
    sections: [
      {
        service: "ecs",
        state: "healthy",
        summary: "Scale-to-Zeroで待機しています。",
        metrics: [{ name: "running", value: 0 }],
      },
    ],
  };
}

function adminPromptsResponse() {
  return {
    schemaVersion: 1,
    mode: "managed",
    activeRevision: `r${"1".repeat(26)}`,
    createdAt: "2026-08-24T03:00:00Z",
    action: "publish",
    prompts: {
      system: "system prompt",
      moderator: "moderator prompt",
      participantA: "arona prompt",
      participantB: "plana prompt",
      participantC: "abe prompt",
    },
  } as const;
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Records API endpoint validation", () => {
  it("accepts the response type assigned to the requested endpoint", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(listResponse()))),
    );

    await expect(getRecords({ sort: "newest" })).resolves.toEqual(listResponse());
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/records?limit=12&sort=newest",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("rejects a different endpoint's otherwise valid response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(rankingsResponse()))),
    );

    await expect(getSession()).rejects.toMatchObject({
      status: 200,
      code: "INVALID_API_RESPONSE",
      requestId: "local-validation",
    });
  });

  it("negotiates the ADMIN session contract while accepting a legacy response", async () => {
    const legacySession = {
      schemaVersion: 1,
      authenticated: false,
      user: null,
      csrfToken: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(legacySession))),
    );

    await expect(getSession()).resolves.toEqual(legacySession);
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/session?contract=admin-v1",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("applies record invariants after record-detail schema validation", async () => {
    const detail = recordDetail();
    const conflictingWinner = {
      ...detail,
      finalDecision: { ...detail.finalDecision, winner: "participant-b" },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(conflictingWinner))),
    );

    await expect(getRecord(RECORD_ID)).rejects.toMatchObject({
      status: 200,
      code: "INVALID_API_RESPONSE",
    });
  });

  it("rejects three participant entries when a slot is duplicated and another is missing", async () => {
    const detail = recordDetail();
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          response({
            ...detail,
            participants: [detail.participants[0], detail.participants[1], detail.participants[0]],
          }),
        ),
      ),
    );

    await expect(getRecord(RECORD_ID)).rejects.toMatchObject({ code: "INVALID_API_RESPONSE" });
  });

  it("rejects unknown fields in an otherwise valid endpoint response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response({ ...recordDetail(), privateId: "forbidden" }))),
    );

    await expect(getRecord(RECORD_ID)).rejects.toMatchObject({
      status: 200,
      code: "INVALID_API_RESPONSE",
      requestId: "local-validation",
    });
  });

  it("validates rankings with only the rankings endpoint schema", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(rankingsResponse()))),
    );

    await expect(getRankings()).resolves.toEqual(rankingsResponse());
  });

  it("validates the separate all-requester affection rankings contract", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(affectionRankingsResponse()))),
    );

    await expect(getAffectionRankings()).resolves.toEqual(affectionRankingsResponse());
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/insights/affection-rankings?limit=50",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("encodes the opaque affection rankings cursor without changing the page limit", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(affectionRankingsResponse()))),
    );

    await getAffectionRankings("cursor+/=");

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/insights/affection-rankings?limit=50&cursor=cursor%2B%2F%3D",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("merges validated affection pages in fixed participant and rank order", () => {
    const merged = mergeAffectionRankingPages(
      [affectionRankingsPage(1, 700, "next"), affectionRankingsPage(2, 600, null)],
      [undefined, "next"],
    );

    expect(merged.nextCursor).toBeNull();
    expect(merged.rankings.map((ranking) => ranking.participant)).toEqual([
      "participant-a",
      "participant-b",
      "participant-c",
    ]);
    expect(merged.rankings.map((ranking) => ranking.entries.map((entry) => entry.rank))).toEqual([
      [1, 2],
      [1, 2],
      [1, 2],
    ]);
  });

  it("rejects duplicate pages, participant mismatches, and invalid cross-page ranks", () => {
    const first = affectionRankingsPage(1, 700, "next");
    const duplicate = { ...first, nextCursor: null };
    const validSecond = affectionRankingsPage(2, 600, null);
    const participantMismatch = {
      ...validSecond,
      rankings: [validSecond.rankings[1]!, validSecond.rankings[0]!, validSecond.rankings[2]!],
    } as AffectionRankingsResponse;

    expect(() => mergeAffectionRankingPages([first, duplicate], [undefined, "next"])).toThrow(
      "サーバーから不正な応答を受信しました。",
    );
    expect(() =>
      mergeAffectionRankingPages([first, participantMismatch], [undefined, "next"]),
    ).toThrow("サーバーから不正な応答を受信しました。");
    expect(() =>
      mergeAffectionRankingPages([first, affectionRankingsPage(1, 600, null)], [undefined, "next"]),
    ).toThrow("サーバーから不正な応答を受信しました。");
  });

  it("rejects private requester identifiers in affection rankings", async () => {
    const rankings = affectionRankingsResponse();
    rankings.rankings[0]!.entries.push({
      rank: 1,
      displayName: "依頼者",
      avatar: placeholder("依頼者", "cyan"),
      score: 500,
      requesterKey: "must-not-pass",
    } as never);
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(rankings))),
    );

    await expect(getAffectionRankings()).rejects.toMatchObject({
      status: 200,
      code: "INVALID_API_RESPONSE",
    });
  });

  it("validates costs with its route-private JPY schema", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(costsResponse()))),
    );

    await expect(getCosts("week")).resolves.toEqual(costsResponse());
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/insights/costs?period=week",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("rejects internal USD ledger fields in a costs response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(response({ ...costsResponse(), amountUsd: "private-ledger-value" })),
      ),
    );

    await expect(getCosts("week")).rejects.toMatchObject({
      status: 200,
      code: "INVALID_API_RESPONSE",
    });
  });

  it("rejects non-canonical JPY precision in a costs response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response({ ...costsResponse(), total: "123.4567890" }))),
    );

    await expect(getCosts("week")).rejects.toMatchObject({
      status: 200,
      code: "INVALID_API_RESPONSE",
    });
  });

  it("requires error responses to match the generated error schema", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          response(
            {
              error: {
                code: "FORBIDDEN",
                message: "Forbidden",
                requestId: "request-id",
                privateId: "must-not-pass",
              },
            },
            403,
          ),
        ),
      ),
    );

    await expect(getSession()).rejects.toMatchObject({
      status: 403,
      code: "INVALID_ERROR_RESPONSE",
      requestId: "local-validation",
    });
  });

  it("rejects private fields in Admin responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(response({ ...adminStatusResponse(), resourceArn: "must-not-pass" })),
      ),
    );

    await expect(getAdminStatus()).rejects.toMatchObject({
      status: 200,
      code: "INVALID_API_RESPONSE",
      requestId: "local-validation",
    });
  });

  it("validates all prompt responses and sends write-boundary headers", async () => {
    const prompts = adminPromptsResponse();
    const revision = {
      revision: `r${"2".repeat(26)}`,
      createdAt: "2026-08-23T03:00:00Z",
      action: "publish",
      baseRevision: null,
      sourceRevision: null,
      checksum: "a".repeat(64),
    } as const;
    const saved = { schemaVersion: 1, revision: `r${"3".repeat(26)}`, state: "saved" } as const;
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(response(prompts))
      .mockResolvedValueOnce(response({ schemaVersion: 1, items: [revision], nextCursor: null }))
      .mockResolvedValueOnce(response({ schemaVersion: 1, ...revision, prompts: prompts.prompts }))
      .mockResolvedValueOnce(response(saved))
      .mockResolvedValueOnce(response(saved));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getAdminPrompts()).resolves.toEqual(prompts);
    await expect(getAdminRevisions()).resolves.toEqual({
      schemaVersion: 1,
      items: [revision],
      nextCursor: null,
    });
    await expect(getAdminRevision(revision.revision)).resolves.toEqual({
      schemaVersion: 1,
      ...revision,
      prompts: prompts.prompts,
    });
    await expect(
      applyAdminPrompts(
        {
          schemaVersion: 1,
          baseRevision: prompts.activeRevision,
          prompts: prompts.prompts,
          systemConfirmation: null,
        },
        "csrf-token",
        "apply-idempotency-key",
      ),
    ).resolves.toEqual(saved);
    await expect(
      rollbackAdminPrompts(
        {
          schemaVersion: 1,
          baseRevision: prompts.activeRevision,
          sourceRevision: revision.revision,
          systemConfirmation: null,
        },
        "csrf-token",
        "rollback-idempotency-key",
      ),
    ).resolves.toEqual(saved);

    const applyHeaders = new Headers(fetchMock.mock.calls[3]?.[1]?.headers);
    const rollbackHeaders = new Headers(fetchMock.mock.calls[4]?.[1]?.headers);
    expect(applyHeaders.get("Content-Type")).toBe("application/json");
    expect(applyHeaders.get("X-CSRF-Token")).toBe("csrf-token");
    expect(applyHeaders.get("X-Idempotency-Key")).toBe("apply-idempotency-key");
    expect(rollbackHeaders.get("X-Idempotency-Key")).toBe("rollback-idempotency-key");
  });

  it.each([429, 503])(
    "retries a throttled Admin GET with status %i exactly once",
    async (status) => {
      vi.useFakeTimers();
      vi.spyOn(Math, "random").mockReturnValue(0);
      const prompts = adminPromptsResponse();
      const fetchMock = vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(
          response(
            {
              error: {
                code: "PROMPT_CONFIGURATION_UNAVAILABLE",
                message: "プロンプト設定を利用できません。",
                requestId: "request-id",
              },
            },
            status,
          ),
        )
        .mockResolvedValueOnce(response(prompts));
      vi.stubGlobal("fetch", fetchMock);

      const request = getAdminPrompts();
      await vi.runAllTimersAsync();

      await expect(request).resolves.toEqual(prompts);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    },
  );

  it("rejects private fields in a prompt response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(response({ ...adminPromptsResponse(), privateUserId: "must-not-pass" })),
      ),
    );

    await expect(getAdminPrompts()).rejects.toMatchObject({
      status: 200,
      code: "INVALID_API_RESPONSE",
      requestId: "local-validation",
    });
  });

  it("rejects inconsistent legacy prompt metadata", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          response({
            ...adminPromptsResponse(),
            mode: "legacy",
            createdAt: null,
            action: null,
          }),
        ),
      ),
    );

    await expect(getAdminPrompts()).rejects.toMatchObject({
      status: 200,
      code: "INVALID_API_RESPONSE",
      requestId: "local-validation",
    });
  });

  it("refreshes Admin status without a request body", async () => {
    const status = adminStatusResponse();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(status))),
    );

    await expect(refreshAdminStatus("csrf-token", "idempotency-key")).resolves.toEqual(status);
    const init = vi.mocked(fetch).mock.calls[0]?.[1];
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeUndefined();
  });

  it("does not automatically retry an Admin write", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      response(
        {
          error: {
            code: "ADMIN_STATUS_UNAVAILABLE",
            message: "稼働状況を取得できません。",
            requestId: "request-id",
          },
        },
        503,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(refreshAdminStatus("csrf-token", "idempotency-key")).rejects.toMatchObject({
      status: 503,
      code: "ADMIN_STATUS_UNAVAILABLE",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
