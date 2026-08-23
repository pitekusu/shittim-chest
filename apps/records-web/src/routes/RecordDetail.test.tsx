import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { RECORD_ID, mockApi, renderRoute } from "../test/recordsTestUtils";
import RecordDetail from "./RecordDetail";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("RecordDetail", () => {
  it("renders named votes and only the saved final result", async () => {
    mockApi();

    renderRoute(<RecordDetail />, {
      initialEntry: `/records/${RECORD_ID}`,
      path: "/records/:recordId",
    });

    expect(await screen.findByRole("heading", { name: "休日の過ごし方を決める" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "3人の意見" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "投票" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "アロナ → プラナ" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "最終決定" })).toBeVisible();
    expect(screen.getByText("勝利しました")).toBeVisible();
    expect(screen.getByText("実行する")).toBeVisible();
    expect(screen.getByText("2026年8月15日 15:00")).toHaveAttribute(
      "datetime",
      "2026-08-15T06:00:00Z",
    );
  });
});
