import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App";
import { isRecordsApiResponse } from "./contracts";

describe("App", () => {
  it("uses the approved product display name", () => {
    render(<App />);

    expect(screen.getByRole("heading", { name: "シッテムの箱 議事録" })).toBeVisible();
    expect(screen.getByText(/完了した議論/)).toBeVisible();
  });

  it("validates API payloads against the generated Python contract", () => {
    expect(
      isRecordsApiResponse({
        schemaVersion: 1,
        authenticated: false,
        user: null,
        csrfToken: null,
      }),
    ).toBe(true);
    expect(isRecordsApiResponse({ authenticated: false })).toBe(false);
    expect(isRecordsApiResponse({ schemaVersion: 1, privateId: "forbidden" })).toBe(false);
  });
});
