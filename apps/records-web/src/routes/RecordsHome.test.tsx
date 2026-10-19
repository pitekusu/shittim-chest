import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import {
  listResponse,
  mockApi,
  placeholder,
  renderRoute,
  response,
} from "../test/recordsTestUtils";
import { useRecordsArchive } from "../hooks/useRecordsArchive";
import RecordsHome from "./RecordsHome";

const home = <RecordsHome csrfToken="csrf-token" />;
type SearchBody = {
  search: string;
  requester?: string;
  winner?: string;
  sort: string;
  cursor?: string;
  limit: number;
};
function item(index: number, questionPreview = `記録 ${index}`) {
  return {
    ...structuredClone(listResponse().items[0]!),
    recordId: String.fromCharCode(65 + index).repeat(43),
    questionPreview,
  };
}
function page(items: ReturnType<typeof item>[], nextCursor: string | null = null) {
  return { schemaVersion: 1, items, nextCursor };
}
function searchMock(
  handler: (body: SearchBody, init?: RequestInit) => Response | Promise<Response>,
  requesters = [listResponse().items[0]!.requester],
) {
  mockApi();
  const original = fetch;
  const bodies: SearchBody[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (path === "/api/v1/records/search?contract=record-attachments-v1") {
        const body = JSON.parse(typeof init?.body === "string" ? init.body : "{}") as SearchBody;
        bodies.push(body);
        return Promise.resolve(handler(body, init));
      }
      if (path.startsWith("/api/v1/records/requesters"))
        return Promise.resolve(response({ schemaVersion: 1, items: requesters, nextCursor: null }));
      return original(input, init);
    }),
  );
  return bodies;
}
function selectRequester(name: string) {
  fireEvent.click(screen.getByRole("combobox", { name: "依頼者" }));
  const option = within(screen.getByRole("listbox", { name: "依頼者" })).getByRole("option", {
    name,
  });
  fireEvent.pointerDown(option, { pointerType: "mouse" });
  fireEvent.click(option);
}
function mockEndSentinel() {
  let notify: IntersectionObserverCallback | undefined;
  const observe = vi.fn<(target: Element) => void>();
  class MockIntersectionObserver implements IntersectionObserver {
    readonly root = null;
    readonly rootMargin = "320px 0px";
    readonly scrollMargin = "";
    readonly thresholds = [0];
    constructor(callback: IntersectionObserverCallback) {
      notify = callback;
    }
    observe = observe;
    disconnect = vi.fn<() => void>();
    unobserve = vi.fn<(target: Element) => void>();
    takeRecords = () => [];
  }
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
  return {
    observe,
    enter: () =>
      notify?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver),
  };
}
function ArchiveLifecycleHarness() {
  const [showArchive, setShowArchive] = useState(true);
  const { reset } = useRecordsArchive();
  return (
    <>
      <button onClick={() => setShowArchive((value) => !value)}>切り替える</button>
      <button onClick={reset}>認証を破棄</button>
      {showArchive ? home : <p>別の画面</p>}
    </>
  );
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("RecordsHome", () => {
  it("renders completed records and requests the selected order", async () => {
    const requests = mockApi();
    renderRoute(home);
    const card = await screen.findByRole("article");
    expect(within(card).getByText("休日の過ごし方を決める")).toBeVisible();
    expect(within(card).getByText("2026年8月15日 15:00")).toHaveAttribute(
      "datetime",
      "2026-08-15T06:00:00Z",
    );
    expect(card.closest("a")).toHaveAccessibleName(
      /2026年8月15日 15:00 休日の過ごし方を決める 依頼者\s*依頼者\s*勝者\s*アロナ 記録を読む/,
    );
    expect(screen.queryByText(/所要時間|Evidence|外部根拠/)).not.toBeInTheDocument();
    expect(requests).toContain(
      "/api/v1/records?limit=12&contract=record-attachments-v1&sort=newest",
    );
    fireEvent.click(screen.getByRole("radio", { name: "古い順" }));
    await waitFor(() =>
      expect(requests).toContain(
        "/api/v1/records?limit=12&contract=record-attachments-v1&sort=oldest",
      ),
    );
  });

  it("identifies attached images in list and search cards without fetching details or images", async () => {
    const records = [
      { ...item(0), attachmentCount: 1 },
      { ...item(1), attachmentCount: 3 },
      { ...item(2), attachmentCount: 0 },
      item(3),
    ];
    searchMock(() => response(page(records)));
    const original = fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
        typeof input === "string" && input.startsWith("/api/v1/records?")
          ? Promise.resolve(response(page(records)))
          : original(input, init),
      ),
    );
    renderRoute(home);
    const oneImage = await screen.findByRole("link", {
      name: /記録 0.*記録を読む/,
    });
    expect(oneImage).toHaveAccessibleName(/画像\s*1枚/);
    expect(within(oneImage).getByText("1枚")).toBeVisible();
    expect(within(oneImage).getByText("1枚").parentElement?.querySelector("svg")).not.toBeNull();
    expect(screen.getByRole("link", { name: /記録 1.*記録を読む/ })).toHaveAccessibleName(
      /画像\s*3枚/,
    );
    expect(screen.getByRole("link", { name: /記録 2.*記録を読む/ })).toBeVisible();
    expect(screen.getByRole("link", { name: /記録 3.*記録を読む/ })).toBeVisible();
    expect(screen.queryByText("0枚")).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "記録" } });
    expect(await screen.findByRole("link", { name: /記録 1.*記録を読む/ })).toHaveAccessibleName(
      /画像\s*3枚/,
    );
    expect(
      vi
        .mocked(fetch)
        .mock.calls.every(
          ([input]) =>
            typeof input === "string" &&
            !/\/api\/v1\/records\/[A-Za-z0-9_-]{43}(?:\?|$)/u.test(input),
        ),
    ).toBe(true);
    expect(screen.queryByRole("img", { name: /議論に添付された画像/ })).not.toBeInTheDocument();
  });

  it("keeps filters and expanded results through a remount using only React memory", async () => {
    const matches = Array.from({ length: 24 }, (_, i) => item(i, `休日 ${i}`));
    searchMock((body) =>
      response(body.cursor ? page(matches.slice(12)) : page(matches.slice(0, 12), "more")),
    );
    const persist = vi.spyOn(Storage.prototype, "setItem");
    renderRoute(<ArchiveLifecycleHarness />);
    await screen.findByRole("article");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "休日" } });
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(12));
    fireEvent.click(screen.getByRole("button", { name: "さらに読み込む" }));
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(24));
    fireEvent.click(screen.getByRole("button", { name: "切り替える" }));
    fireEvent.click(screen.getByRole("button", { name: "切り替える" }));
    expect(screen.getByRole("searchbox")).toHaveValue("休日");
    expect(screen.getAllByRole("article")).toHaveLength(24);
    expect(persist).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "認証を破棄" }));
    expect(screen.getByRole("searchbox")).toHaveValue("");
    expect(await screen.findByRole("article")).toBeVisible();
  });

  it("finds a late full-question match through empty pages without prematurely declaring no matches", async () => {
    let finish!: (value: Response) => void;
    const bodies = searchMock((body, init) => {
      expect(init?.method).toBe("POST");
      expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("csrf-token");
      return body.cursor
        ? new Promise<Response>((resolve) => {
            finish = resolve;
          })
        : response(page([], "late"));
    });
    renderRoute(home);
    await screen.findByRole("article");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "質問の160文字より後ろ" } });
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(screen.queryByText("該当する記録はありません")).not.toBeInTheDocument();
    expect(screen.getByText("全記録から探しています。")).toBeVisible();
    finish(response(page([item(1, "検索語を含まない先頭160文字のプレビュー")])));
    expect(await screen.findByText("検索語を含まない先頭160文字のプレビュー")).toBeVisible();
    expect(screen.getByRole("searchbox")).toHaveAccessibleDescription(
      /全記録の質問文・勝者名を検索/,
    );
    expect(bodies[1]?.cursor).toBe("late");
  });

  it("loads all requester candidates independently and keeps a selection through sort and empty results", async () => {
    const second = { displayName: "全記録の依頼者", avatar: placeholder("全記録の依頼者", "pink") };
    const bodies = searchMock(() => response(page([])));
    const original = fetch;
    const candidateCursors: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        if (!path.startsWith("/api/v1/records/requesters")) return original(input, init);
        candidateCursors.push(path);
        return Promise.resolve(
          response({
            schemaVersion: 1,
            items: path.includes("cursor") ? [second] : [listResponse().items[0]!.requester],
            nextCursor: path.includes("cursor") ? null : "candidates",
          }),
        );
      }),
    );
    renderRoute(home);
    await waitFor(() => expect(candidateCursors).toHaveLength(2));
    selectRequester(second.displayName);
    expect(await screen.findByText("該当する記録はありません")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "依頼者" })).toHaveTextContent(second.displayName);
    fireEvent.click(screen.getByRole("radio", { name: "古い順" }));
    await waitFor(() =>
      expect(bodies.at(-1)).toMatchObject({ requester: second.displayName, sort: "oldest" }),
    );
    expect(screen.getByRole("combobox", { name: "依頼者" })).toHaveTextContent(second.displayName);
    fireEvent.click(screen.getByRole("button", { name: "絞り込みを解除" }));
    expect(screen.getByRole("radio", { name: "古い順" })).toBeChecked();
    expect(await screen.findByRole("article")).toBeVisible();
  });

  it("debounces committed input, skips IME composition and cancels an obsolete search", async () => {
    let aborted = false;
    const bodies = searchMock((body, init) =>
      body.search === "確定した検索"
        ? new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => {
              aborted = true;
              reject(new DOMException("Aborted", "AbortError"));
            });
          })
        : response(page([item(1, "新しい条件の結果")])),
    );
    renderRoute(home);
    await screen.findByRole("article");
    const input = screen.getByRole("searchbox");
    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "変換中" } });
    await new Promise((resolve) => window.setTimeout(resolve, 350));
    expect(bodies).toHaveLength(0);
    fireEvent.change(input, { target: { value: "確定した検索" } });
    fireEvent.compositionEnd(input);
    expect(bodies).toHaveLength(0);
    await waitFor(() => expect(bodies).toHaveLength(1));
    fireEvent.change(input, { target: { value: "新しい" } });
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    await waitFor(() => expect(aborted).toBe(true));
    fireEvent.change(input, { target: { value: "新しい条件" } });
    expect(await screen.findByText("新しい条件の結果")).toBeVisible();
    expect(bodies.map((body) => body.search)).toEqual(["確定した検索", "新しい条件"]);
  });

  it("preserves partial matches after failure and retries the same cursor", async () => {
    let attempts = 0;
    const bodies = searchMock((body) => {
      if (!body.cursor) return response(page([item(1, "途中まで見つかった記録")], "retry-here"));
      if (++attempts === 1)
        return response(
          {
            error: {
              code: "ARCHIVE_UNAVAILABLE",
              message: "一時的な失敗",
              requestId: "test-request",
            },
          },
          503,
        );
      return response(page([item(2, "再試行で見つかった記録")]));
    });
    renderRoute(home);
    await screen.findByRole("article");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "検索" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("一時的な失敗");
    expect(screen.getByText("途中まで見つかった記録")).toBeVisible();
    expect(screen.queryByText("該当する記録はありません")).not.toBeInTheDocument();
    fireEvent.click(within(screen.getByRole("alert")).getByRole("button"));
    expect(await screen.findByText("再試行で見つかった記録")).toBeVisible();
    expect(bodies.map((body) => body.cursor)).toEqual([undefined, "retry-here", "retry-here"]);
  });

  it("restarts current criteria after cursor expiration", async () => {
    let starts = 0;
    const bodies = searchMock((body) => {
      if (body.cursor)
        return response(
          { error: { code: "CURSOR_INVALID", message: "期限切れ", requestId: "test-request" } },
          400,
        );
      return response(++starts === 1 ? page([], "expired") : page([item(2, "再開した検索結果")]));
    });
    renderRoute(home);
    await screen.findByRole("article");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "検索" } });
    expect(await screen.findByText("再開した検索結果")).toBeVisible();
    expect(bodies.map((body) => body.cursor)).toEqual([undefined, "expired", undefined]);
  });

  it("fills the first twelve matches then loads the next group when the sentinel enters", async () => {
    const sentinel = mockEndSentinel();
    const bodies = searchMock((body) =>
      response(
        !body.cursor
          ? page(
              Array.from({ length: 2 }, (_, i) => item(i)),
              "sparse",
            )
          : body.cursor === "sparse"
            ? page(
                Array.from({ length: 12 }, (_, i) => item(i + 2)),
                "next",
              )
            : page(Array.from({ length: 10 }, (_, i) => item(i + 14))),
      ),
    );
    renderRoute(home);
    await screen.findByRole("article");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "検索" } });
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(12));
    expect(bodies).toHaveLength(2);
    expect(screen.queryByText("記録 13")).not.toBeInTheDocument();
    await waitFor(() => expect(sentinel.observe).toHaveBeenCalled());
    sentinel.enter();
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(24));
    expect(bodies).toHaveLength(3);
  });
  it("reobserves the sentinel while revealing successive groups already cached", async () => {
    const sentinel = mockEndSentinel();
    const requests = mockApi();
    const { client } = renderRoute(home);
    await screen.findByRole("article");
    const matches = Array.from({ length: 36 }, (_, index) => item(index));
    await act(() =>
      client.setQueryData(["records", "list", "", "newest", "", ""], {
        pages: [
          page(matches.slice(0, 12), "second"),
          page(matches.slice(12, 24), "third"),
          page(matches.slice(24)),
        ],
        pageParams: [undefined, "second", "third"],
      }),
    );
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(12));
    await waitFor(() => expect(sentinel.observe).toHaveBeenCalledTimes(1));
    sentinel.enter();
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(24));
    await waitFor(() => expect(sentinel.observe).toHaveBeenCalledTimes(2));
    sentinel.enter();
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(36));
    expect(requests.filter((path) => path.startsWith("/api/v1/records?"))).toHaveLength(1);
  });
  it("restores thirteen cached normal records after searching and clearing without viewport loading", async () => {
    const normal = Array.from({ length: 13 }, (_, index) =>
      item(index, index === 12 ? "十三件目の読み込み済み記録" : `通常記録 ${index}`),
    );
    const matches = Array.from({ length: 12 }, (_, index) =>
      item(index, `新しい検索の記録 ${index}`),
    );
    searchMock(() => response(page(matches)));
    const original = fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        if (!path.startsWith("/api/v1/records?")) return original(input, init);
        return Promise.resolve(
          response(
            path.includes("cursor=next")
              ? page(normal.slice(12))
              : page(normal.slice(0, 12), "next"),
          ),
        );
      }),
    );
    renderRoute(home);
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(12));
    fireEvent.click(screen.getByRole("button", { name: "さらに読み込む" }));
    expect(await screen.findByText("十三件目の読み込み済み記録")).toBeVisible();
    expect(screen.getAllByRole("article")).toHaveLength(13);
    const search = screen.getByRole("searchbox");
    fireEvent.change(search, { target: { value: "新しい検索" } });
    expect(await screen.findByText("新しい検索の記録 11")).toBeVisible();
    expect(screen.getAllByRole("article")).toHaveLength(12);
    fireEvent.change(search, { target: { value: "" } });
    expect(await screen.findByText("十三件目の読み込み済み記録")).toBeVisible();
    expect(screen.getAllByRole("article")).toHaveLength(13);
  });
});
