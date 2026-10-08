import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import {
  RecordsArchiveProvider,
  useRecordsArchive,
  useRecordsArchiveActions,
} from "./useRecordsArchive";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function ArchiveView() {
  const { filters, returnTarget, updateFilters, rememberRecord } = useRecordsArchive();
  return (
    <>
      <label>
        検索
        <input
          value={filters.search}
          onChange={(event) => updateFilters({ search: event.target.value })}
        />
      </label>
      <output aria-label="現在の並び順">{filters.sort}</output>
      <output aria-label="記憶した記録">{returnTarget?.recordId ?? "未選択"}</output>
      <button
        type="button"
        onClick={() => {
          updateFilters({ sort: "oldest", winner: "participant-b" });
          rememberRecord("fixture-record");
        }}
      >
        記録を開く
      </button>
    </>
  );
}

describe("archive memory", () => {
  it("does not rerender an actions-only authentication consumer when search or return location changes", () => {
    const actionRenders = vi.fn<() => void>();
    function AuthenticationActions() {
      const { reset } = useRecordsArchiveActions();
      actionRenders();
      return (
        <button type="button" onClick={reset}>
          認証失効
        </button>
      );
    }
    render(
      <RecordsArchiveProvider>
        <AuthenticationActions />
        <ArchiveView />
      </RecordsArchiveProvider>,
    );
    const initialRenders = actionRenders.mock.calls.length;

    fireEvent.change(screen.getByLabelText("検索"), { target: { value: "local-only search" } });
    fireEvent.click(screen.getByRole("button", { name: "記録を開く" }));

    expect(screen.getByLabelText("検索")).toHaveValue("local-only search");
    expect(screen.getByLabelText("現在の並び順")).toHaveTextContent("oldest");
    expect(screen.getByLabelText("記憶した記録")).toHaveTextContent("fixture-record");
    expect(actionRenders).toHaveBeenCalledTimes(initialRenders);
    fireEvent.click(screen.getByRole("button", { name: "認証失効" }));
    expect(screen.getByLabelText("検索")).toHaveValue("");
    expect(screen.getByLabelText("記憶した記録")).toHaveTextContent("未選択");
    expect(actionRenders).toHaveBeenCalledTimes(initialRenders);
  });

  it("resets filters and return location after asynchronous authentication cleanup without persisting them", async () => {
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    const historyWrite = vi.spyOn(window.history, "replaceState");
    const historyPush = vi.spyOn(window.history, "pushState");
    function LogoutActions() {
      const { reset } = useRecordsArchiveActions();
      return (
        <button
          type="button"
          onClick={async () => {
            await Promise.resolve();
            reset();
          }}
        >
          ログアウト完了
        </button>
      );
    }
    const view = render(
      <RecordsArchiveProvider key="first-session">
        <LogoutActions />
        <ArchiveView />
      </RecordsArchiveProvider>,
    );
    fireEvent.change(screen.getByLabelText("検索"), {
      target: { value: "private search fixture" },
    });
    fireEvent.click(screen.getByRole("button", { name: "記録を開く" }));
    fireEvent.click(screen.getByRole("button", { name: "ログアウト完了" }));

    await waitFor(() => expect(screen.getByLabelText("検索")).toHaveValue(""));
    expect(screen.getByLabelText("現在の並び順")).toHaveTextContent("newest");
    expect(screen.getByLabelText("記憶した記録")).toHaveTextContent("未選択");
    fireEvent.change(screen.getByLabelText("検索"), {
      target: { value: "another private fixture" },
    });
    view.rerender(
      <RecordsArchiveProvider key="next-session">
        <ArchiveView />
      </RecordsArchiveProvider>,
    );
    expect(screen.getByLabelText("検索")).toHaveValue("");
    expect(storageWrite).not.toHaveBeenCalled();
    expect(historyWrite).not.toHaveBeenCalled();
    expect(historyPush).not.toHaveBeenCalled();
  });
});
