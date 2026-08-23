import type { CSSProperties } from "react";

export type RouteMotionStyle = CSSProperties & { "--route-motion-delay": string };

export function routeMotionDelay(milliseconds: number): RouteMotionStyle {
  return { "--route-motion-delay": `${milliseconds}ms` };
}
