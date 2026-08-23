import { describe, expect, it } from "vite-plus/test";

import type { ParticipantSlot, RecordDetailResponse } from "./api/types";
import {
  COMPACT_VOTE_LAYOUT,
  createVoteRoutes,
  distance,
  PARTICIPANT_SLOTS,
  pointOnCubicCurve,
  WIDE_VOTE_LAYOUT,
  type VoteGraphLayout,
  type VoteRoute,
} from "./voteGraphGeometry";

const choices: Readonly<Record<ParticipantSlot, readonly ParticipantSlot[]>> = {
  "participant-a": ["participant-b", "participant-c"],
  "participant-b": ["participant-a", "participant-c"],
  "participant-c": ["participant-a", "participant-b"],
};

function votes(
  candidates: readonly [ParticipantSlot, ParticipantSlot, ParticipantSlot],
): RecordDetailResponse["votes"] {
  return PARTICIPANT_SLOTS.map((voter, index) => ({
    voter,
    candidate: candidates[index]!,
    reason: "reason",
  }));
}

function routePoints(route: VoteRoute): readonly { readonly x: number; readonly y: number }[] {
  return [route.start, route.control1, route.control2, route.end];
}

function normalizedVector(start: VoteRoute["start"], end: VoteRoute["end"]): VoteRoute["end"] {
  const x = end.x - start.x;
  const y = end.y - start.y;
  const magnitude = Math.hypot(x, y);
  return { x: x / magnitude, y: y / magnitude };
}

function minimumDistanceToNode(
  route: VoteRoute,
  slot: ParticipantSlot,
  layout: VoteGraphLayout,
): number {
  let minimum = Number.POSITIVE_INFINITY;
  for (let step = 0; step <= 100; step += 1) {
    const point = pointOnCubicCurve(
      route.start,
      route.control1,
      route.control2,
      route.end,
      step / 100,
    );
    minimum = Math.min(minimum, distance(point, layout.nodes[slot]));
  }
  return minimum;
}

describe("vote graph geometry", () => {
  it.each([WIDE_VOTE_LAYOUT, COMPACT_VOTE_LAYOUT])(
    "terminates routes outside the node boundary in the $kind layout",
    (layout) => {
      const routes = createVoteRoutes(
        votes(["participant-b", "participant-a", "participant-a"]),
        layout,
      );
      for (const route of routes) {
        expect(distance(route.start, layout.nodes[route.source])).toBeCloseTo(
          layout.nodeRadius + 11,
          5,
        );
        expect(distance(route.end, layout.nodes[route.target])).toBeCloseTo(
          layout.nodeRadius + 11,
          5,
        );
        expect(Number.isFinite(route.arrowAngle)).toBe(true);
      }
    },
  );

  it("routes an outer vote around the unrelated center node", () => {
    const [outer] = createVoteRoutes(
      [{ voter: "participant-a", candidate: "participant-c", reason: "reason" }],
      WIDE_VOTE_LAYOUT,
    );
    expect(outer).toBeDefined();
    expect(minimumDistanceToNode(outer!, "participant-b", WIDE_VOTE_LAYOUT)).toBeGreaterThanOrEqual(
      WIDE_VOTE_LAYOUT.nodeRadius + 28,
    );
  });

  it("keeps bidirectional routes in distinct lanes", () => {
    const routes = createVoteRoutes(
      [
        { voter: "participant-a", candidate: "participant-b", reason: "reason" },
        { voter: "participant-b", candidate: "participant-a", reason: "reason" },
      ],
      WIDE_VOTE_LAYOUT,
    );
    expect(distance(routes[0]!.midpoint, routes[1]!.midpoint)).toBeGreaterThanOrEqual(12);
  });

  it("separates arrival points when two routes enter the same outer node", () => {
    const routes = createVoteRoutes(
      [
        { voter: "participant-a", candidate: "participant-b", reason: "reason" },
        { voter: "participant-b", candidate: "participant-a", reason: "reason" },
        { voter: "participant-c", candidate: "participant-a", reason: "reason" },
      ],
      WIDE_VOTE_LAYOUT,
    );
    const arrivals = routes.filter((route) => route.target === "participant-a");

    expect(arrivals).toHaveLength(2);
    expect(distance(arrivals[0]!.end, arrivals[1]!.end)).toBeGreaterThanOrEqual(12);
  });

  it.each([WIDE_VOTE_LAYOUT, COMPACT_VOTE_LAYOUT])(
    "keeps all valid vote combinations finite and inside the $kind viewBox",
    (layout) => {
      for (const candidateA of choices["participant-a"]) {
        for (const candidateB of choices["participant-b"]) {
          for (const candidateC of choices["participant-c"]) {
            const routes = createVoteRoutes(votes([candidateA, candidateB, candidateC]), layout);
            expect(new Set(routes.map((route) => route.key)).size).toBe(3);
            for (const route of routes) {
              for (const point of routePoints(route)) {
                expect(Number.isFinite(point.x)).toBe(true);
                expect(Number.isFinite(point.y)).toBe(true);
                expect(point.x).toBeGreaterThanOrEqual(0);
                expect(point.x).toBeLessThanOrEqual(layout.width);
                expect(point.y).toBeGreaterThanOrEqual(0);
                expect(point.y).toBeLessThanOrEqual(layout.height);
              }

              const terminalTangent = normalizedVector(route.control2, route.end);
              const towardTarget = normalizedVector(route.end, layout.nodes[route.target]);
              expect(
                terminalTangent.x * towardTarget.x + terminalTangent.y * towardTarget.y,
              ).toBeGreaterThan(0.999);
            }
          }
        }
      }
    },
  );
});
