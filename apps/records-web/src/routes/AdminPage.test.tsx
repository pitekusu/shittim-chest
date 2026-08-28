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
    },
    {
      service: "ecr",
      state: "healthy",
      summary: "承認済みイメージとrepository保護を確認しました。",
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
        { name: "normal_image_present", value: true },
        { name: "normal_pushed_at", value: "2026-08-24T03:00:00Z" },
        { name: "normal_last_pulled_at", value: "2026-08-24T04:00:00Z" },
        { name: "normal_size_bytes", value: 52428800 },
        { name: "normal_tag_count", value: 1 },
        { name: "normal_media_type", value: "OCI_IMAGE" },
      ],
    },
    {
      service: "inspector",
      state: "unknown",
      summary: "現在は確認できません。",
      metrics: [{ name: "coverage", value: null }],
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
        { name: "records_ready", value: 6 },
        { name: "records_required", value: 6 },
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
      metrics: [{ name: "platform", value: "Notation-OCI-SHA384-ECDSA" }],
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

    expect(await screen.findByRole("heading", { name: "管理コンソール" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "現在の状態" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "サービス状態" })).toBeVisible();
    expect(await screen.findByText("Scale-to-Zeroで待機しています。")).toBeVisible();
    expect(screen.getByText("現在は確認できません。")).toBeVisible();
    expect(screen.getByText("タスク稼働")).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "タスク定義" })).toBeVisible();
    expect(screen.getByText("Fargate On-Demand")).toBeVisible();
    expect(screen.getByText("rev. 42")).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "最終取得記録" })).toBeVisible();
    expect(screen.getByText("合計容量（概算）")).toBeVisible();
    expect(screen.getByText("100 MiB")).toBeVisible();
    expect(screen.getByText("検査対象")).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "自動反映" })).toBeVisible();
    expect(screen.getByRole("rowheader", { name: "Runtime調整" })).toBeVisible();
    expect(screen.getByRole("rowheader", { name: "討論Runtime" })).toBeVisible();
    expect(screen.getByText("Discord連携")).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "実績使用率" })).toBeVisible();
    expect(screen.getByRole("rowheader", { name: "OpenAI Costs" })).toBeVisible();
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
