import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vite-plus/test";

import { App } from "./App";
import { isRecordsApiResponse } from "./contracts";

function recordDetail() {
  const participants = [
    ["participant-a", "参加者A", "cyan"],
    ["participant-b", "参加者B", "pink"],
    ["participant-c", "参加者C", "lavender"],
  ].map(([slot, displayName, fallbackVariant]) => ({
    slot,
    displayName,
    avatar: {
      kind: "placeholder",
      alt: `${displayName}のアバター`,
      fallbackVariant,
    },
  }));
  return {
    schemaVersion: 1,
    recordId: "record-example",
    completedAt: "2026-08-15T06:00:00Z",
    question: "休日の過ごし方を決める",
    requester: {
      displayName: "依頼者",
      avatar: {
        kind: "placeholder",
        alt: "依頼者のアバター",
        fallbackVariant: "cyan",
      },
    },
    participants,
    initialOpinions: participants.map(({ slot }) => ({
      participant: slot,
      summary: "要約",
      proposal: "初回意見",
    })),
    finalProposals: participants.map(({ slot }) => ({
      participant: slot,
      title: "最終案",
      proposal: "提案",
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

describe("App", () => {
  it("uses the approved product display name", () => {
    render(<App />);

    expect(screen.getByRole("heading", { name: "シッテムの箱 議事録" })).toBeVisible();
    expect(screen.getByText(/完了した議論/)).toBeVisible();
  });

  it("validates API payloads against the generated Python contract", () => {
    expect(
      isRecordsApiResponse({
        schemaVersion: 1,
        authenticated: false,
        user: null,
        csrfToken: null,
      }),
    ).toBe(true);
    expect(isRecordsApiResponse({ authenticated: false })).toBe(false);
    expect(isRecordsApiResponse({ schemaVersion: 1, privateId: "forbidden" })).toBe(false);
  });

  it("rejects malformed date-time values from the API", () => {
    const rankings = {
      schemaVersion: 1,
      wins: [],
      requests: [],
      generatedAt: "2026-08-15T06:00:00Z",
    };

    expect(isRecordsApiResponse(rankings)).toBe(true);
    expect(isRecordsApiResponse({ ...rankings, generatedAt: "not-a-date" })).toBe(false);
  });

  it("rejects incomplete participant collections and conflicting winners", () => {
    const detail = recordDetail();
    expect(isRecordsApiResponse(detail)).toBe(true);

    const duplicateParticipant = structuredClone(detail);
    duplicateParticipant.participants[2].slot = "participant-a";
    expect(isRecordsApiResponse(duplicateParticipant)).toBe(false);

    const duplicateInitialOpinion = structuredClone(detail);
    duplicateInitialOpinion.initialOpinions[2].participant = "participant-a";
    expect(isRecordsApiResponse(duplicateInitialOpinion)).toBe(false);

    const duplicateFinalProposal = structuredClone(detail);
    duplicateFinalProposal.finalProposals[2].participant = "participant-a";
    expect(isRecordsApiResponse(duplicateFinalProposal)).toBe(false);

    const duplicateVoter = structuredClone(detail);
    duplicateVoter.votes[2].voter = "participant-a";
    expect(isRecordsApiResponse(duplicateVoter)).toBe(false);

    const conflictingWinner = structuredClone(detail);
    conflictingWinner.finalDecision.winner = "participant-b";
    expect(isRecordsApiResponse(conflictingWinner)).toBe(false);

    const selfVote = structuredClone(detail);
    selfVote.votes[0].candidate = "participant-a";
    expect(isRecordsApiResponse(selfVote)).toBe(false);

    const whitespaceQuestion = structuredClone(detail);
    whitespaceQuestion.question = " \t ";
    expect(isRecordsApiResponse(whitespaceQuestion)).toBe(false);

    const imageWithoutUrl = structuredClone(detail);
    imageWithoutUrl.requester.avatar.kind = "image";
    expect(isRecordsApiResponse(imageWithoutUrl)).toBe(false);

    const mismatchedBallot = structuredClone(detail);
    mismatchedBallot.result.voteCounts = [
      { participant: "participant-a", count: 0 },
      { participant: "participant-b", count: 0 },
      { participant: "participant-c", count: 3 },
    ];
    mismatchedBallot.result.winner = "participant-c";
    mismatchedBallot.finalDecision.winner = "participant-c";
    expect(isRecordsApiResponse(mismatchedBallot)).toBe(false);

    const listResponse = {
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
    expect(isRecordsApiResponse(listResponse)).toBe(true);

    const incompleteListCounts = structuredClone(listResponse);
    incompleteListCounts.items[0].result.voteCounts[2].participant = "participant-a";
    expect(isRecordsApiResponse(incompleteListCounts)).toBe(false);

    const invalidTieSummary = structuredClone(listResponse);
    invalidTieSummary.items[0].result.voteCounts = [
      { participant: "participant-a", count: 1 },
      { participant: "participant-b", count: 1 },
      { participant: "participant-c", count: 1 },
    ];
    invalidTieSummary.items[0].result.tieBreakApplied = false;
    expect(isRecordsApiResponse(invalidTieSummary)).toBe(false);
  });
});
