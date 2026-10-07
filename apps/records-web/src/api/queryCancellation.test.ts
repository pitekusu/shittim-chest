import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { getAffectionRankings } from "./affectionRankings";
import { getCosts } from "./costs";
import { getMomotalkRoom, getMomotalkRooms, getMomotalkWeeks } from "./momotalk";
import { getRankings } from "./rankings";
import { getRecord } from "./recordDetail";
import { getRecords } from "./recordList";
import { getSession, logout } from "./session";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("read cancellation", () => {
  it("forwards the caller's signal across every read and preserves the original abort", async () => {
    const controller = new AbortController();
    controller.abort();
    const abort = new DOMException("The operation was aborted", "AbortError");
    const fetchMock = vi.fn<typeof fetch>().mockRejectedValue(abort);
    vi.stubGlobal("fetch", fetchMock);

    const reads = [
      getSession(controller.signal),
      getRecords({ sort: "newest" }, controller.signal),
      getRecord("r".repeat(43), controller.signal),
      getRankings(controller.signal),
      getCosts("week", controller.signal),
      getAffectionRankings(undefined, controller.signal),
      getMomotalkWeeks(undefined, controller.signal),
      getMomotalkRooms("2026-W40", undefined, controller.signal),
      getMomotalkRoom("2026-W40", "room", controller.signal),
    ];
    const results = await Promise.allSettled(reads);
    expect(fetchMock).toHaveBeenCalledTimes(reads.length);
    for (const [, init] of fetchMock.mock.calls) {
      expect(init?.signal).toBe(controller.signal);
      expect(init?.credentials).toBe("same-origin");
    }
    for (const result of results) {
      expect(result.status).toBe("rejected");
      expect((result as PromiseRejectedResult).reason).toBe(abort);
    }
  });

  it("does not reinterpret a body-read abort as a schema or authentication failure", async () => {
    const abort = new DOMException("The operation was aborted", "AbortError");
    const controller = new AbortController();
    const response = new Response(null, { status: 401 });
    const body = vi.spyOn(response, "json").mockRejectedValue(abort);
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(response));

    await expect(getSession(controller.signal)).rejects.toBe(abort);
    expect(body).toHaveBeenCalledOnce();
  });

  it("keeps logout as an explicit uncancelled POST with the existing CSRF contract", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await logout("test-csrf");
    expect(fetchMock).toHaveBeenCalledOnce();
    const [path, init] = fetchMock.mock.calls[0]!;
    expect(path).toBe("/api/v1/logout");
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-CSRF-Token": "test-csrf" },
    });
    expect(init).not.toHaveProperty("signal");
  });
});
