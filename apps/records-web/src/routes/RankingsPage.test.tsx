import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import {
  affectionRankingsResponse,
  costsResponse,
  rankingsResponse,
  renderRoute,
  response,
} from "../test/recordsTestUtils";
import RankingsPage from "./RankingsPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function requestPath(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
}

function insightResponse(path: string) {
  if (path.startsWith("/api/v1/insights/affection-rankings?")) {
    return affectionRankingsResponse();
  }
  return path.includes("/costs?") ? costsResponse() : rankingsResponse();
}

describe("RankingsPage", () => {
  it("renders rankings and the exact four-part JPY cost dashboard", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = requestPath(input);
        return Promise.resolve(response(insightResponse(path)));
      }),
    );

    renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });

    expect(await screen.findByRole("heading", { name: "いろいろな記録" })).toBeVisible();
    const wins = screen.getByRole("region", { name: "勝利回数ランキング" });
    const requestsPanel = screen.getByRole("region", { name: "依頼回数ランキング" });
    expect(await within(wins).findAllByRole("listitem")).toHaveLength(3);
    expect(within(wins).getByText("安倍晋三AI")).toBeVisible();
    expect(
      within(wins).getByRole("status", { name: "勝利回数ランキングの合計" }),
    ).toHaveTextContent("合計54回");
    expect(
      within(requestsPanel).getByRole("status", { name: "依頼回数ランキングの上位合計" }),
    ).toHaveTextContent("上位合計32回");
    const winPodium = within(wins).getByRole("list", { name: "勝利回数ランキングの表彰台" });
    const requestPodium = within(requestsPanel).getByRole("list", {
      name: "依頼回数ランキングの表彰台",
    });
    expect(winPodium).toHaveAttribute("data-podium-layout", "ranked");
    expect(requestPodium).toHaveAttribute("data-podium-layout", "shared");
    expect(within(winPodium).getByText("20")).toBeVisible();
    expect(within(winPodium).getByText("18")).toBeVisible();
    expect(within(requestPodium).getAllByText("1位")).toHaveLength(2);
    expect(within(requestPodium).getByText("3位")).toBeVisible();
    expect(within(requestPodium).getAllByText("38%")).toHaveLength(2);
    expect(within(wins).getAllByText("アロナ")).toHaveLength(1);
    expect(screen.getByText("2026年8月22日 09:00")).toHaveAttribute(
      "datetime",
      "2026-08-22T00:00:00Z",
    );
    const costs = await screen.findByRole("region", { name: "概算費用" });
    expect(within(costs).getByText("¥124")).toBeVisible();
    expect(within(costs).getByText("¥1")).toBeVisible();
    expect(within(costs).getByText("¥2")).toBeVisible();
    expect(within(costs).getByText("¥100")).toBeVisible();
    expect(within(costs).getByText("¥22")).toBeVisible();
    for (const category of ["Fargate", "Lambda", "OpenAI", "その他AWS"]) {
      expect(within(costs).getByText(category)).toBeVisible();
    }
    expect(within(costs).getByText("一部集計中")).toBeVisible();
    expect(within(costs).getByText(/Route 53は含みません/)).toBeVisible();
    expect(within(costs).getByRole("radio", { name: "直近7日" })).toBeChecked();
    const affection = screen.getByRole("region", { name: "親愛度ランキング" });
    expect(within(affection).getByText("AFFECTION", { exact: true })).toBeVisible();
    expect(within(affection).getByRole("heading", { name: "アロナ" })).toBeVisible();
    expect(within(affection).getByRole("heading", { name: "プラナ" })).toBeVisible();
    expect(within(affection).getByRole("heading", { name: "安倍晋三AI" })).toBeVisible();
    for (const participantName of ["アロナ", "プラナ", "安倍晋三AI"]) {
      expect(
        within(affection).getByRole("img", { name: `${participantName}のアイコン` }),
      ).toBeVisible();
    }
    const fullHearts = within(affection).getByRole("figure", {
      name: "安倍晋三AIからパワー系ウナギへの親愛度 1000点（1000点満点、ハート10個中10個）",
    });
    expect(fullHearts.querySelectorAll('svg[data-filled="true"]')).toHaveLength(10);
    const fiveHearts = within(affection).getByRole("figure", {
      name: "プラナから先生への親愛度 500点（1000点満点、ハート10個中5個）",
    });
    expect(fiveHearts.querySelectorAll('svg[data-filled="true"]')).toHaveLength(5);
    const fourHearts = within(affection).getByRole("figure", {
      name: "安倍晋三AIから先生への親愛度 480点（1000点満点、ハート10個中4個）",
    });
    expect(fourHearts.querySelectorAll('svg[data-filled="true"]')).toHaveLength(4);
    expect(
      within(affection).getAllByText("メモリアルロビーのリセット 2回", { exact: true }),
    ).toHaveLength(3);
  });

  it("keeps podium entries in rank order and retains only lower request ranks in the list", async () => {
    const result = rankingsResponse();
    result.requests = [
      { ...result.requests[0]!, rank: 1, count: 20 },
      { ...result.requests[1]!, rank: 2, count: 15 },
      { ...result.requests[2]!, rank: 3, count: 10 },
      { ...result.requests[2]!, rank: 4, count: 5, displayName: "4位の質問者" },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = requestPath(input);
        return Promise.resolve(
          response(path === "/api/v1/insights/rankings" ? result : insightResponse(path)),
        );
      }),
    );

    renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });
    const requests = await screen.findByRole("region", { name: "依頼回数ランキング" });
    const podium = await within(requests).findByRole("list", {
      name: "依頼回数ランキングの表彰台",
    });
    expect(
      within(podium)
        .getAllByRole("listitem")
        .map((item) => item.getAttribute("value")),
    ).toEqual(["1", "2", "3"]);
    const lowerRanks = within(requests).getByRole("list", { name: "依頼回数ランキングの4位以下" });
    expect(within(lowerRanks).getAllByRole("listitem")).toHaveLength(1);
    expect(within(lowerRanks).getByRole("listitem")).toHaveAttribute("value", "4");
    expect(within(lowerRanks).getByText("4位の質問者")).toBeVisible();
    expect(within(requests).getAllByText("パワー系ウナギ")).toHaveLength(1);
  });

  it.each([1, 2])(
    "handles a ranking with %i entries without inventing podium places",
    async (count) => {
      const result = rankingsResponse();
      result.wins = result.wins.slice(0, count);
      result.requests = result.requests.slice(0, count);
      vi.stubGlobal(
        "fetch",
        vi.fn((input: RequestInfo | URL) => {
          const path = requestPath(input);
          return Promise.resolve(
            response(path === "/api/v1/insights/rankings" ? result : insightResponse(path)),
          );
        }),
      );

      renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });
      const requests = await screen.findByRole("region", { name: "依頼回数ランキング" });
      const podium = await within(requests).findByRole("list", {
        name: "依頼回数ランキングの表彰台",
      });
      expect(within(podium).getAllByRole("listitem")).toHaveLength(count);
      expect(
        within(podium)
          .getAllByRole("listitem")
          .map((item) => item.getAttribute("value")),
      ).toEqual(Array(count).fill("1"));
    },
  );

  it("shows an empty ranking without inventing podium places", async () => {
    const result = { ...rankingsResponse(), wins: [], requests: [] };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = requestPath(input);
        return Promise.resolve(
          response(path === "/api/v1/insights/rankings" ? result : insightResponse(path)),
        );
      }),
    );

    renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });
    const requests = await screen.findByRole("region", { name: "依頼回数ランキング" });
    expect(await within(requests).findByText("まだ集計対象がありません")).toBeVisible();
    expect(within(requests).queryByRole("list")).not.toBeInTheDocument();
  });

  it("shows affection through third rank including ties and stops paging past the visible ranks", async () => {
    const affection = affectionRankingsResponse();
    affection.rankings = affection.rankings.map((ranking) => ({
      ...ranking,
      entries: [1000, 900, 800, 800, 700].map((score, index) => ({
        rank: index === 3 ? 3 : index + 1,
        score,
        displayName: `親愛度の質問者${index + 1}`,
        avatar: ranking.entries[0]!.avatar,
      })),
    }));
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = requestPath(input);
        requests.push(path);
        return Promise.resolve(
          response(
            path.startsWith("/api/v1/insights/affection-rankings?")
              ? { ...affection, nextCursor: "lower-ranks" }
              : insightResponse(path),
          ),
        );
      }),
    );

    renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });
    const panel = await screen.findByRole("region", { name: "親愛度ランキング" });
    await waitFor(() => expect(within(panel).getAllByRole("listitem")).toHaveLength(12));
    expect(within(panel).getAllByText("親愛度の質問者4")).toHaveLength(3);
    expect(within(panel).queryByText("親愛度の質問者5")).not.toBeInTheDocument();
    expect(
      within(panel).queryByRole("button", { name: "親愛度ランキングの続きを読み込む" }),
    ).not.toBeInTheDocument();
    expect(
      requests.filter((path) => path.startsWith("/api/v1/insights/affection-rankings?")),
    ).toEqual(["/api/v1/insights/affection-rankings?limit=50"]);
  });

  it("uses the current persona name for its icon without making hearts live regions", async () => {
    const renamedAffection = affectionRankingsResponse();
    renamedAffection.rankings[0].displayName = "アロナ改";
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = requestPath(input);
        return Promise.resolve(
          response(
            path.startsWith("/api/v1/insights/affection-rankings?")
              ? renamedAffection
              : insightResponse(path),
          ),
        );
      }),
    );

    renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });

    const affection = await screen.findByRole("region", { name: "親愛度ランキング" });
    expect(await within(affection).findByRole("heading", { name: "アロナ改" })).toBeVisible();
    expect(await within(affection).findByRole("img", { name: "アロナ改のアイコン" })).toBeVisible();
    expect(
      within(affection).queryByRole("img", { name: "アロナのアイコン" }),
    ).not.toBeInTheDocument();
    const heartsLabel = "アロナ改からパワー系ウナギへの親愛度 987点（1000点満点、ハート10個中9個）";
    expect(within(affection).getByRole("figure", { name: heartsLabel })).toBeVisible();
    expect(within(affection).queryByRole("status", { name: heartsLabel })).not.toBeInTheDocument();
  });

  it("fetches costs independently when the Japanese period changes", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = requestPath(input);
        requests.push(path);
        if (path.startsWith("/api/v1/insights/affection-rankings?")) {
          return Promise.resolve(response(affectionRankingsResponse()));
        }
        const period = path.includes("period=today") ? "today" : "week";
        return Promise.resolve(
          response(path.includes("/costs?") ? costsResponse(period) : rankingsResponse()),
        );
      }),
    );

    renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });
    await screen.findByText("¥124");
    fireEvent.click(screen.getByRole("radio", { name: "今日" }));

    await waitFor(() => expect(requests).toContain("/api/v1/insights/costs?period=today"));
    expect(requests.filter((request) => request === "/api/v1/insights/rankings")).toHaveLength(1);
  });

  it("appends every participant from the next affection page on explicit request", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = requestPath(input);
        requests.push(path);
        if (path.startsWith("/api/v1/insights/affection-rankings?")) {
          const first = affectionRankingsResponse();
          const cursor = new URL(path, "https://records.example").searchParams.get("cursor");
          return Promise.resolve(
            response(
              cursor === "next-page"
                ? {
                    ...first,
                    rankings: first.rankings.map((ranking) => ({
                      ...ranking,
                      entries: [
                        {
                          rank: 3,
                          displayName: "追加の質問者",
                          avatar: {
                            kind: "placeholder",
                            url: null,
                            alt: "追加の質問者のアバター",
                            fallbackVariant: "lavender",
                          },
                          score: 400,
                        },
                      ],
                    })),
                    nextCursor: null,
                  }
                : { ...first, nextCursor: "next-page" },
            ),
          );
        }
        return Promise.resolve(response(insightResponse(path)));
      }),
    );

    renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });

    const affection = await screen.findByRole("region", { name: "親愛度ランキング" });
    expect(await within(affection).findAllByRole("listitem")).toHaveLength(6);
    fireEvent.click(within(affection).getByRole("button", { name: "4位以下を表示" }));
    fireEvent.click(
      within(affection).getByRole("button", {
        name: "親愛度ランキングの続きを読み込む",
      }),
    );

    await waitFor(() => expect(within(affection).getAllByRole("listitem")).toHaveLength(9));
    expect(within(affection).getAllByText("追加の質問者")).toHaveLength(3);
    expect(
      within(affection).queryByRole("button", {
        name: "親愛度ランキングの続きを読み込む",
      }),
    ).not.toBeInTheDocument();
    expect(requests).toContain("/api/v1/insights/affection-rankings?limit=50&cursor=next-page");
  });

  it("keeps loaded affection entries visible while retrying a failed next page", async () => {
    let nextPageAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = requestPath(input);
        if (path.startsWith("/api/v1/insights/affection-rankings?")) {
          const first = affectionRankingsResponse();
          const cursor = new URL(path, "https://records.example").searchParams.get("cursor");
          if (cursor === null) {
            return Promise.resolve(response({ ...first, nextCursor: "next-page" }));
          }
          nextPageAttempts += 1;
          if (nextPageAttempts === 1) {
            return Promise.resolve(
              response(
                {
                  error: {
                    code: "INTERNAL_ERROR",
                    message: "続きを取得できませんでした。",
                    requestId: "request-id",
                  },
                },
                500,
              ),
            );
          }
          return Promise.resolve(
            response({
              ...first,
              rankings: first.rankings.map((ranking) => ({
                ...ranking,
                entries: [
                  {
                    rank: 3,
                    displayName: "再試行で追加",
                    avatar: {
                      kind: "placeholder",
                      url: null,
                      alt: "再試行で追加のアバター",
                      fallbackVariant: "cyan",
                    },
                    score: 400,
                  },
                ],
              })),
              nextCursor: null,
            }),
          );
        }
        return Promise.resolve(response(insightResponse(path)));
      }),
    );

    renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });
    const affection = await screen.findByRole("region", { name: "親愛度ランキング" });
    fireEvent.click(await within(affection).findByRole("button", { name: "4位以下を表示" }));
    fireEvent.click(
      await within(affection).findByRole("button", {
        name: "親愛度ランキングの続きを読み込む",
      }),
    );

    expect(await within(affection).findByRole("alert")).toHaveTextContent(
      "続きを取得できませんでした。",
    );
    expect(within(affection).getAllByRole("listitem")).toHaveLength(6);
    fireEvent.click(
      within(affection).getByRole("button", {
        name: "親愛度ランキングの続きを読み込む",
      }),
    );

    await waitFor(() => expect(within(affection).getAllByRole("listitem")).toHaveLength(9));
    expect(within(affection).getAllByText("再試行で追加")).toHaveLength(3);
  });

  it("keeps rankings visible when converted costs are unavailable", async () => {
    const unavailableCosts = {
      ...costsResponse(),
      total: "0.000000",
      breakdown: {
        fargate: "0.000000",
        lambda: "0.000000",
        openai: "0.000000",
        otherAws: "0.000000",
      },
      conversion: { ...costsResponse().conversion, updatedAt: null },
      updatedAt: null,
      status: "unavailable",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) =>
        Promise.resolve(
          response(
            requestPath(input).startsWith("/api/v1/insights/affection-rankings?")
              ? affectionRankingsResponse()
              : requestPath(input).includes("/costs?")
                ? unavailableCosts
                : rankingsResponse(),
          ),
        ),
      ),
    );

    renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });

    expect(await screen.findByText("勝利回数ランキング")).toBeVisible();
    const costs = screen.getByRole("region", { name: "概算費用" });
    expect(await within(costs).findByText("費用を取得できません")).toBeVisible();
    expect(within(costs).getByText("有効な日次換算値がまだありません。")).toBeVisible();
  });

  it("shows snapshot preparation independently in both ranking panels", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          response(
            {
              error: {
                code: "INSIGHTS_UNAVAILABLE",
                message: "集計を準備しています。",
                requestId: "request-id",
              },
            },
            503,
          ),
        ),
      ),
    );

    renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });

    expect(await screen.findAllByText("集計を準備しています")).toHaveLength(2);
    expect(screen.getByText("親愛度ランキングを準備しています")).toBeVisible();
    expect(screen.getAllByRole("status")).toHaveLength(3);
  });

  it("keeps existing insights visible when affection rankings fail independently", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = requestPath(input);
        if (path.startsWith("/api/v1/insights/affection-rankings?")) {
          return Promise.resolve(
            response(
              {
                error: {
                  code: "INTERNAL_ERROR",
                  message: "親愛度を取得できませんでした。",
                  requestId: "request-id",
                },
              },
              500,
            ),
          );
        }
        return Promise.resolve(response(insightResponse(path)));
      }),
    );

    renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });

    expect(await screen.findByText("親愛度ランキングを読み込めませんでした")).toBeVisible();
    expect(screen.getByRole("region", { name: "勝利回数ランキング" })).toBeVisible();
    expect(screen.getByRole("region", { name: "概算費用" })).toBeVisible();
  });
});

