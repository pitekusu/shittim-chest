import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { BrandedRouteStage, routeMotionKind } from "./RouteMotion";
import { RecordsArchiveProvider, useRecordsArchive } from "./hooks/useRecordsArchive";
import styles from "./styles/routeMotion.module.css";
import RecordsHome from "./routes/RecordsHome";
import { RECORD_ID, listResponse, response } from "./test/recordsTestUtils";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function ArchiveFocusHarness({ missing = false }: { readonly missing?: boolean }) {
  const navigate = useNavigate();
  const { rememberRecord } = useRecordsArchive();
  const [ready, setReady] = useState(true);
  const recordId = "r".repeat(43);
  return (
    <>
      <button
        type="button"
        onClick={() => {
          rememberRecord(recordId);
          setReady(false);
          void navigate(`/records/${recordId}`);
        }}
      >
        open record
      </button>
      <button type="button" onClick={() => navigate("/")}>
        return archive
      </button>
      <button type="button" onClick={() => setReady(true)}>
        finish archive
      </button>
      <BrandedRouteStage>
        <Routes>
          <Route
            path="/"
            element={
              <section data-route-motion-ready="" data-archive-ready={ready ? "" : undefined}>
                <h1 tabIndex={-1}>議論の記録</h1>
                {ready && !missing && (
                  <a href={`/records/${recordId}`} data-record-id={recordId}>
                    元の記録
                  </a>
                )}
              </section>
            }
          />
          <Route
            path="/records/:recordId"
            element={
              <section data-route-motion-ready="">
                <h1 tabIndex={-1}>議論詳細</h1>
              </section>
            }
          />
        </Routes>
      </BrandedRouteStage>
    </>
  );
}

function CachedArchiveFocusHarness() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate("/")}>
        return archive
      </button>
      <BrandedRouteStage>
        <Routes>
          <Route path="/" element={<RecordsHome />} />
          <Route
            path="/records/:recordId"
            element={
              <section data-route-motion-ready="">
                <h1 tabIndex={-1}>議論詳細</h1>
              </section>
            }
          />
        </Routes>
      </BrandedRouteStage>
    </>
  );
}

function MotionHarness() {
  const navigate = useNavigate();
  const [renderCount, setRenderCount] = useState(0);
  return (
    <>
      <button type="button" onClick={() => navigate("/")}>
        archive
      </button>
      <button type="button" onClick={() => navigate("/insights")}>
        insights
      </button>
      <button type="button" onClick={() => navigate(`/records/${"r".repeat(43)}`)}>
        detail
      </button>
      <button type="button" onClick={() => setRenderCount((value) => value + 1)}>
        rerender {renderCount}
      </button>
      <BrandedRouteStage>
        <Routes>
          <Route
            path="/"
            element={
              <section data-route-motion-ready="">
                <h1 tabIndex={-1}>議論の記録</h1>
              </section>
            }
          />
          <Route
            path="/insights"
            element={
              <section data-route-motion-ready="">
                <h1 tabIndex={-1}>いろいろな記録</h1>
              </section>
            }
          />
          <Route
            path="/records/:recordId"
            element={
              <section data-route-motion-ready="">
                <h1 tabIndex={-1}>議論詳細</h1>
              </section>
            }
          />
        </Routes>
      </BrandedRouteStage>
    </>
  );
}

function DelayedMotionHarness() {
  const navigate = useNavigate();
  const [ready, setReady] = useState(false);
  return (
    <>
      <button type="button" onClick={() => navigate(`/records/${"r".repeat(43)}`)}>
        open detail
      </button>
      <button type="button" onClick={() => setReady(true)}>
        finish loading
      </button>
      <BrandedRouteStage>
        <Routes>
          <Route path="/" element={<h1 tabIndex={-1}>議論の記録</h1>} />
          <Route
            path="/records/:recordId"
            element={
              ready ? (
                <section data-route-motion-ready="">
                  <h1 tabIndex={-1}>非同期の議論詳細</h1>
                  <div className={styles.routeMotionItem} data-route-motion-terminal="" />
                </section>
              ) : (
                <p>読み込み中</p>
              )
            }
          />
        </Routes>
      </BrandedRouteStage>
    </>
  );
}

describe("routeMotionKind", () => {
  it("maps every Records route to its branded motion kind", () => {
    expect(routeMotionKind("/")).toBe("archive");
    expect(routeMotionKind("/insights")).toBe("insights");
    expect(routeMotionKind("/admin")).toBe("admin");
    expect(routeMotionKind("/memorial")).toBe("memorial");
    expect(routeMotionKind(`/records/${"r".repeat(43)}`)).toBe("detail");
    expect(routeMotionKind("/missing")).toBe("other");
  });
});

