import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { describe, expect, it } from "vite-plus/test";

import { BrandedRouteStage, routeMotionKind } from "./RouteMotion";

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
});
