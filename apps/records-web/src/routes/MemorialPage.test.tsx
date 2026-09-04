import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, afterEach, describe, expect, it, vi } from "vite-plus/test";

import {
  getMemorialMemory,
  getMemorialState,
  prepareMemorialUpload,
  queueMemorialGeneration,
  resetMemorial,
  uploadMemorialSource,
} from "../api/memorial";
import { RecordsApiError } from "../api/http";
import type {
  MemoryResponse,
  MemorialStateResponse,
  RequesterSummary,
  UploadResponse,
} from "../api/types";
import MemorialPage from "./MemorialPage";

vi.mock("../api/memorial", () => ({
  getMemorialMemory: vi.fn<typeof import("../api/memorial").getMemorialMemory>(),
  getMemorialState: vi.fn<typeof import("../api/memorial").getMemorialState>(),
  prepareMemorialUpload: vi.fn<typeof import("../api/memorial").prepareMemorialUpload>(),
  queueMemorialGeneration: vi.fn<typeof import("../api/memorial").queueMemorialGeneration>(),
  resetMemorial: vi.fn<typeof import("../api/memorial").resetMemorial>(),
  uploadMemorialSource: vi.fn<typeof import("../api/memorial").uploadMemorialSource>(),
}));

const getMemoryMock = vi.mocked(getMemorialMemory);
const getStateMock = vi.mocked(getMemorialState);
const prepareUploadMock = vi.mocked(prepareMemorialUpload);
const queueGenerationMock = vi.mocked(queueMemorialGeneration);
const resetMock = vi.mocked(resetMemorial);
const uploadSourceMock = vi.mocked(uploadMemorialSource);

const REQUESTER: RequesterSummary = {
  displayName: "先生",
  avatar: {
    kind: "placeholder",
    url: null,
    alt: "先生のアバター",
    fallbackVariant: "cyan",
  },
};

function lockedState(cycle = 1): MemorialStateResponse {
  return {
    schemaVersion: 1,
    state: "locked",
    cycle,
    resetCount: cycle - 1,
    unlockedParticipant: null,
    unlockedAt: null,
    uploadReady: false,
    latestReadyCycle: null,
    memories: [],
  };
}

function unlockedState(
  state: "unlocked" | "queued" | "generating" | "failed" = "unlocked",
  uploadReady = false,
): MemorialStateResponse {
  return {
    schemaVersion: 1,
    state,
    cycle: 1,
    resetCount: 0,
    unlockedParticipant: "participant-a",
    unlockedAt: "2026-09-03T01:00:00Z",
    uploadReady,
    latestReadyCycle: null,
    memories: [],
  };
}

const MEMORY_SUMMARIES: MemorialStateResponse["memories"] = [
  {
    cycle: 1,
    participant: "participant-a",
    unlockedAt: "2026-09-01T01:00:00Z",
    generatedAt: "2026-09-01T01:05:00Z",
  },
  {
    cycle: 2,
    participant: "participant-b",
    unlockedAt: "2026-09-03T01:00:00Z",
    generatedAt: "2026-09-03T01:05:00Z",
  },
];

function resetLockedState(): MemorialStateResponse {
  return {
    ...lockedState(2),
    latestReadyCycle: 1,
    memories: [MEMORY_SUMMARIES[0]!],
  };
}

function readyState(): MemorialStateResponse {
  return {
    schemaVersion: 1,
    state: "ready",
    cycle: 2,
    resetCount: 1,
    unlockedParticipant: "participant-b",
    unlockedAt: "2026-09-03T01:00:00Z",
    uploadReady: false,
    latestReadyCycle: 2,
    memories: MEMORY_SUMMARIES,
  };
}

function memory(cycle: 1 | 2): MemoryResponse {
  const participant = cycle === 1 ? "participant-a" : "participant-b";
  const participantName = cycle === 1 ? "アロナ" : "プラナ";
  const summary = MEMORY_SUMMARIES[cycle - 1]!;
  return {
    schemaVersion: 1,
    cycle,
    participant,
    unlockedAt: summary.unlockedAt,
    generatedAt: summary.generatedAt,
    image: {
      url: `https://records.example.invalid/memorial-${cycle}.webp`,
      width: 1920,
      height: 1080,
      alt: `${participantName}とのメモリアルロビー`,
    },
    narrative: `${participantName}との思い出です。`,
  };
}

