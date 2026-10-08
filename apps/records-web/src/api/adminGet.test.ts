import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { response } from "../test/recordsTestUtils";
import { getAdminPrompts, getAdminRevision, getAdminRevisions } from "./admin";
import { getAdminStatus } from "./adminStatus";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Admin GET cancellation", () => {
  it.each([
    ["prompts", (signal: AbortSignal) => getAdminPrompts(signal)],
    ["status", (signal: AbortSignal) => getAdminStatus(signal)],
    ["revisions", (signal: AbortSignal) => getAdminRevisions("next-page", signal)],
    ["revision", (signal: AbortSignal) => getAdminRevision(`r${"1".repeat(26)}`, signal)],
  ] as const)("passes cancellation to the %s request", async (_name, read) => {
    const controller = new AbortController();
    const fetchMock = vi.fn<typeof fetch>((_input, init) => {
      expect(init?.signal).toBe(controller.signal);
      return new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true });
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const request = read(controller.signal).catch((error: unknown) => error);
    controller.abort();
    expect(await request).toMatchObject({ name: "AbortError" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not fetch after the signal is already aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);

    await expect(getAdminPrompts(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([429, 503])("cancels the delayed retry after status %i", async (status) => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      response(
        {
          error: {
            code: "PROMPT_CONFIGURATION_UNAVAILABLE",
            message: "設定を取得できません。",
            requestId: "request-id",
          },
        },
        status,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const request = getAdminPrompts(controller.signal).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(1);

    controller.abort();
    expect(await request).toMatchObject({ name: "AbortError" });
    await vi.runAllTimersAsync();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });
});
