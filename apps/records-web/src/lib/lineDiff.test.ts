import { describe, expect, it } from "vite-plus/test";

import { lineDiff } from "./lineDiff";

describe("lineDiff", () => {
  it("shows current removals before selected-revision additions", () => {
    expect(lineDiff("same\nold\ntail", "same\nnew\ntail")).toEqual([
      { kind: "context", beforeLine: 1, afterLine: 1, text: "same" },
      { kind: "removed", beforeLine: 2, afterLine: null, text: "old" },
      { kind: "added", beforeLine: null, afterLine: 2, text: "new" },
      { kind: "context", beforeLine: 3, afterLine: 3, text: "tail" },
    ]);
  });

  it("normalizes line endings and preserves inserted blank lines", () => {
    expect(lineDiff("one\r\ntwo", "one\n\ntwo")).toEqual([
      { kind: "context", beforeLine: 1, afterLine: 1, text: "one" },
      { kind: "added", beforeLine: null, afterLine: 2, text: "" },
      { kind: "context", beforeLine: 2, afterLine: 3, text: "two" },
    ]);
  });
});