function uploadTicket(): UploadResponse {
  return {
    schemaVersion: 1,
    cycle: 1,
    method: "POST",
    uploadUrl: "https://upload.example.invalid/",
    expiresAt: "2026-09-03T01:10:00Z",
    fields: {
      key: "uploads/opaque-source",
      "Content-Type": "image/png",
      "x-amz-checksum-sha256": "A".repeat(43) + "=",
      "x-amz-algorithm": "AWS4-HMAC-SHA256",
      "x-amz-credential": "credential/scope",
      "x-amz-date": "20260903T010000Z",
      policy: "cG9saWN5",
      "x-amz-signature": "d".repeat(64),
    },
  };
}

function createClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Number.POSITIVE_INFINITY },
      mutations: { retry: false },
    },
  });
}

function renderMemorial(state?: MemorialStateResponse, client = createClient()) {
  if (state !== undefined) {
    client.setQueryData(["memorial"], state);
    getStateMock.mockResolvedValue(state);
  }
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <MemorialPage csrfToken="csrf-token" requester={REQUESTER} />
      </QueryClientProvider>,
    ),
  };
}

function useReducedMotion(): void {
  vi.spyOn(window, "matchMedia").mockImplementation(
    (query) =>
      ({
        matches: true,
        media: query,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as MediaQueryList,
  );
}

async function finishStandardEntry(): Promise<void> {
  await act(async () => vi.advanceTimersByTime(3_000));
  expect(screen.getByRole("heading", { name: "メモリアルロビー" })).toBeVisible();
}

beforeEach(() => {
  getMemoryMock.mockReset();
  getStateMock.mockReset();
  prepareUploadMock.mockReset();
  queueGenerationMock.mockReset();
  resetMock.mockReset();
  uploadSourceMock.mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("MemorialPage", () => {
  it("shows the locked explanation without playing the entry transition", () => {
    renderMemorial(lockedState());

    expect(screen.getByRole("heading", { name: "メモリアルロビー" })).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "まだメモリアルロビーにはログインできません" }),
    ).toBeVisible();
    expect(screen.getByText("CYCLE 1")).toBeVisible();
    expect(screen.queryByLabelText("メモリアルロビーへログインしています")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("メモリアル用の画像を選択")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "親愛度をリセット" })).not.toBeInTheDocument();
  });

  it("keeps the full-screen entry transition active for exactly three seconds", async () => {
    vi.useFakeTimers();
    renderMemorial(unlockedState());

    const transition = screen.getByLabelText("メモリアルロビーへログインしています");
    expect(transition).toHaveAttribute("aria-busy", "true");
    expect(transition).toHaveAttribute("data-reduced-motion", "false");
    expect(screen.getByRole("heading", { name: "先生" })).toBeVisible();

    await act(async () => vi.advanceTimersByTime(2_999));
    expect(transition).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTime(1));

    expect(transition).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "メモリアルロビー" })).toBeVisible();
  });

  it("shortens the entry transition for reduced-motion visitors", async () => {
    vi.useFakeTimers();
    useReducedMotion();
    renderMemorial(unlockedState());

    const transition = screen.getByLabelText("メモリアルロビーへログインしています");
    expect(transition).toHaveAttribute("data-reduced-motion", "true");
    await act(async () => vi.advanceTimersByTime(159));
    expect(transition).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTime(1));
    expect(transition).not.toBeInTheDocument();
  });

  it.each([
    ["queued", "メモリアル生成を受け付けました"],
    ["generating", "思い出を画像と文章にしています"],
  ] as const)("renders the %s state as indeterminate progress", async (state, message) => {
    vi.useFakeTimers();
    renderMemorial(unlockedState(state));

    await finishStandardEntry();

    expect(screen.getByText(message)).toBeVisible();
    expect(screen.getByText("進行中", { exact: true })).toBeVisible();
    expect(screen.getByRole("progressbar", { name: "メモリアル生成の進捗" })).not.toHaveAttribute(
      "value",
    );
    expect(screen.queryByLabelText("メモリアル用の画像を選択")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "親愛度をリセット" })).not.toBeInTheDocument();
  });

  it("selects a file, confirms the one-generation warning, and calls APIs in order", async () => {
    vi.useFakeTimers();
    const state = unlockedState();
    const ticket = uploadTicket();
    prepareUploadMock.mockResolvedValue(ticket);
    uploadSourceMock.mockResolvedValue(undefined);
    queueGenerationMock.mockResolvedValue(unlockedState("queued"));
    renderMemorial(state);
    await finishStandardEntry();
    vi.useRealTimers();

    const input = screen.getByLabelText("メモリアル用の画像を選択");
    const generateButton = screen.getByRole("button", { name: "メモリアルロビーを開放" });
    expect(input).toHaveAttribute("accept", "image/jpeg,image/png,image/webp");
    expect(generateButton).toBeDisabled();

    fireEvent.change(input, {
      target: { files: [new File(["not-an-image"], "source.gif", { type: "image/gif" })] },
    });
    expect(screen.getByRole("alert")).toHaveTextContent("JPEG、PNG、WebPのいずれか");
    expect(generateButton).toBeDisabled();

    const source = new File([Uint8Array.of(1, 2, 3)], "memory.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [source] } });
    expect(screen.getByText(/memory\.png/)).toBeVisible();
    expect(generateButton).toBeEnabled();
    generateButton.focus();
    fireEvent.click(generateButton);

    const dialog = screen.getByRole("dialog", { name: "この思い出を一度だけ生成します" });
    const confirm = within(dialog).getByRole("button", { name: "理解して生成する" });
    expect(confirm).toHaveFocus();
    expect(within(dialog).getByText("このcycleで生成に成功できるのは一度だけです。")).toBeVisible();
    expect(within(dialog).getByText(/選んだ画像をAI生成に使用/)).toBeVisible();
    expect(within(dialog).getByText(/アロナとの画像/)).toBeVisible();
    fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(generateButton).toHaveFocus();
    fireEvent.click(generateButton);
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "理解して生成する" }),
    );

    await waitFor(() => expect(queueGenerationMock).toHaveBeenCalledTimes(1));
    expect(prepareUploadMock).toHaveBeenCalledWith(
      source,
      1,
      "csrf-token",
      expect.stringMatching(/^memorial-/),
    );
    expect(uploadSourceMock).toHaveBeenCalledWith(ticket, source);
    expect(queueGenerationMock).toHaveBeenCalledWith(
      1,
      "GENERATE MEMORIAL",
      "csrf-token",
      expect.stringMatching(/^memorial-/),
    );
    expect(prepareUploadMock.mock.invocationCallOrder[0]).toBeLessThan(
      uploadSourceMock.mock.invocationCallOrder[0]!,
    );
    expect(uploadSourceMock.mock.invocationCallOrder[0]).toBeLessThan(
      queueGenerationMock.mock.invocationCallOrder[0]!,
    );
    expect(await screen.findByText("メモリアル生成を受け付けました")).toBeVisible();
  });

  it("reuses both idempotency keys and the upload ticket across safe retries", async () => {
    vi.useFakeTimers();
    const state = unlockedState();
    const ticket = uploadTicket();
    prepareUploadMock
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValueOnce(ticket);
    uploadSourceMock.mockRejectedValueOnce(new Error("upload interrupted")).mockResolvedValueOnce();
    queueGenerationMock.mockResolvedValue(unlockedState("queued"));
    renderMemorial(state);
    await finishStandardEntry();
    vi.useRealTimers();

    const source = new File([Uint8Array.of(1, 2, 3)], "memory.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("メモリアル用の画像を選択"), {
      target: { files: [source] },
    });
    fireEvent.click(screen.getByRole("button", { name: "メモリアルロビーを開放" }));
    fireEvent.click(screen.getByRole("button", { name: "理解して生成する" }));

    const prepareRetry = await screen.findByRole("button", { name: "生成準備を再試行" });
    const firstPrepareKey = prepareUploadMock.mock.calls[0]?.[3];
    fireEvent.click(prepareRetry);
    const uploadRetry = await screen.findByRole("button", { name: "画像アップロードを再試行" });
    expect(prepareUploadMock.mock.calls[1]?.[3]).toBe(firstPrepareKey);
    fireEvent.click(uploadRetry);

    expect(await screen.findByText("メモリアル生成を受け付けました")).toBeVisible();
    expect(prepareUploadMock).toHaveBeenCalledTimes(2);
    expect(uploadSourceMock).toHaveBeenCalledTimes(2);
    expect(uploadSourceMock).toHaveBeenNthCalledWith(1, ticket, source);
    expect(uploadSourceMock).toHaveBeenNthCalledWith(2, ticket, source);
    expect(queueGenerationMock).toHaveBeenCalledTimes(1);
  });

  it("allows an expired upload reservation to be replaced after a reload", async () => {
    vi.useFakeTimers();
    const ticket = uploadTicket();
    prepareUploadMock.mockResolvedValue(ticket);
    uploadSourceMock.mockResolvedValue(undefined);
    queueGenerationMock.mockResolvedValue(unlockedState("queued"));
    renderMemorial(unlockedState("unlocked", true));
    await finishStandardEntry();
    vi.useRealTimers();

    const input = screen.getByLabelText("メモリアル用の画像を選択");
    expect(input).toBeEnabled();
    expect(screen.getByRole("button", { name: "準備済みの画像で生成を続ける" })).toBeVisible();

    const source = new File([Uint8Array.of(1, 2, 3)], "replacement.png", {
      type: "image/png",
    });
    fireEvent.change(input, { target: { files: [source] } });
    expect(
      screen.queryByRole("button", { name: "準備済みの画像で生成を続ける" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "メモリアルロビーを開放" }));
    fireEvent.click(screen.getByRole("button", { name: "理解して生成する" }));

    await waitFor(() => expect(queueGenerationMock).toHaveBeenCalledTimes(1));
    expect(prepareUploadMock).toHaveBeenCalledWith(
      source,
      1,
      "csrf-token",
      expect.stringMatching(/^memorial-/),
    );
  });

  it("can discard a failed local upload attempt and select the image again", async () => {
    vi.useFakeTimers();
    prepareUploadMock.mockRejectedValue(new Error("upload reservation unavailable"));
    renderMemorial(unlockedState());
    await finishStandardEntry();
    vi.useRealTimers();

    const input = screen.getByLabelText("メモリアル用の画像を選択");
    fireEvent.change(input, {
      target: {
        files: [new File([Uint8Array.of(1)], "first.png", { type: "image/png" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "メモリアルロビーを開放" }));
    fireEvent.click(screen.getByRole("button", { name: "理解して生成する" }));

    fireEvent.click(await screen.findByRole("button", { name: "画像を選び直す" }));
    expect(input).toBeEnabled();
    expect(screen.getByText("画像をドロップ、またはファイルを選択")).toBeVisible();
    expect(screen.getByRole("button", { name: "メモリアルロビーを開放" })).toBeDisabled();
  });

  it("accepts a supported image through drag and drop", async () => {
    vi.useFakeTimers();
    renderMemorial(unlockedState());
    await finishStandardEntry();
    vi.useRealTimers();

    const dropZone = screen.getByRole("button", { name: /画像をドロップ/u });
    const source = new File([Uint8Array.of(1, 2, 3)], "dropped.webp", {
      type: "image/webp",
    });
    fireEvent.dragEnter(dropZone);
    expect(dropZone).toHaveAttribute("data-dragging", "true");
    fireEvent.drop(dropZone, { dataTransfer: { files: [source] } });

    expect(dropZone).toHaveAttribute("data-dragging", "false");
    expect(screen.getByText(/dropped\.webp/u)).toBeVisible();
    expect(screen.getByRole("button", { name: "メモリアルロビーを開放" })).toBeEnabled();
  });

  it("offers a failed generation retry without preparing another upload", async () => {
    vi.useFakeTimers();
    queueGenerationMock.mockResolvedValue(unlockedState("queued"));
    renderMemorial(unlockedState("failed"));
    await finishStandardEntry();
    vi.useRealTimers();

    expect(screen.getByLabelText("メモリアル用の画像を選択")).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "前回の生成を再開" }));

    await waitFor(() => expect(queueGenerationMock).toHaveBeenCalledTimes(1));
    expect(queueGenerationMock).toHaveBeenCalledWith(
      1,
      "GENERATE MEMORIAL",
      "csrf-token",
      expect.stringMatching(/^memorial-/),
    );
    expect(prepareUploadMock).not.toHaveBeenCalled();
    expect(uploadSourceMock).not.toHaveBeenCalled();
  });

  it("requires the reset warning before resetting affection and starting the next cycle", async () => {
    vi.useFakeTimers();
    const { client } = renderMemorial(unlockedState());
    await finishStandardEntry();
    vi.useRealTimers();
    const invalidateQueries = vi.spyOn(client, "invalidateQueries");
    getMemoryMock.mockResolvedValue(memory(1));
    resetMock.mockResolvedValue(resetLockedState());

    fireEvent.click(screen.getByRole("button", { name: "親愛度をリセット" }));
    const dialog = screen.getByRole("dialog", { name: "親愛度をリセットしますか？" });
    expect(within(dialog).getByText("3人の親愛度をすべて500点に戻します。")).toBeVisible();
    expect(within(dialog).getByText(/もう一度誰かとの親愛度を1000点/)).toBeVisible();
    expect(within(dialog).getByText("これまでに生成したメモリアルは残ります。")).toBeVisible();
    fireEvent.click(within(dialog).getByRole("button", { name: "500点にリセット" }));

    await waitFor(() => expect(resetMock).toHaveBeenCalledTimes(1));
    expect(resetMock).toHaveBeenCalledWith(
      1,
      "RESET AFFECTION",
      "csrf-token",
      expect.stringMatching(/^memorial-/),
    );
    expect(
      await screen.findByRole("heading", {
        name: "次のメモリアルロビーはまだ開放されていません",
      }),
    ).toBeVisible();
    expect(screen.getByText("CYCLE 2")).toBeVisible();
    expect(screen.getByRole("heading", { name: "ふたりの思い出" })).toBeVisible();
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["affection-rankings"] });
  });

  it("plays the entry transition when a reset owner revisits the archive-only lobby", async () => {
    vi.useFakeTimers();
    getMemoryMock.mockResolvedValue(memory(1));
    renderMemorial(resetLockedState());

    expect(screen.getByLabelText("メモリアルロビーへログインしています")).toBeVisible();
    await finishStandardEntry();

    expect(
      screen.getByRole("heading", {
        name: "次のメモリアルロビーはまだ開放されていません",
      }),
    ).toBeVisible();
    expect(screen.getByText(/これまでの思い出はいつでも閲覧できます/u)).toBeVisible();
    expect(screen.getByRole("heading", { name: "ふたりの思い出" })).toBeVisible();
    expect(screen.queryByLabelText("メモリアル用の画像を選択")).not.toBeInTheDocument();
  });

  it("loads the latest ready memory and switches owner-only history tabs", async () => {
    useReducedMotion();
    getMemoryMock.mockImplementation((cycle) => Promise.resolve(memory(cycle as 1 | 2)));
    renderMemorial(readyState());

    expect(await screen.findByRole("heading", { name: "メモリアルロビー" })).toBeVisible();
    expect(screen.getByRole("progressbar", { name: "メモリアル生成の進捗" })).toHaveAttribute(
      "value",
      "100",
    );
    const latestImage = await screen.findByRole("img", { name: "プラナとのメモリアルロビー" });
    expect(latestImage).toHaveAttribute("width", "1920");
    expect(latestImage).toHaveAttribute("height", "1080");
    expect(latestImage).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(screen.getByText("プラナとの思い出です。")).toBeVisible();
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(2);
    expect(tabs[1]).toHaveAttribute("aria-selected", "true");

    fireEvent.click(tabs[0]!);

    expect(await screen.findByRole("img", { name: "アロナとのメモリアルロビー" })).toBeVisible();
    expect(getMemoryMock).toHaveBeenNthCalledWith(1, 2);
    expect(getMemoryMock).toHaveBeenNthCalledWith(2, 1);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
  });

  it("switches from an older memory to the newly completed cycle", async () => {
    useReducedMotion();
    const generatingCycleTwo: MemorialStateResponse = {
      ...unlockedState("generating"),
      cycle: 2,
      resetCount: 1,
      unlockedParticipant: "participant-b",
      latestReadyCycle: 1,
      memories: [MEMORY_SUMMARIES[0]!],
    };
    getMemoryMock.mockImplementation((cycle) => Promise.resolve(memory(cycle as 1 | 2)));
    const { client } = renderMemorial(generatingCycleTwo);

    expect(await screen.findByRole("img", { name: "アロナとのメモリアルロビー" })).toBeVisible();
    await act(async () => client.setQueryData(["memorial"], readyState()));

    expect(await screen.findByRole("img", { name: "プラナとのメモリアルロビー" })).toBeVisible();
    expect(getMemoryMock).toHaveBeenNthCalledWith(1, 1);
    expect(getMemoryMock).toHaveBeenNthCalledWith(2, 2);
  });

  it("shows a history-level error and retries only that memory", async () => {
    useReducedMotion();
    getMemoryMock
      .mockRejectedValueOnce(
        new RecordsApiError(
          503,
          "MEMORIAL_MEMORY_UNAVAILABLE",
          "思い出を読み込めませんでした。",
          "request-memory",
        ),
      )
      .mockResolvedValueOnce(memory(2));
    renderMemorial(readyState());

    const retry = await screen.findByRole("button", { name: "もう一度読み込む" });
    expect(screen.getByRole("alert")).toHaveTextContent("思い出を読み込めませんでした。");
    expect(screen.getByRole("heading", { name: "ふたりの思い出" })).toBeVisible();
    fireEvent.click(retry);

    expect(await screen.findByRole("img", { name: "プラナとのメモリアルロビー" })).toBeVisible();
    expect(getMemoryMock).toHaveBeenCalledTimes(2);
    expect(getStateMock).not.toHaveBeenCalled();
  });

  it("shows the state request error and recovers through its retry action", async () => {
    getStateMock
      .mockRejectedValueOnce(
        new RecordsApiError(
          503,
          "MEMORIAL_STATE_INVALID",
          "メモリアル状態を確認できませんでした。",
          "request-state",
        ),
      )
      .mockResolvedValueOnce(lockedState());
    renderMemorial();

    expect(
      await screen.findByRole("heading", { name: "メモリアルロビーを開けません" }),
    ).toBeVisible();
    expect(screen.getByText("メモリアル状態を確認できませんでした。")).toBeVisible();
    expect(screen.getByText("照会ID: request-state")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "もう一度試す" }));

    expect(
      await screen.findByRole("heading", { name: "まだメモリアルロビーにはログインできません" }),
    ).toBeVisible();
    expect(getStateMock).toHaveBeenCalledTimes(2);
  });

  it("invalidates the session and removes Memorial data after a 401 response", async () => {
    const client = createClient();
    client.setQueryData(["records-session"], { authenticated: true });
    const invalidateQueries = vi.spyOn(client, "invalidateQueries");
    const removeQueries = vi.spyOn(client, "removeQueries");
    getStateMock.mockRejectedValue(
      new RecordsApiError(
        401,
        "AUTHENTICATION_REQUIRED",
        "ログインし直してください。",
        "request-auth",
      ),
    );

    renderMemorial(undefined, client);

    expect(
      await screen.findByRole("heading", { name: "メモリアルロビーを開けません" }),
    ).toBeVisible();
    await waitFor(() =>
      expect(invalidateQueries).toHaveBeenCalledWith({
        queryKey: ["records-session"],
        exact: true,
      }),
    );
    await waitFor(() => expect(removeQueries).toHaveBeenCalledTimes(1));
    const predicate = removeQueries.mock.calls[0]?.[0]?.predicate;
    expect(predicate?.({ queryKey: ["memorial"] } as never)).toBe(true);
    expect(predicate?.({ queryKey: ["costs"] } as never)).toBe(false);
  });
});
