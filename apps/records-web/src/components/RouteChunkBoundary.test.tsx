import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { lazy, Suspense } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { BrandedRouteStage } from "../RouteMotion";
import { placeholder } from "../test/recordsTestUtils";
import { Layout } from "./Layout";
import { RouteChunkBoundary, RouteLoadingFallback } from "./RouteChunkBoundary";

function BrokenRoute(): never {
  throw new Error("private route failure");
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("RouteChunkBoundary", () => {
  it("keeps successful route content unchanged", () => {
    render(
      <RouteChunkBoundary>
        <h1>議論の記録</h1>
      </RouteChunkBoundary>,
    );

    expect(screen.getByRole("heading", { name: "議論の記録" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("replaces failed route content with a reload action", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const reload = vi.fn<() => void>();
    const browserWindow = window;
    const testWindow = Object.create(browserWindow) as Window & typeof globalThis;
    Object.defineProperty(testWindow, "location", {
      configurable: true,
      value: { reload },
    });
    vi.stubGlobal("window", testWindow);

    render(
      <RouteChunkBoundary>
        <BrokenRoute />
      </RouteChunkBoundary>,
    );

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("画面を読み込めませんでした");
    expect(alert).toHaveAttribute("data-route-motion-ready");
    expect(alert).toHaveAttribute("data-route-motion-terminal");

    fireEvent.click(screen.getByRole("button", { name: "再読み込み" }));
    expect(reload).toHaveBeenCalledOnce();
  });

  it("handles a rejected lazy route import without exposing the exception", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const LazyRoute = lazy(() =>
      Promise.reject(new TypeError("Failed to fetch dynamically imported module: private-url")),
    );

    render(
      <RouteChunkBoundary>
        <Suspense fallback={<RouteLoadingFallback />}>
          <LazyRoute />
        </Suspense>
      </RouteChunkBoundary>,
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("画面を読み込めませんでした");
    expect(alert).not.toHaveTextContent("private-url");
  });
});

describe("RouteLoadingFallback", () => {
  it("keeps the shell responsive while a lazy route module resolves", async () => {
    let resolveRoute: ((module: { default: () => React.JSX.Element }) => void) | undefined;
    const LazyRoute = lazy(
      () =>
        new Promise<{ default: () => React.JSX.Element }>((resolve) => {
          resolveRoute = resolve;
        }),
    );

    render(
      <RouteChunkBoundary>
        <Suspense fallback={<RouteLoadingFallback />}>
          <LazyRoute />
        </Suspense>
      </RouteChunkBoundary>,
    );

    const loading = screen.getByText("画面を読み込んでいます。");
    expect(loading).toHaveAttribute("aria-busy", "true");
    expect(loading).toHaveAttribute("aria-live", "polite");

    await act(async () => {
      resolveRoute?.({ default: () => <h1>遅延読み込み済み</h1> });
    });

    expect(screen.getByRole("heading", { name: "遅延読み込み済み" })).toBeInTheDocument();
    expect(screen.queryByText("画面を読み込んでいます。")).not.toBeInTheDocument();
  });

  it("keeps Layout mounted while a route resolves and then focuses its heading", async () => {
    let resolveRoute: ((module: { default: () => React.JSX.Element }) => void) | undefined;
    const LazyRoute = lazy(
      () =>
        new Promise<{ default: () => React.JSX.Element }>((resolve) => {
          resolveRoute = resolve;
        }),
    );

    function Harness() {
      const location = useLocation();
      return (
        <Layout
          displayName="閲覧者"
          avatar={placeholder("閲覧者", "cyan")}
          onLogout={() => undefined}
          theme="light"
          onThemeToggle={() => undefined}
        >
          <BrandedRouteStage>
            <RouteChunkBoundary key={location.pathname}>
              <Suspense fallback={<RouteLoadingFallback />}>
                <Routes>
                  <Route path="/" element={<h1 tabIndex={-1}>議論の記録</h1>} />
                  <Route path="/insights" element={<LazyRoute />} />
                </Routes>
              </Suspense>
            </RouteChunkBoundary>
          </BrandedRouteStage>
        </Layout>
      );
    }

    render(
      <MemoryRouter initialEntries={["/"]}>
        <Harness />
      </MemoryRouter>,
    );

    const sidebar = screen.getByRole("complementary", { name: "主要ナビゲーション" });
    fireEvent.click(screen.getByRole("link", { name: "いろいろな記録" }));
    expect(screen.getByText("画面を読み込んでいます。")).toBeVisible();
    expect(document.querySelector('[data-route-scene="/insights"]')).toHaveAttribute(
      "data-route-motion",
      "waiting",
    );
    expect(screen.getByRole("complementary", { name: "主要ナビゲーション" })).toBe(sidebar);

    await act(async () => {
      resolveRoute?.({
        default: () => (
          <section data-route-motion-ready="" data-route-motion-terminal="">
            <h1 tabIndex={-1}>いろいろな記録</h1>
          </section>
        ),
      });
    });

    const heading = screen.getByRole("heading", { name: "いろいろな記録" });
    await waitFor(() => expect(heading).toHaveFocus());
    await waitFor(() =>
      expect(document.querySelector('[data-route-scene="/insights"]')).toHaveAttribute(
        "data-route-motion",
        "active",
      ),
    );
    expect(screen.getByRole("complementary", { name: "主要ナビゲーション" })).toBe(sidebar);
  });
});
