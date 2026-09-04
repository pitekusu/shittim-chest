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

function sameCycleReadyState(): MemorialStateResponse {
  return {
    ...unlockedState(),
    state: "ready",
    uploadReady: false,
    latestReadyCycle: 1,
    memories: [MEMORY_SUMMARIES[0]!],
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

function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
  readonly reject: (reason: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((complete, fail) => {
    resolve = complete;
    reject = fail;
  });
  return { promise, resolve, reject };
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
    expect(transition.parentElement).toBe(document.body);
    expect(transition).toHaveAttribute("open");
    expect(transition).toHaveFocus();
    fireEvent(transition, new Event("cancel", { bubbles: false, cancelable: true }));
    expect(transition).toHaveAttribute("open");
    expect(screen.getByRole("heading", { name: "先生" })).toBeVisible();

    await act(async () => vi.advanceTimersByTime(2_999));
    expect(transition).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTime(1));

    expect(transition).not.toBeInTheDocument();
    const heading = screen.getByRole("heading", { name: "メモリアルロビー" });
    expect(heading).toBeVisible();
    expect(heading).toHaveFocus();
  });

  it("does not make the temporary state-loading heading the route focus target", () => {
    getStateMock.mockReturnValue(new Promise<MemorialStateResponse>(() => undefined));

    renderMemorial();

    expect(screen.getByRole("heading", { name: "思い出を確認しています" })).not.toHaveAttribute(
      "tabindex",
    );
  });

  it("does not restart the entry timer when memorial state refreshes", async () => {
    vi.useFakeTimers();
    const { client } = renderMemorial(unlockedState());

    const transition = screen.getByLabelText("メモリアルロビーへログインしています");
    await act(async () => vi.advanceTimersByTime(2_999));

    await act(async () => {
      client.setQueryData(["memorial"], {
        ...unlockedState(),
        uploadReady: true,
      });
    });
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

  it("refreshes a stable locked state and starts entry when the lobby unlocks", async () => {
    vi.useFakeTimers();
    const client = createClient();
    client.setQueryData(["memorial"], lockedState());
    getStateMock.mockResolvedValue(unlockedState());
    render(
      <QueryClientProvider client={client}>
        <MemorialPage csrfToken="csrf-token" requester={REQUESTER} />
      </QueryClientProvider>,
    );

    await act(async () => vi.advanceTimersByTimeAsync(29_999));
    expect(getStateMock).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    await act(async () => vi.advanceTimersByTimeAsync(1));

    expect(getStateMock).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("メモリアルロビーへログインしています")).toBeVisible();
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

  it("selects a file, clears the input after generation, and calls APIs in order", async () => {
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
    Object.defineProperty(input, "value", {
      configurable: true,
      value: String.raw`C:\fakepath\memory.png`,
      writable: true,
    });
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
    expect(screen.getByRole("heading", { name: "メモリアルロビー" })).toHaveFocus();
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
    expect(input).toHaveValue("");
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
    const source = new File([Uint8Array.of(1)], "first.png", { type: "image/png" });
    fireEvent.change(input, {
      target: {
        files: [source],
      },
    });
    Object.defineProperty(input, "value", {
      configurable: true,
      value: String.raw`C:\fakepath\first.png`,
      writable: true,
    });
    fireEvent.click(screen.getByRole("button", { name: "メモリアルロビーを開放" }));
    fireEvent.click(screen.getByRole("button", { name: "理解して生成する" }));

    fireEvent.click(await screen.findByRole("button", { name: "画像を選び直す" }));
    expect(input).toBeEnabled();
    expect(input).toHaveValue("");
    expect(screen.getByText("画像をドロップ、またはファイルを選択")).toBeVisible();
    expect(screen.getByRole("button", { name: "メモリアルロビーを開放" })).toBeDisabled();

    fireEvent.change(input, { target: { files: [source] } });
    expect(screen.getByText(/first\.png/u)).toBeVisible();
    expect(screen.getByRole("button", { name: "メモリアルロビーを開放" })).toBeEnabled();
  });

  it("discards a stale local attempt only when the observed cycle advances", async () => {
    vi.useFakeTimers();
    const initialState = unlockedState();
    const nextState = { ...unlockedState(), cycle: 2, resetCount: 1 };
    prepareUploadMock.mockRejectedValue(
      new RecordsApiError(
        409,
        "MEMORIAL_STATE_CONFLICT",
        "状態が更新されました。",
        "request-cycle-conflict",
      ),
    );
    const { client } = renderMemorial(initialState);
    getStateMock.mockReset();
    getStateMock.mockResolvedValueOnce(initialState).mockResolvedValue(nextState);
    await finishStandardEntry();
    vi.useRealTimers();

    const input = screen.getByLabelText("メモリアル用の画像を選択");
    const source = new File([Uint8Array.of(1)], "same.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [source] } });
    Object.defineProperty(input, "value", {
      configurable: true,
      value: String.raw`C:\fakepath\same.png`,
      writable: true,
    });

    fireEvent.click(screen.getByRole("button", { name: "メモリアルロビーを開放" }));
    fireEvent.click(screen.getByRole("button", { name: "理解して生成する" }));

    expect(await screen.findByRole("button", { name: "生成準備を再試行" })).toBeVisible();
    await waitFor(() => expect(getStateMock).toHaveBeenCalledTimes(1));
    expect(input).toHaveValue(String.raw`C:\fakepath\same.png`);
    expect(screen.getByText(/same\.png/u)).toBeVisible();

    await act(async () => client.invalidateQueries({ queryKey: ["memorial"], exact: true }));
    expect(await screen.findByText("CYCLE 2")).toBeVisible();
    await waitFor(() => expect(input).toBeEnabled());
    expect(input).toHaveValue("");
    expect(screen.getByText("画像をドロップ、またはファイルを選択")).toBeVisible();
    expect(screen.queryByRole("button", { name: "生成準備を再試行" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("progressbar", { name: "メモリアル生成の進捗" }),
    ).not.toBeInTheDocument();

    fireEvent.change(input, { target: { files: [source] } });
    expect(screen.getByText(/same\.png/u)).toBeVisible();
    expect(screen.getByRole("button", { name: "メモリアルロビーを開放" })).toBeEnabled();
  });

  it("does not restore an upload attempt after the cached cycle advances", async () => {
    vi.useFakeTimers();
    const initialState = unlockedState();
    const nextState = { ...unlockedState(), cycle: 2, resetCount: 1 };
    const pendingUpload = deferred<void>();
    prepareUploadMock.mockResolvedValue(uploadTicket());
    uploadSourceMock.mockReturnValue(pendingUpload.promise);
    queueGenerationMock.mockResolvedValue(unlockedState("queued"));
    const { client } = renderMemorial(initialState);
    await finishStandardEntry();
    vi.useRealTimers();

    const input = screen.getByLabelText("メモリアル用の画像を選択");
    const source = new File([Uint8Array.of(1)], "in-flight.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [source] } });
    Object.defineProperty(input, "value", {
      configurable: true,
      value: String.raw`C:\fakepath\in-flight.png`,
      writable: true,
    });
    fireEvent.click(screen.getByRole("button", { name: "メモリアルロビーを開放" }));
    fireEvent.click(screen.getByRole("button", { name: "理解して生成する" }));
    await waitFor(() => expect(uploadSourceMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      client.setQueryData(["memorial"], nextState);
      pendingUpload.resolve(undefined);
      await pendingUpload.promise;
    });

    expect(await screen.findByText("CYCLE 2")).toBeVisible();
    await waitFor(() => expect(input).toBeEnabled());
    expect(queueGenerationMock).not.toHaveBeenCalled();
    expect(input).toHaveValue("");
    expect(screen.getByText("画像をドロップ、またはファイルを選択")).toBeVisible();
    expect(screen.queryByRole("button", { name: /再試行/u })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not continue an upload after the same cycle becomes ready", async () => {
    vi.useFakeTimers();
    const pendingUpload = deferred<void>();
    prepareUploadMock.mockResolvedValue(uploadTicket());
    uploadSourceMock.mockReturnValue(pendingUpload.promise);
    queueGenerationMock.mockResolvedValue(unlockedState("queued"));
    const { client } = renderMemorial(unlockedState());
    await finishStandardEntry();
    vi.useRealTimers();

    const source = new File([Uint8Array.of(1)], "in-flight.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("メモリアル用の画像を選択"), {
      target: { files: [source] },
    });
    fireEvent.click(screen.getByRole("button", { name: "メモリアルロビーを開放" }));
    fireEvent.click(screen.getByRole("button", { name: "理解して生成する" }));
    await waitFor(() => expect(uploadSourceMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      client.setQueryData(["memorial"], sameCycleReadyState());
      pendingUpload.resolve(undefined);
      await pendingUpload.promise;
    });

    expect(await screen.findByText("メモリアルが完成しました")).toBeVisible();
    expect(queueGenerationMock).not.toHaveBeenCalled();
    expect(screen.queryByText("画像を一時保管しています")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not apply a generation response after the cached cycle advances", async () => {
    vi.useFakeTimers();
    const pendingQueue = deferred<MemorialStateResponse>();
    prepareUploadMock.mockResolvedValue(uploadTicket());
    uploadSourceMock.mockResolvedValue(undefined);
    queueGenerationMock.mockReturnValue(pendingQueue.promise);
    const { client } = renderMemorial(unlockedState());
    await finishStandardEntry();
    vi.useRealTimers();

    const source = new File([Uint8Array.of(1)], "queued.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("メモリアル用の画像を選択"), {
      target: { files: [source] },
    });
    fireEvent.click(screen.getByRole("button", { name: "メモリアルロビーを開放" }));
    fireEvent.click(screen.getByRole("button", { name: "理解して生成する" }));
    await waitFor(() => expect(queueGenerationMock).toHaveBeenCalledTimes(1));

    const nextState = { ...unlockedState(), cycle: 2, resetCount: 1 };
    await act(async () => {
      client.setQueryData(["memorial"], nextState);
      pendingQueue.resolve(unlockedState("queued"));
      await pendingQueue.promise;
    });

    expect(await screen.findByText("CYCLE 2")).toBeVisible();
    expect(screen.queryByText("メモリアル生成を受け付けました")).not.toBeInTheDocument();
    expect(client.getQueryData<MemorialStateResponse>(["memorial"])).toEqual(nextState);
  });

  it("cancels an older state refresh before applying a generation response", async () => {
    vi.useFakeTimers();
    const initialState = unlockedState();
    const staleRefresh = deferred<MemorialStateResponse>();
    prepareUploadMock.mockResolvedValue(uploadTicket());
    uploadSourceMock.mockResolvedValue(undefined);
    queueGenerationMock.mockResolvedValue(unlockedState("queued"));
    const { client } = renderMemorial(initialState);
    await finishStandardEntry();
    vi.useRealTimers();

    getStateMock.mockReset();
    getStateMock.mockReturnValue(staleRefresh.promise);
    const refresh = client.invalidateQueries({ queryKey: ["memorial"], exact: true });
    await waitFor(() => expect(getStateMock).toHaveBeenCalledTimes(1));

    const source = new File([Uint8Array.of(1)], "queued.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("メモリアル用の画像を選択"), {
      target: { files: [source] },
    });
    fireEvent.click(screen.getByRole("button", { name: "メモリアルロビーを開放" }));
    fireEvent.click(screen.getByRole("button", { name: "理解して生成する" }));

    await waitFor(() =>
      expect(client.getQueryData<MemorialStateResponse>(["memorial"])?.state).toBe("queued"),
    );
    await act(async () => {
      staleRefresh.resolve(initialState);
      await staleRefresh.promise;
      await refresh;
    });

    expect(client.getQueryData<MemorialStateResponse>(["memorial"])?.state).toBe("queued");
  });

  it("clears the picker after a dropped image replaces its selection", async () => {
    vi.useFakeTimers();
    renderMemorial(unlockedState());
    await finishStandardEntry();
    vi.useRealTimers();

    const input = screen.getByLabelText("メモリアル用の画像を選択");
    const dropZone = screen.getByRole("button", { name: /画像をドロップ/u });
    const picked = new File([Uint8Array.of(1)], "picked.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [picked] } });
    Object.defineProperty(input, "value", {
      configurable: true,
      value: String.raw`C:\fakepath\picked.png`,
      writable: true,
    });

    const dropped = new File([Uint8Array.of(1, 2, 3)], "dropped.webp", {
      type: "image/webp",
    });
    fireEvent.dragEnter(dropZone);
    expect(dropZone).toHaveAttribute("data-dragging", "true");
    fireEvent.drop(dropZone, { dataTransfer: { files: [dropped] } });

    expect(dropZone).toHaveAttribute("data-dragging", "false");
    expect(input).toHaveValue("");
    expect(screen.getByText(/dropped\.webp/u)).toBeVisible();

    fireEvent.change(input, { target: { files: [picked] } });
    expect(screen.getByText(/picked\.png/u)).toBeVisible();
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

  it.each(["success", "error"] as const)(
    "ignores a stale retry %s after the same cycle becomes ready",
    async (outcome) => {
      vi.useFakeTimers();
      const pendingRetry = deferred<MemorialStateResponse>();
      queueGenerationMock.mockReturnValue(pendingRetry.promise);
      const { client } = renderMemorial(unlockedState("failed"));
      await finishStandardEntry();
      vi.useRealTimers();

      fireEvent.click(screen.getByRole("button", { name: "前回の生成を再開" }));
      await waitFor(() => expect(queueGenerationMock).toHaveBeenCalledTimes(1));

      const progressed = sameCycleReadyState();
      await act(async () => {
        client.setQueryData(["memorial"], progressed);
        if (outcome === "success") pendingRetry.resolve(unlockedState("queued"));
        else pendingRetry.reject(new Error("stale retry failed"));
        await pendingRetry.promise.catch(() => undefined);
      });

      expect(await screen.findByText("メモリアルが完成しました")).toBeVisible();
      expect(client.getQueryData<MemorialStateResponse>(["memorial"])).toEqual(progressed);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    },
  );

  it("reuses a recovery key after response loss until the cycle or state changes", async () => {
    vi.useFakeTimers();
    queueGenerationMock.mockRejectedValue(new Error("response lost"));
    const { client } = renderMemorial(unlockedState("failed"));
    await finishStandardEntry();
    vi.useRealTimers();

    const failedRetry = screen.getByRole("button", { name: "前回の生成を再開" });
    fireEvent.click(failedRetry);
    await waitFor(() => expect(queueGenerationMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(failedRetry).toBeEnabled());
    const firstKey = queueGenerationMock.mock.calls[0]?.[3];

    fireEvent.click(failedRetry);
    await waitFor(() => expect(queueGenerationMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(failedRetry).toBeEnabled());
    expect(queueGenerationMock.mock.calls[1]?.[3]).toBe(firstKey);

    await act(async () => client.setQueryData(["memorial"], unlockedState("unlocked", true)));
    expect(client.getQueryData<MemorialStateResponse>(["memorial"])?.state).toBe("unlocked");
    fireEvent.click(await screen.findByRole("button", { name: "準備済みの画像で生成を続ける" }));
    await waitFor(() => expect(queueGenerationMock).toHaveBeenCalledTimes(3));
    const secondKey = queueGenerationMock.mock.calls[2]?.[3];
    expect(secondKey).not.toBe(firstKey);

    await act(async () =>
      client.setQueryData(["memorial"], {
        ...unlockedState("unlocked", true),
        cycle: 2,
        resetCount: 1,
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "準備済みの画像で生成を続ける" }));
    await waitFor(() => expect(queueGenerationMock).toHaveBeenCalledTimes(4));
    expect(queueGenerationMock.mock.calls[3]?.[3]).not.toBe(secondKey);
  });

  it("requires the reset warning, clears the input, and starts the next cycle", async () => {
    vi.useFakeTimers();
    const { client } = renderMemorial(unlockedState());
    await finishStandardEntry();
    vi.useRealTimers();
    const invalidateQueries = vi.spyOn(client, "invalidateQueries");
    getMemoryMock.mockResolvedValue(memory(1));
    resetMock.mockResolvedValue(resetLockedState());

    const input = screen.getByLabelText("メモリアル用の画像を選択");
    fireEvent.change(input, {
      target: { files: [new File([Uint8Array.of(1)], "memory.png", { type: "image/png" })] },
    });
    Object.defineProperty(input, "value", {
      configurable: true,
      value: String.raw`C:\fakepath\memory.png`,
      writable: true,
    });

    const resetButton = screen.getByRole("button", { name: "親愛度をリセット" });
    resetButton.focus();
    fireEvent.click(resetButton);
    const dialog = screen.getByRole("dialog", { name: "親愛度をリセットしますか？" });
    expect(within(dialog).getByText("3人の親愛度をすべて500点に戻します。")).toBeVisible();
    expect(within(dialog).getByText(/もう一度誰かとの親愛度を1000点/)).toBeVisible();
    expect(within(dialog).getByText("これまでに生成したメモリアルは残ります。")).toBeVisible();
    fireEvent.click(within(dialog).getByRole("button", { name: "キャンセル" }));
    expect(resetButton).toHaveFocus();

    fireEvent.click(resetButton);
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "親愛度をリセットしますか？" })).getByRole(
        "button",
        { name: "500点にリセット" },
      ),
    );

    await waitFor(() => expect(resetMock).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("heading", { name: "メモリアルロビー" })).toHaveFocus();
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
    expect(input).toHaveValue("");
  });

  it("applies a successful reset after a same-cycle state refresh", async () => {
    vi.useFakeTimers();
    const pendingReset = deferred<MemorialStateResponse>();
    resetMock.mockReturnValue(pendingReset.promise);
    getMemoryMock.mockResolvedValue(memory(1));
    const { client } = renderMemorial(unlockedState());
    const invalidateQueries = vi.spyOn(client, "invalidateQueries");
    await finishStandardEntry();
    vi.useRealTimers();

    fireEvent.click(screen.getByRole("button", { name: "親愛度をリセット" }));
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "親愛度をリセットしますか？" })).getByRole(
        "button",
        { name: "500点にリセット" },
      ),
    );
    await waitFor(() => expect(resetMock).toHaveBeenCalledTimes(1));

    const next = resetLockedState();
    await act(async () => {
      client.setQueryData(["memorial"], sameCycleReadyState());
      pendingReset.resolve(next);
      await pendingReset.promise;
    });

    expect(
      await screen.findByRole("heading", {
        name: "次のメモリアルロビーはまだ開放されていません",
      }),
    ).toBeVisible();
    expect(client.getQueryData<MemorialStateResponse>(["memorial"])).toEqual(next);
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["affection-rankings"] });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps the reset warning open across a same-cycle ready refetch", async () => {
    useReducedMotion();
    getMemoryMock.mockResolvedValue(memory(2));
    const initial = { ...readyState(), memories: [MEMORY_SUMMARIES[1]!] };
    const { client } = renderMemorial(initial);

    expect(await screen.findByRole("img", { name: "プラナとのメモリアルロビー" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "親愛度をリセット" }));
    expect(screen.getByRole("dialog", { name: "親愛度をリセットしますか？" })).toBeVisible();

    await act(async () => client.setQueryData(["memorial"], readyState()));

    expect(screen.getByRole("dialog", { name: "親愛度をリセットしますか？" })).toBeVisible();
  });

  it("ignores a stale reset error after the same cycle state advances", async () => {
    vi.useFakeTimers();
    const pendingReset = deferred<MemorialStateResponse>();
    resetMock.mockReturnValue(pendingReset.promise);
    getMemoryMock.mockResolvedValue(memory(1));
    const { client } = renderMemorial(unlockedState());
    await finishStandardEntry();
    vi.useRealTimers();

    fireEvent.click(screen.getByRole("button", { name: "親愛度をリセット" }));
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "親愛度をリセットしますか？" })).getByRole(
        "button",
        { name: "500点にリセット" },
      ),
    );
    await waitFor(() => expect(resetMock).toHaveBeenCalledTimes(1));

    const progressed = sameCycleReadyState();
    await act(async () => {
      client.setQueryData(["memorial"], progressed);
      pendingReset.reject(new Error("stale reset failed"));
      await pendingReset.promise.catch(() => undefined);
    });

    expect(await screen.findByText("メモリアルが完成しました")).toBeVisible();
    expect(client.getQueryData<MemorialStateResponse>(["memorial"])).toEqual(progressed);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("reuses the reset key after response loss until the cycle changes", async () => {
    vi.useFakeTimers();
    resetMock.mockRejectedValue(new Error("response lost"));
    const { client } = renderMemorial(unlockedState());
    await finishStandardEntry();
    vi.useRealTimers();

    const confirmReset = () => {
      fireEvent.click(screen.getByRole("button", { name: "親愛度をリセット" }));
      fireEvent.click(
        within(screen.getByRole("dialog", { name: "親愛度をリセットしますか？" })).getByRole(
          "button",
          { name: "500点にリセット" },
        ),
      );
    };

    confirmReset();
    await waitFor(() => expect(resetMock).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "親愛度をリセット" })).toBeEnabled(),
    );
    const firstKey = resetMock.mock.calls[0]?.[3];
    expect(firstKey).toEqual(expect.any(String));

    confirmReset();
    await waitFor(() => expect(resetMock).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "親愛度をリセット" })).toBeEnabled(),
    );
    expect(resetMock.mock.calls[1]?.[3]).toBe(firstKey);

    await act(async () =>
      client.setQueryData(["memorial"], {
        ...unlockedState(),
        cycle: 2,
        resetCount: 1,
      }),
    );
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    confirmReset();
    await waitFor(() => expect(resetMock).toHaveBeenCalledTimes(3));
    expect(resetMock.mock.calls[2]?.[3]).not.toBe(firstKey);
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

  it("recovers authentication when a Memorial mutation reports an expired session", async () => {
    vi.useFakeTimers();
    const client = createClient();
    client.setQueryData(["records-session"], { authenticated: true });
    const invalidateQueries = vi.spyOn(client, "invalidateQueries");
    const removeQueries = vi.spyOn(client, "removeQueries");
    prepareUploadMock.mockRejectedValue(
      new RecordsApiError(
        401,
        "AUTHENTICATION_REQUIRED",
        "ログインし直してください。",
        "request-mutation-auth",
      ),
    );
    renderMemorial(unlockedState(), client);
    await finishStandardEntry();
    vi.useRealTimers();

    const source = new File([Uint8Array.of(1)], "memory.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("メモリアル用の画像を選択"), {
      target: { files: [source] },
    });
    fireEvent.click(screen.getByRole("button", { name: "メモリアルロビーを開放" }));
    fireEvent.click(screen.getByRole("button", { name: "理解して生成する" }));

    await waitFor(() =>
      expect(invalidateQueries).toHaveBeenCalledWith({
        queryKey: ["records-session"],
        exact: true,
      }),
    );
    await waitFor(() => expect(removeQueries).toHaveBeenCalledTimes(1));
    const predicate = removeQueries.mock.calls[0]?.[0]?.predicate;
    expect(predicate?.({ queryKey: ["memorial"] } as never)).toBe(true);
  });
});
