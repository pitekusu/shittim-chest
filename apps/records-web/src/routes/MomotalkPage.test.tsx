import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import {
  getMomotalkRoom,
  getMomotalkRooms,
  getMomotalkWeeks,
  type MomotalkRoomResponse,
  type MomotalkWeek,
} from "../api/momotalk";
import { placeholder, recordDetail } from "../test/recordsTestUtils";
import MomotalkPage from "./MomotalkPage";

vi.mock("../api/momotalk", () => ({
  getMomotalkRoom: vi.fn<typeof import("../api/momotalk").getMomotalkRoom>(),
  getMomotalkRooms: vi.fn<typeof import("../api/momotalk").getMomotalkRooms>(),
  getMomotalkWeeks: vi.fn<typeof import("../api/momotalk").getMomotalkWeeks>(),
}));

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

const week: MomotalkWeek = {
  weekId: "2026-09-13",
  periodStart: "2026-09-06T09:00:00Z",
  periodEnd: "2026-09-13T09:00:00Z",
  publishAt: "2026-09-13T11:00:00Z",
};
const nextWeek: MomotalkWeek = {
  weekId: "2026-09-20",
  periodStart: "2026-09-13T09:00:00Z",
  periodEnd: "2026-09-20T09:00:00Z",
  publishAt: "2026-09-20T11:00:00Z",
};
const conversation: MomotalkRoomResponse = {
  schemaVersion: 1,
  week,
  room: {
    roomId: "a".repeat(43),
    requester: { displayName: "週の先生", avatar: placeholder("先生", "cyan") },
    questionCount: 1,
    state: "ready",
  },
  participants: recordDetail().participants,
  messages: [{ id: 1, participant: "participant-a", text: "閲覧中の週の会話です。" }],
  images: [],
};

describe("Momotalk week selection", () => {
  it("keeps the open conversation when polling adds a newer week, while permitting explicit week changes", async () => {
    const weeks = vi
      .mocked(getMomotalkWeeks)
      .mockResolvedValueOnce({ schemaVersion: 1, weeks: [week], nextCursor: null })
      .mockResolvedValue({ schemaVersion: 1, weeks: [nextWeek, week], nextCursor: null });
    const rooms = vi.mocked(getMomotalkRooms).mockImplementation((weekId) =>
      Promise.resolve({
        schemaVersion: 1,
        week: weekId === nextWeek.weekId ? nextWeek : week,
        rooms: [conversation.room],
        nextCursor: null,
      }),
    );
    const room = vi.mocked(getMomotalkRoom).mockResolvedValue(conversation);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/momotalk"]}>
          <MomotalkPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /週の先生/ }));
    fireEvent.click(await screen.findByRole("button", { name: "すべて表示" }));
    expect(screen.getByText("閲覧中の週の会話です。")).toBeVisible();

    // Exercise the same query refresh used by the periodic poll, without waiting a minute.
    await act(async () => client.refetchQueries({ queryKey: ["momotalk", "weeks"], exact: true }));
    expect(weeks).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: /2026年9月13日の週/ })).toBeVisible();
    expect(screen.getByText("閲覧中の週の会話です。")).toBeVisible();
    expect(room).toHaveBeenCalledTimes(1);
    expect(rooms.mock.calls.some(([weekId]) => weekId === nextWeek.weekId)).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: /2026年9月13日の週/ }));
    fireEvent.click(await screen.findByRole("button", { name: /2026年9月20日/ }));
    await waitFor(() =>
      expect(rooms.mock.calls.some(([weekId]) => weekId === nextWeek.weekId)).toBe(true),
    );
    expect(screen.queryByText("閲覧中の週の会話です。")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "質問者を選択してください。" })).toBeVisible();
    client.clear();
  });
});
