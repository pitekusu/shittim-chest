import type { ParticipantSlot, RecordDetailResponse } from "./api";

export interface VotePoint {
  readonly x: number;
  readonly y: number;
}

export interface VoteGraphLayout {
  readonly kind: "wide" | "compact";
  readonly width: number;
  readonly height: number;
  readonly nodeRadius: number;
  readonly nodes: Readonly<Record<ParticipantSlot, VotePoint>>;
}

export interface VoteRoute {
  readonly key: string;
  readonly source: ParticipantSlot;
  readonly target: ParticipantSlot;
  readonly start: VotePoint;
  readonly control1: VotePoint;
  readonly control2: VotePoint;
  readonly end: VotePoint;
  readonly midpoint: VotePoint;
  readonly arrowAngle: number;
  readonly path: string;
}

export const PARTICIPANT_SLOTS: readonly ParticipantSlot[] = [
  "participant-a",
  "participant-b",
  "participant-c",
];

export const WIDE_VOTE_LAYOUT: VoteGraphLayout = {
  kind: "wide",
  width: 720,
  height: 300,
  nodeRadius: 50,
  nodes: {
    "participant-a": { x: 110, y: 162 },
    "participant-b": { x: 360, y: 162 },
    "participant-c": { x: 610, y: 162 },
  },
};

export const COMPACT_VOTE_LAYOUT: VoteGraphLayout = {
  kind: "compact",
  width: 320,
  height: 350,
  nodeRadius: 43,
  nodes: {
    "participant-a": { x: 78, y: 86 },
    "participant-b": { x: 242, y: 86 },
    "participant-c": { x: 160, y: 264 },
  },
};

// The node has a five-pixel visual halo; eleven units leave a visible 4–8 px gap
// at the supported rendered sizes instead of hiding the arrow tip beneath it.
const NODE_GAP = 11;
const OBSTACLE_CLEARANCE = 28;
const CONTROL_STEP = 8;
const MAX_CONTROL_OFFSET = 184;

function add(left: VotePoint, right: VotePoint): VotePoint {
  return { x: left.x + right.x, y: left.y + right.y };
}

function subtract(left: VotePoint, right: VotePoint): VotePoint {
  return { x: left.x - right.x, y: left.y - right.y };
}

function scale(point: VotePoint, amount: number): VotePoint {
  return { x: point.x * amount, y: point.y * amount };
}

function length(point: VotePoint): number {
  return Math.hypot(point.x, point.y);
}

function normalize(point: VotePoint): VotePoint {
  const magnitude = length(point);
  if (magnitude === 0) throw new Error("vote route cannot connect the same point");
  return scale(point, 1 / magnitude);
}

function interpolate(start: VotePoint, end: VotePoint, amount: number): VotePoint {
  return add(start, scale(subtract(end, start), amount));
}

function keepControlOutsideNode(
  center: VotePoint,
  control: VotePoint,
  minimumDistance: number,
): VotePoint {
  const direction = subtract(control, center);
  const currentDistance = length(direction);
  if (currentDistance >= minimumDistance) return control;
  return add(center, scale(normalize(direction), minimumDistance));
}

export function pointOnCubicCurve(
  start: VotePoint,
  control1: VotePoint,
  control2: VotePoint,
  end: VotePoint,
  amount: number,
): VotePoint {
  const inverse = 1 - amount;
  return {
    x:
      inverse ** 3 * start.x +
      3 * inverse ** 2 * amount * control1.x +
      3 * inverse * amount ** 2 * control2.x +
      amount ** 3 * end.x,
    y:
      inverse ** 3 * start.y +
      3 * inverse ** 2 * amount * control1.y +
      3 * inverse * amount ** 2 * control2.y +
      amount ** 3 * end.y,
  };
}

export function distance(left: VotePoint, right: VotePoint): number {
  return length(subtract(left, right));
}

function canonicalNormal(
  source: ParticipantSlot,
  target: ParticipantSlot,
  layout: VoteGraphLayout,
): VotePoint {
  const sourceIndex = PARTICIPANT_SLOTS.indexOf(source);
  const targetIndex = PARTICIPANT_SLOTS.indexOf(target);
  const lower = sourceIndex < targetIndex ? source : target;
  const higher = sourceIndex < targetIndex ? target : source;
  const vector = subtract(layout.nodes[higher], layout.nodes[lower]);
  return normalize({ x: -vector.y, y: vector.x });
}

