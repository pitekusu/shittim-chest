import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { App } from "./App";
import type { SessionResponse } from "./api";
import { isRecordsApiResponse } from "./contracts";

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

function authenticatedSession() {
  return {
    schemaVersion: 1,
    authenticated: true,
    user: { displayName: "閲覧者", avatar: placeholder("閲覧者", "cyan") },
    csrfToken: "csrf-token",
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

function response(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockApi(session: SessionResponse = authenticatedSession() as SessionResponse) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const path =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (path === "/api/v1/session") return Promise.resolve(response(session));
      if (path.startsWith("/api/v1/records?")) return Promise.resolve(response(listResponse()));
      if (path === `/api/v1/records/${RECORD_ID}`) {
        return Promise.resolve(response(recordDetail()));
      }
      throw new Error(`Unexpected request: ${path}`);
    }),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});

describe("App", () => {
  it("shows the approved login page for an anonymous Guild visitor", async () => {
    mockApi({ schemaVersion: 1, authenticated: false, user: null, csrfToken: null });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "シッテムの箱 議事録" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Discordでログイン" })).toHaveAttribute(
      "href",
      "/api/v1/auth/discord/start?returnTo=%2F",
    );
    expect(screen.getByText(/Guildのメンバーだけ/)).toBeVisible();
  });

  it("renders completed records without duration or Evidence", async () => {
    mockApi();

    render(<App />);

    expect(await screen.findByRole("heading", { name: "議論の記録" })).toBeVisible();
    const card = await screen.findByRole("article");
    expect(within(card).getByText("休日の過ごし方を決める")).toBeVisible();
    expect(within(card).getAllByText("依頼者")).toHaveLength(2);
    expect(within(card).getByText("アロナ")).toBeVisible();
    expect(screen.queryByText(/所要時間|Evidence|外部根拠/)).not.toBeInTheDocument();
  });

  it("renders named votes and only the saved final result on detail", async () => {
    window.history.replaceState(null, "", `/records/${RECORD_ID}`);
    mockApi();

    render(<App />);

    expect(await screen.findByRole("heading", { name: "休日の過ごし方を決める" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "3人の意見" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "投票" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "アロナ → プラナ" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "最終決定" })).toBeVisible();
    expect(screen.getByText("勝利しました")).toBeVisible();
    expect(screen.getByText("実行する")).toBeVisible();
  });

  it("validates API payloads against the generated Python contract", () => {
    expect(isRecordsApiResponse(recordDetail())).toBe(true);
    expect(isRecordsApiResponse({ authenticated: false })).toBe(false);
    expect(isRecordsApiResponse({ schemaVersion: 1, privateId: "forbidden" })).toBe(false);

    const conflictingWinner = structuredClone(recordDetail());
    conflictingWinner.finalDecision.winner = "participant-b";
    expect(isRecordsApiResponse(conflictingWinner)).toBe(false);
  });
});
