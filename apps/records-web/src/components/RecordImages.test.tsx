import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";

import { getRecord } from "../api/recordDetail";
import type { RecordAttachment } from "../api/types";
import { RecordDocument } from "../routes/RecordDetail";
import { recordDetail, renderRoute } from "../test/recordsTestUtils";

vi.mock("../api/recordDetail", () => ({ getRecord: vi.fn<typeof getRecord>() }));

const image: RecordAttachment = {
  attachmentId: "00000000-0000-4000-8000-00000000000a",
  contentType: "image/png",
  sizeBytes: 100,
  sha256: "a".repeat(64),
  width: 800,
  height: 600,
  url: "https://example.test/original.png?signature=old",
  thumbnailUrl: "https://example.test/thumbnail.webp?signature=old",
  urlExpiresAt: "2026-10-11T00:05:00Z",
};

describe("record images", () => {
  beforeEach(() => {
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
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("opens the full image and restores focus when closed", () => {
    renderRoute(<RecordDocument record={{ ...recordDetail(), attachments: [image] }} />);
    const trigger = screen.getByRole("button", { name: "添付画像 1を拡大" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "添付画像 1" });
    expect(within(dialog).getByRole("img")).toHaveAttribute("src", image.url);
    fireEvent.click(within(dialog).getByRole("button", { name: "原寸で見る" }));
    expect(within(dialog).getByRole("button", { name: "画面に合わせる" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent(dialog, new Event("cancel", { bubbles: true, cancelable: true }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(screen.getByRole("heading", { name: "最終決定" })).toBeVisible();
  });

  it("places every attachment beside its question and opens the selected original", () => {
    const images = [0, 1, 2].map((index) => ({
      ...image,
      attachmentId: `00000000-0000-4000-8000-00000000000${index}`,
      url: `https://example.test/original-${index}.png`,
    }));
    renderRoute(<RecordDocument record={{ ...recordDetail(), attachments: images }} />);
    const question = screen.getByRole("heading", { level: 1 });
    const region = within(question.parentElement!).getByRole("region", { name: "添付画像 3枚" });
    expect(within(region).getAllByRole("button")).toHaveLength(3);
    const trigger = within(region).getByRole("button", { name: "添付画像 3を拡大" });
    expect(trigger).toHaveAttribute("aria-haspopup", "dialog");
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "添付画像 3" });
    expect(within(dialog).getByRole("img")).toHaveAttribute("src", images[2]!.url);
    fireEvent.click(within(dialog).getByRole("button", { name: "閉じる" }));
    expect(trigger).toHaveFocus();
  });

  it("omits attachment controls for legacy records without images", () => {
    renderRoute(<RecordDocument record={recordDetail()} />);
    expect(screen.queryByRole("region", { name: /添付画像/ })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "最終決定" })).toBeVisible();
  });

  it("refreshes an expired URL without hiding the debate body", async () => {
    const updated = { ...image, url: "https://example.test/original.png?signature=new" };
    vi.mocked(getRecord).mockResolvedValue({ ...recordDetail(), attachments: [updated] });
    renderRoute(<RecordDocument record={{ ...recordDetail(), attachments: [image] }} />);
    fireEvent.click(screen.getByRole("button", { name: "添付画像 1を拡大" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.error(within(dialog).getByRole("img"));
    fireEvent.click(within(dialog).getByRole("button", { name: "画像を再読込" }));
    await waitFor(() =>
      expect(within(dialog).getByRole("img")).toHaveAttribute("src", updated.url),
    );
    expect(getRecord).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("heading", { name: "最終決定" })).toBeVisible();
  });

  it("keeps text readable when a changed digest is returned", async () => {
    vi.mocked(getRecord).mockResolvedValue({
      ...recordDetail(),
      attachments: [{ ...image, sha256: "b".repeat(64) }],
    });
    renderRoute(<RecordDocument record={{ ...recordDetail(), attachments: [image] }} />);
    fireEvent.error(screen.getByRole("img", { name: "議論に添付された画像 1" }));
    fireEvent.click(screen.getByRole("button", { name: "画像を再読込" }));
    await screen.findByText("画像を更新できませんでした。再度お試しください。");
    expect(screen.getByRole("heading", { name: "最終決定" })).toBeVisible();
    expect(screen.getByText("画像を読み込めませんでした")).toBeVisible();
  });
});