function lowerAffection() {
  const first = affectionRankingsResponse();
  return {
    ...first,
    rankings: first.rankings.map((ranking) => ({
      ...ranking,
      entries: [
        ...ranking.entries,
        { ...ranking.entries[1]!, rank: 3, displayName: "3位の先生", score: 400 },
        { ...ranking.entries[1]!, rank: 4, displayName: "4位の先生", score: 300 },
      ],
    })),
  };
}

it("opens all three affection lists without fetching and preserves lower ranks when closed", async () => {
  let reads = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path.includes("affection-rankings")) {
        reads++;
        return Promise.resolve(response(lowerAffection()));
      }
      return Promise.resolve(response(insightResponse(path)));
    }),
  );
  renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });
  const panel = await screen.findByRole("region", { name: "親愛度ランキング" });
  const toggle = await within(panel).findByRole("button", { name: "4位以下を表示" });
  expect(within(panel).getAllByRole("listitem")).toHaveLength(9);
  expect(within(panel).queryByText("4位の先生")).not.toBeInTheDocument();
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(document.getElementById(toggle.getAttribute("aria-controls")!)).toBeInTheDocument();
  expect(within(panel).getAllByText("4位の先生")).toHaveLength(3);
  expect(reads).toBe(1);
  fireEvent.click(toggle);
  expect(within(panel).getAllByRole("listitem")).toHaveLength(9);
  fireEvent.click(toggle);
  expect(within(panel).getAllByRole("listitem")).toHaveLength(12);
  expect(reads).toBe(1);
});

