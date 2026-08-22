import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
} from "react";

import type { ParticipantSlot, RecordDetailResponse } from "./api";
import styles from "./VoteGraph.module.css";
import {
  COMPACT_VOTE_LAYOUT,
  createVoteRoutes,
  nodeStyle,
  PARTICIPANT_SLOTS,
  WIDE_VOTE_LAYOUT,
  type VoteRoute,
} from "./voteGraphGeometry";

const COMPACT_QUERY = "(max-width: 899px)";

interface VoteAnimationStyle extends CSSProperties {
  readonly "--edge-delay": string;
}

function subscribeToCompactLayout(callback: () => void): () => void {
  const query = window.matchMedia(COMPACT_QUERY);
  query.addEventListener("change", callback);
  return () => query.removeEventListener("change", callback);
}

function compactLayoutSnapshot(): boolean {
  return window.matchMedia(COMPACT_QUERY).matches;
}

function useCompactLayout(): boolean {
  return useSyncExternalStore(subscribeToCompactLayout, compactLayoutSnapshot, () => false);
}

function relationForRoute(
  route: VoteRoute,
  activeNode: ParticipantSlot | null,
  activeVote: string | null,
): "active" | "incoming" | "outgoing" | "unrelated" | "default" {
  if (activeVote !== null) return route.key === activeVote ? "active" : "unrelated";
  if (activeNode === null) return "default";
  if (route.source === activeNode) return "outgoing";
  if (route.target === activeNode) return "incoming";
  return "unrelated";
}

