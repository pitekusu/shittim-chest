import affectionRankingsResponseValidator from "../generated/affection-rankings-response-validator.mjs";
import { RecordsApiError, requestJson } from "./http";
import type { AffectionRankingsResponse, ParticipantSlot } from "./types";

export const AFFECTION_RANKINGS_PAGE_LIMIT = 50;

const PARTICIPANTS: readonly ParticipantSlot[] = [
  "participant-a",
  "participant-b",
  "participant-c",
];

function hasConsistentRankingPage(response: AffectionRankingsResponse): boolean {
  if (response.rankings.length !== PARTICIPANTS.length) return false;
  return response.rankings.every((ranking, index) => {
    if (ranking.participant !== PARTICIPANTS[index]) return false;
    let previousScore: number | undefined;
    let previousRank: number | undefined;
    for (const entry of ranking.entries) {
      if (previousScore !== undefined && previousRank !== undefined) {
        if (entry.score > previousScore) return false;
        if (entry.score === previousScore && entry.rank !== previousRank) return false;
        if (entry.score < previousScore && entry.rank <= previousRank) return false;
      }
      previousScore = entry.score;
      previousRank = entry.rank;
    }
    return true;
  });
}

function isAffectionRankingsResponse(value: unknown): value is AffectionRankingsResponse {
  return (
    affectionRankingsResponseValidator(value) &&
    hasConsistentRankingPage(value as AffectionRankingsResponse)
  );
}

function invalidMergedResponse(): never {
  throw new RecordsApiError(
    200,
    "INVALID_API_RESPONSE",
    "サーバーから不正な応答を受信しました。",
    "local-validation",
  );
}

function pageSignature(page: AffectionRankingsResponse): string {
  return JSON.stringify(page.rankings.map((ranking) => ranking.entries));
}

export function mergeAffectionRankingPages(
  pages: readonly AffectionRankingsResponse[],
  pageParams: readonly unknown[] = [],
): AffectionRankingsResponse {
  const first = pages[0];
  if (first === undefined) invalidMergedResponse();
  if (pageParams.length !== 0 && pageParams.length !== pages.length) invalidMergedResponse();

  const seenPageParams = new Set<string>();
  for (const pageParam of pageParams) {
    if (pageParam === undefined) continue;
    if (typeof pageParam !== "string" || seenPageParams.has(pageParam)) invalidMergedResponse();
    seenPageParams.add(pageParam);
  }

  const seenPages = new Set<string>();
  for (const page of pages) {
    if (
      !hasConsistentRankingPage(page) ||
      page.schemaVersion !== first.schemaVersion ||
      page.generatedAt !== first.generatedAt ||
      page.defaultScore !== first.defaultScore ||
      page.maxScore !== first.maxScore ||
      page.rankings.some(
        (ranking, index) =>
          ranking.participant !== first.rankings[index]?.participant ||
          ranking.displayName !== first.rankings[index]?.displayName,
      )
    ) {
      invalidMergedResponse();
    }
    const signature = pageSignature(page);
    const hasEntries = page.rankings.some((ranking) => ranking.entries.length > 0);
    if (hasEntries && seenPages.has(signature)) invalidMergedResponse();
    seenPages.add(signature);
  }

  const rankings = first.rankings.map((ranking, participantIndex) => {
    const entries = pages.flatMap((page) => page.rankings[participantIndex]?.entries ?? []);
    let previousScore: number | undefined;
    let previousRank: number | undefined;
    for (const [entryIndex, entry] of entries.entries()) {
      const expectedRank = previousScore === entry.score ? previousRank : entryIndex + 1;
      if (
        entry.rank !== expectedRank ||
        (previousScore !== undefined && entry.score > previousScore)
      ) {
        invalidMergedResponse();
      }
      previousScore = entry.score;
      previousRank = entry.rank;
    }
    return { ...ranking, entries };
  });
  const last = pages.at(-1);
  if (last === undefined) invalidMergedResponse();
  return { ...first, rankings, nextCursor: last.nextCursor };
}

export async function getAffectionRankings(cursor?: string): Promise<AffectionRankingsResponse> {
  const query = new URLSearchParams({ limit: String(AFFECTION_RANKINGS_PAGE_LIMIT) });
  if (cursor !== undefined) query.set("cursor", cursor);
  return requestJson(
    `/api/v1/insights/affection-rankings?${query.toString()}`,
    isAffectionRankingsResponse,
  );
}