it("keeps a pending page when collapsed and never reopens on completion", async () => {
  const first = { ...lowerAffection(), nextCursor: "next-page" };
  let finishPage: ((value: Response) => void) | undefined;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path.includes("affection-rankings")) {
        if (path.includes("cursor="))
          return new Promise<Response>((resolve) => {
            finishPage = resolve;
          });
        return Promise.resolve(response(first));
      }
      return Promise.resolve(response(insightResponse(path)));
    }),
  );
  renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });
  const panel = await screen.findByRole("region", { name: "親愛度ランキング" });
  const toggle = await within(panel).findByRole("button", { name: "4位以下を表示" });
  fireEvent.click(toggle);
  fireEvent.click(within(panel).getByRole("button", { name: "親愛度ランキングの続きを読み込む" }));
  await waitFor(() => expect(finishPage).toBeDefined());
  fireEvent.click(toggle);
  await act(async () =>
    finishPage!(
      response({
        ...first,
        nextCursor: null,
        rankings: first.rankings.map((ranking) => ({
          ...ranking,
          entries: [{ ...ranking.entries[1]!, rank: 5, displayName: "5位の先生", score: 200 }],
        })),
      }),
    ),
  );
  await waitFor(() => expect(panel).toHaveAttribute("aria-busy", "false"));
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(within(panel).queryByText("5位の先生")).not.toBeInTheDocument();
  fireEvent.click(toggle);
  expect(await within(panel).findAllByText("5位の先生")).toHaveLength(3);
  expect(
    within(panel).queryByRole("button", { name: "親愛度ランキングの続きを読み込む" }),
  ).not.toBeInTheDocument();
});

it("shows refetch failure while collapsed without discarding top entries", async () => {
  let failed = false;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path.includes("affection-rankings"))
        return Promise.resolve(
          failed
            ? response(
                {
                  error: {
                    code: "INTERNAL_ERROR",
                    message: "更新できませんでした",
                    requestId: "test-request",
                  },
                },
                500,
              )
            : response(lowerAffection()),
        );
      return Promise.resolve(response(insightResponse(path)));
    }),
  );
  const { client } = renderRoute(<RankingsPage />, {
    initialEntry: "/insights",
    path: "/insights",
  });
  const panel = await screen.findByRole("region", { name: "親愛度ランキング" });
  await within(panel).findByRole("button", { name: "4位以下を表示" });
  failed = true;
  await act(async () => {
    await client.invalidateQueries({ queryKey: ["affection-rankings"] });
  });
  expect(await within(panel).findByRole("alert")).toHaveTextContent("更新できませんでした");
  expect(within(panel).getAllByRole("listitem")).toHaveLength(9);
  expect(within(panel).getByRole("button", { name: "4位以下を表示" })).toHaveAttribute(
    "aria-expanded",
    "false",
  );
});
