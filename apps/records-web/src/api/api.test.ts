import { afterEach, describe, expect, it, vi } from "vite-plus/test";

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

const RECORD_ID = "r".repeat(43);

function placeholder(displayName: string, fallbackVariant: "cyan" | "pink" | "lavender") {
  return {
    kind: "placeholder",
    url: null,
    alt: `${displayName}のアバター`,
    fallbackVariant,
  };
}

function recordDetail() {
  const participants = [
    ["participant-a", "アロナ", "cyan"],
    ["participant-b", "プラナ", "pink"],
    ["participant-c", "安倍晋三AI", "lavender"],
  ].map(([slot, displayName, fallbackVariant]) => ({
    slot,
    displayName,
    avatar: placeholder(displayName!, fallbackVariant as "cyan" | "pink" | "lavender"),
  }));
  return {
    schemaVersion: 1,
    recordId: RECORD_ID,
    completedAt: "2026-08-15T06:00:00Z",
    question: "休日の過ごし方を決める",
    requester: { displayName: "依頼者", avatar: placeholder("依頼者", "cyan") },
    participants,
    initialOpinions: participants.map(({ slot }) => ({
      participant: slot,
      summary: "要約",
      proposal: "初回意見",
    })),
    finalProposals: participants.map(({ slot }) => ({
      participant: slot,
      title: "最終案",
      proposal: "完成した提案",
    })),
    votes: [
      { voter: "participant-a", candidate: "participant-b", reason: "理由A" },
      { voter: "participant-b", candidate: "participant-a", reason: "理由B" },
      { voter: "participant-c", candidate: "participant-a", reason: "理由C" },
    ],
    result: {
      winner: "participant-a",
      voteCounts: [
        { participant: "participant-a", count: 2 },
        { participant: "participant-b", count: 1 },
        { participant: "participant-c", count: 0 },
      ],
      tieBreakApplied: false,
    },
    finalDecision: {
      winner: "participant-a",
      victoryMessage: "勝利しました",
      decision: "最終決定",
      actions: ["実行する"],
      caveats: ["注意する"],
    },
  };
}

function listResponse() {
  const detail = recordDetail();
  return {
    schemaVersion: 1,
    items: [
      {
        schemaVersion: 1,
        recordId: detail.recordId,
        completedAt: detail.completedAt,
        questionPreview: detail.question,
        requester: detail.requester,
        participants: detail.participants,
        result: detail.result,
      },
    ],
    nextCursor: null,
  };
}

function rankingsResponse() {
  return {
    schemaVersion: 1,
    wins: [],
    requests: [],
    generatedAt: "2026-08-22T00:00:00Z",
  };
}

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

function response(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
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
    const conflictingWinner = recordDetail();
    conflictingWinner.finalDecision.winner = "participant-b";
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(conflictingWinner))),
    );

    await expect(getRecord(RECORD_ID)).rejects.toMatchObject({
      status: 200,
      code: "INVALID_API_RESPONSE",
    });
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
});
