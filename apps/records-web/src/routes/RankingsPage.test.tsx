import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { costsResponse, rankingsResponse, renderRoute, response } from "../test/recordsTestUtils";
import RankingsPage from "./RankingsPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function requestPath(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
}

describe("RankingsPage", () => {
  it("renders rankings and the exact four-part JPY cost dashboard", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = requestPath(input);
        return Promise.resolve(
          response(path.includes("/costs?") ? costsResponse() : rankingsResponse()),
        );
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
    expect(screen.getByText("最終集計:")).toHaveTextContent("2026年8月22日 09:00");
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
  });

  it("fetches costs independently when the Japanese period changes", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = requestPath(input);
        requests.push(path);
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
          response(requestPath(input).includes("/costs?") ? unavailableCosts : rankingsResponse()),
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
          response(requestPath(input).includes("/costs?") ? costsResponse() : tiedRankings),
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
    expect(screen.getAllByRole("status")).toHaveLength(2);
  });
});
