import { describe, expect, it } from "vite-plus/test";

import { formatCompletedDateTime } from "./dateTime";

describe("formatCompletedDateTime", () => {
  it("formats completed timestamps in fixed JST to minute precision", () => {
    expect(formatCompletedDateTime("2026-08-15T06:00:00Z")).toBe("2026年8月15日 15:00");
    expect(formatCompletedDateTime("2026-12-31T15:00:00Z")).toBe("2027年1月1日 00:00");
    expect(formatCompletedDateTime("2026-08-15T14:59:59Z")).toBe("2026年8月15日 23:59");
  });
});
