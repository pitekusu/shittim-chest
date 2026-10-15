import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { recordDetail } from "../test/recordsTestUtils";
import { getMomotalkRoom, type MomotalkRoomResponse } from "./momotalk";

afterEach(() => {
  vi.unstubAllGlobals();
});

function roomResponse(): MomotalkRoomResponse {
  const detail = recordDetail();
  return {
    schemaVersion: 1,
    week: {
      weekId: "2026-10-11",
      periodStart: "2026-10-04T09:00:00Z",
      periodEnd: "2026-10-11T09:00:00Z",
      publishAt: "2026-10-11T11:00:00Z",
    },
    room: { roomId: "r".repeat(43), requester: detail.requester, questionCount: 1, state: "ready" },
    participants: detail.participants,
    messages: [],
    images: detail.participants.map(({ slot }) => ({
      participant: slot,
      mood: "happy",
      state: "ready",
      url: `/images/${slot}.webp`,
      thumbnailUrl: `/images/${slot}-thumbnail.webp`,
      downloadUrl: `/images/${slot}.webp?download=1`,
    })),
  };
}

describe("Momotalk image contract", () => {
  it("negotiates all three participants even when their moods match", async () => {
    const payload = roomResponse();
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response(JSON.stringify(payload)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getMomotalkRoom(payload.week.weekId, payload.room.roomId)).resolves.toEqual(
      payload,
    );
    expect(fetchMock.mock.calls[0]![0]).toBe(
      `/api/v1/momotalk/weeks/${payload.week.weekId}/rooms/${payload.room.roomId}?contract=momotalk-images-v2`,
    );
  });

  it("rejects duplicate participant images even when their URLs differ", async () => {
    const payload = roomResponse();
    const first = payload.images[0]!;
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(
          new Response(
            JSON.stringify({ ...payload, images: [first, { ...first, url: "/different.webp" }] }),
          ),
        ),
    );

    await expect(getMomotalkRoom(payload.week.weekId, payload.room.roomId)).rejects.toMatchObject({
      code: "INVALID_API_RESPONSE",
    });
  });
});