export function VoteGraph({ record }: { readonly record: RecordDetailResponse }) {
  const compact = useCompactLayout();
  const layout = compact ? COMPACT_VOTE_LAYOUT : WIDE_VOTE_LAYOUT;
  const routes = useMemo(() => createVoteRoutes(record.votes, layout), [layout, record.votes]);
  const participantBySlot = useMemo(
    () => new Map(record.participants.map((participant) => [participant.slot, participant])),
    [record.participants],
  );
  const figureReference = useRef<HTMLElement>(null);
  const [hasRevealed, setHasRevealed] = useState(false);
  const [isVisible, setIsVisible] = useState(false);
  const [activeNode, setActiveNode] = useState<ParticipantSlot | null>(null);
  const [activeVote, setActiveVote] = useState<string | null>(null);
  const rawId = useId().replaceAll(":", "");
  const titleId = `vote-graph-title-${rawId}`;
  const descriptionId = `vote-graph-description-${rawId}`;
  const tooltipId = `vote-graph-tooltip-${rawId}`;

  useEffect(() => {
    const element = figureReference.current;
    if (element === null) return;
    if (!("IntersectionObserver" in window)) {
      setHasRevealed(true);
      setIsVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry === undefined) return;
        setIsVisible(entry.isIntersecting);
        if (entry.isIntersecting && entry.intersectionRatio >= 0.25) setHasRevealed(true);
      },
      { threshold: [0, 0.25] },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const pulseTargets = useMemo(() => {
    const targets = new Map<ParticipantSlot, number>();
    for (const [index, route] of routes.entries()) {
      if (!targets.has(route.target)) targets.set(route.target, index);
    }
    return [...targets.entries()];
  }, [routes]);
  const selectedRoute = routes.find((route) => route.key === activeVote) ?? null;

  const clearSelection = () => {
    setActiveNode(null);
    setActiveVote(null);
  };
  const activateNode = (slot: ParticipantSlot) => {
    setActiveVote(null);
    setActiveNode(slot);
  };

  return (
    <figure
      ref={figureReference}
      className={styles.voteGraph}
      data-layout={layout.kind}
      data-revealed={hasRevealed}
      data-visible={isVisible}
      data-testid="vote-graph"
    >
      <div
        className={styles.graphStage}
        style={{ aspectRatio: `${layout.width} / ${layout.height}` }}
      >
        <svg
          className={styles.graphCanvas}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          aria-labelledby={`${titleId} ${descriptionId}`}
        >
          <title id={titleId}>参加者間の投票関係</title>
          <desc id={descriptionId}>
            矢印の始点が投票者、矢印の先が投票先です。詳細は直後の一覧にも記載しています。
          </desc>
          <defs>
            {routes.map((route) => {
              const gradientId = `vote-gradient-${rawId}-${route.key}`;
              return (
                <linearGradient
                  key={gradientId}
                  id={gradientId}
                  gradientUnits="userSpaceOnUse"
                  x1={route.start.x}
                  y1={route.start.y}
                  x2={route.end.x}
                  y2={route.end.y}
                >
                  <stop offset="0" className={styles.gradientStart} />
                  <stop offset="1" className={styles.gradientEnd} />
                </linearGradient>
              );
            })}
            {routes.map((route, index) => {
              const markerId = `vote-arrow-${rawId}-${route.key}`;
              const animationStyle: VoteAnimationStyle = { "--edge-delay": `${index * 140}ms` };
              return (
                <marker
                  key={markerId}
                  id={markerId}
                  viewBox="-1 -8 16 16"
                  markerWidth="16"
                  markerHeight="16"
                  refX="14"
                  refY="0"
                  markerUnits="userSpaceOnUse"
                  orient="auto"
                  overflow="visible"
                >
                  <polygon
                    className={styles.arrowShape}
                    data-part="arrow"
                    points="0,-7 14,0 0,7 4,0"
                    style={animationStyle}
                  />
                </marker>
              );
            })}
          </defs>
          {routes.map((route, index) => {
            const sourceName = participantBySlot.get(route.source)?.displayName ?? route.source;
            const targetName = participantBySlot.get(route.target)?.displayName ?? route.target;
            const label = `${sourceName}が${targetName}に投票`;
            const gradientId = `vote-gradient-${rawId}-${route.key}`;
            const markerId = `vote-arrow-${rawId}-${route.key}`;
            const relation = relationForRoute(route, activeNode, activeVote);
            const animationStyle: VoteAnimationStyle = { "--edge-delay": `${index * 140}ms` };
            return (
              <g
                key={route.key}
                className={styles.voteEdge}
                data-vote-key={route.key}
                data-relation={relation}
                style={animationStyle}
                onPointerEnter={(event) => {
                  if (event.pointerType === "mouse") setActiveVote(route.key);
                }}
                onPointerLeave={(event) => {
                  if (event.pointerType === "mouse") {
                    setActiveVote((current) => (current === route.key ? null : current));
                  }
                }}
              >
                <path className={styles.edgeHalo} d={route.path} pathLength={1} />
                <path
                  className={styles.edgeLine}
                  data-part="line"
                  d={route.path}
                  pathLength={1}
                  stroke={`url(#${gradientId})`}
                  markerEnd={`url(#${markerId})`}
                />
                <path className={styles.edgeFlow} data-part="flow" d={route.path} pathLength={1} />
                <circle className={styles.startDot} cx={route.start.x} cy={route.start.y} r="3.5" />
                <path
                  className={styles.edgeHitArea}
                  d={route.path}
                  pathLength={1}
                  // oxlint-disable-next-line jsx-a11y/prefer-tag-over-role -- An SVG route needs a keyboard-selectable accessible name.
                  role="button"
                  aria-label={label}
                  aria-describedby={activeVote === route.key ? tooltipId : undefined}
                  tabIndex={0}
                  onFocus={() => setActiveVote(route.key)}
                  onBlur={() =>
                    setActiveVote((current) => (current === route.key ? null : current))
                  }
                  onClick={() =>
                    setActiveVote((current) => (current === route.key ? null : route.key))
                  }
                  onKeyDown={(event) => {
                    if (event.key === "Escape") clearSelection();
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setActiveVote((current) => (current === route.key ? null : route.key));
                    }
                  }}
                />
              </g>
            );
          })}
          {pulseTargets.map(([slot, index]) => {
            const point = layout.nodes[slot];
            const animationStyle: VoteAnimationStyle = { "--edge-delay": `${index * 140}ms` };
            return (
              <circle
                key={slot}
                className={styles.targetPulse}
                cx={point.x}
                cy={point.y}
                r={layout.nodeRadius + 6}
                style={animationStyle}
                aria-hidden="true"
              />
            );
          })}
        </svg>
        {record.participants.map((participant) => {
          const highlighted =
            activeNode === participant.slot ||
            (selectedRoute !== null &&
              (selectedRoute.source === participant.slot ||
                selectedRoute.target === participant.slot));
          return (
            <button
              key={participant.slot}
              type="button"
              className={styles.voteNode}
              style={nodeStyle(participant.slot, layout)}
              data-highlighted={highlighted}
              aria-label={`${participant.displayName}に関係する投票を強調`}
              aria-pressed={activeNode === participant.slot}
              onPointerEnter={(event) => {
                if (event.pointerType === "mouse") activateNode(participant.slot);
              }}
              onPointerLeave={(event) => {
                if (event.pointerType === "mouse") {
                  setActiveNode((current) => (current === participant.slot ? null : current));
                }
              }}
              onFocus={() => activateNode(participant.slot)}
              onBlur={() =>
                setActiveNode((current) => (current === participant.slot ? null : current))
              }
              onClick={() => {
                setActiveVote(null);
                setActiveNode((current) =>
                  current === participant.slot ? null : participant.slot,
                );
              }}
              onKeyDown={(event) => {
                if (event.key === "Escape") clearSelection();
              }}
            >
              {participant.displayName}
            </button>
          );
        })}
        {selectedRoute !== null && (
          <div
            id={tooltipId}
            className={styles.voteTooltip}
            role="tooltip"
            style={{
              left: `${(selectedRoute.midpoint.x / layout.width) * 100}%`,
              top: `${(selectedRoute.midpoint.y / layout.height) * 100}%`,
            }}
          >
            {participantBySlot.get(selectedRoute.source)?.displayName ?? selectedRoute.source}が
            {participantBySlot.get(selectedRoute.target)?.displayName ?? selectedRoute.target}に投票
          </div>
        )}
      </div>
      <figcaption>ドットが投票者、矢印の先が投票先です。</figcaption>
    </figure>
  );
}

export { PARTICIPANT_SLOTS };
