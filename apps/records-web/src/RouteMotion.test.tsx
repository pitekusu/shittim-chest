import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { describe, expect, it } from "vite-plus/test";

import { BrandedRouteStage, routeMotionKind } from "./RouteMotion";
import styles from "./App.module.css";

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
          <Route path="/" element={<h1 tabIndex={-1}>議論の記録</h1>} />
          <Route path="/insights" element={<h1 tabIndex={-1}>いろいろな記録</h1>} />
          <Route path="/records/:recordId" element={<h1 tabIndex={-1}>議論詳細</h1>} />
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
    expect(routeMotionKind(`/records/${"r".repeat(43)}`)).toBe("detail");
    expect(routeMotionKind("/missing")).toBe("other");
  });
});

describe("BrandedRouteStage", () => {
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

  it("keeps motion active until asynchronously mounted route content finishes", async () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/"]}>
        <DelayedMotionHarness />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "open detail" }));
    const scene = container.querySelector<HTMLElement>('[data-route-scene^="/records/"]')!;
    expect(scene).toHaveAttribute("data-route-motion", "active");

    fireEvent.animationEnd(scene);
    expect(scene).toHaveAttribute("data-route-motion", "active");

    fireEvent.click(screen.getByRole("button", { name: "finish loading" }));
    const heading = screen.getByRole("heading", { name: "非同期の議論詳細" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(scene).toHaveAttribute("data-route-motion", "active");

    fireEvent.animationEnd(scene.querySelector<HTMLElement>("[data-route-motion-terminal]")!);
    await waitFor(() => expect(scene).toHaveAttribute("data-route-motion", "settled"));
  });
});