describe("BrandedRouteStage", () => {
  it("waits for a cached archive to refresh before restoring a record removed by that refresh", async () => {
    const cached = listResponse();
    const record = cached.items[0]!;
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
    });
    const queryKey = ["records", "", "newest"];
    client.setQueryData(queryKey, { pages: [cached], pageParams: [undefined] });
    let completeRefresh!: (value: Response) => void;
    const refreshed = new Promise<Response>((resolve) => {
      completeRefresh = resolve;
    });
    const fetchMock = vi.fn<typeof fetch>().mockReturnValue(refreshed);
    vi.stubGlobal("fetch", fetchMock);
    const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
    render(
      <QueryClientProvider client={client}>
        <RecordsArchiveProvider>
          <MemoryRouter>
            <CachedArchiveFocusHarness />
          </MemoryRouter>
        </RecordsArchiveProvider>
      </QueryClientProvider>,
    );

    const recordLabel = `「${record.questionPreview}」の記録を読む`;
    fireEvent.click(await screen.findByRole("link", { name: recordLabel }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "議論詳細" })).toHaveFocus());
    await act(async () => {
      await client.invalidateQueries({ queryKey, refetchType: "none" });
    });
    fireEvent.click(screen.getByRole("button", { name: "return archive" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    const cachedCard = screen.getByRole("link", { name: recordLabel });
    expect(cachedCard).toBeVisible();
    expect(cachedCard).not.toHaveFocus();
    expect(scroll).not.toHaveBeenCalled();

    await act(async () => {
      completeRefresh(
        response({
          ...cached,
          items: [
            {
              ...record,
              recordId: "s".repeat(RECORD_ID.length),
              questionPreview: "更新後に追加された記録",
            },
          ],
        }),
      );
      await refreshed;
    });
    await waitFor(() => expect(screen.getByRole("heading", { name: "議論の記録" })).toHaveFocus());
    expect(cachedCard).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "「更新後に追加された記録」の記録を読む" }),
    ).toBeVisible();
    expect(scroll).not.toHaveBeenCalled();
  });

  it("disconnects motion observation and listeners as soon as a route settles", () => {
    const disconnect = vi.spyOn(MutationObserver.prototype, "disconnect");
    const { container } = render(
      <MemoryRouter>
        <MotionHarness />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "insights" }));
    const scene = container.querySelector<HTMLElement>("[data-route-scene]")!;
    const remove = vi.spyOn(scene, "removeEventListener");
    expect(scene).toHaveAttribute("data-route-motion", "active");

    fireEvent.animationEnd(scene);
    expect(scene).toHaveAttribute("data-route-motion", "settled");
    expect(disconnect).toHaveBeenCalledOnce();
    expect(remove).toHaveBeenCalledWith("animationend", expect.any(Function));
    expect(remove).toHaveBeenCalledWith("animationcancel", expect.any(Function));
    fireEvent.animationEnd(scene);
    expect(disconnect).toHaveBeenCalledOnce();
  });

  it("settles active motion immediately when the reduced-motion preference changes", () => {
    const target = new EventTarget();
    const preference = {
      matches: false,
      addEventListener: vi.fn<(name: string, listener: EventListener) => void>((name, listener) =>
        target.addEventListener(name, listener),
      ),
      removeEventListener: vi.fn<(name: string, listener: EventListener) => void>(
        (name, listener) => target.removeEventListener(name, listener),
      ),
    };
    vi.spyOn(window, "matchMedia").mockReturnValue(preference as unknown as MediaQueryList);
    const disconnect = vi.spyOn(MutationObserver.prototype, "disconnect");
    const { container } = render(
      <MemoryRouter>
        <MotionHarness />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "insights" }));
    const scene = container.querySelector<HTMLElement>("[data-route-scene]")!;
    expect(scene).toHaveAttribute("data-route-motion", "active");

    act(() => {
      preference.matches = true;
      target.dispatchEvent(new Event("change"));
    });
    expect(scene).toHaveAttribute("data-route-motion", "settled");
    expect(disconnect).toHaveBeenCalledOnce();
    expect(preference.removeEventListener).toHaveBeenCalledWith("change", expect.any(Function));
    act(() => {
      preference.matches = false;
      target.dispatchEvent(new Event("change"));
    });
    expect(scene).toHaveAttribute("data-route-motion", "settled");
  });

  it("waits for the returning card and restores its focus without scrolling to the heading", async () => {
    const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    render(
      <RecordsArchiveProvider>
        <MemoryRouter>
          <ArchiveFocusHarness />
        </MemoryRouter>
      </RecordsArchiveProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "open record" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "議論詳細" })).toHaveFocus());
    fireEvent.click(screen.getByRole("button", { name: "return archive" }));
    expect(screen.getByRole("heading", { name: "議論の記録" })).not.toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "finish archive" }));
    await waitFor(() => expect(screen.getByRole("link", { name: "元の記録" })).toHaveFocus());
    expect(scroll).toHaveBeenCalledWith({ top: 0, behavior: "instant" });
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
  });

  it("falls back to the archive heading only after a missing returning card has settled", async () => {
    render(
      <RecordsArchiveProvider>
        <MemoryRouter>
          <ArchiveFocusHarness missing />
        </MemoryRouter>
      </RecordsArchiveProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "open record" }));
    fireEvent.click(screen.getByRole("button", { name: "return archive" }));
    expect(screen.getByRole("heading", { name: "議論の記録" })).not.toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "finish archive" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "議論の記録" })).toHaveFocus());
  });
  it("stays still initially and remounts only for pathname navigation", async () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/"]}>
        <MotionHarness />
      </MemoryRouter>,
    );

    const initialScene = container.querySelector<HTMLElement>("[data-route-scene]");
    expect(initialScene).toHaveAttribute("data-route-scene", "/");
    expect(initialScene).toHaveAttribute("data-route-motion", "idle");
    expect(initialScene?.parentElement).toHaveAttribute("data-route-kind", "archive");

    fireEvent.click(screen.getByRole("button", { name: /rerender/ }));
    expect(container.querySelector("[data-route-scene]")).toBe(initialScene);
    expect(initialScene).toHaveAttribute("data-route-motion", "idle");

    fireEvent.click(screen.getByRole("button", { name: "insights" }));
    const insightsHeading = screen.getByRole("heading", { name: "いろいろな記録" });
    const insightsScene = container.querySelector<HTMLElement>("[data-route-scene]");
    expect(insightsScene).not.toBe(initialScene);
    expect(insightsScene).toHaveAttribute("data-route-scene", "/insights");
    expect(insightsScene).toHaveAttribute("data-route-motion", "active");
    expect(insightsScene?.parentElement).toHaveAttribute("data-route-kind", "insights");
    await waitFor(() => expect(insightsHeading).toHaveFocus());

    fireEvent.click(screen.getByRole("button", { name: /rerender/ }));
    expect(container.querySelector("[data-route-scene]")).toBe(insightsScene);

    fireEvent.click(screen.getByRole("button", { name: "detail" }));
    const detailHeading = screen.getByRole("heading", { name: "議論詳細" });
    expect(container.querySelector("[data-route-scene]")?.parentElement).toHaveAttribute(
      "data-route-kind",
      "detail",
    );
    expect(container.querySelector("[data-route-scene]")).toHaveAttribute(
      "data-route-motion",
      "active",
    );
    await waitFor(() => expect(detailHeading).toHaveFocus());
  });

  it.each(["animationend", "animationcancel"])(
    "settles asynchronously mounted content on %s without waiting indefinitely",
    async (eventName) => {
      const { container } = render(
        <MemoryRouter initialEntries={["/"]}>
          <DelayedMotionHarness />
        </MemoryRouter>,
      );

      fireEvent.click(screen.getByRole("button", { name: "open detail" }));
      const scene = container.querySelector<HTMLElement>('[data-route-scene^="/records/"]')!;
      expect(scene).toHaveAttribute("data-route-motion", "waiting");

      fireEvent(scene, new Event(eventName, { bubbles: true }));
      expect(scene).toHaveAttribute("data-route-motion", "waiting");

      fireEvent.click(screen.getByRole("button", { name: "finish loading" }));
      const heading = screen.getByRole("heading", { name: "非同期の議論詳細" });
      await waitFor(() => expect(heading).toHaveFocus());
      await waitFor(() => expect(scene).toHaveAttribute("data-route-motion", "active"));

      fireEvent(
        scene.querySelector<HTMLElement>("[data-route-motion-terminal]")!,
        new Event(eventName, { bubbles: true }),
      );
      await waitFor(() => expect(scene).toHaveAttribute("data-route-motion", "settled"));
    },
  );
});