function controlsForOffset(
  source: VotePoint,
  target: VotePoint,
  normal: VotePoint,
  offset: number,
  controlInset: number,
): readonly [VotePoint, VotePoint] {
  const lane = scale(normal, offset);
  return [
    add(interpolate(source, target, controlInset), lane),
    add(interpolate(source, target, 1 - controlInset), lane),
  ];
}

function clearsUnrelatedNodes(
  sourceSlot: ParticipantSlot,
  targetSlot: ParticipantSlot,
  source: VotePoint,
  control1: VotePoint,
  control2: VotePoint,
  target: VotePoint,
  layout: VoteGraphLayout,
): boolean {
  const minimumDistance = layout.nodeRadius + OBSTACLE_CLEARANCE;
  const unrelated = PARTICIPANT_SLOTS.filter((slot) => slot !== sourceSlot && slot !== targetSlot);
  for (let step = 2; step < 99; step += 2) {
    const point = pointOnCubicCurve(source, control1, control2, target, step / 100);
    if (unrelated.some((slot) => distance(point, layout.nodes[slot]) < minimumDistance)) {
      return false;
    }
  }
  return true;
}

function format(point: VotePoint): string {
  return `${point.x.toFixed(2)} ${point.y.toFixed(2)}`;
}

function createRoute(
  sourceSlot: ParticipantSlot,
  targetSlot: ParticipantSlot,
  layout: VoteGraphLayout,
): VoteRoute {
  const source = layout.nodes[sourceSlot];
  const target = layout.nodes[targetSlot];
  const sourceIndex = PARTICIPANT_SLOTS.indexOf(sourceSlot);
  const targetIndex = PARTICIPANT_SLOTS.indexOf(targetSlot);
  const normal = canonicalNormal(sourceSlot, targetSlot, layout);
  const direction = sourceIndex < targetIndex ? -1 : 1;
  const signedNormal = scale(normal, direction);
  const controlInset = Math.abs(sourceIndex - targetIndex) > 1 ? 0.18 : 0.32;
  let offset = layout.kind === "wide" ? 42 : 28;
  let [control1, control2] = controlsForOffset(source, target, signedNormal, offset, controlInset);

  while (
    !clearsUnrelatedNodes(sourceSlot, targetSlot, source, control1, control2, target, layout) &&
    offset < MAX_CONTROL_OFFSET
  ) {
    offset += CONTROL_STEP;
    [control1, control2] = controlsForOffset(source, target, signedNormal, offset, controlInset);
  }

  const connectionRadius = layout.nodeRadius + NODE_GAP;
  // A Bézier's terminal tangent points from control2 to end. If control2 sits
  // inside the clipped node radius, end falls beyond it and the tangent flips
  // away from the target. Keep both terminal controls outside their endpoint
  // before clipping so the curve and marker always approach the node.
  const minimumControlDistance = connectionRadius + 20;
  control1 = keepControlOutsideNode(source, control1, minimumControlDistance);
  control2 = keepControlOutsideNode(target, control2, minimumControlDistance);
  const start = add(source, scale(normalize(subtract(control1, source)), connectionRadius));
  const incomingDirection = normalize(subtract(target, control2));
  const end = subtract(target, scale(incomingDirection, connectionRadius));
  const midpoint = pointOnCubicCurve(start, control1, control2, end, 0.5);
  const terminalTangent = normalize(subtract(end, control2));
  const arrowAngle = (Math.atan2(terminalTangent.y, terminalTangent.x) * 180) / Math.PI;

  return {
    key: `${sourceSlot}-${targetSlot}`,
    source: sourceSlot,
    target: targetSlot,
    start,
    control1,
    control2,
    end,
    midpoint,
    arrowAngle,
    path: `M ${format(start)} C ${format(control1)}, ${format(control2)}, ${format(end)}`,
  };
}

export function createVoteRoutes(
  votes: RecordDetailResponse["votes"],
  layout: VoteGraphLayout,
): readonly VoteRoute[] {
  return [...votes]
    .sort(
      (left, right) =>
        PARTICIPANT_SLOTS.indexOf(left.voter) - PARTICIPANT_SLOTS.indexOf(right.voter),
    )
    .map((vote) => createRoute(vote.voter, vote.candidate, layout));
}

export function nodeStyle(
  slot: ParticipantSlot,
  layout: VoteGraphLayout,
): Readonly<Record<"left" | "top" | "width", string>> {
  const point = layout.nodes[slot];
  return {
    left: `${(point.x / layout.width) * 100}%`,
    top: `${(point.y / layout.height) * 100}%`,
    width: `${((layout.nodeRadius * 2) / layout.width) * 100}%`,
  };
}
