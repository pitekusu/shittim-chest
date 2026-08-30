import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { RECORD_ID, mockApi, recordDetail, renderRoute } from "../test/recordsTestUtils";
import RecordDetail, { RecordDocument } from "./RecordDetail";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("RecordDetail", () => {
  it("renders named votes and only the saved final result", async () => {
    mockApi();

    renderRoute(<RecordDetail />, {
      initialEntry: `/records/${RECORD_ID}`,
      path: "/records/:recordId",
    });

    expect(await screen.findByRole("heading", { name: "休日の過ごし方を決める" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "3人の意見" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "投票" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "アロナ → プラナ" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "最終決定" })).toBeVisible();
    expect(screen.getByText("勝利しました")).toBeVisible();
    expect(screen.getByText("実行する")).toBeVisible();
    expect(screen.getByText("2026年8月15日 15:00")).toHaveAttribute(
      "datetime",
      "2026-08-15T06:00:00Z",
    );
  });

  it("renders the applied affection change for all three participants", () => {
    const detail = {
      ...recordDetail(),
      schemaVersion: 2 as const,
      affection: {
        status: "applied" as const,
        rubricVersion: "v1",
        participants: [
          {
            participant: "participant-a" as const,
            before: 590,
            questionScore: 35,
            appliedDelta: 35,
            after: 625,
          },
          {
            participant: "participant-b" as const,
            before: 98,
            questionScore: -43,
            appliedDelta: -43,
            after: 55,
          },
          {
            participant: "participant-c" as const,
            before: 987,
            questionScore: 50,
            appliedDelta: 13,
            after: 1000,
          },
        ],
      },
    };

    renderRoute(<RecordDocument record={detail} />);

    expect(screen.getByRole("heading", { name: "親愛度の変化" })).toBeVisible();
    expect(screen.getByLabelText("実増減 +35点")).toBeVisible();
    expect(screen.getByLabelText("実増減 -43点")).toBeVisible();
    expect(screen.getByLabelText("実増減 +13点")).toBeVisible();
    expect(
      screen.getByRole("meter", { name: "安倍晋三AIの親愛度 1000点（1000点満点）" }),
    ).toHaveAttribute("value", "1000");
  });

  it("explains that affection stayed unchanged when evaluation was unavailable", () => {
    const detail = {
      ...recordDetail(),
      schemaVersion: 2 as const,
      affection: {
        status: "unavailable" as const,
        rubricVersion: "v1",
        participants: ["participant-a", "participant-b", "participant-c"].map((participant) => ({
          participant: participant as "participant-a" | "participant-b" | "participant-c",
          before: 500,
          questionScore: null,
          appliedDelta: 0,
          after: 500,
        })),
      },
    };

    renderRoute(<RecordDocument record={detail} />);

    expect(
      screen.getByText("質問の評価を完了できなかったため、親愛度は変更されませんでした。"),
    ).toBeVisible();
  });
});
