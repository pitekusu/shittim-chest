import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vite-plus/test";

import type { AvatarRef, SessionResponse } from "../api/types";

export const RECORD_ID = "r".repeat(43);

export function placeholder(
  displayName: string,
  fallbackVariant: AvatarRef["fallbackVariant"],
): AvatarRef {
  return {
    kind: "placeholder",
    url: null,
    alt: `${displayName}のアバター`,
    fallbackVariant,
  };
}

export function recordDetail() {
  const participants = [
    ["participant-a", "アロナ", "cyan"],
    ["participant-b", "プラナ", "pink"],
    ["participant-c", "安倍晋三AI", "lavender"],
  ].map(([slot, displayName, fallbackVariant]) => ({
    slot,
    displayName,
    avatar: placeholder(displayName!, fallbackVariant as AvatarRef["fallbackVariant"]),
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

export function authenticatedSession(): SessionResponse & { authenticated: true } {
  return {
    schemaVersion: 1,
    authenticated: true,
    user: { displayName: "閲覧者", avatar: placeholder("閲覧者", "cyan") },
    csrfToken: "csrf-token",
  };
}

export function listResponse() {
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

export function rankingsResponse() {
  return {
    schemaVersion: 1,
    wins: [
      { rank: 1, displayName: "アロナ", avatar: placeholder("アロナ", "cyan"), count: 20 },
      { rank: 2, displayName: "プラナ", avatar: placeholder("プラナ", "pink"), count: 18 },
      {
        rank: 3,
        displayName: "安倍晋三AI",
        avatar: placeholder("安倍晋三AI", "lavender"),
        count: 16,
      },
    ],
    requests: [
      {
        rank: 1,
        displayName: "パワー系ウナギ",
        avatar: placeholder("パワー系ウナギ", "cyan"),
        count: 12,
      },
      {
        rank: 1,
        displayName: "吹雪型JC",
        avatar: placeholder("吹雪型JC", "pink"),
        count: 12,
      },
      {
        rank: 3,
        displayName: "先生",
        avatar: placeholder("先生", "lavender"),
        count: 8,
      },
    ],
    generatedAt: "2026-08-22T00:00:00Z",
  };
}

export function response(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function mockApi(
  session: SessionResponse = authenticatedSession(),
  rankings: unknown = rankingsResponse(),
) {
  const requests: string[] = [];
  let currentSession = session;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const path =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      requests.push(path);
      if (path === "/api/v1/session") return Promise.resolve(response(currentSession));
      if (path === "/api/v1/logout") {
        currentSession = {
          schemaVersion: 1,
          authenticated: false,
          user: null,
          csrfToken: null,
        };
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      if (path.startsWith("/api/v1/records?")) return Promise.resolve(response(listResponse()));
      if (path === `/api/v1/records/${RECORD_ID}`) {
        return Promise.resolve(response(recordDetail()));
      }
      if (path === "/api/v1/insights/rankings") {
        return Promise.resolve(response(rankings));
      }
      throw new Error(`Unexpected request: ${path}`);
    }),
  );
  return requests;
}

export function renderRoute(
  element: ReactElement,
  {
    initialEntry = "/",
    path = "/",
  }: { readonly initialEntry?: string; readonly path?: string } = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
  });
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route path={path} element={element} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}
