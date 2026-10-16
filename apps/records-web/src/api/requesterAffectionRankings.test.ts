import { afterEach, expect, it, vi } from "vite-plus/test";

import { affectionRankingsResponse, placeholder, response } from "../test/recordsTestUtils";
import { getRequesterAffectionRankings } from "./requesterAffectionRankings";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function requesterResponse() {
  const source = affectionRankingsResponse();
  return {
    schemaVersion: 1,
    generatedAt: source.generatedAt,
    defaultScore: 500,
    maxScore: 1000,
    requesters: [
      {
        displayName: "先生",
        avatar: placeholder("先生", "cyan"),
        participants: source.rankings.map((ranking) => ({
          participant: ranking.participant,
          displayName: ranking.displayName,
          score: 500,
          resetCount: 0,
        })),
      },
    ],
    nextCursor: null,
  };
}

it("requests ten grouped requesters and encodes the view-specific cursor", async () => {
  const page = requesterResponse();
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(response(page))),
  );
  await expect(getRequesterAffectionRankings("cursor+/=")).resolves.toEqual(page);
  expect(fetch).toHaveBeenCalledWith(
    "/api/v1/insights/affection-rankings?view=requesters&limit=10&cursor=cursor%2B%2F%3D",
    expect.objectContaining({ credentials: "same-origin" }),
  );
});

it("rejects a cursor that does not advance and pages exceeding the requested ten people", async () => {
  const page = requesterResponse();
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(response({ ...page, nextCursor: "same" }))),
  );
  await expect(getRequesterAffectionRankings("same")).rejects.toMatchObject({
    code: "INVALID_API_RESPONSE",
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(response({ ...page, requesters: Array(11).fill(page.requesters[0]) })),
    ),
  );
  await expect(getRequesterAffectionRankings()).rejects.toMatchObject({
    code: "INVALID_API_RESPONSE",
  });
});

it.each(["duplicate-participant", "missing-participant", "private-key", "invalid-score"])(
  "rejects malformed requester grouping: %s",
  async (kind) => {
    const page = requesterResponse();
    const requester = page.requesters[0]!;
    if (kind === "duplicate-participant") requester.participants[1] = requester.participants[0]!;
    if (kind === "missing-participant") requester.participants.pop();
    if (kind === "private-key") Object.assign(requester, { requesterKey: "must-not-pass" });
    if (kind === "invalid-score") requester.participants[0]!.score = 1001;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(page))),
    );
    await expect(getRequesterAffectionRankings()).rejects.toMatchObject({
      code: "INVALID_API_RESPONSE",
    });
  },
);
