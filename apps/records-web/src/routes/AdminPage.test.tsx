import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import type { AdminStatusResponse } from "../api/types";
import { response } from "../test/recordsTestUtils";
import AdminPage from "./AdminPage";

const statusResponse: AdminStatusResponse = {
  schemaVersion: 1,
  generatedAt: "2026-08-24T03:00:00Z",
  expiresAt: "2026-08-24T03:01:00Z",
  stale: false,
  overall: { state: "warning", criticalAlarms: 0, warningAlarms: 1, partial: true },
  sections: [
    {
      service: "ecs",
      state: "healthy",
      summary: "Scale-to-Zeroで待機しています。",
      metrics: [
        { name: "desired_count", value: 0 },
        { name: "running_count", value: 0 },
      ],
    },
    {
      service: "inspector",
      state: "unknown",
      summary: "現在は確認できません。",
      metrics: [{ name: "coverage", value: null }],
    },
  ],
};

function renderAdmin(isAdmin = true) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdminPage isAdmin={isAdmin} csrfToken="csrf-token" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("AdminPage", () => {
  it("does not request Admin data for a non-admin member", () => {
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);

    renderAdmin(false);

    expect(screen.getByRole("heading", { name: "ACCESS DENIED" })).toBeVisible();
    expect(screen.getByText("403")).toBeVisible();
    expect(screen.getByRole("link", { name: "記録一覧へ戻る" })).toHaveAttribute("href", "/");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows overview and panel-level AWS states", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(statusResponse))),
    );

    renderAdmin();

    expect(await screen.findByRole("heading", { name: "ADMIN" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "現在の状態" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "サービス状態" })).toBeVisible();
    expect(await screen.findByText("Scale-to-Zeroで待機しています。")).toBeVisible();
    expect(screen.getByText("現在は確認できません。")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "プロンプト管理" })).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "一部のサービスを確認できませんでした。取得できた状態だけを表示しています。",
      ),
    ).toBeVisible();
  });

  it("refreshes status with the write-boundary headers", async () => {
    const requests: { path: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        requests.push({ path, init });
        return Promise.resolve(response(statusResponse));
      }),
    );

    renderAdmin();
    fireEvent.click(await screen.findByRole("button", { name: "状態を更新" }));

    await waitFor(() => expect(requests).toHaveLength(2));
    expect(requests[1]?.path).toBe("/api/v1/admin/status/refresh");
    expect(new Headers(requests[1]?.init?.headers).get("X-CSRF-Token")).toBe("csrf-token");
    expect(new Headers(requests[1]?.init?.headers).get("X-Idempotency-Key")).toMatch(
      /^[0-9a-f-]{36}$/,
    );
  });
});
