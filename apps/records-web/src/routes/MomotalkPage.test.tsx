import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import {
  getMomotalkRoom,
  getMomotalkRooms,
  getMomotalkWeeks,
  type MomotalkRoomResponse,
  type MomotalkImage,
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
    const rooms = vi.mocked(getMomotalkRooms);
    vi.when(rooms, { onUnmatched: "throw" })
      .calledWith(week.weekId, undefined, expect.any(AbortSignal))
      .thenResolve({ schemaVersion: 1, week, rooms: [conversation.room], nextCursor: null })
      .calledWith(nextWeek.weekId, undefined, expect.any(AbortSignal))
      .thenResolve({
        schemaVersion: 1,
        week: nextWeek,
        rooms: [conversation.room],
        nextCursor: null,
      });
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

async function openConversation(images: readonly MomotalkImage[]) {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    },
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.removeAttribute("open");
    },
  });
  vi.mocked(getMomotalkWeeks).mockResolvedValue({
    schemaVersion: 1,
    weeks: [week],
    nextCursor: null,
  });
  vi.mocked(getMomotalkRooms).mockResolvedValue({
    schemaVersion: 1,
    week,
    rooms: [conversation.room],
    nextCursor: null,
  });
  vi.mocked(getMomotalkRoom).mockResolvedValue({ ...conversation, images });
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
  return client;
}

const selfie: MomotalkImage = {
  participant: "participant-a",
  mood: "happy",
  subjects: [
    { participant: "participant-a", mood: "happy" },
    { participant: "participant-b", mood: "unhappy" },
    { participant: "participant-c", mood: "happy" },
  ],
  state: "ready",
  url: "/selfie.webp",
  thumbnailUrl: "/thumbnail.webp",
  downloadUrl: "/download.webp",
};

describe("Momotalk selfies", () => {
  it.each([1, 2, 3])("shows %i subjects in one photo and saves that photo", async (count) => {
    const image = { ...selfie, subjects: selfie.subjects!.slice(0, count) };
    const client = await openConversation([image]);
    const names = conversation.participants
      .slice(0, count)
      .map((participant) => participant.displayName)
      .join("・");
    expect(screen.getAllByRole("button", { name: /画像を拡大/ })).toHaveLength(1);
    const trigger = screen.getByRole("button", { name: `${names}の画像を拡大` });
    const expressions = image.subjects
      .map(
        (subject, index) =>
          `${conversation.participants[index]!.displayName}：${subject.mood === "happy" ? "楽しそう" : "嫌そう"}`,
      )
      .join(" / ");
    expect(screen.getByText(expressions, { exact: true })).toBeVisible();
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "モモトークの画像" });
    expect(within(dialog).getByRole("img")).toHaveAttribute("src", image.url);
    expect(within(dialog).getByRole("img")).toHaveAttribute("alt", `${names}の自撮り（AI生成）`);
    expect(within(dialog).getByRole("link", { name: "画像を保存" })).toHaveAttribute(
      "href",
      image.downloadUrl,
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "閉じる" }));
    expect(trigger).toHaveFocus();
    client.clear();
  });

  it("keeps both existing single-person photos independently viewable", async () => {
    const images: MomotalkImage[] = conversation.participants
      .slice(0, 2)
      .map(({ slot }, index) => ({
        participant: slot,
        mood: index === 0 ? "happy" : "unhappy",
        state: "ready",
        url: `/legacy-${slot}.webp`,
        thumbnailUrl: `/legacy-thumbnail-${slot}.webp`,
        downloadUrl: `/legacy-download-${slot}.webp`,
      }));
    const client = await openConversation(images);
    for (const [index, participant] of conversation.participants.slice(0, 2).entries()) {
      fireEvent.click(
        screen.getByRole("button", { name: `${participant.displayName}の画像を拡大` }),
      );
      const dialog = screen.getByRole("dialog", { name: "モモトークの画像" });
      expect(within(dialog).getByRole("img")).toHaveAttribute("src", images[index]!.url);
      expect(within(dialog).getByRole("link", { name: "画像を保存" })).toHaveAttribute(
        "href",
        images[index]!.downloadUrl,
      );
      fireEvent.click(within(dialog).getByRole("button", { name: "閉じる" }));
    }
    client.clear();
  });
});
