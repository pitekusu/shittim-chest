import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import type { AdminPrompts, AdminStatusResponse } from "../api/types";
import { response } from "../test/recordsTestUtils";
import AdminPromptManager from "./AdminPromptManager";

const CURRENT_REVISION = `r${"1".repeat(26)}`;
const LATEST_REVISION = `r${"2".repeat(26)}`;
const PREVIOUS_REVISION = `r${"3".repeat(26)}`;
const SYSTEM_CONFIRMATION = "APPLY SYSTEM PROMPT";
const PROMPTS: AdminPrompts = {
  system: "system prompt",
  moderator: "moderator prompt",
  participantA: "arona prompt",
  participantB: "plana prompt",
  participantC: "abe prompt",
};

const STATUS: AdminStatusResponse = {
  schemaVersion: 1,
  generatedAt: "2026-08-29T03:00:00Z",
  expiresAt: "2026-08-29T03:01:00Z",
  stale: false,
  overall: { state: "healthy", criticalAlarms: 0, warningAlarms: 0, partial: false },
  sections: [
    {
      service: "ecs",
      state: "healthy",
      summary: "Scale-to-Zeroで待機しています。",
      metrics: [
        { name: "desired_count", value: 0 },
        { name: "running_count", value: 0 },
        { name: "runtime_prompt_revision", value: null },
      ],
    },
  ],
};

function currentPrompts(revision = CURRENT_REVISION, prompts = PROMPTS) {
  return {
    schemaVersion: 1,
    mode: "managed",
    activeRevision: revision,
    createdAt: "2026-08-29T03:00:00Z",
    action: "publish",
    prompts,
  } as const;
}

const REVISION_SUMMARY = {
  revision: PREVIOUS_REVISION,
  createdAt: "2026-08-28T03:00:00Z",
  action: "publish",
  baseRevision: null,
  sourceRevision: null,
  checksum: "a".repeat(64),
} as const;

const CURRENT_REVISION_SUMMARY = {
  revision: CURRENT_REVISION,
  createdAt: "2026-08-29T03:00:00Z",
  action: "publish",
  baseRevision: PREVIOUS_REVISION,
  sourceRevision: null,
  checksum: "b".repeat(64),
} as const;

function renderManager(canWrite = true): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <AdminPromptManager canWrite={canWrite} csrfToken="csrf-token" />
    </QueryClientProvider>,
  );
  return client;
}

function requestPath(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
}

function requestBody(init: RequestInit | undefined): unknown {
  if (typeof init?.body !== "string") throw new Error("expected JSON request body");
  return JSON.parse(init.body) as unknown;
}

