import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import {
  listResponse,
  mockApi,
  placeholder,
  renderRoute,
  response,
} from "../test/recordsTestUtils";
import RecordsHome from "./RecordsHome";

function mockEndSentinel() {
  let notify: IntersectionObserverCallback | undefined;
  const observe = vi.fn<(target: Element) => void>();
  const unobserve = vi.fn<(target: Element) => void>();
  class MockIntersectionObserver implements IntersectionObserver {
    public readonly root = null;
    public readonly rootMargin = "320px 0px";
    public readonly scrollMargin = "";
    public readonly thresholds = [0];

    public constructor(callback: IntersectionObserverCallback) {
      notify = callback;
    }

    public observe = observe;
    public disconnect = vi.fn<() => void>();
    public unobserve = unobserve;
    public takeRecords = () => [];
  }
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
  return {
    observe,
    unobserve,
    enter: () =>
      notify?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver),
  };
}

function mockNextPage(nextItems: ReturnType<typeof listResponse>["items"]) {
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const path =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      requests.push(path);
      if (!path.startsWith("/api/v1/records?")) throw new Error(`Unexpected request: ${path}`);
      return Promise.resolve(
        response(
          path.includes("cursor=next-page")
            ? { schemaVersion: 1, items: nextItems, nextCursor: null }
            : { ...listResponse(), nextCursor: "next-page" },
        ),
      );
    }),
  );
  return requests;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("RecordsHome", () => {
  it("renders completed records without duration or Evidence", async () => {
    const requests = mockApi();

    renderRoute(<RecordsHome />);

    expect(await screen.findByRole("heading", { name: "議論の記録" })).toBeVisible();
    const card = await screen.findByRole("article");
    expect(within(card).getByText("休日の過ごし方を決める")).toBeVisible();
    expect(within(card).getByText("2026年8月15日 15:00")).toHaveAttribute(
      "datetime",
      "2026-08-15T06:00:00Z",
    );
    expect(within(card).getAllByText("依頼者")).toHaveLength(2);
    expect(within(card).getByText("アロナ")).toBeVisible();
    expect(screen.queryByText(/所要時間|Evidence|外部根拠/)).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "新しい順" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "古い順" })).not.toBeChecked();
    const cardLink = screen.getByRole("link", {
      name: "「休日の過ごし方を決める」の記録を読む",
    });
    expect(cardLink).toContainElement(card);
    expect(within(card).queryByRole("link")).not.toBeInTheDocument();
    expect(requests).toContain("/api/v1/records?limit=12&sort=newest");
  });

  it("requests the complete archive in the selected order", async () => {
    const requests = mockApi();
    renderRoute(<RecordsHome />);
    await screen.findByRole("heading", { name: "議論の記録" });

    fireEvent.click(screen.getByRole("radio", { name: "古い順" }));

    await waitFor(() => {
      expect(requests).toContain("/api/v1/records?limit=12&sort=oldest");
    });
    expect(screen.getByRole("radio", { name: "古い順" })).toBeChecked();
  });

  it("keeps the latest choice after rapid sort changes", async () => {
    const requests = mockApi();
    renderRoute(<RecordsHome />);
    await screen.findByRole("heading", { name: "議論の記録" });

    const newest = screen.getByRole("radio", { name: "新しい順" });
    const oldest = screen.getByRole("radio", { name: "古い順" });
    fireEvent.click(oldest);
    fireEvent.click(newest);
    fireEvent.click(oldest);

    await waitFor(() => expect(oldest).toBeChecked());
    expect(newest).not.toBeChecked();
    expect(requests).toContain("/api/v1/records?limit=12&sort=oldest");
  });

  it("filters loaded records by the selected requester", async () => {
    const first = listResponse().items[0]!;
    const second = {
      ...structuredClone(first),
      recordId: "s".repeat(43),
      questionPreview: "別の依頼",
      requester: {
        displayName: "パワー系ウナギ",
        avatar: placeholder("パワー系ウナギ", "pink"),
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        if (path.startsWith("/api/v1/records?")) {
          const items = path.includes("sort=oldest") ? [first] : [first, second];
          return Promise.resolve(response({ schemaVersion: 1, items, nextCursor: null }));
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );

    renderRoute(<RecordsHome />);

    expect(await screen.findByText("別の依頼")).toBeVisible();
    const requesterFilter = screen.getByRole("button", { name: "依頼者" });
    fireEvent.click(requesterFilter);
    const requesterOption = within(screen.getByRole("listbox", { name: "依頼者" })).getByRole(
      "option",
      { name: "パワー系ウナギ" },
    );
    expect(requesterOption.firstElementChild).toHaveAttribute("aria-hidden", "true");
    fireEvent.click(requesterOption);

    expect(screen.getByText("別の依頼")).toBeVisible();
    expect(screen.queryByText("休日の過ごし方を決める")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "勝者" }));
    const winnerOption = within(screen.getByRole("listbox", { name: "勝者" })).getByRole("option", {
      name: "アロナ",
    });
    expect(winnerOption.firstElementChild).toHaveAttribute("aria-hidden", "true");
    fireEvent.keyDown(winnerOption, { key: "Escape" });

    fireEvent.click(screen.getByRole("radio", { name: "古い順" }));

    await waitFor(() => expect(requesterFilter).toHaveTextContent("すべて"));
    expect(await screen.findByText("休日の過ごし方を決める")).toBeVisible();
  });

  it("automatically loads the next page when the end sentinel enters the viewport", async () => {
    const sentinel = mockEndSentinel();
    const first = listResponse().items[0]!;
    const second = {
      ...structuredClone(first),
      recordId: "s".repeat(43),
      questionPreview: "自動で読み込まれた議論",
    };
    const requests = mockNextPage([second]);

    renderRoute(<RecordsHome />);

    expect(await screen.findByText(first.questionPreview)).toBeVisible();
    await waitFor(() => expect(sentinel.observe).toHaveBeenCalledOnce());
    sentinel.enter();

    expect(await screen.findByText(second.questionPreview)).toBeVisible();
    expect(requests.filter((path) => path.includes("cursor=next-page"))).toHaveLength(1);
    expect(sentinel.unobserve).toHaveBeenCalledOnce();
  });

  it("does not drain remaining pages automatically while a local filter is active", async () => {
    const sentinel = mockEndSentinel();
    const first = listResponse().items[0]!;
    const requests = mockNextPage([]);

    renderRoute(<RecordsHome />);

    expect(await screen.findByText(first.questionPreview)).toBeVisible();
    fireEvent.change(screen.getByLabelText("フリーワード検索"), {
      target: { value: "一致しない検索" },
    });
    sentinel.enter();

    await waitFor(() => {
      expect(requests.filter((path) => path.includes("cursor=next-page"))).toHaveLength(0);
    });
    fireEvent.click(screen.getByRole("button", { name: "検索対象をさらに読み込む" }));
    await waitFor(() => {
      expect(requests.filter((path) => path.includes("cursor=next-page"))).toHaveLength(1);
    });
  });
});
