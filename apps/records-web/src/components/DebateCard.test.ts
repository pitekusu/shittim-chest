import { describe, expect, it } from "vite-plus/test";

import { cardDecorationForRecord } from "./DebateCard";

describe("cardDecorationForRecord", () => {
  it("selects a stable and broadly varied card ornament from the record identity", () => {
    const identities = Array.from({ length: 256 }, (_, index) =>
      `${index.toString(36)}-${(index * 2_654_435_761).toString(36)}`.padEnd(43, "x").slice(0, 43),
    );
    const decorations = identities.map(cardDecorationForRecord);

    expect(cardDecorationForRecord(identities[0]!)).toEqual(decorations[0]);
    expect(new Set(decorations.map(({ variant }) => variant)).size).toBe(12);
    expect(new Set(decorations.map(({ frame }) => frame)).size).toBe(5);
    expect(new Set(decorations.map(({ accent }) => accent)).size).toBe(4);
    expect(
      new Set(decorations.map(({ variant, frame, accent }) => `${variant}:${frame}:${accent}`))
        .size,
    ).toBeGreaterThanOrEqual(100);
    for (const decoration of decorations) {
      expect(decoration.rotation).toBeGreaterThanOrEqual(-12);
      expect(decoration.rotation).toBeLessThanOrEqual(12);
      expect(decoration.shiftX).toBeGreaterThanOrEqual(-14);
      expect(decoration.shiftX).toBeLessThanOrEqual(14);
      expect(decoration.shiftY).toBeGreaterThanOrEqual(-10);
      expect(decoration.shiftY).toBeLessThanOrEqual(10);
      expect(decoration.scale).toBeGreaterThanOrEqual(0.9);
      expect(decoration.scale).toBeLessThanOrEqual(1.1);
      expect(decoration.opacity).toBeGreaterThanOrEqual(0.34);
      expect(decoration.opacity).toBeLessThanOrEqual(0.46);
    }
  });
});
