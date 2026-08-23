import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { rankingsResponse, renderRoute, response } from "../test/recordsTestUtils";
import RankingsPage from "./RankingsPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("RankingsPage", () => {
  it("renders the two ranking panels with competition ranks and no cost placeholder", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(rankingsResponse()))),
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
    expect(screen.queryByText(/費用|Fargate|OpenAI/)).not.toBeInTheDocument();
  });

  it("keeps all winners at the same podium height when every participant is tied", async () => {
    const tiedRankings = rankingsResponse();
    tiedRankings.wins = tiedRankings.wins.map((entry) => ({ ...entry, rank: 1, count: 20 }));
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(tiedRankings))),
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
