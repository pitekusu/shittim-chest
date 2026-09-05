import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  overall: {
    state: "warning",
    criticalAlarms: 0,
    warningAlarms: 1,
    partial: true,
    activeAlarms: [{ code: "dynamo-db-throttle", severity: "warning", service: "dynamodb" }],
  },
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
      service: "dynamodb",
      state: "healthy",
      summary: "Table状態と保護設定を確認しました。",
      metrics: [{ name: "affection_ranking_fresh", value: true }],
    },
    {
      service: "inspector",
      state: "unknown",
      summary: "状態を取得できません。",
      metrics: [],
    },
  ],
};

function renderAdmin(isAdmin = true, view: "status" | "prompts" = "status") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdminPage isAdmin={isAdmin} csrfToken="csrf-token" view={view} />
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
  it("loads service status for a non-admin member", async () => {
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(response(statusResponse)));
    vi.stubGlobal("fetch", fetchMock);

    renderAdmin(false);

    expect(await screen.findByRole("heading", { name: "サービス状態確認" })).toBeVisible();
    expect(await screen.findByText("Scale-to-Zeroで待機しています。")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "ACCESS DENIED" })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual(["/api/v1/admin/status"]);
  });

  it("loads prompt management and its independent application-status snapshot", async () => {
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);

    renderAdmin(true, "prompts");

    expect(screen.getByRole("heading", { name: "プロンプト管理" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "プロンプト編集" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "変更履歴" })).toBeVisible();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual(
      expect.arrayContaining([
        "/api/v1/admin/prompts",
        "/api/v1/admin/prompts/revisions",
        "/api/v1/admin/status",
      ]),
    );
  });

  it("keeps healthy panels and alarm navigation usable when a service is unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(statusResponse))),
    );

    renderAdmin();

    expect(await screen.findByText("Scale-to-Zeroで待機しています。")).toBeVisible();
    expect(screen.getByRole("link", { name: /DynamoDBのスロットリング/ })).toHaveAttribute(
      "href",
      "#admin-service-dynamodb",
    );
    expect(screen.getByRole("link", { name: "Inspector" })).toHaveAttribute(
      "href",
      "#admin-service-inspector",
    );
    expect(screen.getByText("状態を取得できません。")).toBeVisible();
    expect(
      screen.getByText(
        "一部のサービスを確認できませんでした。取得できた状態だけを表示しています。",
      ),
    ).toBeVisible();
  });

  it("shows animated graphical progress while the AWS snapshot is pending", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => undefined)),
    );

    renderAdmin();

    expect(screen.getByRole("status")).toHaveTextContent("AWSの状態を読み込んでいます");
    expect(screen.getByRole("button", { name: "状態を更新" })).toBeDisabled();
  });

  it("highlights DynamoDB when the affection ranking checkpoint is stale", async () => {
    const warningResponse: AdminStatusResponse = {
      ...statusResponse,
      sections: statusResponse.sections.map((section) =>
        section.service === "dynamodb"
          ? {
              ...section,
              state: "warning",
              summary: "親愛度データの初期化またはランキング更新を確認してください。",
              metrics: section.metrics.map((metric) =>
                metric.name === "affection_ranking_fresh" ? { ...metric, value: false } : metric,
              ),
            }
          : section,
      ),
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(warningResponse))),
    );

    renderAdmin();

    const link = await screen.findByRole("link", { name: "DynamoDB（注意）" });
    expect(link).toHaveAttribute("data-state", "warning");
    expect(link).toHaveAttribute("href", "#admin-service-dynamodb");
  });

  it("groups image tags and keeps findings visible when translations or details are missing", async () => {
    const responseWithMissingDetails: AdminStatusResponse = {
      ...statusResponse,
      sections: statusResponse.sections.map((section) =>
        section.service === "inspector"
          ? {
              ...section,
              details: {
                kind: "inspector",
                images: [
                  {
                    tags: ["missing-details", "stable"],
                    scanStatus: "ACTIVE",
                    lastScannedAt: "2026-08-24T04:10:00Z",
                    counts: {
                      total: 3,
                      critical: 1,
                      high: 1,
                      medium: 0,
                      low: 0,
                      untriaged: 0,
                    },
                    findings: [
                      {
                        vulnerabilityId: "CVE-2026-12345",
                        severity: "high",
                        summaryJa: null,
                        affectedPackages: [
                          {
                            name: "example-package",
                            installedVersion: "1.0.0",
                            fixedVersion: null,
                            packageManager: "OS",
                          },
                        ],
                        fixAvailable: "NO",
                      },
                    ],
                  },
                ],
              },
            }
          : section,
      ),
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(responseWithMissingDetails))),
    );

    renderAdmin();

    const imageHeading = await screen.findByRole("heading", { name: /^missing-details/ });
    expect(within(imageHeading).getByText("missing-details")).toBeVisible();
    expect(within(imageHeading).getByText("stable")).toBeVisible();
    const image = imageHeading.closest("section");
    expect(image).not.toBeNull();
    const inspectorImage = within(image as HTMLElement);
    expect(inspectorImage.getByText("CVE-2026-12345")).toBeVisible();
    expect(inspectorImage.getByText("example-package")).toBeVisible();
    expect(image?.querySelector('[data-pending="true"]')).toHaveTextContent(/日本語.+準備/);
    expect(inspectorImage.getByText("合計").parentElement).toHaveTextContent("3");
    expect(
      inspectorImage.queryByText("重大・高の脆弱性は検出されていません。"),
    ).not.toBeInTheDocument();
    expect(
      inspectorImage.getByText("重大・高のうち1件はパッケージ詳細を取得できませんでした。"),
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
    const refreshButton = await screen.findByRole("button", { name: "状態を更新" });
    await waitFor(() => expect(refreshButton).toBeEnabled());
    fireEvent.click(refreshButton);

    await waitFor(() => expect(requests).toHaveLength(2));
    expect(requests[1]?.path).toBe("/api/v1/admin/status/refresh");
    expect(new Headers(requests[1]?.init?.headers).get("X-CSRF-Token")).toBe("csrf-token");
    expect(new Headers(requests[1]?.init?.headers).get("X-Idempotency-Key")).toMatch(
      /^[0-9a-f-]{36}$/,
    );
  });
});
