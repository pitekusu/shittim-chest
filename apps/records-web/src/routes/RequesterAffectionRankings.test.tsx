import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vite-plus/test";

import type { ParticipantSlot, RequesterAffectionRankingsResponse } from "../api/types";
import {
  affectionRankingsResponse,
  costsResponse,
  placeholder,
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

function requesterPage(name = "架空の依頼者"): RequesterAffectionRankingsResponse {
  const source = affectionRankingsResponse();
  return {
    schemaVersion: 1,
    generatedAt: source.generatedAt,
    defaultScore: 500,
    maxScore: 1000,
    requesters: [
      {
        displayName: name,
        avatar: placeholder(name, "cyan"),
        participants: source.rankings.map((ranking) => ({
          participant: ranking.participant as ParticipantSlot,
          displayName: ranking.displayName,
          score: 500,
          resetCount: 0,
        })),
      },
    ],
    nextCursor: "requester-next",
  };
}

function mockApi(handler: (url: URL) => Response | Promise<Response>) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const path =
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const url = new URL(path, "https://example.test");
      if (url.searchParams.get("view") === "requesters") return Promise.resolve(handler(url));
      return Promise.resolve(
        response(
          url.pathname.endsWith("/affection-rankings")
            ? affectionRankingsResponse()
            : url.pathname.endsWith("/costs")
              ? costsResponse()
              : rankingsResponse(),
        ),
      );
    }),
  );
}

async function openRequesterView() {
  const rendered = renderRoute(<RankingsPage />, { initialEntry: "/insights", path: "/insights" });
  const panel = screen.getByRole("region", { name: "親愛度ランキング" });
  fireEvent.click(within(panel).getByRole("radio", { name: "依頼者別" }));
  return { ...rendered, panel };
}

it("preserves the current requester page after a failed next page and retries explicitly", async () => {
  let failNext = true;
  let calls = 0;
  mockApi((url) => {
    calls += 1;
    if (url.searchParams.has("cursor")) {
      if (failNext)
        return response(
          {
            error: {
              code: "INSIGHTS_UNAVAILABLE",
              message: "集計を確認できません。",
              requestId: "test",
            },
          },
          503,
        );
      return response({ ...requesterPage("次の依頼者"), nextCursor: null });
    }
    return response(requesterPage());
  });
  const { panel } = await openRequesterView();
  await within(panel).findByRole("region", { name: "架空の依頼者" });
  fireEvent.click(within(panel).getByRole("button", { name: "次の10人" }));
  expect(await within(panel).findByRole("alert")).toHaveTextContent(
    "次の依頼者を読み込めませんでした",
  );
  expect(within(panel).getByRole("region", { name: "架空の依頼者" })).toBeVisible();
  expect(calls).toBe(2);
  failNext = false;
  fireEvent.click(within(panel).getByRole("button", { name: "もう一度試す" }));
  expect(await within(panel).findByRole("region", { name: "次の依頼者" })).toBeVisible();
  expect(within(panel).getByRole("button", { name: "次の10人" })).toBeDisabled();
  fireEvent.click(within(panel).getByRole("button", { name: "前の10人" }));
  expect(within(panel).getByRole("region", { name: "架空の依頼者" })).toBeVisible();
  expect(calls).toBe(3);
});

it("keeps a pending requester page change when the grouping is switched", async () => {
  let finish: ((value: Response) => void) | undefined;
  mockApi((url) =>
    url.searchParams.has("cursor")
      ? new Promise<Response>((resolve) => {
          finish = resolve;
        })
      : response(requesterPage()),
  );
  const { panel } = await openRequesterView();
  await within(panel).findByRole("region", { name: "架空の依頼者" });
  fireEvent.click(within(panel).getByRole("button", { name: "次の10人" }));
  await waitFor(() => expect(finish).toBeDefined());
  fireEvent.click(within(panel).getByRole("radio", { name: "人格別" }));
  await act(async () => finish?.(response({ ...requesterPage("次の依頼者"), nextCursor: null })));
  expect(within(panel).getByRole("radio", { name: "人格別" })).toBeChecked();
  fireEvent.click(within(panel).getByRole("radio", { name: "依頼者別" }));
  expect(await within(panel).findByRole("region", { name: "次の依頼者" })).toBeVisible();
});

it("shows a retryable preparing state independently from participant rankings", async () => {
  mockApi(() =>
    response(
      {
        error: {
          code: "INSIGHTS_UNAVAILABLE",
          message: "集計を準備しています。",
          requestId: "test",
        },
      },
      503,
    ),
  );
  const { panel } = await openRequesterView();
  expect(
    await within(panel).findByText(/依頼者別の親愛度ランキングを準備しています/),
  ).toBeVisible();
  expect(within(panel).getByRole("button", { name: "もう一度試す" })).toBeEnabled();
  fireEvent.click(within(panel).getByRole("radio", { name: "人格別" }));
  expect(await within(panel).findByRole("heading", { name: "アロナ" })).toBeVisible();
});

it("shows an empty requester page with no available navigation", async () => {
  mockApi(() => response({ ...requesterPage(), requesters: [], nextCursor: null }));
  const { panel } = await openRequesterView();
  expect(await within(panel).findByText("まだ集計対象がありません。")).toBeVisible();
  expect(within(panel).getByRole("button", { name: "前の10人" })).toBeDisabled();
  expect(within(panel).getByRole("button", { name: "次の10人" })).toBeDisabled();
});

it("restarts from the latest first page when the snapshot cursor has expired", async () => {
  let firstPageRequests = 0;
  mockApi((url) => {
    if (url.searchParams.has("cursor")) {
      return response(
        { error: { code: "CURSOR_INVALID", message: "期限が切れました。", requestId: "test" } },
        400,
      );
    }
    firstPageRequests += 1;
    return response(requesterPage(firstPageRequests === 1 ? "以前の依頼者" : "最新の依頼者"));
  });
  const { panel } = await openRequesterView();
  await within(panel).findByRole("region", { name: "以前の依頼者" });
  fireEvent.click(within(panel).getByRole("button", { name: "次の10人" }));
  const restart = await within(panel).findByRole("button", { name: "最初から読み直す" });
  expect(within(panel).getByRole("region", { name: "以前の依頼者" })).toBeVisible();
  fireEvent.click(restart);
  expect(await within(panel).findByRole("region", { name: "最新の依頼者" })).toBeVisible();
  expect(firstPageRequests).toBe(2);
  expect(within(panel).getByRole("button", { name: "前の10人" })).toBeDisabled();
});
