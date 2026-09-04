import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
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
    expect(
      screen.queryByText("これまでの議論を、ランキングで振り返れます。"),
    ).not.toBeInTheDocument();
    const wins = screen.getByRole("region", { name: "勝利回数ランキング" });
    const requestsPanel = screen.getByRole("region", { name: "依頼回数ランキング" });
    expect(await within(wins).findAllByRole("listitem")).toHaveLength(3);
    expect(within(wins).getAllByText("安倍晋三AI")).toHaveLength(2);
    expect(
      within(wins).getByRole("status", { name: "勝利回数ランキングの合計" }),
    ).toHaveTextContent("合計54回");
    expect(
      within(requestsPanel).getByRole("status", { name: "依頼回数ランキングの上位合計" }),
    ).toHaveTextContent("上位合計32回");
    const aronaBar = within(wins).getByRole("meter", {
      name: "アロナ: 20回（最多20回との比較）",
    });
    const planaBar = within(wins).getByRole("meter", {
      name: "プラナ: 18回（最多20回との比較）",
    });
    expect(aronaBar).toHaveAttribute("max", "20");
    expect(aronaBar).toHaveAttribute("value", "20");
    expect(planaBar).toHaveAttribute("value", "18");
    expect(within(requestsPanel).getAllByText("1位")).toHaveLength(2);
    expect(within(requestsPanel).getByText("3位")).toBeInTheDocument();
    expect(within(requestsPanel).getAllByRole("meter")).toHaveLength(3);
    expect(
      within(requestsPanel).getByRole("meter", {
        name: "パワー系ウナギ: 12回（上位合計の38%、最多12回との比較）",
      }),
    ).toHaveAttribute("value", "12");
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
    expect(
      within(affection).queryByText("AFFECTION RANKINGS", { exact: true }),
    ).not.toBeInTheDocument();
    expect(
      within(affection).queryByText(
        "質問者ごとの現在の親愛度です。人格ごとに1000点満点で表示します。",
        { exact: true },
      ),
    ).not.toBeInTheDocument();
    expect(within(affection).queryByText("親愛度", { exact: true })).not.toBeInTheDocument();
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

  it("keeps all winners at the same podium height when every participant is tied", async () => {
    const tiedRankings = rankingsResponse();
    tiedRankings.wins = tiedRankings.wins.map((entry) => ({ ...entry, rank: 1, count: 20 }));
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) =>
        Promise.resolve(
          response(
            requestPath(input).startsWith("/api/v1/insights/affection-rankings?")
              ? affectionRankingsResponse()
              : requestPath(input).includes("/costs?")
                ? costsResponse()
                : tiedRankings,
          ),
        ),
      ),
    );

    const { container } = renderRoute(<RankingsPage />, {
      initialEntry: "/insights",
      path: "/insights",
    });

    await screen.findByRole("heading", { name: "いろいろな記録" });
    await within(screen.getByRole("region", { name: "勝利回数ランキング" })).findAllByRole(
      "listitem",
    );
    expect(container.querySelector('[data-podium-layout="shared"]')).toBeInTheDocument();
    expect(container.querySelector('[data-podium-layout="ranked"]')).not.toBeInTheDocument();
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
