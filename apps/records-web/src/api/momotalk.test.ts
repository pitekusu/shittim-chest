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
    images: [
      {
        participant: "participant-a",
        mood: "neutral",
        animated: true,
        subjects: [
          { participant: "participant-a", mood: "neutral" },
          { participant: "participant-b", mood: "unhappy" },
          { participant: "participant-c", mood: "happy" },
        ],
        state: "ready",
        url: "/images/selfie.gif",
        thumbnailUrl: "/images/selfie-thumbnail.webp",
        downloadUrl: "/images/selfie.gif?download=1",
      },
    ],
  };
}

describe("Momotalk image contract", () => {
  it("negotiates every subject and individual mood in one selfie", async () => {
    const payload = roomResponse();
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response(JSON.stringify(payload)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getMomotalkRoom(payload.week.weekId, payload.room.roomId)).resolves.toEqual(
      payload,
    );
    expect(fetchMock.mock.calls[0]![0]).toBe(
      `/api/v1/momotalk/weeks/${payload.week.weekId}/rooms/${payload.room.roomId}?contract=momotalk-selfie-v3`,
    );
  });

  it("accepts static two-photo rooms with one subject per photo", async () => {
    const payload = roomResponse();
    const legacy = {
      animated: false,
      state: "ready",
      url: "/images/legacy.webp",
      thumbnailUrl: "/images/legacy-thumb.webp",
      downloadUrl: "/images/legacy.webp?download=1",
    } as const;
    const subjects = [
      { participant: "participant-a", mood: "happy" },
      { participant: "participant-b", mood: "unhappy" },
    ];
    const images = subjects.map((subject) => ({ ...legacy, ...subject, subjects: [subject] }));
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ ...payload, images }))),
    );
    await expect(getMomotalkRoom(payload.week.weekId, payload.room.roomId)).resolves.toMatchObject({
      images,
    });
  });

  it("rejects duplicate participants inside the same selfie", async () => {
    const payload = roomResponse();
    const first = payload.images[0]!;
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(
          JSON.stringify({
            ...payload,
            images: [
              {
                ...first,
                subjects: [
                  { participant: "participant-a", mood: "happy" },
                  { participant: "participant-a", mood: "unhappy" },
                ],
              },
            ],
          }),
        ),
      ),
    );

    await expect(getMomotalkRoom(payload.week.weekId, payload.room.roomId)).rejects.toMatchObject({
      code: "INVALID_API_RESPONSE",
    });
  });
});