function installApi({
  mode = "managed",
  detailPrompts = { ...PROMPTS, moderator: "previous moderator" },
  applyConflict = false,
}: {
  readonly mode?: "legacy" | "managed";
  readonly detailPrompts?: AdminPrompts;
  readonly applyConflict?: boolean;
} = {}) {
  const requests: { path: string; init?: RequestInit }[] = [];
  let promptReads = 0;
  let applyCalls = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      requests.push({ path, init });
      if (path === "/api/v1/admin/prompts") {
        promptReads += 1;
        if (mode === "legacy") {
          return Promise.resolve(
            response({
              schemaVersion: 1,
              mode: "legacy",
              activeRevision: null,
              createdAt: null,
              action: null,
              prompts: PROMPTS,
            }),
          );
        }
        return Promise.resolve(
          response(
            promptReads > 1 && applyConflict
              ? currentPrompts(LATEST_REVISION, { ...PROMPTS, participantB: "latest plana" })
              : currentPrompts(),
          ),
        );
      }
      if (path === "/api/v1/admin/status") return Promise.resolve(response(STATUS));
      if (path === "/api/v1/admin/prompts/revisions") {
        return Promise.resolve(
          response({
            schemaVersion: 1,
            items: [CURRENT_REVISION_SUMMARY, REVISION_SUMMARY],
            nextCursor: null,
          }),
        );
      }
      if (path === `/api/v1/admin/prompts/revisions/${PREVIOUS_REVISION}`) {
        return Promise.resolve(
          response({ schemaVersion: 1, ...REVISION_SUMMARY, prompts: detailPrompts }),
        );
      }
      if (path === "/api/v1/admin/prompts/apply") {
        applyCalls += 1;
        if (applyConflict && applyCalls === 1) {
          return Promise.resolve(
            response(
              {
                error: {
                  code: "PROMPT_REVISION_CONFLICT",
                  message: "最新revisionを確認してください。",
                  requestId: "request-id",
                },
              },
              409,
            ),
          );
        }
        return Promise.resolve(
          response({ schemaVersion: 1, revision: `r${"4".repeat(26)}`, state: "saved" }),
        );
      }
      if (path === "/api/v1/admin/prompts/rollback") {
        return Promise.resolve(
          response({ schemaVersion: 1, revision: `r${"5".repeat(26)}`, state: "saved" }),
        );
      }
      throw new Error(`unexpected request: ${path}`);
    }),
  );
  return requests;
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("AdminPromptManager", () => {
  it("lets a non-admin read current and historical prompts without write controls", async () => {
    const requests = installApi();
    renderManager(false);

    expect(await screen.findByRole("heading", { name: "プロンプト参照" })).toBeVisible();
    expect(
      screen.getByText(
        "閲覧専用です。プロンプトの反映とrevisionの復元は管理者だけが実行できます。",
      ),
    ).toBeVisible();
    expect(await screen.findByLabelText("システムプロンプト")).toHaveAttribute("readonly");
    expect(screen.queryByRole("button", { name: "変更を反映" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "現在の設定を管理版として登録" }),
    ).not.toBeInTheDocument();
    expect(await screen.findByText(PREVIOUS_REVISION)).toBeVisible();
    expect(screen.queryByRole("button", { name: "復元" })).not.toBeInTheDocument();
    expect(screen.getByText("使用中")).toBeVisible();
    expect(screen.queryByRole("button", { name: "内容を見る" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "変更点を見る" }));

    const moderatorSummary = await screen.findByText("事前調査AI", { selector: "summary" });
    fireEvent.click(moderatorSummary);
    const moderatorDiff = within(moderatorSummary.closest("details")!);
    expect(moderatorDiff.getByText("moderator prompt", { selector: "code" })).toBeVisible();
    expect(moderatorDiff.getByText("previous moderator", { selector: "code" })).toBeVisible();
    expect(moderatorDiff.getByText("− 選択revision")).toBeVisible();
    expect(moderatorDiff.getByText("＋ 現在")).toBeVisible();
    expect(screen.queryByRole("button", { name: "このrevisionを復元" })).not.toBeInTheDocument();
    expect(
      requests.some(
        ({ path }) =>
          path === "/api/v1/admin/prompts/apply" || path === "/api/v1/admin/prompts/rollback",
      ),
    ).toBe(false);
  });

  it("edits five keyboard tabs and requires exact confirmation for a normalized system change", async () => {
    const requests = installApi();
    renderManager();

    const systemTab = await screen.findByRole("tab", { name: "システム" });
    systemTab.focus();
    fireEvent.keyDown(systemTab, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "事前調査AI" })).toHaveFocus();
    fireEvent.click(systemTab);

    const textarea = screen.getByLabelText("システムプロンプト");
    fireEvent.change(textarea, { target: { value: "updated\r\nsystem e\u0301" } });
    expect(screen.getByText(/17 \/ 3,500 bytes/)).toBeVisible();
    const apply = screen.getByRole("button", { name: "変更を反映" });
    expect(apply).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/変更用確認文字列/), {
      target: { value: SYSTEM_CONFIRMATION },
    });
    expect(apply).toBeEnabled();
    fireEvent.click(apply);

    await waitFor(() =>
      expect(requests.some(({ path }) => path === "/api/v1/admin/prompts/apply")).toBe(true),
    );
    const sent = requests.find(({ path }) => path === "/api/v1/admin/prompts/apply");
    expect(requestBody(sent?.init)).toEqual({
      schemaVersion: 1,
      baseRevision: CURRENT_REVISION,
      prompts: { ...PROMPTS, system: "updated\nsystem é" },
      systemConfirmation: SYSTEM_CONFIRMATION,
    });
    const headers = new Headers(sent?.init?.headers);
    expect(headers.get("X-CSRF-Token")).toBe("csrf-token");
    expect(headers.get("X-Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("allows an unchanged legacy configuration to be registered explicitly", async () => {
    installApi({ mode: "legacy" });
    renderManager();

    const register = await screen.findByRole("button", {
      name: "現在の設定を管理版として登録",
    });
    expect(register).toBeEnabled();
    expect(screen.getAllByText("既存設定").length).toBeGreaterThan(0);
  });

  it("loads revision bodies only after selection and rolls back by creating a new revision", async () => {
    const requests = installApi();
    const client = renderManager();

    await screen.findByText(PREVIOUS_REVISION);
    expect(requests.some(({ path }) => path.endsWith(`/revisions/${PREVIOUS_REVISION}`))).toBe(
      false,
    );
    const activeRow = screen.getByText(CURRENT_REVISION, { selector: "strong" }).closest("li");
    expect(activeRow).not.toBeNull();
    expect(within(activeRow!).getByText("使用中")).toBeVisible();
    expect(within(activeRow!).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "内容を見る" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "変更点を見る" }));
    const moderatorSummary = await screen.findByText("事前調査AI", { selector: "summary" });
    fireEvent.click(moderatorSummary);
    const moderatorDiff = within(moderatorSummary.closest("details")!);
    expect(moderatorDiff.getByText("moderator prompt", { selector: "code" })).toBeVisible();
    expect(moderatorDiff.getByText("previous moderator", { selector: "code" })).toBeVisible();
    expect(
      moderatorDiff
        .getByText("previous moderator", { selector: "code" })
        .closest('[data-kind="removed"]'),
    ).not.toBeNull();
    expect(
      moderatorDiff
        .getByText("moderator prompt", { selector: "code" })
        .closest('[data-kind="added"]'),
    ).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "このrevisionを復元" }));
    fireEvent.click(screen.getByRole("button", { name: "新しい版として復元" }));

    await waitFor(() =>
      expect(requests.some(({ path }) => path === "/api/v1/admin/prompts/rollback")).toBe(true),
    );
    const sent = requests.find(({ path }) => path === "/api/v1/admin/prompts/rollback");
    expect(requestBody(sent?.init)).toEqual({
      schemaVersion: 1,
      baseRevision: CURRENT_REVISION,
      sourceRevision: PREVIOUS_REVISION,
      systemConfirmation: null,
    });
    expect(await screen.findByText(/を復元版として保存しました/)).toBeVisible();
    expect(screen.queryByRole("region", { name: "revisionを比較" })).not.toBeInTheDocument();
    expect(client.getQueryData(["admin", "prompt-revision", PREVIOUS_REVISION])).toBeUndefined();
  });

  it("preserves a local draft on 409 and rebases only after explicit confirmation", async () => {
    const requests = installApi({ applyConflict: true });
    renderManager();

    const textarea = await screen.findByLabelText("システムプロンプト");
    fireEvent.change(textarea, { target: { value: "local draft" } });
    fireEvent.change(screen.getByLabelText(/変更用確認文字列/), {
      target: { value: SYSTEM_CONFIRMATION },
    });
    fireEvent.click(screen.getByRole("button", { name: "変更を反映" }));

    expect(await screen.findByText("別の画面で新しいrevisionが保存されました。")).toBeVisible();
    expect(textarea).toHaveValue("local draft");
    fireEvent.click(screen.getByRole("button", { name: "最新revisionを基準にする" }));
    expect(textarea).toHaveValue("local draft");
    fireEvent.click(screen.getByRole("button", { name: "変更を反映" }));

    await waitFor(() =>
      expect(requests.filter(({ path }) => path === "/api/v1/admin/prompts/apply")).toHaveLength(2),
    );
    const second = requests.filter(({ path }) => path === "/api/v1/admin/prompts/apply")[1];
    expect(requestBody(second?.init)).toMatchObject({ baseRevision: LATEST_REVISION });
  });

  it("does not persist prompt bodies or idempotency identifiers in browser storage", async () => {
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    installApi();
    renderManager();
    const textarea = await screen.findByLabelText("システムプロンプト");
    fireEvent.change(textarea, { target: { value: "local-only body" } });
    fireEvent.change(screen.getByLabelText(/変更用確認文字列/), {
      target: { value: SYSTEM_CONFIRMATION },
    });
    fireEvent.click(screen.getByRole("button", { name: "変更を反映" }));
    await screen.findByText(/を保存しました/);

    expect(storageWrite).not.toHaveBeenCalled();
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
  });
});
