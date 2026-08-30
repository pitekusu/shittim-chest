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
        { name: "pending_count", value: 0 },
        { name: "deployment_count", value: 1 },
        { name: "service_status", value: "ACTIVE" },
        { name: "scheduling_strategy", value: "REPLICA" },
        { name: "launch_mode", value: "FARGATE" },
        { name: "platform_version", value: "1.4.0" },
        { name: "task_definition_revision", value: 42 },
        { name: "rollout_state", value: "COMPLETED" },
        { name: "failed_task_count", value: 0 },
        { name: "deployment_updated_at", value: "2026-08-24T03:00:00Z" },
        { name: "deployment_controller", value: "ECS" },
        { name: "minimum_healthy_percent", value: 0 },
        { name: "maximum_percent", value: 100 },
        { name: "circuit_breaker_enabled", value: true },
        { name: "circuit_breaker_rollback", value: true },
        { name: "execute_command_enabled", value: false },
        { name: "active_debates", value: 0 },
        { name: "outbox_pending", value: 0 },
        { name: "runtime_prompt_revision", value: null },
        { name: "heartbeat_age_seconds", value: "42.000" },
      ],
      details: {
        kind: "ecs",
        nextTaskImageTags: ["release-2026-08-24", "stable"],
      },
    },
    {
      service: "ecr",
      state: "healthy",
      summary: "タグ付きイメージと保管庫の保護を確認しました。",
      metrics: [
        { name: "tag_mutability", value: "IMMUTABLE" },
        { name: "encryption_type", value: "KMS" },
        { name: "repository_created_at", value: "2026-07-24T03:00:00Z" },
        { name: "scan_on_push", value: false },
        { name: "repository_image_count", value: 6 },
        { name: "repository_tagged_image_count", value: 2 },
        { name: "repository_untagged_image_count", value: 4 },
        { name: "repository_total_size_bytes", value: 104857600 },
        { name: "repository_latest_pushed_at", value: "2026-08-24T03:00:00Z" },
      ],
      details: {
        kind: "ecr",
        images: [
          {
            tags: ["release-2026-08-24", "stable"],
            mediaType: "OCI_IMAGE",
            sizeBytes: 52428800,
            pushedAt: "2026-08-24T03:00:00Z",
            lastPulledAt: "2026-08-24T04:00:00Z",
          },
          {
            tags: ["release-2026-08-17"],
            mediaType: "OCI_IMAGE",
            sizeBytes: 52428800,
            pushedAt: "2026-08-17T03:00:00Z",
            lastPulledAt: "2026-08-20T04:00:00Z",
          },
        ],
      },
    },
    {
      service: "inspector",
      state: "critical",
      summary: "タグ付きコンテナイメージ別の検出結果を確認しました。",
      metrics: [
        { name: "active_critical", value: 1 },
        { name: "active_high", value: 1 },
        { name: "active_medium", value: 2 },
        { name: "active_low", value: 3 },
        { name: "active_untriaged", value: 0 },
        { name: "coverage_count", value: 1 },
        { name: "coverage_active", value: 1 },
        { name: "last_scanned_at", value: "2026-08-24T04:10:00Z" },
        { name: "translation_cache_count", value: 1 },
        { name: "translation_missing_count", value: 1 },
        { name: "translation_last_translated_at", value: "2026-08-24T04:05:00Z" },
      ],
      details: {
        kind: "inspector",
        images: [
          {
            tags: ["release-2026-08-24", "stable"],
            scanStatus: "ACTIVE",
            lastScannedAt: "2026-08-24T04:10:00Z",
            counts: { total: 7, critical: 1, high: 1, medium: 2, low: 3, untriaged: 0 },
            findings: [
              {
                vulnerabilityId: "CVE-2026-12345",
                severity: "critical",
                summaryJa:
                  "入力値の境界確認が不十分なため、遠隔の攻撃者が細工したデータを送ると、対象プロセスが本来の範囲外にあるメモリを読み取る可能性があります。その結果、処理の異常終了や、プロセス内で扱われる情報の一部が意図せず露出するおそれがある脆弱性です。",
                affectedPackages: [
                  {
                    name: "example-package",
                    installedVersion: "1.2.3-4",
                    fixedVersion: "1.2.4-1",
                    packageManager: "OS",
                  },
                ],
                fixAvailable: "YES",
              },
              {
                vulnerabilityId: "CVE-2026-67890",
                severity: "high",
                summaryJa: null,
                affectedPackages: [
                  {
                    name: "second-package",
                    installedVersion: "2.0.0",
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
    },
    {
      service: "dynamodb",
      state: "healthy",
      summary: "Table状態と保護設定を確認しました。",
      metrics: [
        ...["debate", "archive", "statistics", "session"].flatMap((key, index) => [
          { name: `${key}_status`, value: "ACTIVE" },
          { name: `${key}_pitr`, value: "ENABLED" },
          { name: `${key}_deletion_protection`, value: true },
          { name: `${key}_ttl`, value: key === "session" ? "ENABLED" : "DISABLED" },
          { name: `${key}_item_count`, value: [2231, 684, 12, 7][index] ?? 0 },
          { name: `${key}_read_throttles`, value: 0 },
          { name: `${key}_write_throttles`, value: 0 },
        ]),
        { name: "debate_stream_enabled", value: true },
        { name: "debate_stream_view_type", value: "NEW_IMAGE" },
      ],
    },
    {
      service: "lambda",
      state: "healthy",
      summary: "Lambda状態と直近1時間の指標を確認しました。",
      metrics: [
        { name: "records_admin_config_state", value: "ACTIVE" },
        { name: "records_admin_config_update", value: "SUCCESSFUL" },
        { name: "records_admin_config_hour_invocations", value: 0 },
        { name: "records_admin_config_hour_errors", value: 0 },
        { name: "records_admin_config_hour_throttles", value: 0 },
        { name: "records_admin_config_hour_duration", value: null },
      ],
    },
    {
      service: "apigateway",
      state: "healthy",
      summary: "HTTP APIを確認しました。",
      metrics: [
        { name: "discord_protocol", value: "HTTP" },
        { name: "discord_auto_deploy", value: true },
        { name: "discord_hour_requests", value: 12 },
        { name: "discord_hour_4xx", value: 0 },
        { name: "discord_hour_5xx", value: 0 },
        { name: "discord_hour_latency", value: "44.500" },
        { name: "discord_hour_integration_latency", value: "31.250" },
      ],
    },
    {
      service: "eventbridge",
      state: "healthy",
      summary: "定期実行を確認しました。",
      metrics: [
        { name: "runtime_state", value: "ENABLED" },
        { name: "runtime_expression", value: "rate(1 minute)" },
        { name: "runtime_retry_attempts", value: 2 },
      ],
    },
    {
      service: "cloudformation",
      state: "healthy",
      summary: "Stackを確認しました。",
      metrics: [
        { name: "runtime_status", value: "UPDATE_COMPLETE" },
        { name: "runtime_drift", value: "IN_SYNC" },
        { name: "runtime_termination_protection", value: true },
        { name: "runtime_updated_at", value: "2026-08-24T03:00:00Z" },
      ],
    },
    {
      service: "sns",
      state: "healthy",
      summary: "通知を確認しました。",
      metrics: [{ name: "confirmed_subscriptions", value: 1 }],
    },
    {
      service: "ssm",
      state: "healthy",
      summary: "設定metadataを確認しました。",
      metrics: [
        { name: "discord_ready", value: 5 },
        { name: "discord_required", value: 5 },
        { name: "runtime_ready", value: 6 },
        { name: "runtime_required", value: 6 },
        { name: "records_ready", value: 7 },
        { name: "records_required", value: 7 },
        { name: "cost_ready", value: 2 },
        { name: "cost_required", value: 2 },
        { name: "runtime_prompt_pointer_present", value: false },
      ],
    },
    {
      service: "cost_governance",
      state: "healthy",
      summary: "予算と異常通知を確認しました。",
      metrics: [
        { name: "project_actual_percent", value: "24.5" },
        { name: "project_forecast_percent", value: "31.2" },
        { name: "project_health", value: "HEALTHY" },
      ],
    },
    {
      service: "signer",
      state: "healthy",
      summary: "署名profileを確認しました。",
      metrics: [
        { name: "status", value: "Active" },
        { name: "platform", value: "Notation-OCI-SHA384-ECDSA" },
        { name: "validity_value", value: 12 },
        { name: "validity_unit", value: "MONTHS" },
      ],
    },
    {
      service: "external",
      state: "healthy",
      summary: "外部集計を確認しました。",
      metrics: [
        { name: "openai_initial_complete", value: true },
        { name: "openai_fresh", value: true },
        { name: "openai_last_success_at", value: "2026-08-24T03:00:00Z" },
      ],
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

  it("shows overview and panel-level AWS states", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(response(statusResponse))),
    );

    const { container } = renderAdmin();

    expect(await screen.findByRole("heading", { name: "サービス状態確認" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "現在の状態" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "サービス状態" })).toBeVisible();
    expect(screen.getByText("SERVICE STATUS")).toBeVisible();
    expect(await screen.findByText("Scale-to-Zeroで待機しています。")).toBeVisible();
    const alarmLink = screen.getByRole("link", { name: /DynamoDBのスロットリング/ });
    expect(alarmLink).toHaveAttribute("href", "#admin-service-dynamodb");
    expect(screen.getByRole("link", { name: "ECS" })).toHaveAttribute("href", "#admin-service-ecs");
    const inspectorLink = screen.getByRole("link", { name: "Inspector（異常）" });
    expect(inspectorLink).toHaveAttribute("data-state", "critical");
    expect(inspectorLink.querySelector("svg")).not.toBeNull();
    expect(screen.getByRole("link", { name: "ECS" })).not.toHaveAttribute("data-state");
    expect(screen.getByText("タスク稼働")).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "タスク定義" })).toBeVisible();
    expect(screen.getByText("Fargate On-Demand")).toBeVisible();
    expect(screen.getAllByText("rev. 42")).toHaveLength(2);
    const nextTaskImage = screen.getByRole("region", {
      name: "次回起動タスクのコンテナイメージ",
    });
    expect(nextTaskImage).toHaveTextContent("NEXT");
    expect(nextTaskImage).not.toHaveTextContent("NEXT TASK IMAGE");
    expect(nextTaskImage).toHaveTextContent("次回起動時に使用");
    expect(nextTaskImage).toHaveTextContent("rev. 42");
    expect(nextTaskImage).toHaveTextContent("release-2026-08-24");
    expect(nextTaskImage).toHaveTextContent("stable");
    expect(within(nextTaskImage).getByRole("link", { name: /ECR一覧で確認/ })).toHaveAttribute(
      "href",
      "#admin-service-ecr",
    );
    expect(screen.getByRole("columnheader", { name: "最終取得記録" })).toBeVisible();
    expect(screen.getAllByText("release-2026-08-24").length).toBeGreaterThan(0);
    const ecrImages = screen.getByRole("region", { name: "タグ付きECRイメージ" });
    const nextTaskRow = within(ecrImages)
      .getByText("release-2026-08-24", { exact: true })
      .closest("tr");
    const previousTaskRow = within(ecrImages)
      .getByText("release-2026-08-17", { exact: true })
      .closest("tr");
    expect(nextTaskRow).toHaveAttribute("data-next-task-image", "true");
    expect(nextTaskRow).toHaveTextContent("次回起動時に使用");
    expect(previousTaskRow).not.toHaveAttribute("data-next-task-image");
    expect(previousTaskRow).not.toHaveTextContent("次回起動時に使用");
    const alternateTagLabels = screen.getAllByText("同一イメージの別タグ");
    expect(alternateTagLabels).toHaveLength(3);
    for (const label of alternateTagLabels) {
      expect(label.parentElement).toHaveTextContent("stable");
    }
    expect(screen.queryByText("本番版")).not.toBeInTheDocument();
    expect(screen.getByText("合計容量（概算）")).toBeVisible();
    expect(screen.getByText("100 MiB")).toBeVisible();
    expect(screen.getByText("CVE-2026-12345")).toBeVisible();
    expect(screen.getAllByText("影響を受けるパッケージ")).toHaveLength(2);
    expect(
      screen.getByText("Inspectorの説明文をもとに、日本語概要を準備しています。"),
    ).toBeVisible();
    expect(screen.getAllByText("example-package").length).toBeGreaterThan(0);
    expect(screen.getByText("1.2.3-4")).toBeVisible();
    expect(screen.queryByText("閲覧専用")).not.toBeInTheDocument();
    const translationCache = screen.getByRole("region", {
      name: "脆弱性概要翻訳キャッシュ",
    });
    expect(
      within(translationCache).getByText("翻訳キャッシュ件数").parentElement,
    ).toHaveTextContent("1");
    expect(within(translationCache).getByText("未翻訳件数").parentElement).toHaveTextContent("1");
    expect(within(translationCache).getByText("最終翻訳日時").parentElement).toHaveTextContent(
      "2026年8月24日 13:05",
    );
    expect(screen.getByRole("columnheader", { name: "自動反映" })).toBeVisible();
    const lambdaTable = screen.getByRole("region", { name: "Lambda関数状態" });
    expect(within(lambdaTable).getByRole("rowheader", { name: "Runtime調整" })).toBeVisible();
    expect(within(lambdaTable).getByRole("rowheader", { name: "プロンプト管理API" })).toBeVisible();
    expect(screen.getByRole("rowheader", { name: "討論Runtime" })).toBeVisible();
    expect(screen.getByText("Discord連携")).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "実績使用率" })).toBeVisible();
    expect(screen.getByRole("rowheader", { name: "OpenAI Costs" })).toBeVisible();
    expect(screen.getByText("署名時から12か月間有効")).toBeVisible();
    expect(container.querySelector("#admin-service-sns")?.nextElementSibling).toBe(
      container.querySelector("#admin-service-signer"),
    );
    expect(screen.queryByText("desired_count")).not.toBeInTheDocument();
    expect(screen.queryByText("coverage")).not.toBeInTheDocument();
    expect(screen.queryByText("project_actual_percent")).not.toBeInTheDocument();
    expect(screen.queryByText("アクセス")).not.toBeInTheDocument();
    expect(
      screen.queryByText("AWSの稼働状態を、安全な境界の内側で確認します。"),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "プロンプト管理" })).not.toBeInTheDocument();
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

    const { container } = renderAdmin();

    expect(screen.getByRole("status")).toHaveTextContent("AWSの状態を読み込んでいます");
    expect(screen.getByRole("status")).toHaveTextContent("16サービスを並列で確認しています");
    expect(screen.getByRole("button", { name: "状態を更新" })).toBeDisabled();
    expect(container.querySelectorAll("ol li")).toHaveLength(4);
  });

  it("shows the canonical Inspector total without masking unavailable details", async () => {
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
                    tags: ["missing-details"],
                    scanStatus: "ACTIVE",
                    lastScannedAt: "2026-08-24T04:10:00Z",
                    counts: {
                      total: 3,
                      critical: 1,
                      high: 0,
                      medium: 0,
                      low: 0,
                      untriaged: 0,
                    },
                    findings: [],
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

    const imageHeading = await screen.findByRole("heading", { name: "missing-details" });
    const image = imageHeading.closest("section");
    expect(image).not.toBeNull();
    const inspectorImage = within(image as HTMLElement);
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
